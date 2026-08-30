"""Real PostgreSQL evidence for direct node-owned L1 publication."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg2

os.environ.setdefault("NEURON_PASSWORD", "test-neuron-secret")
os.environ.setdefault("NANOMQ_API_PASSWORD", "test-nanomq-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value-that-is-long-enough")

from tests import test_data_frames_postgres as frame_runtime
from tests import test_committed_frame_consumers_migration_postgres as frame_consumers


REFERENCE_DIR = Path(__file__).resolve().parents[2] / "reference-point-processings"
MIGRATION_050 = Path(__file__).resolve().parents[2] / "init-db" / "migration_050_node_l0_usability.sql"
MIGRATION_051 = Path(__file__).resolve().parents[2] / "init-db" / "migration_051_node_private_point_processing.sql"
NODE_ID = UUID("92000000-0000-0000-0000-000000000001")


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL point-processing tests",
)
class PointProcessingPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Point-processing tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA IF EXISTS zizu_internal CASCADE")
            connection.commit()
        frame_runtime.DataFramesPostgresTest.setUpClass()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    frame_consumers.MIGRATION_049.read_text(encoding="utf-8")
                )
                cursor.execute(MIGRATION_050.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_051.read_text(encoding="utf-8"))
        from app.services.telemetry_store import init_db_pool

        init_db_pool(1, 4)
        self._seed_node_and_tags()

    def tearDown(self) -> None:
        from app.services.telemetry_store import close_db_pool

        close_db_pool()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA IF EXISTS zizu_internal CASCADE")
            connection.commit()

    @staticmethod
    def _raw(name: str) -> dict:
        return json.loads(
            (REFERENCE_DIR / f"{name}.zizu-point-processing.json").read_text(
                encoding="utf-8"
            )
        )

    def _seed_node_and_tags(self) -> None:
        from app.services.telemetry_store import get_connection

        contracts: dict[str, tuple[str, str | None]] = {}
        for name in ("pcs-brand-a", "pcs-brand-b"):
            for item in self._raw(name)["inputs"]:
                contracts[item["sourceKey"]] = (item["dataType"], item.get("unit"))
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled,layer) "
                    "VALUES(%s,'PCS-TEST','PCS',TRUE,1)",
                    (NODE_ID,),
                )
                for key, (data_type, unit) in sorted(contracts.items()):
                    cursor.execute(
                        """
                        INSERT INTO t_tags
                          (id,node_id,name,data_type,unit,read_write,enabled,
                           timestamp_trusted)
                        VALUES(%s,%s,%s,%s,%s,'R',TRUE,FALSE)
                        """,
                        (
                            uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/{key}"),
                            NODE_ID,
                            key,
                            data_type,
                            unit,
                        ),
                    )
            connection.commit()

    def _service_and_revision(self, name: str):
        from app.services.point_processing import PointProcessingService
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )

        registered = PostgresPointProcessingTemplates().import_template(
            self._raw(name),
            actor="test:engineer",
        )
        repository = PostgresPointProcessingRepository()
        return (
            PointProcessingService(repository, PostgresPointProcessingCatalog()),
            repository,
            registered.revision_id,
        )

    def _plan(self, service, revision_id):
        from app.services.point_processing import PreviewPointProcessing

        return service.preview(
            PreviewPointProcessing(
                node_id=NODE_ID,
                template_revision_id=revision_id,
                input_selections={},
                actor="test:engineer",
            )
        )

    def test_apply_publishes_one_revision_and_attaches_l2_directly_to_node(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan
        from app.services.telemetry_store import get_connection

        service, repository, revision_id = self._service_and_revision("pcs-brand-a")
        plan = self._plan(service, revision_id)
        application = service.apply(
            ApplyPointProcessingPlan(plan.id, plan.digest, "apply-1", "test:engineer")
        )

        self.assertEqual(0, plan.base_configuration_revision)
        self.assertEqual(1, application.configuration_revision)
        self.assertEqual(1, repository.configuration_revision())
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT node_id FROM t_entity_instances "
                    "WHERE id=ANY(%s)",
                    (list(application.output_entity_instance_ids),),
                )
                self.assertEqual([(NODE_ID,)], cursor.fetchall())

    def test_stale_plan_writes_no_installation(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan, PointProcessingError
        from app.services.telemetry_store import get_connection

        service, _repository, revision_id = self._service_and_revision("pcs-brand-a")
        plan = self._plan(service, revision_id)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_configuration_revisions
                      (revision,previous_revision,actor,action,resource_kind,
                       resource_id,after_digest)
                    VALUES(1,0,'test','test.bump','test','test',%s)
                    """,
                    ("f" * 64,),
                )
                cursor.execute(
                    "UPDATE t_configuration_state SET current_revision=1 "
                    "WHERE singleton=TRUE"
                )
            connection.commit()

        with self.assertRaises(PointProcessingError) as caught:
            service.apply(
                ApplyPointProcessingPlan(plan.id, plan.digest, "stale", "test:engineer")
            )
        self.assertEqual("POINT_PROCESSING_PLAN_STALE", caught.exception.code)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_installed_point_processings")
                self.assertEqual(0, cursor.fetchone()[0])

    def test_brand_replacement_preserves_l2_entity_ids(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan

        service, _repository, revision_a = self._service_and_revision("pcs-brand-a")
        plan_a = self._plan(service, revision_a)
        first = service.apply(
            ApplyPointProcessingPlan(plan_a.id, plan_a.digest, "brand-a", "test:engineer")
        )
        service, _repository, revision_b = self._service_and_revision("pcs-brand-b")
        plan_b = self._plan(service, revision_b)
        second = service.apply(
            ApplyPointProcessingPlan(plan_b.id, plan_b.digest, "brand-b", "test:engineer")
        )

        self.assertEqual(first.output_entity_instance_ids, second.output_entity_instance_ids)
        self.assertEqual(2, second.configuration_revision)

    def test_failure_after_l2_bindings_rolls_back_revision_and_installation(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan, PointProcessingService
        from app.services.point_processing_postgres import PostgresPointProcessingCatalog
        from app.services.telemetry_store import get_connection

        _service, repository, revision_id = self._service_and_revision("pcs-brand-a")
        service = PointProcessingService(repository, PostgresPointProcessingCatalog())
        plan = self._plan(service, revision_id)
        original = repository._install_bindings

        def fail_after_bindings(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected failure")

        with patch.object(repository, "_install_bindings", side_effect=fail_after_bindings):
            with self.assertRaises(RuntimeError):
                service.apply(
                    ApplyPointProcessingPlan(
                        plan.id,
                        plan.digest,
                        "rollback",
                        "test:engineer",
                    )
                )
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_revision FROM t_configuration_state")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_installed_point_processings")
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM t_entity_instances")
                self.assertEqual(0, cursor.fetchone()[0])

    def test_local_l0_formula_loads_bound_tags_without_cross_entity_dependencies(self) -> None:
        from app.services.data_trunk_contracts import (
            FramedRawObservation,
            FormulaTransform,
            FrozenFrameCandidate,
            InputReference,
            RawObservation,
            SourceOrder,
            TrunkQuality,
            TypedValue,
        )
        from app.services.data_trunk_conversion import evaluate_processing
        from app.services.data_trunk_postgres import PostgresFrameRepository
        from app.services.frame_processor import FrameProcessor
        from app.services.point_processing import ApplyPointProcessingPlan, PointProcessingService
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
            PostgresPointProcessingTrialEvaluator,
        )
        from app.services.telemetry_store import get_connection

        active_id = uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/ActivePowerRaw")
        auxiliary_id = uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/ReactivePowerRaw")
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id,node_id,name,data_type,unit,read_write,enabled,timestamp_trusted)
                    VALUES(%s,%s,'ReactivePowerRaw','FLOAT','W','R',TRUE,FALSE)
                    """,
                    (auxiliary_id, NODE_ID),
                )
            connection.commit()
        observed_at = datetime.now(UTC)
        framed = []
        for index, (tag_id, key, value) in enumerate((
            (active_id, "ActivePowerRaw", 20.0),
            (auxiliary_id, "ReactivePowerRaw", 22.0),
        ), start=1):
            raw_observation = RawObservation(
                observation_id=uuid4(),
                node_id=NODE_ID,
                tag_id=tag_id,
                source_key=key,
                value=TypedValue.float(value),
                raw_unit="W",
                quality=TrunkQuality.GOOD,
                source_timestamp=observed_at,
                received_at=observed_at,
                source_message_id=f"trial-{index}",
                source_sequence=index,
                source_digest=hashlib.sha256(f"trial-{index}".encode()).hexdigest(),
                event_time_basis="received_at",
                source_order=SourceOrder.received_at(observed_at, index),
            )
            framed.append(
                FramedRawObservation(raw_observation, 1, TrunkQuality.GOOD)
            )
        frame_repository = PostgresFrameRepository()
        frame_repository.commit_pending(
            FrozenFrameCandidate(
                frame_id=uuid4(),
                candidate_digest=hashlib.sha256(b"point-processing-trial-frame").hexdigest(),
                generation=1,
                capture_beat=1,
                shot_at=observed_at,
                configuration_revision=0,
                cells=MappingProxyType({item.observation.tag_id: item for item in framed}),
                changed_l0=tuple(framed),
            )
        )
        terminal = FrameProcessor(
            frame_repository,
            evaluator=evaluate_processing,
            clock=lambda: observed_at,
        ).process_next(observed_at)
        self.assertEqual("COMPLETE", terminal.status.value)
        raw = {
            "schemaVersion": "zizu.point-processing/v1alpha1",
            "id": "pcs.local-formula",
            "kind": "point_processing_template",
            "displayName": "PCS 本地多点加工",
            "deviceCategory": "PCS",
            "brand": "ZiZu",
            "model": "INLINE",
            "revision": 1,
            "status": "active",
            "inputs": [
                {"id": "active", "sourceKind": "l0", "sourceKey": "ActivePowerRaw", "aliases": [], "dataType": "FLOAT", "unit": "W", "required": True},
                {"id": "reactive", "sourceKind": "l0", "sourceKey": "ReactivePowerRaw", "aliases": [], "dataType": "FLOAT", "unit": "W", "required": True},
            ],
            "outputs": [{
                "id": "combined",
                "entityDefinition": "pcs.combined_power",
                "dataType": "FLOAT",
                "unit": "W",
                "freshness": "5s",
                "transform": {"kind": "formula", "expression": "active + reactive", "scheduleSeconds": 1, "controlEligible": False},
            }],
        }
        revision = PostgresPointProcessingTemplates().import_template(
            raw, actor="test:engineer"
        ).revision_id
        repository = PostgresPointProcessingRepository()
        service = PointProcessingService(
            repository,
            PostgresPointProcessingCatalog(),
            trial_evaluator=PostgresPointProcessingTrialEvaluator(),
        )
        plan = self._plan(service, revision)
        trial = service.trial(plan)
        self.assertIsNotNone(trial)
        self.assertEqual(42.0, trial.outputs[0]["value"])
        self.assertEqual(192, trial.outputs[0]["quality"])
        self.assertEqual(2, len(trial.outputs[0]["source_ids"]))
        application = service.apply(
            ApplyPointProcessingPlan(plan.id, plan.digest, "local-formula", "test:engineer")
        )

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_point_processing_dependencies")
                self.assertEqual(0, cursor.fetchone()[0])
                installed = PostgresFrameRepository._load_frame_formula_processings(
                    cursor, application.configuration_revision
                )

        self.assertEqual(1, len(installed))
        self.assertIsInstance(installed[0].transform, FormulaTransform)
        self.assertEqual((InputReference.l0(active_id),), installed[0].transform.sources["active"])
        self.assertEqual((InputReference.l0(auxiliary_id),), installed[0].transform.sources["reactive"])

    def test_trial_marks_expired_l0_input_stale(self) -> None:
        from app.services.data_trunk_contracts import (
            FramedRawObservation,
            FrozenFrameCandidate,
            RawObservation,
            SourceOrder,
            TrunkQuality,
            TypedValue,
        )
        from app.services.data_trunk_conversion import evaluate_processing
        from app.services.data_trunk_postgres import PostgresFrameRepository
        from app.services.frame_processor import FrameProcessor
        from app.services.point_processing import PointProcessingService
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTrialEvaluator,
        )

        tag_id = uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/ActivePowerRaw")
        observed_at = datetime.now(UTC) - timedelta(seconds=10)
        raw = RawObservation(
            observation_id=uuid4(),
            node_id=NODE_ID,
            tag_id=tag_id,
            source_key="ActivePowerRaw",
            value=TypedValue.float(20.0),
            raw_unit="W",
            quality=TrunkQuality.GOOD,
            source_timestamp=observed_at,
            received_at=observed_at,
            source_message_id="expired-trial-source",
            source_sequence=1,
            source_digest=hashlib.sha256(b"expired-trial-source").hexdigest(),
            event_time_basis="received_at",
            source_order=SourceOrder.received_at(observed_at, 1),
        )
        framed = FramedRawObservation(raw, 1, TrunkQuality.GOOD)
        frame_repository = PostgresFrameRepository()
        frame_repository.commit_pending(
            FrozenFrameCandidate(
                frame_id=uuid4(),
                candidate_digest=hashlib.sha256(b"expired-trial-frame").hexdigest(),
                generation=1,
                capture_beat=1,
                shot_at=observed_at,
                configuration_revision=0,
                cells=MappingProxyType({tag_id: framed}),
                changed_l0=(framed,),
            )
        )
        terminal = FrameProcessor(
            frame_repository,
            evaluator=evaluate_processing,
            clock=lambda: observed_at,
        ).process_next(observed_at)
        self.assertEqual("COMPLETE", terminal.status.value)

        registered = self._service_and_revision("pcs-brand-a")[2]
        service = PointProcessingService(
            PostgresPointProcessingRepository(),
            PostgresPointProcessingCatalog(),
            trial_evaluator=PostgresPointProcessingTrialEvaluator(),
        )
        trial = service.trial(self._plan(service, registered))
        self.assertIsNotNone(trial)
        output = next(
            item
            for item in trial.outputs
            if item["entity_definition_id"] == "pcs.active_power"
        )

        self.assertEqual(20.0, output["value"] * 1000)
        self.assertEqual(int(TrunkQuality.STALE), output["quality"])
        self.assertEqual("INPUT_STALE", output["reason"])

    def test_trial_reads_current_processing_revision_for_l2_formula_input(self) -> None:
        from app.services.point_processing import (
            InMemoryPointProcessingCatalog,
            PointProcessingPlan,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingTrialEvaluator,
        )
        from app.services.point_processing_templates import (
            parse_point_processing_template,
        )
        from app.services.telemetry_store import get_connection

        source_entity_id = uuid4()
        target_entity_id = uuid4()
        event_id = uuid4()
        processing_revision_id = uuid4()
        runtime_id = uuid4()
        frame_id = uuid4()
        observed_at = datetime.now(UTC)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET session_replication_role=replica")
                cursor.execute(
                    """
                    INSERT INTO t_data_frames
                      (frame_id,candidate_digest,capture_beat,shot_at,
                       configuration_revision,status,attempt_count,finished_at)
                    VALUES(%s,%s,1,%s,0,'COMPLETE',1,%s)
                    RETURNING frame_sequence
                    """,
                    (frame_id, "d" * 64, observed_at, observed_at),
                )
                frame_sequence = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id,node_id,definition_id,display_name,data_type,unit,
                       direction,freshness_seconds,active,source_kind)
                    VALUES(%s,%s,'pcs.source_power','Source power','FLOAT','kW',
                           'R',30,TRUE,'point_processing')
                    """,
                    (source_entity_id, NODE_ID),
                )
                cursor.execute(
                    """
                    INSERT INTO t_l2_latest
                      (entity_instance_id,event_id,observed_at,received_at,
                       calculated_at,value_float,quality,processing_revision_id,
                       configuration_revision,source_digest,source_order_key,
                       producing_runtime_instance_id,event_time_basis,frame_sequence)
                    VALUES(%s,%s,%s,%s,%s,12.5,192,%s,0,%s,'trial-l2',%s,
                           'received_at',%s)
                    """,
                    (
                        source_entity_id,
                        event_id,
                        observed_at,
                        observed_at,
                        observed_at,
                        processing_revision_id,
                        "e" * 64,
                        runtime_id,
                        frame_sequence,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO t_data_frames
                      (frame_id,candidate_digest,capture_beat,shot_at,
                       configuration_revision,status,attempt_count,finished_at,
                       failure_code)
                    VALUES(%s,%s,2,%s,0,'FAILED',3,%s,'TEST_FAILURE')
                    RETURNING frame_sequence
                    """,
                    (uuid4(), "f" * 64, observed_at, observed_at),
                )
                failed_frame_sequence = int(cursor.fetchone()[0])
                cursor.execute("SET session_replication_role=origin")
            connection.commit()

        template_revision_id = uuid4()
        template = parse_point_processing_template(
            {
                "schemaVersion": "zizu.point-processing/v1alpha1",
                "id": "pcs.l2-trial",
                "kind": "point_processing_template",
                "displayName": "PCS L2 trial",
                "deviceCategory": "PCS",
                "brand": "ZiZu",
                "model": "TRIAL",
                "revision": 1,
                "status": "active",
                "inputs": [
                    {
                        "id": "source",
                        "sourceKind": "l2",
                        "sourceKey": "pcs.source_power",
                        "aliases": [],
                        "dataType": "FLOAT",
                        "unit": "kW",
                        "required": True,
                    }
                ],
                "outputs": [
                    {
                        "id": "derived",
                        "entityDefinition": "pcs.derived_power",
                        "dataType": "FLOAT",
                        "unit": "kW",
                        "freshness": "5s",
                        "transform": {
                            "kind": "formula",
                            "expression": "source",
                            "scheduleSeconds": 1,
                            "controlEligible": False,
                        },
                    }
                ],
            }
        )
        catalog = InMemoryPointProcessingCatalog(
            templates={template_revision_id: template},
            sources=(),
        )
        plan = PointProcessingPlan(
            id=uuid4(),
            node_id=NODE_ID,
            template_revision_id=template_revision_id,
            base_configuration_revision=0,
            source_catalog_digest="a" * 64,
            status="ready",
            items=(
                {
                    "kind": "input_binding",
                    "input_id": "source",
                    "selected_source_id": str(source_entity_id),
                },
                {
                    "kind": "output_binding",
                    "output_id": "derived",
                    "output_entity_instance_id": str(target_entity_id),
                },
            ),
            blockers=(),
            digest="b" * 64,
            planned_by="test:engineer",
        )

        trial = PostgresPointProcessingTrialEvaluator().evaluate(plan, catalog)

        self.assertGreater(failed_frame_sequence, frame_sequence)
        self.assertEqual(frame_sequence, trial.frame_sequence)
        self.assertEqual(12.5, trial.outputs[0]["value"])
        self.assertEqual(
            (str(event_id),),
            trial.outputs[0]["source_ids"],
        )


if __name__ == "__main__":
    unittest.main()
