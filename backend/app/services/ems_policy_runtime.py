"""Evaluate installed declarative EMS policies through unified commands only."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import math
from collections.abc import Iterator
from threading import RLock
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.automated_control_commands import (
    AutomatedControlCommandRequest,
    AutomatedControlCommands,
)
from app.services.entity_instance_catalog import EntityInstanceCatalog, EntityInstanceDescriptor
from app.services.entity_instance_registry import EntityInstanceError
from app.services.entity_instance_runtime import EntityInstanceRuntime
from app.services.solution_delivery_contracts import DeliveryError, DeliveryRepository


class PolicyActivationRepository(Protocol):
    def enable(self, site_configuration_version: int, policy_id: str, actor: str) -> None: ...

    def disable(self, site_configuration_version: int, policy_id: str, actor: str) -> None: ...

    def enabled(self, site_configuration_version: int, policy_id: str) -> bool: ...

    def active(self, site_configuration_version: int, policy_id: str) -> Iterator[bool]: ...


class InMemoryPolicyActivationRepository:
    """Test adapter; production wiring uses the PostgreSQL implementation below."""

    def __init__(self) -> None:
        self._enabled: set[tuple[int, str]] = set()
        self._lock = RLock()

    def enable(self, site_configuration_version: int, policy_id: str, actor: str) -> None:
        del actor
        with self._lock:
            self._enabled.add((site_configuration_version, policy_id))

    def disable(self, site_configuration_version: int, policy_id: str, actor: str) -> None:
        del actor
        with self._lock:
            self._enabled.discard((site_configuration_version, policy_id))

    def enabled(self, site_configuration_version: int, policy_id: str) -> bool:
        with self._lock:
            return (site_configuration_version, policy_id) in self._enabled

    @contextmanager
    def active(self, site_configuration_version: int, policy_id: str) -> Iterator[bool]:
        with self._lock:
            yield (site_configuration_version, policy_id) in self._enabled


class PostgresPolicyActivationRepository:
    """Persist explicit engineer approval; a restart must not silently enable policies."""

    def enable(self, site_configuration_version: int, policy_id: str, actor: str) -> None:
        from app.services.telemetry_store import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_ems_policy_activations
                      (site_configuration_version, policy_id, enabled_by, enabled_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (site_configuration_version, policy_id) DO NOTHING
                    """,
                    (site_configuration_version, policy_id, actor, datetime.now(timezone.utc)),
                )
            conn.commit()

    def disable(self, site_configuration_version: int, policy_id: str, actor: str) -> None:
        del actor
        from app.services.telemetry_store import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM t_ems_policy_activations
                    WHERE site_configuration_version = %s AND policy_id = %s
                    """,
                    (site_configuration_version, policy_id),
                )
            conn.commit()

    def enabled(self, site_configuration_version: int, policy_id: str) -> bool:
        from app.services.telemetry_store import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS(
                      SELECT 1 FROM t_ems_policy_activations
                      WHERE site_configuration_version = %s AND policy_id = %s
                    )
                    """,
                    (site_configuration_version, policy_id),
                )
                return bool(cur.fetchone()[0])

    @contextmanager
    def active(self, site_configuration_version: int, policy_id: str) -> Iterator[bool]:
        """Keep disable waiting until an already-approved dispatch is recorded."""
        from app.services.telemetry_store import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM t_ems_policy_activations
                    WHERE site_configuration_version = %s AND policy_id = %s
                    FOR SHARE
                    """,
                    (site_configuration_version, policy_id),
                )
                try:
                    yield cur.fetchone() is not None
                except Exception:
                    conn.rollback()
                    raise
            conn.commit()


class EmsPolicyRuntime:
    """Hide installed-package lookup, instance resolution, simulation, and command evidence."""

    def __init__(
        self,
        delivery: DeliveryRepository,
        catalog: EntityInstanceCatalog,
        observations: EntityInstanceRuntime,
        commands: AutomatedControlCommands,
        activations: PolicyActivationRepository | None = None,
    ) -> None:
        self._delivery = delivery
        self._catalog = catalog
        self._observations = observations
        self._commands = commands
        self._activations = activations or InMemoryPolicyActivationRepository()

    def simulate(
        self,
        policy_id: str,
        installation_id: UUID | None = None,
    ) -> dict[str, Any]:
        policy, _ = self._policy(policy_id, installation_id=installation_id)
        simulation = policy["simulation"]
        return {
            "policy_id": policy["id"],
            "revision": policy["revision"],
            "input": dict(simulation["input"]),
            "result": {
                "triggered": simulation["expected"]["triggered"],
                "action_value": simulation["expected"]["actionValue"],
            },
        }

    def evaluate(self, policy_id: str) -> dict[str, Any]:
        version = self._delivery.site_configuration_version()
        with self._activations.active(version, policy_id) as active:
            if not active:
                raise DeliveryError("POLICY_NOT_ENABLED", "EMS policy must be enabled by an engineer")
            return self._evaluate_active(policy_id)

    def _evaluate_active(self, policy_id: str) -> dict[str, Any]:
        policy, descriptors = self._policy(policy_id)
        source = self._reference(policy["input"], descriptors)
        try:
            observation = self._observations.read(source.id)
        except EntityInstanceError as exc:
            raise DeliveryError(exc.code, str(exc)) from exc
        value = observation.value
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DeliveryError("POLICY_INPUT_INVALID", "Installed policy input is not numeric")
        triggered = _matches(float(value), policy["condition"])
        evidence = {
            "policy_id": policy["id"],
            "revision": policy["revision"],
            "input": {
                "instance": str(source.id),
                "definition": source.definition_id,
                "value": value,
                "unit": source.unit,
                "observed_at": observation.observed_at.isoformat(),
            },
            "condition": dict(policy["condition"]),
        }
        authorization = policy["action"].get("high_risk_authorization")
        if authorization is not None:
            evidence["high_risk_authorization"] = {
                **authorization,
                "policy_id": policy["id"],
                "revision": policy["revision"],
                "action_key": policy["action"]["id"],
            }
        if not triggered:
            return {"policy_id": policy["id"], "triggered": False, "input": evidence["input"], "command": None}
        target = self._reference(policy["action"]["target"], descriptors)
        command = self._commands.submit(
            AutomatedControlCommandRequest(
                source_type="policy",
                subject_id=uuid5(NAMESPACE_URL, f"zizu/ems-policy/{policy['id']}"),
                subject_version=policy["revision"],
                action_key=policy["action"]["id"],
                entity_instance_id=target.id,
                value=policy["action"]["value"],
                trigger_evidence=evidence,
            )
        )
        return {"policy_id": policy["id"], "triggered": True, "input": evidence["input"], "command": command.public_dict()}

    def enable(self, policy_id: str, actor: str) -> dict[str, Any]:
        """Validate a live, confirmed input before the scheduler can run a policy."""
        version = self._delivery.site_configuration_version()
        policy, descriptors = self._policy(policy_id)
        source = self._reference(policy["input"], descriptors)
        try:
            observation = self._observations.read(source.id)
        except EntityInstanceError as exc:
            raise DeliveryError(exc.code, str(exc)) from exc
        if not isinstance(observation.value, (int, float)) or isinstance(observation.value, bool):
            raise DeliveryError("POLICY_INPUT_INVALID", "Installed policy input is not numeric")
        self._activations.enable(version, policy_id, actor)
        return {
            "policy_id": policy["id"],
            "site_configuration_version": version,
            "status": "enabled",
            "input": {
                "instance": str(source.id),
                "value": observation.value,
                "unit": source.unit,
                "observed_at": observation.observed_at.isoformat(),
            },
        }

    def disable(self, policy_id: str, actor: str) -> dict[str, Any]:
        """Stop future scheduler evaluations for the active site configuration."""
        version = self._delivery.site_configuration_version()
        policy, _ = self._policy(policy_id)
        self._activations.disable(version, policy_id, actor)
        return {
            "policy_id": policy["id"],
            "site_configuration_version": version,
            "status": "disabled",
        }

    def authorizes_high_risk_command(self, request: Any) -> bool:
        """Recheck installed policy and activation state; never trust evidence alone."""
        if request.source_type != "policy":
            return False
        evidence = request.origin_evidence
        subject = evidence.get("subject") if isinstance(evidence, dict) else None
        action_key = evidence.get("action_key") if isinstance(evidence, dict) else None
        trigger = evidence.get("trigger") if isinstance(evidence, dict) else None
        if (
            not isinstance(subject, dict)
            or not isinstance(action_key, str)
            or not isinstance(trigger, dict)
            or not isinstance(trigger.get("policy_id"), str)
        ):
            return False
        policy_id = trigger["policy_id"]
        version = self._delivery.site_configuration_version()
        if not self._activations.enabled(version, policy_id):
            return False
        try:
            policy, descriptors = self._policy(policy_id)
            target = self._reference(policy["action"]["target"], descriptors)
        except (DeliveryError, EntityInstanceError):
            return False
        authorization = policy["action"].get("high_risk_authorization")
        expected_subject = str(uuid5(NAMESPACE_URL, f"zizu/ems-policy/{policy_id}"))
        if (
            not isinstance(authorization, dict)
            or subject != {"type": "policy", "id": expected_subject, "version": policy["revision"]}
            or trigger.get("revision") != policy["revision"]
            or action_key != policy["action"]["id"]
            or request.actor != f"policy:{expected_subject}"
            or request.entity_instance_id != target.id
            or request.value != policy["action"]["value"]
            or not isinstance(request.value, (int, float))
            or isinstance(request.value, bool)
            or not math.isfinite(float(request.value))
        ):
            return False
        maximum = authorization.get("maximum_absolute_value")
        if not isinstance(maximum, (int, float)) or not math.isfinite(float(maximum)):
            return False
        return abs(float(request.value)) <= float(maximum)

    def acceptance_evidence(
        self,
        policy_id: str,
        expected_action: str,
        command_id: str,
        installation_id: str,
    ) -> dict[str, Any]:
        """Verify a command already issued through the public policy/control workflow."""
        try:
            installation = UUID(installation_id)
        except ValueError as exc:
            raise DeliveryError("POLICY_EXECUTION_COMMAND_NOT_FOUND", "Policy installation is unavailable") from exc
        policy, descriptors = self._policy(policy_id, installation_id=installation)
        try:
            command = self._commands.get(UUID(command_id))
        except (KeyError, ValueError) as exc:
            raise DeliveryError("POLICY_EXECUTION_COMMAND_NOT_FOUND", "Policy command is unavailable") from exc
        evidence = command.origin_evidence
        subject = evidence.get("subject") if isinstance(evidence, dict) else None
        action_key = evidence.get("action_key") if isinstance(evidence, dict) else None
        trigger = evidence.get("trigger") if isinstance(evidence, dict) else None
        expected_subject = str(uuid5(NAMESPACE_URL, f"zizu/ems-policy/{policy['id']}"))
        if (
            command.source_type != "policy"
            or command.status != "readback_confirmed"
            or action_key != expected_action
            or expected_action != policy["action"]["id"]
            or not isinstance(subject, dict)
            or subject.get("id") != expected_subject
            or subject.get("version") != policy["revision"]
            or not isinstance(trigger, dict)
            or trigger.get("policy_id") != policy["id"]
            or command.entity_instance_id not in {item.id for item in descriptors}
        ):
            raise DeliveryError(
                "POLICY_EXECUTION_INCOMPLETE",
                "Policy command does not prove the declared execution and readback",
            )
        return {
            "simulation": self.simulate(policy_id, installation_id=installation),
            "input": trigger.get("input"),
            "command": command.public_dict(),
        }

    def tick(self) -> dict[str, int]:
        """Evaluate every installed policy once; the scheduler owns cadence, not package code."""
        result = {"evaluated": 0, "commands": 0, "errors": 0}
        try:
            policies, _ = self._policies()
        except DeliveryError as exc:
            if exc.code == "POLICY_NOT_INSTALLED":
                return result
            raise
        for policy in policies:
            try:
                outcome = self.evaluate(policy["id"])
            except DeliveryError as exc:
                if exc.code == "POLICY_NOT_ENABLED":
                    continue
                result["errors"] += 1
            except Exception:
                result["errors"] += 1
            else:
                result["evaluated"] += 1
                result["commands"] += int(outcome["command"] is not None)
        return result

    def _policy(
        self,
        policy_id: str,
        installation_id: UUID | None = None,
    ) -> tuple[dict[str, Any], tuple[EntityInstanceDescriptor, ...]]:
        policies, descriptors = self._policies(installation_id)
        policy = next((item for item in policies if item["id"] == policy_id), None)
        if policy is None:
            raise DeliveryError("POLICY_NOT_INSTALLED", "EMS policy is not installed")
        return policy, descriptors

    def _policies(
        self,
        installation_id: UUID | None = None,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[EntityInstanceDescriptor, ...]]:
        if installation_id is None:
            version = self._delivery.site_configuration_version()
            configuration = self._delivery.get_site_configuration_version(version)
            if version < 1 or configuration is None:
                raise DeliveryError("POLICY_NOT_INSTALLED", "No site configuration is installed")
            installation_id = configuration.installation_id
        installation = self._delivery.get_installation(installation_id)
        if installation is None:
            raise DeliveryError("POLICY_NOT_INSTALLED", "Current installation is unavailable")
        package = self._delivery.package_for_installation(installation)
        if package is None:
            raise DeliveryError("POLICY_NOT_INSTALLED", "Installed package is unavailable")
        policies = tuple(package.manifest.get("_policy_assets", ()))
        if not policies:
            raise DeliveryError("POLICY_NOT_INSTALLED", "Installed package has no EMS policy")
        allowed_ids = set(installation.entity_instance_ids)
        descriptors = tuple(item for item in self._catalog.list() if item.id in allowed_ids)
        return policies, descriptors

    @staticmethod
    def _reference(reference: dict[str, str], descriptors: tuple[EntityInstanceDescriptor, ...]) -> EntityInstanceDescriptor:
        matched = [
            item for item in descriptors
            if item.slot_id == reference["slot"] and item.definition_id == reference["definition"]
        ]
        if len(matched) != 1:
            raise DeliveryError(
                "POLICY_REFERENCE_UNRESOLVED",
                "EMS policy needs exactly one active confirmed entity instance per reference",
            )
        return matched[0]


def _matches(value: float, condition: dict[str, Any]) -> bool:
    threshold = float(condition["threshold"])
    return {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
    }[condition["operator"]]
