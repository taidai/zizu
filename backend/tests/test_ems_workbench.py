from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

from fastapi import FastAPI

from app.api import ems_workbench as ems_workbench_api
from app.services.configuration_revision import ConfigurationRevisionError, GateState
from app.services.data_trunk_contracts import DataTrunkError
from app.services.ems_workbench_slots import (
    EmsWorkbenchSlotError,
    EmsWorkbenchSlots,
    WorkbenchSlotWriteReceipt,
    resolve_workbench_slots,
)
from app.services.entity_instance_catalog import EntityInstanceCatalog, EntityInstanceDescriptor
from app.services.ems_workbench import EmsWorkbench
from tests.api_test_client import AuthenticatedApiClient


NODE_ID = UUID("91000000-0000-0000-0000-000000000001")
PCS_POWER_ID = UUID("91000000-0000-0000-0000-000000000101")
SECOND_ENTITY_ID = UUID("91000000-0000-0000-0000-000000000102")


def _entity(
    entity_id: UUID,
    definition_id: str,
    *,
    data_type: str = "FLOAT",
    unit: str | None = "kW",
    confirmed: bool = True,
    display_name: str = "PCS 实时有功功率",
) -> EntityInstanceDescriptor:
    return EntityInstanceDescriptor(
        id=entity_id,
        node_id=NODE_ID,
        node_type="PCS",
        node_display_name="PCS-01",
        definition_id=definition_id,
        display_name=display_name,
        data_type=data_type,
        unit=unit,
        direction="R",
        freshness_seconds=10,
        confirmed=confirmed,
    )


class WorkbenchSlotResolutionTest(unittest.TestCase):
    def test_one_exact_definition_binds_the_storage_power_slot(self) -> None:
        # Break caught: returning the first fuzzy/priority match instead of the
        # one confirmed entity whose definition is an explicit exact alias.
        resolved = resolve_workbench_slots(
            (_entity(PCS_POWER_ID, "pcs.active_power"),),
            {},
        )

        slot = next(item for item in resolved if item.key == "storage-power")
        self.assertEqual("exact", slot.binding_mode)
        self.assertEqual("bound", slot.binding_status)
        self.assertEqual(PCS_POWER_ID, slot.entity.id if slot.entity else None)

    def test_all_five_slots_remain_visible_when_no_entity_matches(self) -> None:
        # Break caught: omitting empty fixed slots makes a missing binding look
        # like a missing product feature instead of an explicit configuration gap.
        resolved = resolve_workbench_slots((), {})

        self.assertEqual(
            [
                "site-power",
                "pv-power",
                "storage-power",
                "storage-soc",
                "charging-power",
            ],
            [item.key for item in resolved],
        )
        self.assertEqual({"unconfigured"}, {item.binding_status for item in resolved})

    def test_two_exact_candidates_are_ambiguous_instead_of_using_row_order(self) -> None:
        # Break caught: silently choosing the first of two exact L2 candidates.
        resolved = resolve_workbench_slots(
            (
                _entity(PCS_POWER_ID, "pcs.active_power"),
                _entity(SECOND_ENTITY_ID, "storage.active_power"),
            ),
            {},
        )

        slot = next(item for item in resolved if item.key == "storage-power")
        self.assertEqual("ambiguous", slot.binding_mode)
        self.assertEqual("ambiguous", slot.binding_status)
        self.assertIsNone(slot.entity)
        self.assertEqual(2, slot.candidate_count)

    def test_definition_substring_and_display_name_never_auto_bind(self) -> None:
        # Break caught: guessing from a friendly name or partial definition key.
        resolved = resolve_workbench_slots(
            (
                _entity(
                    PCS_POWER_ID,
                    "vendor.pcs.active_power.raw",
                    display_name="储能功率 pcs.active_power",
                ),
            ),
            {},
        )

        slot = next(item for item in resolved if item.key == "storage-power")
        self.assertEqual("unconfigured", slot.binding_status)
        self.assertIsNone(slot.entity)

    def test_exact_candidate_with_wrong_contract_fails_closed(self) -> None:
        # Break caught: showing a boolean or watt value as a trusted kW KPI.
        cases = (
            ("BOOL", "kW"),
            ("FLOAT", "W"),
            ("INT", None),
        )
        for data_type, unit in cases:
            with self.subTest(data_type=data_type, unit=unit):
                resolved = resolve_workbench_slots(
                    (
                        _entity(
                            PCS_POWER_ID,
                            "pcs.active_power",
                            data_type=data_type,
                            unit=unit,
                        ),
                    ),
                    {},
                )
                slot = next(item for item in resolved if item.key == "storage-power")
                self.assertEqual("unconfigured", slot.binding_status)
                self.assertEqual("WORKBENCH_SLOT_CANDIDATE_INCOMPATIBLE", slot.reason)
                self.assertIsNone(slot.entity)

    def test_manual_compatible_entity_wins_without_requiring_an_auto_alias(self) -> None:
        # Break caught: overriding an engineer's explicit choice with an exact-key
        # candidate, or unnecessarily rejecting a custom but compatible L2.
        manual_id = SECOND_ENTITY_ID
        resolved = resolve_workbench_slots(
            (
                _entity(PCS_POWER_ID, "pcs.active_power"),
                _entity(manual_id, "site.custom_storage_dispatch_power"),
            ),
            {"storage-power": manual_id},
        )

        slot = next(item for item in resolved if item.key == "storage-power")
        self.assertEqual("manual", slot.binding_mode)
        self.assertEqual("bound", slot.binding_status)
        self.assertEqual(manual_id, slot.entity.id if slot.entity else None)

    def test_missing_or_unconfirmed_manual_target_stays_manual_but_invalid(self) -> None:
        # Break caught: falling back to an automatic candidate after a persisted
        # manual target is retired, deleted, or no longer confirmed.
        cases = (
            ((), SECOND_ENTITY_ID),
            (
                (
                    _entity(PCS_POWER_ID, "pcs.active_power"),
                    _entity(SECOND_ENTITY_ID, "custom.power", confirmed=False),
                ),
                SECOND_ENTITY_ID,
            ),
        )
        for descriptors, manual_id in cases:
            with self.subTest(descriptors=len(descriptors)):
                resolved = resolve_workbench_slots(
                    descriptors,
                    {"storage-power": manual_id},
                )
                slot = next(item for item in resolved if item.key == "storage-power")
                self.assertEqual("manual", slot.binding_mode)
                self.assertEqual("invalid", slot.binding_status)
                self.assertEqual("manual", slot.binding_source)
                self.assertIsNone(slot.entity)

    def test_manual_target_with_wrong_type_or_unit_is_invalid(self) -> None:
        # Break caught: a persisted manual choice bypassing the same slot contract
        # enforced for deterministic automatic matching.
        cases = (("BOOL", "kW"), ("FLOAT", "W"), ("INT", None))
        for data_type, unit in cases:
            with self.subTest(data_type=data_type, unit=unit):
                resolved = resolve_workbench_slots(
                    (
                        _entity(
                            PCS_POWER_ID,
                            "custom.power",
                            data_type=data_type,
                            unit=unit,
                        ),
                    ),
                    {"storage-power": PCS_POWER_ID},
                )
                slot = next(item for item in resolved if item.key == "storage-power")
                self.assertEqual("manual", slot.binding_mode)
                self.assertEqual("invalid", slot.binding_status)
                self.assertEqual("WORKBENCH_SLOT_MANUAL_TARGET_INCOMPATIBLE", slot.reason)
                self.assertIsNone(slot.entity)

    def test_soc_requires_numeric_percent(self) -> None:
        # Break caught: treating a unitless or boolean state as storage SOC.
        valid = resolve_workbench_slots(
            (_entity(PCS_POWER_ID, "bms.soc", data_type="INT", unit="%"),),
            {},
        )
        invalid = resolve_workbench_slots(
            (_entity(PCS_POWER_ID, "bms.soc", data_type="FLOAT", unit="1"),),
            {},
        )

        valid_slot = next(item for item in valid if item.key == "storage-soc")
        invalid_slot = next(item for item in invalid if item.key == "storage-soc")
        self.assertEqual("bound", valid_slot.binding_status)
        self.assertEqual("unconfigured", invalid_slot.binding_status)


class _CatalogRepository:
    def __init__(self, descriptors: tuple[EntityInstanceDescriptor, ...]) -> None:
        self.descriptors = descriptors
        self.list_calls = 0

    def list_instances(self) -> tuple[EntityInstanceDescriptor, ...]:
        self.list_calls += 1
        return self.descriptors

    def preview_legacy(self) -> tuple:
        return ()


class _SlotRepository:
    def __init__(self, bindings: dict[str, UUID] | None = None) -> None:
        self.bindings = dict(bindings or {})
        self.calls: list[dict] = []
        self.replay: WorkbenchSlotWriteReceipt | None = None

    def list_manual_bindings(self) -> dict[str, UUID]:
        return dict(self.bindings)

    def find_replay(self, **kwargs) -> WorkbenchSlotWriteReceipt | None:
        return self.replay

    def set_binding(self, **kwargs) -> WorkbenchSlotWriteReceipt:
        self.calls.append(kwargs)
        entity_id = kwargs["entity_instance_id"]
        if entity_id is None:
            self.bindings.pop(kwargs["slot_key"], None)
        else:
            self.bindings[kwargs["slot_key"]] = entity_id
        return WorkbenchSlotWriteReceipt(
            slot_key=kwargs["slot_key"],
            entity_instance_id=entity_id,
            configuration_revision=kwargs["base_configuration_revision"] + 1,
            replayed=False,
        )


class _ReplayAfterWriteRepository(_SlotRepository):
    def set_binding(self, **kwargs) -> WorkbenchSlotWriteReceipt:
        receipt = super().set_binding(**kwargs)
        self.replay = WorkbenchSlotWriteReceipt(
            receipt.slot_key,
            receipt.entity_instance_id,
            receipt.configuration_revision,
            True,
        )
        return receipt


class WorkbenchSlotWriteTest(unittest.TestCase):
    def _subject(
        self,
        descriptors: tuple[EntityInstanceDescriptor, ...],
    ) -> tuple[EmsWorkbenchSlots, _SlotRepository]:
        repository = _SlotRepository()
        return (
            EmsWorkbenchSlots(
                EntityInstanceCatalog(_CatalogRepository(descriptors)),
                repository,
            ),
            repository,
        )

    def test_compatible_manual_target_is_persisted_with_revision_and_actor(self) -> None:
        # Break caught: bypassing the explicit actor/base-revision/idempotency
        # contract when saving a valid manual slot binding.
        subject, repository = self._subject(
            (_entity(PCS_POWER_ID, "custom.storage_power"),)
        )

        receipt = subject.bind(
            slot_key="storage-power",
            entity_instance_id=PCS_POWER_ID,
            base_configuration_revision=7,
            actor="user:engineer",
            idempotency_key="bind-storage-power-v1",
        )

        self.assertEqual(8, receipt.configuration_revision)
        self.assertEqual(
            {
                "slot_key": "storage-power",
                "entity_instance_id": PCS_POWER_ID,
                "base_configuration_revision": 7,
                "actor": "user:engineer",
                "idempotency_key": "bind-storage-power-v1",
            },
            repository.calls[0],
        )

    def test_clear_restores_auto_mode_without_requiring_an_entity(self) -> None:
        # Break caught: clear being rejected by entity validation or writing a
        # synthetic null manual binding instead of removing the override.
        subject, repository = self._subject(())

        receipt = subject.bind(
            slot_key="storage-power",
            entity_instance_id=None,
            base_configuration_revision=7,
            actor="user:engineer",
            idempotency_key="clear-storage-power-v1",
        )

        self.assertIsNone(receipt.entity_instance_id)
        self.assertIsNone(repository.calls[0]["entity_instance_id"])

    def test_unknown_slot_is_rejected_before_persistence(self) -> None:
        # Break caught: accepting arbitrary slot keys turns the fixed workbench
        # into an unbounded page designer.
        subject, repository = self._subject(())

        with self.assertRaises(EmsWorkbenchSlotError) as raised:
            subject.bind(
                slot_key="custom-widget",
                entity_instance_id=None,
                base_configuration_revision=7,
                actor="user:engineer",
                idempotency_key="custom-widget-v1",
            )

        self.assertEqual("WORKBENCH_SLOT_NOT_FOUND", raised.exception.code)
        self.assertEqual([], repository.calls)

    def test_unavailable_or_unconfirmed_target_is_rejected_before_persistence(self) -> None:
        # Break caught: persisting a retired/deleted/unconfirmed L2 reference.
        for descriptors in (
            (),
            (_entity(PCS_POWER_ID, "custom.power", confirmed=False),),
        ):
            with self.subTest(descriptors=len(descriptors)):
                subject, repository = self._subject(descriptors)
                with self.assertRaises(EmsWorkbenchSlotError) as raised:
                    subject.bind(
                        slot_key="storage-power",
                        entity_instance_id=PCS_POWER_ID,
                        base_configuration_revision=7,
                        actor="user:engineer",
                        idempotency_key="invalid-target-v1",
                    )
                self.assertEqual(
                    "WORKBENCH_SLOT_ENTITY_NOT_AVAILABLE",
                    raised.exception.code,
                )
                self.assertEqual([], repository.calls)

    def test_wrong_type_or_unit_target_is_rejected_before_persistence(self) -> None:
        # Break caught: manual writes bypassing the power/SOC type-unit contract.
        cases = (
            ("storage-power", "BOOL", "kW", "WORKBENCH_SLOT_ENTITY_TYPE_INVALID"),
            ("storage-power", "FLOAT", "W", "WORKBENCH_SLOT_ENTITY_UNIT_INVALID"),
            ("storage-soc", "FLOAT", "1", "WORKBENCH_SLOT_ENTITY_UNIT_INVALID"),
        )
        for slot_key, data_type, unit, code in cases:
            with self.subTest(slot_key=slot_key, data_type=data_type, unit=unit):
                subject, repository = self._subject(
                    (
                        _entity(
                            PCS_POWER_ID,
                            "custom.measurement",
                            data_type=data_type,
                            unit=unit,
                        ),
                    )
                )
                with self.assertRaises(EmsWorkbenchSlotError) as raised:
                    subject.bind(
                        slot_key=slot_key,
                        entity_instance_id=PCS_POWER_ID,
                        base_configuration_revision=7,
                        actor="user:engineer",
                        idempotency_key="invalid-contract-v1",
                    )
                self.assertEqual(code, raised.exception.code)
                self.assertEqual([], repository.calls)

    def test_idempotent_replay_precedes_current_target_validation_and_runtime_gate(self) -> None:
        # Break caught: retrying a successful request with its original base
        # revision failing after the selected entity was later retired.
        subject, repository = self._subject(())
        repository.replay = WorkbenchSlotWriteReceipt(
            "storage-power",
            PCS_POWER_ID,
            8,
            True,
        )
        gate = _Gate()

        receipt = subject.bind(
            slot_key="storage-power",
            entity_instance_id=PCS_POWER_ID,
            base_configuration_revision=7,
            actor="user:engineer",
            idempotency_key="replay-after-retire",
            runtime_gate=gate,
        )

        self.assertTrue(receipt.replayed)
        self.assertEqual([], repository.calls)
        self.assertEqual([], gate.events)

    def test_quiesced_replay_retries_runtime_reconciliation_without_a_second_write(self) -> None:
        # Break caught: the database commit succeeding but runtime rebuild
        # failing, followed by an idempotent retry returning a false 200 while
        # the data trunk remains quiesced.
        repository = _ReplayAfterWriteRepository()
        subject = EmsWorkbenchSlots(
            EntityInstanceCatalog(
                _CatalogRepository((_entity(PCS_POWER_ID, "pcs.active_power"),))
            ),
            repository,
        )
        gate = _Gate(failed_reconciliations=1)
        request = {
            "slot_key": "storage-power",
            "entity_instance_id": PCS_POWER_ID,
            "base_configuration_revision": 7,
            "actor": "user:engineer",
            "idempotency_key": "recover-runtime-v1",
            "runtime_gate": gate,
        }

        with self.assertRaises(DataTrunkError) as first:
            subject.bind(**request)
        receipt = subject.bind(**request)

        self.assertEqual(
            "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
            first.exception.code,
        )
        self.assertTrue(receipt.replayed)
        self.assertEqual(1, len(repository.calls))
        self.assertEqual(
            [("begin", 7), "reconcile", "reconcile"],
            gate.events,
        )
        self.assertIs(GateState.RUNNING, gate.state)

    def test_busy_runtime_never_turns_an_idempotent_replay_into_false_success(self) -> None:
        # Break caught: a replay returning 200 while another configuration
        # change is still draining or closing the runtime.
        for state in (GateState.DRAINING, GateState.CLOSING):
            with self.subTest(state=state):
                subject, repository = self._subject(())
                repository.replay = WorkbenchSlotWriteReceipt(
                    "storage-power",
                    PCS_POWER_ID,
                    8,
                    True,
                )
                gate = _Gate(state=state)

                with self.assertRaises(DataTrunkError) as raised:
                    subject.bind(
                        slot_key="storage-power",
                        entity_instance_id=PCS_POWER_ID,
                        base_configuration_revision=7,
                        actor="user:engineer",
                        idempotency_key="busy-replay-v1",
                        runtime_gate=gate,
                    )

                self.assertEqual(
                    "CONFIGURATION_RUNTIME_BUSY",
                    raised.exception.code,
                )
                self.assertEqual([], repository.calls)
                self.assertEqual([], gate.events)


class _Runtime:
    def read(self, entity_id: UUID):
        return SimpleNamespace(
            value=123.5,
            observed_at=datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
            quality=192,
        )

    def history(self, entity_id: UUID, range_key: str) -> list:
        return []


class _IncrementingRuntime(_Runtime):
    def __init__(self) -> None:
        self.read_calls: dict[UUID, int] = {}

    def read(self, entity_id: UUID):
        count = self.read_calls.get(entity_id, 0) + 1
        self.read_calls[entity_id] = count
        return SimpleNamespace(
            value=count,
            observed_at=datetime(2026, 9, 8, 8, 0, tzinfo=UTC),
            quality=192,
        )


class _Gate:
    def __init__(
        self,
        *,
        state: GateState = GateState.RUNNING,
        failed_reconciliations: int = 0,
    ) -> None:
        self.events: list[object] = []
        self._state = state
        self._failed_reconciliations = failed_reconciliations

    @property
    def state(self) -> GateState:
        return self._state

    def begin_configuration_publish(self, revision: int) -> None:
        self.events.append(("begin", revision))
        self._state = GateState.QUIESCED

    def cancel_configuration_publish(self) -> None:
        self.events.append("cancel")
        self._state = GateState.RUNNING

    def reconcile_configuration_runtime(self) -> None:
        self.events.append("reconcile")
        if self._failed_reconciliations:
            self._failed_reconciliations -= 1
            raise DataTrunkError(
                "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
                "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
            )
        self._state = GateState.RUNNING


class _Pipeline:
    def __init__(self, gate: _Gate) -> None:
        self.data_trunk = SimpleNamespace(configuration_gate=gate)


class EmsWorkbenchProjectionTest(unittest.TestCase):
    def test_workbench_returns_five_slots_with_binding_metadata_and_no_fake_zero(self) -> None:
        # Break caught: omitting unconfigured slots or publishing a synthetic 0
        # instead of the resolved real L2 observation.
        catalog = EntityInstanceCatalog(
            _CatalogRepository((_entity(PCS_POWER_ID, "pcs.active_power"),))
        )
        slots = EmsWorkbenchSlots(catalog, _SlotRepository())
        subject = EmsWorkbench(catalog, _Runtime(), lambda: 7, slots)

        payload = subject.read()

        self.assertEqual(7, payload["configuration_revision"])
        self.assertEqual(5, len(payload["kpis"]))
        storage = next(item for item in payload["kpis"] if item["id"] == "storage-power")
        solar = next(item for item in payload["kpis"] if item["id"] == "pv-power")
        self.assertEqual("exact", storage["binding_mode"])
        self.assertEqual("bound", storage["binding_status"])
        self.assertEqual(123.5, storage["entity"]["value"])
        self.assertEqual("unconfigured", solar["binding_status"])
        self.assertIsNone(solar["entity"])
        self.assertNotIn("value", solar)

    def test_groups_and_slots_share_one_runtime_observation_per_entity(self) -> None:
        # Break caught: a changing L2 value being read once for the device group
        # and again for its KPI, exposing two values in one response.
        catalog = EntityInstanceCatalog(
            _CatalogRepository((_entity(PCS_POWER_ID, "pcs.active_power"),))
        )
        runtime = _IncrementingRuntime()
        subject = EmsWorkbench(
            catalog,
            runtime,
            lambda: 7,
            EmsWorkbenchSlots(catalog, _SlotRepository()),
        )

        payload = subject.read()

        group_entity = payload["groups"][0]["entities"][0]
        slot_entity = next(
            item for item in payload["kpis"] if item["id"] == "storage-power"
        )["entity"]
        self.assertEqual(1, group_entity["value"])
        self.assertEqual(group_entity, slot_entity)
        self.assertEqual({PCS_POWER_ID: 1}, runtime.read_calls)

    def test_workbench_resolves_slots_from_the_same_catalog_snapshot(self) -> None:
        # Break caught: a read querying the entity catalog once for groups and
        # again for slots can expose two different configuration snapshots.
        repository = _CatalogRepository(
            (_entity(PCS_POWER_ID, "pcs.active_power"),)
        )
        catalog = EntityInstanceCatalog(repository)
        subject = EmsWorkbench(
            catalog,
            _Runtime(),
            lambda: 7,
            EmsWorkbenchSlots(catalog, _SlotRepository()),
        )

        subject.read()

        self.assertEqual(1, repository.list_calls)


class _ErrorSlotRepository(_SlotRepository):
    def set_binding(self, **kwargs) -> WorkbenchSlotWriteReceipt:
        raise ConfigurationRevisionError(
            "CONFIGURATION_REVISION_STALE",
            "Configuration changed after the page was loaded",
        )


class EmsWorkbenchPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.catalog_repository = _CatalogRepository(
            (_entity(PCS_POWER_ID, "custom.storage_power"),)
        )
        self.slot_repository = _SlotRepository()
        self.slots = EmsWorkbenchSlots(
            EntityInstanceCatalog(self.catalog_repository),
            self.slot_repository,
        )
        self.gate = _Gate()
        app = FastAPI()
        app.include_router(ems_workbench_api.router, prefix="/api/v1")
        app.dependency_overrides[ems_workbench_api.get_ems_workbench_slots] = (
            lambda: self.slots
        )
        app.dependency_overrides[ems_workbench_api.get_ems_workbench] = lambda: EmsWorkbench(
            EntityInstanceCatalog(self.catalog_repository),
            _Runtime(),
            lambda: 7,
            self.slots,
        )
        app.dependency_overrides[ems_workbench_api.get_ems_workbench_runtime] = (
            lambda: _Pipeline(self.gate)
        )
        self.app = app

    async def _put(self, client, *, role: str, body: dict, key: str = "slot-write-v1"):
        return await client._client.put(
            "/api/v1/ems-workbench/slots/storage-power",
            json=body,
            headers={
                "Authorization": await client._bearer(role),
                "Idempotency-Key": key,
            },
        )

    async def test_engineer_and_admin_can_save_or_clear_a_slot(self) -> None:
        # Break caught: a declared configuration role cannot reach the formal
        # write seam, or clear is represented as a synthetic entity.
        async with AuthenticatedApiClient(self.app) as client:
            saved = await self._put(
                client,
                role="engineer",
                body={
                    "entity_instance_id": str(PCS_POWER_ID),
                    "base_configuration_revision": 7,
                },
            )
            cleared = await self._put(
                client,
                role="admin",
                body={
                    "entity_instance_id": None,
                    "base_configuration_revision": 8,
                },
                key="slot-clear-v1",
            )
            self.slot_repository.replay = WorkbenchSlotWriteReceipt(
                "storage-power",
                PCS_POWER_ID,
                8,
                True,
            )
            replayed = await self._put(
                client,
                role="engineer",
                body={
                    "entity_instance_id": str(PCS_POWER_ID),
                    "base_configuration_revision": 7,
                },
            )

        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(str(PCS_POWER_ID), saved.json()["entity_instance_id"])
        self.assertEqual(8, saved.json()["configuration_revision"])
        self.assertEqual(200, cleared.status_code, cleared.text)
        self.assertIsNone(cleared.json()["entity_instance_id"])
        self.assertEqual(200, replayed.status_code, replayed.text)
        self.assertTrue(replayed.json()["replayed"])
        self.assertEqual(8, replayed.json()["configuration_revision"])
        self.assertEqual(
            [
                ("begin", 7),
                "reconcile",
                ("begin", 8),
                "reconcile",
            ],
            self.gate.events,
        )

    async def test_operator_cannot_write_a_slot(self) -> None:
        # Break caught: client-side hiding being the only write protection.
        async with AuthenticatedApiClient(self.app) as client:
            response = await self._put(
                client,
                role="operator",
                body={
                    "entity_instance_id": str(PCS_POWER_ID),
                    "base_configuration_revision": 7,
                },
            )

        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual("PERMISSION_DENIED", response.json()["detail"]["code"])
        self.assertEqual([], self.slot_repository.calls)
        self.assertEqual([], self.gate.events)

    async def test_get_exposes_only_the_v105_single_entity_slot_contract(self) -> None:
        # Break caught: reintroducing the deleted legacy `entities` list during
        # the coordinated v1.0.5 hard cut.
        async with AuthenticatedApiClient(self.app) as client:
            response = await client.get("/api/v1/ems-workbench")

        self.assertEqual(200, response.status_code, response.text)
        slots = response.json()["kpis"]
        self.assertEqual(
            {
                "site-power",
                "pv-power",
                "storage-power",
                "storage-soc",
                "charging-power",
            },
            {slot["id"] for slot in slots},
        )
        for slot in slots:
            self.assertIn("entity", slot)
            self.assertNotIn("entities", slot)
            self.assertTrue(
                {
                    "binding_mode",
                    "binding_status",
                    "binding_source",
                    "reason",
                    "candidate_count",
                }.issubset(slot)
            )

    async def test_stale_revision_is_a_stable_409(self) -> None:
        # Break caught: configuration concurrency becoming a generic 500/422.
        self.slots = EmsWorkbenchSlots(
            EntityInstanceCatalog(self.catalog_repository),
            _ErrorSlotRepository(),
        )
        self.app.dependency_overrides[ems_workbench_api.get_ems_workbench_slots] = (
            lambda: self.slots
        )
        async with AuthenticatedApiClient(self.app) as client:
            response = await self._put(
                client,
                role="engineer",
                body={
                    "entity_instance_id": str(PCS_POWER_ID),
                    "base_configuration_revision": 7,
                },
            )

        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("CONFIGURATION_REVISION_STALE", response.json()["detail"]["code"])
        self.assertEqual([("begin", 7), "cancel"], self.gate.events)

    async def test_invalid_target_contracts_have_stable_http_errors(self) -> None:
        # Break caught: nonexistent/type/unit failures leaking as unstructured 500s.
        cases = (
            ((), str(SECOND_ENTITY_ID), 404, "WORKBENCH_SLOT_ENTITY_NOT_AVAILABLE"),
            (
                (_entity(PCS_POWER_ID, "custom", data_type="BOOL", unit="kW"),),
                str(PCS_POWER_ID),
                422,
                "WORKBENCH_SLOT_ENTITY_TYPE_INVALID",
            ),
            (
                (_entity(PCS_POWER_ID, "custom", data_type="FLOAT", unit="W"),),
                str(PCS_POWER_ID),
                422,
                "WORKBENCH_SLOT_ENTITY_UNIT_INVALID",
            ),
        )
        async with AuthenticatedApiClient(self.app) as client:
            for index, (descriptors, entity_id, expected_status, expected_code) in enumerate(cases):
                with self.subTest(code=expected_code):
                    self.catalog_repository.descriptors = descriptors
                    response = await self._put(
                        client,
                        role="engineer",
                        body={
                            "entity_instance_id": entity_id,
                            "base_configuration_revision": 7,
                        },
                        key=f"invalid-slot-{index}",
                    )
                    self.assertEqual(expected_status, response.status_code, response.text)
                    self.assertEqual(expected_code, response.json()["detail"]["code"])


if __name__ == "__main__":
    unittest.main()
