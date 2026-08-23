"""Deterministic planning and application of L1 point processing."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from uuid import UUID

from app.services.solution_delivery import InMemoryDeliveryRepository, SolutionDelivery
from app.services.solution_point_processings import (
    parse_point_processing_asset,
    point_processing_assets,
)


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
SITE_FORMULA_REVISION_ID = UUID("81000000-0000-0000-0000-00000000000d")
PCS_POWER_1 = UUID("85000000-0000-0000-0000-000000000001")
PCS_POWER_2 = UUID("85000000-0000-0000-0000-000000000002")
GRID_POWER = UUID("85000000-0000-0000-0000-000000000003")


def _site_formula_asset():
    return parse_point_processing_asset(
        {
            "schemaVersion": "zizu.point-processing/v1alpha1",
            "id": "site.total-pcs-power",
            "kind": "point_processing_template",
            "displayName": "站级 PCS 总功率",
            "deviceCategory": "SITE",
            "brand": "ZiZu",
            "model": "SITE-POWER",
            "revision": 1,
            "status": "active",
            "inputs": [
                {
                    "id": "pcs_power",
                    "sourceKind": "l2",
                    "sourceKey": "pcs.active_power",
                    "aliases": [],
                    "dataType": "FLOAT",
                    "unit": "kW",
                    "required": True,
                    "cardinality": "many",
                    "selector": {
                        "scope": "descendants",
                        "nodeType": "PCS",
                        "entityDefinition": "pcs.active_power",
                    },
                }
            ],
            "outputs": [
                {
                    "id": "total_power",
                    "entityDefinition": "site.total_pcs_power",
                    "dataType": "FLOAT",
                    "unit": "kW",
                    "freshness": "5s",
                    "transform": {
                        "kind": "formula",
                        "expression": "sum(pcs_power)",
                        "scheduleSeconds": 1,
                        "controlEligible": False,
                    },
                }
            ],
        }
    )


def _site_mixed_formula_asset():
    payload = {
        "schemaVersion": "zizu.point-processing/v1alpha1",
        "id": "site.net-power",
        "kind": "point_processing_template",
        "displayName": "站级净功率",
        "deviceCategory": "SITE",
        "brand": "ZiZu",
        "model": "SITE-NET-POWER",
        "revision": 1,
        "status": "active",
        "inputs": [
            {
                "id": "pcs_power",
                "sourceKind": "l2",
                "sourceKey": "pcs.active_power",
                "aliases": [],
                "dataType": "FLOAT",
                "unit": "kW",
                "required": True,
                "cardinality": "many",
                "selector": {
                    "scope": "descendants",
                    "nodeType": "PCS",
                    "entityDefinition": "pcs.active_power",
                },
            },
            {
                "id": "grid_power",
                "sourceKind": "l2",
                "sourceKey": "grid.active_power",
                "aliases": [],
                "dataType": "FLOAT",
                "unit": "kW",
                "required": True,
            },
        ],
        "outputs": [
            {
                "id": "net_power",
                "entityDefinition": "site.net_power",
                "dataType": "FLOAT",
                "unit": "kW",
                "freshness": "5s",
                "transform": {
                    "kind": "formula",
                    "expression": "sum(pcs_power) + grid_power",
                    "scheduleSeconds": 1,
                    "controlEligible": False,
                },
            }
        ],
    }
    return parse_point_processing_asset(payload)


def _assets():
    package = SolutionDelivery(
        InMemoryDeliveryRepository(),
        platform_version="0.4.77",
    ).import_package(builder.build_archive(), "user:test-engineer")
    return {item.asset_id: item for item in point_processing_assets(package)}


class PointProcessingTest(unittest.TestCase):
    def test_formula_plan_accepts_selector_and_single_l2_inputs(self) -> None:
        from app.services.data_trunk_contracts import InputReference
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PointProcessingDelivery,
            PointProcessingSource,
            PreviewPointProcessing,
        )

        repository = InMemoryPointProcessingRepository()
        catalog = InMemoryPointProcessingCatalog(
            templates={SITE_FORMULA_REVISION_ID: _site_mixed_formula_asset()},
            sources=(
                PointProcessingSource(
                    GRID_POWER,
                    "l2",
                    NODE_ID,
                    "grid.active_power",
                    "FLOAT",
                    "kW",
                    True,
                ),
            ),
            selector_members={
                (NODE_ID, "PCS", "pcs.active_power"): (PCS_POWER_1, PCS_POWER_2),
            },
        )
        service = PointProcessingDelivery(repository, catalog)

        plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=SITE_FORMULA_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )

        self.assertEqual("ready", plan.status)
        dag_item = next(item for item in plan.items if item["kind"] == "dag_validation")
        self.assertEqual(3, len(dag_item["planned_edges"]))
        service.apply(
            ApplyPointProcessingPlan(plan.id, plan.digest, "mixed-formula", "user:engineer")
        )
        transform = repository.installed_processings(catalog)[0].transform
        self.assertEqual(transform.sources["grid_power"], (InputReference.l2(GRID_POWER),))

    def test_formula_plan_freezes_selector_and_builds_site_dag(self) -> None:
        from app.services.data_trunk_contracts import FormulaTransform, InputReference
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PointProcessingDelivery,
            PreviewPointProcessing,
        )

        repository = InMemoryPointProcessingRepository()
        catalog = InMemoryPointProcessingCatalog(
            templates={SITE_FORMULA_REVISION_ID: _site_formula_asset()},
            selector_members={
                (NODE_ID, "PCS", "pcs.active_power"): (PCS_POWER_2, PCS_POWER_1),
            },
        )
        service = PointProcessingDelivery(repository, catalog)

        plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=SITE_FORMULA_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )

        selector_item = next(item for item in plan.items if item["kind"] == "selector_binding")
        dag_item = next(item for item in plan.items if item["kind"] == "dag_validation")
        self.assertEqual(selector_item["selected_source_ids"], (str(PCS_POWER_1), str(PCS_POWER_2)))
        self.assertEqual(selector_item["action"], "add")
        self.assertEqual(dag_item["max_depth"], 2)
        self.assertEqual(plan.status, "ready")

        application = service.apply(
            ApplyPointProcessingPlan(plan.id, plan.digest, "site-formula", "user:engineer")
        )
        installed = repository.installed_processings(catalog)
        self.assertEqual(installed[0].installation_id, application.installed_processing_id)
        self.assertIsInstance(installed[0].transform, FormulaTransform)
        self.assertEqual(
            installed[0].transform.sources["pcs_power"],
            (InputReference.l2(PCS_POWER_1), InputReference.l2(PCS_POWER_2)),
        )

    def test_formula_apply_rejects_selector_members_changed_after_preview(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PointProcessingDelivery,
            PointProcessingError,
            PreviewPointProcessing,
        )

        repository = InMemoryPointProcessingRepository()
        catalog = InMemoryPointProcessingCatalog(
            templates={SITE_FORMULA_REVISION_ID: _site_formula_asset()},
            selector_members={
                (NODE_ID, "PCS", "pcs.active_power"): (PCS_POWER_1, PCS_POWER_2),
            },
        )
        service = PointProcessingDelivery(repository, catalog)
        plan = service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=SITE_FORMULA_REVISION_ID,
                input_selections={},
                actor="user:engineer",
                entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
                solution_installation_id=SOLUTION_INSTALLATION_ID,
            )
        )
        catalog.replace_selector_members(
            {(NODE_ID, "PCS", "pcs.active_power"): (PCS_POWER_1,)}
        )

        with self.assertRaises(PointProcessingError) as raised:
            service.apply(
                ApplyPointProcessingPlan(plan.id, plan.digest, "stale-selector", "user:engineer")
            )

        self.assertEqual(raised.exception.code, "POINT_PROCESSING_SELECTOR_STALE")
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

    def test_en9_source_contract_disambiguates_same_name_in_command_group(self) -> None:
        from app.services.neuron_point_processing_catalog import NeuronPointCatalog
        from app.services.point_processing import (
            InMemoryPointProcessingCatalog,
            InMemoryPointProcessingRepository,
            PointProcessingDelivery,
            PreviewPointProcessing,
        )
        from tests.test_neuron_point_processing_catalog import FakeNeuron

        class MultiGroupNeuron(FakeNeuron):
            def get_groups(self, node_name: str) -> list[dict]:
                self.node_name = node_name
                return [
                    {"name": "data", "interval": self.interval},
                    {"name": "command", "interval": self.interval},
                ]

            def get_tags(self, node_name: str, group_name: str) -> list[dict]:
                if group_name == "data":
                    return list(self.tags)
                command = dict(self.tags[0])
                command["attribute"] = 2
                return [command]

        service = PointProcessingDelivery(
            InMemoryPointProcessingRepository(),
            InMemoryPointProcessingCatalog(
                templates={EN9_REVISION_ID: _assets()["pcs.en9"]},
                node_source_keys={NODE_ID: "EN9-PCS"},
            ),
            point_scanner=NeuronPointCatalog(MultiGroupNeuron()),
        )

        plan = service.preview(PreviewPointProcessing(
            node_id=NODE_ID,
            template_revision_id=EN9_REVISION_ID,
            input_selections={},
            actor="user:engineer",
            entity_identity_installation_id=ENTITY_IDENTITY_INSTALLATION_ID,
            solution_installation_id=SOLUTION_INSTALLATION_ID,
        ))

        self.assertEqual("ready", plan.status)
        self.assertEqual((), plan.blockers)

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
