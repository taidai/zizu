"""Deterministic planning and application of L1 point processing."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from uuid import UUID

from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery
from app.services.solution_point_processings import point_processing_assets


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
EN9_REVISION_ID = UUID("81000000-0000-0000-0000-00000000000c")


def _assets():
    package = SolutionDelivery(
        InMemoryDeliveryRepository(),
        platform_version="0.4.77",
    ).import_package(builder.build_archive(), "user:test-engineer")
    return {item.asset_id: item for item in point_processing_assets(package)}


class PointProcessingTest(unittest.TestCase):
    def test_en9_scan_produces_one_unified_l0_l1_l2_plan(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PreviewPointProcessing,
            PointProcessingDelivery,
        )
        from tests.test_neuron_point_processing_catalog import FakeNeuron

        assets = _assets()
        repository = InMemoryPointProcessingRepository()
        neuron = FakeNeuron()
        catalog = InMemoryPointProcessingCatalog(
            templates={EN9_REVISION_ID: assets["pcs.en9"]},
            node_source_keys={NODE_ID: "EN9-PCS"},
        )
        service = PointProcessingDelivery(
            repository,
            catalog,
            point_scanner=NeuronPointCatalog(neuron),
        )

        plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=EN9_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )

        self.assertEqual("ready", plan.status)
        self.assertEqual((), plan.blockers)
        self.assertEqual({"L0", "L1", "L2"}, {item["layer"] for item in plan.items})
        self.assertEqual(
            {"L0": 90, "L1": 90, "L2": 3},
            {
                layer: sum(item["layer"] == layer for item in plan.items)
                for layer in ("L0", "L1", "L2")
            },
        )
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "install-en9",
                "user:engineer",
            )
        )
        snapshot = repository.installed_processings(catalog)
        fault_processing = next(
            item for item in snapshot
            if item.entity_definition_id == "pcs.fault_codes"
        )
        self.assertEqual(3, len(snapshot))
        self.assertEqual(
            application.installed_processing_id,
            fault_processing.installation_id,
        )
        self.assertEqual(88, len(fault_processing.transform.inputs))
        power = next(
            item for item in plan.items
            if item["layer"] == "L0" and item["resource_key"] == "1!424634"
        )
        self.assertEqual(
            {
                "wire_data_type": "INT16",
                "value_data_type": "FLOAT",
                "decimal": 0.1,
                "read_only": True,
                "freshness_seconds": 5.0,
            },
            {
                key: power["after"][key]
                for key in (
                    "wire_data_type",
                    "value_data_type",
                    "decimal",
                    "read_only",
                    "freshness_seconds",
                )
            },
        )

    def test_en9_apply_rejects_point_catalog_changed_after_preview(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PointProcessingDelivery,
            PointProcessingError,
            PreviewPointProcessing,
        )
        from tests.test_neuron_point_processing_catalog import FakeNeuron

        assets = _assets()
        repository = InMemoryPointProcessingRepository()
        neuron = FakeNeuron()
        service = PointProcessingDelivery(
            repository,
            InMemoryPointProcessingCatalog(
                templates={EN9_REVISION_ID: assets["pcs.en9"]},
                node_source_keys={NODE_ID: "EN9-PCS"},
            ),
            point_scanner=NeuronPointCatalog(neuron),
        )
        plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=EN9_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )
        neuron.tags[0]["decimal"] = 1.0

        with self.assertRaises(PointProcessingError) as caught:
            service.apply(
                ApplyPointProcessingPlan(
                    plan.id,
                    plan.digest,
                    "stale-en9",
                    "user:engineer",
                )
            )

        self.assertEqual("POINT_PROCESSING_PLAN_STALE", caught.exception.code)

    def test_en9_preview_blocks_decimal_contract_mismatch(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog
        from app.services.point_processing import (
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PointProcessingDelivery,
            PreviewPointProcessing,
        )
        from tests.test_neuron_point_processing_catalog import FakeNeuron

        neuron = FakeNeuron()
        neuron.tags[0]["decimal"] = 1.0
        service = PointProcessingDelivery(
            InMemoryPointProcessingRepository(),
            InMemoryPointProcessingCatalog(
                templates={EN9_REVISION_ID: _assets()["pcs.en9"]},
                node_source_keys={NODE_ID: "EN9-PCS"},
            ),
            point_scanner=NeuronPointCatalog(neuron),
        )

        plan = service.preview(PreviewPointProcessing(
            node_id=NODE_ID,
            template_revision_id=EN9_REVISION_ID,
            input_selections={},
            actor="user:engineer",
            entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
            solution_installation_id=SOLUTION_INSTALLATION_ID,
        ))

        self.assertEqual("blocked", plan.status)
        self.assertIn(
            "NEURON_POINT_CONTRACT_MISMATCH",
            {item["code"] for item in plan.blockers},
        )

    def test_applied_template_exposes_installed_runtime_snapshot(self) -> None:
        from app.services.data_trunk_contracts import (
            EnumTransform,
            FaultCodeTransform,
            InputReference,
            NumericTransform,
        )
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PreviewPointProcessing,
            PointProcessingDelivery,
            PointProcessingSource,
        )

        source_ids = {
            "active_power_raw": UUID("84000000-0000-0000-0000-000000000001"),
            "operating_state_raw": UUID("84000000-0000-0000-0000-000000000002"),
            "fault_codes_raw": UUID("84000000-0000-0000-0000-000000000003"),
        }
        assets = _assets()
        repository = InMemoryPointProcessingRepository()
        catalog = InMemoryPointProcessingCatalog(
            templates={BRAND_A_REVISION_ID: assets["pcs.brand-a"]},
            sources=(
                PointProcessingSource(source_ids["active_power_raw"], "l0", NODE_ID, "ActivePowerRaw", "FLOAT", "W", True),
                PointProcessingSource(source_ids["operating_state_raw"], "l0", NODE_ID, "RunningState", "STRING", None, True),
                PointProcessingSource(source_ids["fault_codes_raw"], "l0", NODE_ID, "FaultCodeText", "STRING", None, True),
            ),
        )
        service = PointProcessingDelivery(repository, catalog)
        plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=BRAND_A_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "runtime-snapshot",
                "user:engineer",
            )
        )

        snapshot = repository.installed_processings(catalog)

        self.assertEqual(len(snapshot), 3)
        self.assertEqual(
            {item.installation_id for item in snapshot},
            {application.installed_processing_id},
        )
        by_definition = {item.entity_definition_id: item for item in snapshot}
        self.assertIsInstance(
            by_definition["pcs.active_power"].transform,
            NumericTransform,
        )
        self.assertEqual(
            by_definition["pcs.active_power"].transform.input,
            InputReference.l0(source_ids["active_power_raw"]),
        )
        self.assertEqual(
            by_definition["pcs.active_power"].transform.scale,
            0.001,
        )
        self.assertIsInstance(
            by_definition["pcs.operating_state"].transform,
            EnumTransform,
        )
        self.assertEqual(
            by_definition["pcs.operating_state"].transform.entries["2"],
            "RUNNING",
        )
        self.assertIsInstance(
            by_definition["pcs.fault_codes"].transform,
            FaultCodeTransform,
        )
        self.assertEqual(
            by_definition["pcs.fault_codes"].transform.entries["E30"],
            "COMPRESSOR_FAULT",
        )

    def test_plan_blocks_missing_and_ambiguous_required_inputs(self) -> None:
        from app.services.point_processing import (
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PreviewPointProcessing,
            PointProcessingDelivery,
            PointProcessingSource,
        )

        assets = _assets()
        repository = InMemoryPointProcessingRepository()
        catalog = InMemoryPointProcessingCatalog(
            templates={BRAND_A_REVISION_ID: assets["pcs.brand-a"]},
            sources=(
                PointProcessingSource(
                    UUID("82000000-0000-0000-0000-000000000001"),
                    "l0",
                    NODE_ID,
                    "ActivePowerRaw",
                    "FLOAT",
                    "W",
                    True,
                ),
                PointProcessingSource(
                    UUID("82000000-0000-0000-0000-000000000002"),
                    "l0",
                    NODE_ID,
                    "RunningState",
                    "STRING",
                    None,
                    True,
                ),
                PointProcessingSource(
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
        service = PointProcessingDelivery(repository, catalog)

        plan = service.preview(
            PreviewPointProcessing(
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
                "POINT_PROCESSING_INPUT_MISSING",
                "POINT_PROCESSING_INPUT_AMBIGUOUS",
            },
            {item["code"] for item in plan.blockers},
        )
        self.assertEqual(0, repository.application_count())

    def test_brand_replacement_preserves_output_entity_ids(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PreviewPointProcessing,
            PointProcessingDelivery,
            PointProcessingSource,
        )

        assets = _assets()
        repository = InMemoryPointProcessingRepository()
        catalog = InMemoryPointProcessingCatalog(
            templates={
                BRAND_A_REVISION_ID: assets["pcs.brand-a"],
                BRAND_B_REVISION_ID: assets["pcs.brand-b"],
            },
            sources=(
                PointProcessingSource(UUID("83000000-0000-0000-0000-000000000001"), "l0", NODE_ID, "ActivePowerRaw", "FLOAT", "W", True),
                PointProcessingSource(UUID("83000000-0000-0000-0000-000000000002"), "l0", NODE_ID, "RunningState", "STRING", None, True),
                PointProcessingSource(UUID("83000000-0000-0000-0000-000000000003"), "l0", NODE_ID, "FaultCodeText", "STRING", None, True),
            ),
        )
        service = PointProcessingDelivery(repository, catalog)
        first_plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=BRAND_A_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )
        first_command = ApplyPointProcessingPlan(
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
                PointProcessingSource(UUID("83000000-0000-0000-0000-000000000011"), "l0", NODE_ID, "PActKw", "FLOAT", "kW", True),
                PointProcessingSource(UUID("83000000-0000-0000-0000-000000000012"), "l0", NODE_ID, "ModeCode", "STRING", None, True),
                PointProcessingSource(UUID("83000000-0000-0000-0000-000000000013"), "l0", NODE_ID, "AlarmList", "STRING", None, True),
            )
        )
        second_plan = service.preview(
            PreviewPointProcessing(
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
            ApplyPointProcessingPlan(
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
