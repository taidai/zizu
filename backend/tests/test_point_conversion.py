"""Deterministic planning and application of L1 point conversions."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from uuid import UUID

from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery
from app.services.solution_point_conversions import point_conversion_assets


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_reference_delivery.py"
SPEC = importlib.util.spec_from_file_location("build_reference_delivery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

NODE_ID = UUID("81000000-0000-0000-0000-000000000001")
ENTITY_IDENTITY_INSTALLATION_ID = UUID("81000000-0000-0000-0000-000000000002")
SOLUTION_INSTALLATION_ID = UUID("81000000-0000-0000-0000-000000000003")
BRAND_A_REVISION_ID = UUID("81000000-0000-0000-0000-00000000000a")
BRAND_B_REVISION_ID = UUID("81000000-0000-0000-0000-00000000000b")


def _assets():
    package = SolutionDelivery(
        InMemoryDeliveryRepository(),
        platform_version="0.4.77",
    ).import_package(builder.build_archive())
    return {item.asset_id: item for item in point_conversion_assets(package)}


class PointConversionTest(unittest.TestCase):
    def test_plan_blocks_missing_and_ambiguous_required_inputs(self) -> None:
        from app.services.point_conversion import (
            InMemoryPointConversionCatalog,
            InMemoryPointConversionRepository,
            PlanPointConversion,
            PointConversion,
            PointConversionSource,
        )

        assets = _assets()
        repository = InMemoryPointConversionRepository()
        catalog = InMemoryPointConversionCatalog(
            templates={BRAND_A_REVISION_ID: assets["pcs.brand-a"]},
            sources=(
                PointConversionSource(
                    UUID("82000000-0000-0000-0000-000000000001"),
                    "l0",
                    NODE_ID,
                    "ActivePowerRaw",
                    "FLOAT",
                    "W",
                    True,
                ),
                PointConversionSource(
                    UUID("82000000-0000-0000-0000-000000000002"),
                    "l0",
                    NODE_ID,
                    "RunningState",
                    "STRING",
                    None,
                    True,
                ),
                PointConversionSource(
                    UUID("82000000-0000-0000-0000-000000000003"),
                    "l0",
                    NODE_ID,
                    "RunningState",
                    "STRING",
                    None,
                    True,
                ),
            ),
        )
        service = PointConversion(repository, catalog)

        plan = service.plan(
            PlanPointConversion(
                node_id=NODE_ID,
                template_revision_id=BRAND_A_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )

        self.assertEqual("blocked", plan.status)
        self.assertEqual(
            {
                "POINT_CONVERSION_INPUT_MISSING",
                "POINT_CONVERSION_INPUT_AMBIGUOUS",
            },
            {item["code"] for item in plan.blockers},
        )
        self.assertEqual(0, repository.application_count())

    def test_brand_replacement_preserves_output_entity_ids(self) -> None:
        from app.services.point_conversion import (
            ApplyPointConversionPlan,
            InMemoryPointConversionCatalog,
            InMemoryPointConversionRepository,
            PlanPointConversion,
            PointConversion,
            PointConversionSource,
        )

        assets = _assets()
        repository = InMemoryPointConversionRepository()
        catalog = InMemoryPointConversionCatalog(
            templates={
                BRAND_A_REVISION_ID: assets["pcs.brand-a"],
                BRAND_B_REVISION_ID: assets["pcs.brand-b"],
            },
            sources=(
                PointConversionSource(UUID("83000000-0000-0000-0000-000000000001"), "l0", NODE_ID, "ActivePowerRaw", "FLOAT", "W", True),
                PointConversionSource(UUID("83000000-0000-0000-0000-000000000002"), "l0", NODE_ID, "RunningState", "STRING", None, True),
                PointConversionSource(UUID("83000000-0000-0000-0000-000000000003"), "l0", NODE_ID, "FaultCodeText", "STRING", None, True),
            ),
        )
        service = PointConversion(repository, catalog)
        first_plan = service.plan(
            PlanPointConversion(
                node_id=NODE_ID,
                template_revision_id=BRAND_A_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )
        first_command = ApplyPointConversionPlan(
            first_plan.id,
            first_plan.digest,
            "install-brand-a",
            "user:engineer",
        )
        first = service.apply(first_command)
        replayed = service.apply(first_command)
        self.assertEqual(first, replayed)
        self.assertEqual(1, repository.application_count())

        catalog.replace_sources(
            (
                PointConversionSource(UUID("83000000-0000-0000-0000-000000000011"), "l0", NODE_ID, "PActKw", "FLOAT", "kW", True),
                PointConversionSource(UUID("83000000-0000-0000-0000-000000000012"), "l0", NODE_ID, "ModeCode", "STRING", None, True),
                PointConversionSource(UUID("83000000-0000-0000-0000-000000000013"), "l0", NODE_ID, "AlarmList", "STRING", None, True),
            )
        )
        second_plan = service.plan(
            PlanPointConversion(
                node_id=NODE_ID,
                template_revision_id=BRAND_B_REVISION_ID,
                input_selections={},
                actor="user:engineer",
            )
        )
        self.assertEqual(
            {"update"},
            {
                item["action"]
                for item in second_plan.items
                if item["kind"] == "input_binding"
            },
        )
        self.assertEqual(
            {"preserve"},
            {
                item["action"]
                for item in second_plan.items
                if item["kind"] == "output_binding"
            },
        )
        second = service.apply(
            ApplyPointConversionPlan(
                second_plan.id,
                second_plan.digest,
                "replace-with-brand-b",
                "user:engineer",
            )
        )

        self.assertNotEqual(first.revision_id, second.revision_id)
        self.assertEqual(
            first.output_entity_instance_ids,
            second.output_entity_instance_ids,
        )


if __name__ == "__main__":
    unittest.main()
