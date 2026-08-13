from __future__ import annotations

import unittest
from uuid import UUID

from app.services.entity_instance_registry import (
    ApplyEntityInstancePlan,
    EntityInstanceError,
    EntityInstanceRegistry,
    InMemoryEntityInstanceRepository,
    InMemorySourceCatalog,
    PlanEntityInstances,
    SourceDescriptor,
)


INSTALLATION_ID = UUID("10000000-0000-0000-0000-000000000001")
TAG_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_TAG_ID = UUID("20000000-0000-0000-0000-000000000002")


def source(
    tag_id: UUID = TAG_ID,
    *,
    data_type: str = "FLOAT",
    unit: str | None = "kW",
    direction: str = "R",
    tag_name: str = "ActivePower",
) -> SourceDescriptor:
    return SourceDescriptor(
        tag_id=tag_id,
        device_key="PCS-01",
        device_name="Primary PCS",
        tag_name=tag_name,
        data_type=data_type,
        unit=unit,
        direction=direction,
        enabled=True,
    )


def slot(*, display_name: str = "PCS 1") -> dict:
    return {
        "id": "pcs.primary",
        "device_category": "pcs",
        "instance_key": "PCS-01",
        "display_name": display_name,
        "freshness_seconds": 30.0,
        "definitions": [
            {
                "id": "pcs.activePower",
                "display_name": "Active power",
                "data_type": "FLOAT",
                "unit": "kW",
                "direction": "R",
                "matcher": {
                    "id": "matcher.pcs-active-power",
                    "device_key": "PCS-01",
                    "tag_name": "ActivePower",
                },
            }
        ],
    }


class EntityInstanceRegistryTest(unittest.TestCase):
    def build_registry(
        self,
        sources: tuple[SourceDescriptor, ...],
    ) -> tuple[
        EntityInstanceRegistry,
        InMemoryEntityInstanceRepository,
        InMemorySourceCatalog,
    ]:
        repository = InMemoryEntityInstanceRepository()
        catalog = InMemorySourceCatalog(sources)
        registry = EntityInstanceRegistry(
            repository,
            catalog,
            current_site_configuration_version=lambda transaction=None: 3,
        )
        return registry, repository, catalog

    @staticmethod
    def request(
        *,
        slots: tuple[dict, ...] | None = None,
        selections: dict[str, UUID] | None = None,
    ) -> PlanEntityInstances:
        return PlanEntityInstances(
            package_digest="a" * 64,
            site_configuration_version=3,
            installation_id=INSTALLATION_ID,
            slots=slots or (slot(),),
            selections=selections or {},
            actor="user:00000000-0000-0000-0000-000000000002",
        )

    def test_unique_candidate_is_explainable_idempotent_and_resolvable(self) -> None:
        registry, repository, catalog = self.build_registry((source(),))

        plan = registry.plan(self.request())

        self.assertEqual("ready", plan.status)
        self.assertEqual((), plan.blockers)
        item = plan.items[0]
        self.assertEqual("ENTITY_BINDING_READY", item["code"])
        self.assertEqual(str(TAG_ID), item["selected_tag_id"])
        self.assertEqual(
            "matcher.pcs-active-power: device_key=PCS-01, tag_name=ActivePower",
            item["candidates"][0]["reason"],
        )

        command = ApplyEntityInstancePlan(
            plan_id=plan.id,
            plan_digest=plan.digest,
            actor=self.request().actor,
        )
        first = registry.apply(command)
        second = registry.apply(command)
        self.assertEqual(first, second)
        self.assertEqual(1, repository.device_instance_count)
        self.assertEqual(1, repository.entity_instance_count)
        self.assertEqual(1, repository.binding_count)

        resolved = registry.resolve(first.entity_instance_ids[0])
        self.assertEqual(TAG_ID, resolved.tag_id)
        self.assertEqual("pcs.activePower", resolved.definition_id)
        self.assertEqual("PCS-01", resolved.instance_key)
        self.assertEqual("matcher.pcs-active-power", resolved.matcher_id)

        catalog.replace(
            (
                SourceDescriptor(
                    **{
                        **source().__dict__,
                        "enabled": False,
                    }
                ),
            )
        )
        with self.assertRaises(EntityInstanceError) as disabled:
            registry.resolve(first.entity_instance_ids[0])
        self.assertEqual("ENTITY_BINDING_SOURCE_INVALID", disabled.exception.code)
        catalog.replace((source(),))

        # Display changes and catalog query order do not alter identity/source.
        catalog.replace((source(OTHER_TAG_ID, tag_name="Other"), source()))
        upgraded = registry.plan(
            self.request(slots=(slot(display_name="Renamed PCS"),))
        )
        self.assertEqual(
            str(first.entity_instance_ids[0]),
            upgraded.items[0]["entity_instance_id"],
        )
        self.assertEqual(str(TAG_ID), upgraded.items[0]["selected_tag_id"])

    def test_zero_and_multiple_candidates_block_without_writes(self) -> None:
        for sources, expected_code in (
            ((), "ENTITY_BINDING_MISSING"),
            (
                (source(), source(OTHER_TAG_ID)),
                "ENTITY_BINDING_AMBIGUOUS",
            ),
        ):
            with self.subTest(expected_code=expected_code):
                registry, repository, _ = self.build_registry(sources)
                plan = registry.plan(self.request())
                self.assertEqual("blocked", plan.status)
                self.assertEqual(expected_code, plan.blockers[0]["code"])
                with self.assertRaises(EntityInstanceError) as raised:
                    registry.apply(
                        ApplyEntityInstancePlan(
                            plan.id,
                            plan.digest,
                            self.request().actor,
                        )
                    )
                self.assertEqual("ENTITY_INSTANCE_PLAN_BLOCKED", raised.exception.code)
                self.assertEqual(0, repository.entity_instance_count)
                self.assertEqual(0, repository.binding_count)

    def test_engineer_selection_resolves_an_ambiguous_candidate(self) -> None:
        registry, _, _ = self.build_registry((source(), source(OTHER_TAG_ID)))
        key = "pcs.primary/PCS-01/pcs.activePower"

        plan = registry.plan(self.request(selections={key: OTHER_TAG_ID}))

        self.assertEqual("ready", plan.status)
        self.assertEqual(str(OTHER_TAG_ID), plan.items[0]["selected_tag_id"])
        self.assertEqual("engineer_selection", plan.items[0]["selection_source"])

    def test_incompatible_source_reports_stable_machine_code(self) -> None:
        cases = (
            (source(data_type="INT"), "ENTITY_BINDING_TYPE_MISMATCH"),
            (source(unit="W"), "ENTITY_BINDING_UNIT_MISMATCH"),
            (source(direction="W"), "ENTITY_BINDING_DIRECTION_MISMATCH"),
        )
        for candidate, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                registry, repository, _ = self.build_registry((candidate,))
                plan = registry.plan(self.request())
                self.assertEqual("blocked", plan.status)
                self.assertEqual(expected_code, plan.blockers[0]["code"])
                self.assertEqual(0, repository.entity_instance_count)

    def test_catalog_change_makes_plan_stale_and_unbound_never_falls_back(self) -> None:
        registry, repository, catalog = self.build_registry((source(),))
        plan = registry.plan(self.request())
        catalog.replace((source(), source(OTHER_TAG_ID, tag_name="Other")))

        with self.assertRaises(EntityInstanceError) as raised:
            registry.apply(
                ApplyEntityInstancePlan(plan.id, plan.digest, self.request().actor)
            )
        self.assertEqual("ENTITY_BINDING_PLAN_STALE", raised.exception.code)
        self.assertEqual(0, repository.binding_count)

        unknown = UUID("30000000-0000-0000-0000-000000000001")
        with self.assertRaises(EntityInstanceError) as unresolved:
            registry.resolve(unknown)
        self.assertEqual("ENTITY_INSTANCE_NOT_BOUND", unresolved.exception.code)


if __name__ == "__main__":
    unittest.main()
