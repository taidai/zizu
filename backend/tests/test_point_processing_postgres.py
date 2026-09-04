"""Real PostgreSQL evidence for direct node-owned L1 publication."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
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
MIGRATION_056 = Path(__file__).resolve().parents[2] / "init-db" / "migration_056_point_processing_deactivation.sql"
MIGRATION_057 = Path(__file__).resolve().parents[2] / "init-db" / "migration_057_retired_node_processing.sql"
MIGRATION_058 = Path(__file__).resolve().parents[2] / "init-db" / "migration_058_retired_node_entities.sql"
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
                cursor.execute(MIGRATION_056.read_text(encoding="utf-8"))
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

    def test_passthrough_revision_persists_and_reloads_canonical_json(self) -> None:
        from app.services.point_processing_postgres import (
            PostgresPointProcessingTemplates,
        )
        from app.services.telemetry_store import get_connection

        raw = self._raw("pcs-brand-a")
        raw["id"] = "pcs-passthrough"
        raw["revision"] = 1
        raw["inputs"] = [raw["inputs"][0]]
        raw["outputs"] = [raw["outputs"][0]]
        raw["outputs"][0]["dataType"] = raw["inputs"][0]["dataType"]
        raw["outputs"][0]["unit"] = raw["inputs"][0].get("unit")
        raw["outputs"][0]["transform"] = {
            "kind": "passthrough",
            "input": raw["inputs"][0]["id"],
        }
        templates = PostgresPointProcessingTemplates()

        registered = templates.import_template(raw, actor="test:engineer")

        self.assertEqual(raw, templates.export_template(registered.revision_id))
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT input.input_key FROM t_point_processing_passthrough_rules rule "
                "JOIN t_point_processing_inputs input ON input.id=rule.input_id "
                "JOIN t_point_processing_outputs output ON output.id=rule.output_id "
                "WHERE output.revision_id=%s",
                (registered.revision_id,),
            )
            self.assertEqual((raw["inputs"][0]["id"],), cursor.fetchone())

    def test_rw_passthrough_publishes_bounded_control_entity_and_binding(self) -> None:
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingService,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )
        from app.services.telemetry_store import get_connection
        from tests.test_point_processing_templates import PointProcessingTemplateTest

        raw = PointProcessingTemplateTest.passthrough_template("FLOAT", "W")
        raw["id"] = "pcs-controlled-passthrough"
        raw["outputs"][0]["entityDefinition"] = "pcs.max_discharge_power_limit"
        raw["outputs"][0]["control"] = {
            "minimum": 0,
            "maximum": 200000,
            "tolerance": 100,
            "cooldownSeconds": 5,
            "timeoutSeconds": 15,
            "highRisk": False,
        }
        tag_id = uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/ActivePowerRaw")
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_tags SET read_write='RW',read_only=FALSE,"
                "source_type='neuron',source_path='PCS-TEST/group0/ActivePowerRaw' "
                "WHERE id=%s",
                (tag_id,),
            )
            connection.commit()

        revision = PostgresPointProcessingTemplates().import_template(
            raw,
            actor="test:engineer",
        ).revision_id
        service = PointProcessingService(
            PostgresPointProcessingRepository(),
            PostgresPointProcessingCatalog(),
        )
        plan = self._plan(service, revision)
        self.assertEqual("ready", plan.status)
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "controlled-passthrough",
                "test:engineer",
            )
        )
        entity_id = application.output_entity_instance_ids[0]

        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT direction,control_policy FROM t_entity_instances WHERE id=%s",
                (entity_id,),
            )
            direction, policy = cursor.fetchone()
            self.assertEqual("RW", direction)
            self.assertEqual(0, policy["minimum"])
            self.assertEqual(200000, policy["maximum"])
            self.assertEqual(100, policy["tolerance"])
            self.assertEqual(5, policy["cooldown_seconds"])
            self.assertEqual(15, policy["timeout_seconds"])
            self.assertEqual("pcs.max_discharge_power_limit", policy["readback_definition"])
            self.assertEqual([], policy["interlocks"])
            self.assertFalse(policy["high_risk"])
            cursor.execute(
                "SELECT l0_tag_id FROM t_l2_control_bindings "
                "WHERE entity_instance_id=%s",
                (entity_id,),
            )
            self.assertEqual((tag_id,), cursor.fetchone())

    def test_passthrough_applies_and_reloads_as_a_runtime_transform(self) -> None:
        from app.services.data_trunk_contracts import (
            PassthroughTransform,
            RawObservation,
            SourceOrder,
            TrunkQuality,
            TypedValue,
        )
        from app.services.data_trunk_postgres import PostgresFrameRepository
        from app.services.data_trunk_conversion import evaluate_processing
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingService,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )
        from app.services.telemetry_store import get_connection

        raw = self._raw("pcs-brand-b")
        raw["id"] = "pcs-passthrough-runtime"
        raw["inputs"] = [raw["inputs"][0]]
        raw["outputs"] = [raw["outputs"][0]]
        raw["outputs"][0]["transform"] = {
            "kind": "passthrough",
            "input": raw["inputs"][0]["id"],
        }
        revision = PostgresPointProcessingTemplates().import_template(
            raw,
            actor="test:engineer",
        ).revision_id
        service = PointProcessingService(
            PostgresPointProcessingRepository(),
            PostgresPointProcessingCatalog(),
        )
        plan = self._plan(service, revision)
        service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "passthrough-runtime",
                "test:engineer",
            )
        )
        observed_at = datetime.now(UTC)
        tag_id = uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/PActKw")
        observation = RawObservation(
            observation_id=uuid4(),
            node_id=NODE_ID,
            tag_id=tag_id,
            source_key="PActKw",
            value=TypedValue.float(12.5),
            raw_unit="kW",
            quality=TrunkQuality.GOOD,
            source_timestamp=observed_at,
            received_at=observed_at,
            source_message_id="passthrough-runtime",
            source_sequence=1,
            source_digest=hashlib.sha256(b"passthrough-runtime").hexdigest(),
            event_time_basis="received_at",
            source_order=SourceOrder.received_at(observed_at, 1),
        )

        with get_connection() as connection, connection.cursor() as cursor:
            snapshot = PostgresFrameRepository._load_conversion_snapshot(
                cursor,
                (observation,),
                calculated_at=observed_at,
            )

        self.assertEqual(1, len(snapshot.installed))
        self.assertIsInstance(snapshot.installed[0].transform, PassthroughTransform)

        stale = replace(observation, quality=TrunkQuality.STALE)
        output = evaluate_processing(
            installed=snapshot.installed,
            current_inputs={snapshot.installed[0].transform.input: stale},
            configuration_revision=1,
            calculated_at=observed_at,
        )[0]
        frame_id = uuid4()
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t_data_frames
                  (frame_id,candidate_digest,capture_beat,shot_at,
                   configuration_revision,status,attempt_count)
                VALUES(%s,%s,1,%s,1,'PENDING',0)
                RETURNING frame_sequence
                """,
                (frame_id, hashlib.sha256(b"passthrough-stale").hexdigest(), observed_at),
            )
            frame_sequence = cursor.fetchone()[0]
            stored = replace(
                output,
                frame_id=frame_id,
                frame_sequence=frame_sequence,
            )
            PostgresFrameRepository._ensure_runtime(cursor)
            PostgresFrameRepository._insert_frame_l2(cursor, (stored,))
            PostgresFrameRepository._advance_frame_l2_latest(cursor, (stored,))
            connection.commit()
            cursor.execute(
                "SELECT value_float,quality,reason FROM t_l2_observations "
                "WHERE event_id=%s",
                (stored.event_id,),
            )
            self.assertEqual((12.5, 1, "INPUT_STALE"), cursor.fetchone())
            cursor.execute(
                "SELECT value_float,value_observed_at,quality,reason "
                "FROM t_l2_latest WHERE entity_instance_id=%s",
                (stored.entity_instance_id,),
            )
            self.assertEqual((None, None, 1, "INPUT_STALE"), cursor.fetchone())

    def test_boolean_map_revision_persists_compiled_rule_and_reloads(self) -> None:
        from app.services.data_trunk_contracts import (
            BooleanMapTransform,
            RawObservation,
            SourceOrder,
            TrunkQuality,
            TypedValue,
        )
        from app.services.data_trunk_postgres import PostgresFrameRepository
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingService,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )
        from app.services.telemetry_store import get_connection
        from tests.test_point_processing_templates import PointProcessingTemplateTest

        raw = PointProcessingTemplateTest.passthrough_template("INT", None)
        raw["id"] = "pcs-boolean-map-postgres"
        raw["outputs"][0]["dataType"] = "BOOL"
        raw["outputs"][0]["transform"] = {
            "kind": "boolean_map",
            "input": "active_power_raw",
            "trueWhen": 1,
        }
        templates = PostgresPointProcessingTemplates()

        registered = templates.import_template(raw, actor="test:engineer")

        self.assertEqual(raw, templates.export_template(registered.revision_id))
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT rule.true_when,length(rule.ast_digest),rule.compiled_ast "
                "FROM t_point_processing_boolean_map_rules rule "
                "JOIN t_point_processing_outputs output ON output.id=rule.output_id "
                "WHERE output.revision_id=%s",
                (registered.revision_id,),
            )
            true_when, digest_length, compiled_ast = cursor.fetchone()
            self.assertEqual(1, true_when)
            self.assertEqual(64, digest_length)
            self.assertEqual("==", compiled_ast["compare"])
            cursor.execute(
                "UPDATE t_tags SET data_type='INT',value_data_type='INT',unit=NULL "
                "WHERE node_id=%s AND name='ActivePowerRaw'",
                (NODE_ID,),
            )
            connection.commit()

        service = PointProcessingService(
            PostgresPointProcessingRepository(),
            PostgresPointProcessingCatalog(),
        )
        plan = self._plan(service, registered.revision_id)
        self.assertEqual("ready", plan.status)
        service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "boolean-map-runtime",
                "test:engineer",
            )
        )
        observed_at = datetime.now(UTC)
        tag_id = uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/ActivePowerRaw")
        observation = RawObservation(
            observation_id=uuid4(),
            node_id=NODE_ID,
            tag_id=tag_id,
            source_key="ActivePowerRaw",
            value=TypedValue.integer(1),
            raw_unit=None,
            quality=TrunkQuality.GOOD,
            source_timestamp=observed_at,
            received_at=observed_at,
            source_message_id="boolean-map-runtime",
            source_sequence=1,
            source_digest=hashlib.sha256(b"boolean-map-runtime").hexdigest(),
            event_time_basis="received_at",
            source_order=SourceOrder.received_at(observed_at, 1),
        )
        with get_connection() as connection, connection.cursor() as cursor:
            snapshot = PostgresFrameRepository._load_conversion_snapshot(
                cursor,
                (observation,),
                calculated_at=observed_at,
            )

        self.assertEqual(1, len(snapshot.installed))
        self.assertIsInstance(snapshot.installed[0].transform, BooleanMapTransform)

    def test_hard_cut_replaces_legacy_bit_identity_with_immutable_boolean_map(self) -> None:
        from app.services.l0_raw_cutover import (
            apply_cutover,
            clear_runtime_test_data,
            inspect_cutover,
        )
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingService,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )
        from app.services.telemetry_store import get_connection
        from tests.test_point_processing_templates import PointProcessingTemplateTest

        raw = PointProcessingTemplateTest.passthrough_template("BOOL", None)
        raw["id"] = "pcs-legacy-bit-identity"
        raw["inputs"].append(
            {
                "id": "unrelated_power",
                "sourceKind": "l0",
                "sourceKey": "PActKw",
                "aliases": [],
                "dataType": "FLOAT",
                "unit": "kW",
                "required": True,
            }
        )
        raw["outputs"][0]["transform"] = {
            "kind": "formula",
            "expression": "active_power_raw",
            "scheduleSeconds": 1,
            "controlEligible": False,
        }
        registered = PostgresPointProcessingTemplates().import_template(
            raw,
            actor="test:engineer",
        )
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_tags SET data_type='BOOL',value_data_type='BOOL',"
                "wire_data_type='BIT',unit=NULL WHERE node_id=%s AND name='ActivePowerRaw'",
                (NODE_ID,),
            )
            connection.commit()

        repository = PostgresPointProcessingRepository()
        service = PointProcessingService(
            repository,
            PostgresPointProcessingCatalog(),
        )
        plan = self._plan(service, registered.revision_id)
        application = service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "legacy-bit-install",
                "test:engineer",
            )
        )
        entity_id = application.output_entity_instance_ids[0]
        old_installed_id = application.installed_processing_id
        old_application_id = application.id

        uuid_as_text = psycopg2.extensions.new_type(
            (2950,), "UUID_AS_TEXT", lambda value, _cursor: value
        )
        with psycopg2.connect(**self.connection_kwargs) as string_uuid_connection:
            psycopg2.extensions.register_type(uuid_as_text, string_uuid_connection)
            string_uuid_report = inspect_cutover(string_uuid_connection)
        self.assertFalse(string_uuid_report.blockers)
        self.assertEqual(1, len(string_uuid_report.deterministic_output_ids))

        with get_connection() as connection:
            report = inspect_cutover(connection)
            self.assertFalse(report.blockers)
            self.assertEqual(1, len(report.deterministic_output_ids))
            new_revisions = apply_cutover(
                connection,
                expected_digest=report.digest,
                actor="release-v0.6.8",
            )

        self.assertEqual(1, len(new_revisions))
        self.assertNotEqual(registered.revision_id, new_revisions[0])
        exported = PostgresPointProcessingTemplates().export_template(new_revisions[0])
        self.assertEqual("INT", exported["inputs"][0]["dataType"])
        self.assertEqual(
            {"kind": "boolean_map", "input": "active_power_raw", "trueWhen": 1},
            exported["outputs"][0]["transform"],
        )
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,revision_id,configuration_revision FROM "
                "t_installed_point_processings WHERE current=TRUE AND node_id=%s",
                (NODE_ID,),
            )
            new_installed_id, active_revision_id, configuration_revision = cursor.fetchone()
            self.assertNotEqual(old_installed_id, new_installed_id)
            self.assertEqual((new_revisions[0], 2), (active_revision_id, configuration_revision))
            cursor.execute(
                "SELECT revision_id,current FROM t_installed_point_processings WHERE id=%s",
                (old_installed_id,),
            )
            self.assertEqual((registered.revision_id, False), cursor.fetchone())
            cursor.execute(
                "SELECT installed_processing_id,configuration_revision "
                "FROM t_point_processing_applications WHERE id=%s",
                (old_application_id,),
            )
            self.assertEqual((old_installed_id, 1), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_point_processing_applications "
                "WHERE installed_processing_id=%s AND configuration_revision=2",
                (new_installed_id,),
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_point_processing_plans "
                "WHERE id=(SELECT source_plan_id FROM t_installed_point_processings "
                "WHERE id=%s) AND status='applied'",
                (new_installed_id,),
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM t_audit_events "
                "WHERE details->>'kind'='l0_raw_bit_hard_cut' "
                "AND details->>'old_installation_id'=%s",
                (str(old_installed_id),),
            )
            self.assertEqual((1,), cursor.fetchone())
            cursor.execute(
                """
                SELECT count(*)
                FROM t_point_processing_input_bindings AS binding
                JOIN t_point_processing_inputs AS input ON input.id=binding.input_id
                JOIN t_installed_point_processings AS installed
                  ON installed.id=binding.installed_processing_id
                WHERE installed.current=TRUE
                  AND installed.node_id=%s
                  AND input.revision_id<>installed.revision_id
                """,
                (NODE_ID,),
            )
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute(
                """
                SELECT count(*)
                FROM t_point_processing_input_bindings AS binding
                JOIN t_point_processing_inputs AS input ON input.id=binding.input_id
                WHERE binding.installed_processing_id=%s
                  AND input.revision_id<>%s
                """,
                (old_installed_id, registered.revision_id),
            )
            self.assertEqual((0,), cursor.fetchone())
            cursor.execute(
                "SELECT entity_instance_id FROM t_point_processing_output_bindings "
                "WHERE installed_processing_id=(SELECT id FROM "
                "t_installed_point_processings WHERE current=TRUE AND node_id=%s)",
                (NODE_ID,),
            )
            self.assertEqual((entity_id,), cursor.fetchone())
            cursor.execute(
                "SELECT data_type,value_data_type FROM t_tags "
                "WHERE node_id=%s AND name='ActivePowerRaw'",
                (NODE_ID,),
            )
            self.assertEqual(("INT", "INT"), cursor.fetchone())

        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_nodes UNION ALL SELECT count(*) FROM t_tags "
                "UNION ALL SELECT count(*) FROM t_point_processing_revisions "
                "UNION ALL SELECT count(*) FROM t_entity_instances"
            )
            configuration_counts = tuple(row[0] for row in cursor.fetchall())
            tag_id = uuid5(NAMESPACE_URL, f"test/tag/{NODE_ID}/ActivePowerRaw")
            observed_at = datetime.now(UTC)
            cursor.execute(
                "INSERT INTO t_telemetry(ts,node_id,tag_id,value_int,quality) "
                "VALUES(%s,%s,%s,1,192)",
                (observed_at, NODE_ID, tag_id),
            )
            cursor.execute(
                """
                INSERT INTO t_data_frames
                  (frame_id,candidate_digest,capture_beat,shot_at,
                   configuration_revision,status,attempt_count)
                VALUES(%s,%s,1,%s,2,'PENDING',0)
                """,
                (uuid4(), "c" * 64, observed_at),
            )
            connection.commit()

        with get_connection() as connection:
            deleted = clear_runtime_test_data(
                connection,
                expected_configuration_revision=2,
            )
        self.assertEqual(1, deleted["t_telemetry"])
        self.assertEqual(1, deleted["t_data_frames"])
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM t_nodes UNION ALL SELECT count(*) FROM t_tags "
                "UNION ALL SELECT count(*) FROM t_point_processing_revisions "
                "UNION ALL SELECT count(*) FROM t_entity_instances"
            )
            self.assertEqual(
                configuration_counts,
                tuple(row[0] for row in cursor.fetchall()),
            )
            cursor.execute(
                "SELECT (SELECT count(*) FROM t_telemetry),"
                "(SELECT count(*) FROM t_data_frames),"
                "(SELECT count(*) FROM t_l2_observations)"
            )
            self.assertEqual((0, 0, 0), cursor.fetchone())

    def test_hard_cut_blocks_complex_legacy_bit_formula(self) -> None:
        from app.services.l0_raw_cutover import inspect_cutover
        from app.services.point_processing import (
            ApplyPointProcessingPlan,
            PointProcessingService,
        )
        from app.services.point_processing_postgres import (
            PostgresPointProcessingCatalog,
            PostgresPointProcessingRepository,
            PostgresPointProcessingTemplates,
        )
        from app.services.telemetry_store import get_connection
        from tests.test_point_processing_templates import PointProcessingTemplateTest

        raw = PointProcessingTemplateTest.passthrough_template("BOOL", None)
        raw["id"] = "pcs-legacy-bit-complex"
        raw["outputs"][0]["transform"] = {
            "kind": "formula",
            "expression": "not active_power_raw",
            "scheduleSeconds": 1,
            "controlEligible": False,
        }
        registered = PostgresPointProcessingTemplates().import_template(
            raw,
            actor="test:engineer",
        )
        with get_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE t_tags SET data_type='BOOL',value_data_type='BOOL',"
                "wire_data_type='BIT',unit=NULL WHERE node_id=%s AND name='ActivePowerRaw'",
                (NODE_ID,),
            )
            connection.commit()
        service = PointProcessingService(
            PostgresPointProcessingRepository(),
            PostgresPointProcessingCatalog(),
        )
        plan = self._plan(service, registered.revision_id)
        service.apply(
            ApplyPointProcessingPlan(
                plan.id,
                plan.digest,
                "legacy-bit-complex",
                "test:engineer",
            )
        )

        with get_connection() as connection:
            report = inspect_cutover(connection)

        self.assertFalse(report.deterministic_output_ids)
        self.assertEqual(1, len(report.blockers))
        self.assertEqual("BIT_FORMULA_REQUIRES_REVIEW", report.blockers[0].code)

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

    def test_raw_point_maintenance_blocks_disabling_a_current_l1_input(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan
        from app.services.raw_point_maintenance import (
            PostgresRawPointMaintenance,
            RawPointMaintenanceError,
        )
        from app.services.telemetry_store import get_connection

        service, _repository, revision_id = self._service_and_revision("pcs-brand-a")
        plan = self._plan(service, revision_id)
        installed = service.apply(ApplyPointProcessingPlan(
            plan.id,
            plan.digest,
            "install-before-l0-stop",
            "test:engineer",
        ))
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT binding.l0_tag_id
                    FROM t_point_processing_input_bindings AS binding
                    JOIN t_installed_point_processings AS installed
                      ON installed.id=binding.installed_processing_id
                     AND installed.current=TRUE
                    WHERE binding.source_kind='l0'
                    ORDER BY binding.l0_tag_id
                    LIMIT 1
                    """
                )
                tag_id = cursor.fetchone()[0]

        maintenance = PostgresRawPointMaintenance()
        with self.assertRaisesRegex(RawPointMaintenanceError, "RAW_POINT_IN_USE"):
            maintenance.update(
                tag_ids=(tag_id,),
                changes={"enabled": False},
                actor="test:engineer",
                base_revision=installed.configuration_revision,
            )

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT enabled FROM t_tags WHERE id=%s", (tag_id,))
                self.assertEqual((True,), cursor.fetchone())
                cursor.execute("SELECT current_revision FROM t_configuration_state")
                self.assertEqual((installed.configuration_revision,), cursor.fetchone())

    def test_raw_point_maintenance_updates_and_restores_an_unused_point(self) -> None:
        from app.services.raw_point_maintenance import PostgresRawPointMaintenance
        from app.services.telemetry_store import get_connection

        tag_id = uuid4()
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id,node_id,name,display_name,data_type,source_type,
                       source_path,enabled)
                    VALUES(%s,%s,'Spare','备用点','FLOAT','neuron',
                           'PCS/data/Spare',TRUE)
                    """,
                    (tag_id, NODE_ID),
                )
            connection.commit()

        maintenance = PostgresRawPointMaintenance()
        stopped = maintenance.update(
            tag_ids=(tag_id,),
            changes={"display_name": "  备用测量  ", "enabled": False},
            actor="test:engineer",
            base_revision=0,
        )
        restored = maintenance.update(
            tag_ids=(tag_id,),
            changes={"enabled": True},
            actor="test:engineer",
            base_revision=stopped["configuration_revision"],
        )

        self.assertEqual(1, stopped["configuration_revision"])
        self.assertEqual(2, restored["configuration_revision"])
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id,name,display_name,source_path,enabled FROM t_tags WHERE id=%s",
                    (tag_id,),
                )
                self.assertEqual(
                    (tag_id, "Spare", "备用测量", "PCS/data/Spare", True),
                    cursor.fetchone(),
                )

    def test_raw_point_maintenance_deletes_unused_point_and_measurements(self) -> None:
        from app.services.raw_point_maintenance import PostgresRawPointMaintenance
        from app.services.telemetry_store import get_connection

        tag_id = uuid4()
        observation_id = uuid4()
        observed_at = datetime.now(UTC)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id,node_id,name,display_name,data_type,source_type,
                       source_path,enabled)
                    VALUES(%s,%s,'DeleteMe','待删除点','FLOAT','neuron',
                           'PCS/data/DeleteMe',TRUE)
                    """,
                    (tag_id, NODE_ID),
                )
                cursor.execute(
                    "INSERT INTO t_telemetry(ts,node_id,tag_id,value_float) "
                    "VALUES(%s,%s,%s,12.5)",
                    (observed_at, NODE_ID, tag_id),
                )
                cursor.execute(
                    "INSERT INTO t_telemetry_latest"
                    "(node_id,tag_id,ts,value_float,frame_sequence,accepted_beat) "
                    "VALUES(%s,%s,%s,12.5,0,0)",
                    (NODE_ID, tag_id, observed_at),
                )
                cursor.execute(
                    """
                    INSERT INTO t_l0_observation_dedup
                      (observation_id,tag_id,observed_at,source_digest)
                    VALUES(%s,%s,%s,%s)
                    """,
                    (observation_id, tag_id, observed_at, "d" * 64),
                )
            connection.commit()

        deleted = PostgresRawPointMaintenance().delete(
            tag_ids=(tag_id,),
            actor="test:engineer",
            base_revision=0,
        )

        self.assertEqual(1, deleted["deleted"])
        self.assertEqual([str(tag_id)], deleted["deleted_ids"])
        self.assertEqual(1, deleted["configuration_revision"])
        with get_connection() as connection:
            with connection.cursor() as cursor:
                for table in (
                    "t_tags",
                    "t_telemetry",
                    "t_telemetry_latest",
                    "t_l0_observation_dedup",
                ):
                    key = "id" if table == "t_tags" else "tag_id"
                    cursor.execute(
                        f"SELECT count(*) FROM {table} WHERE {key}=%s",
                        (tag_id,),
                    )
                    self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT action FROM t_configuration_revisions WHERE revision=1"
                )
                self.assertEqual(("raw_point.delete",), cursor.fetchone())

    def test_raw_point_maintenance_blocks_deleting_an_l1_input(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan
        from app.services.raw_point_maintenance import (
            PostgresRawPointMaintenance,
            RawPointMaintenanceError,
        )
        from app.services.telemetry_store import get_connection

        service, _repository, revision_id = self._service_and_revision("pcs-brand-a")
        plan = self._plan(service, revision_id)
        installed = service.apply(ApplyPointProcessingPlan(
            plan.id,
            plan.digest,
            "install-before-l0-delete",
            "test:engineer",
        ))
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT l0_tag_id
                    FROM t_point_processing_input_bindings
                    WHERE source_kind='l0'
                    ORDER BY l0_tag_id
                    LIMIT 1
                    """
                )
                tag_id = cursor.fetchone()[0]

        with self.assertRaisesRegex(RawPointMaintenanceError, "RAW_POINT_IN_USE"):
            PostgresRawPointMaintenance().delete(
                tag_ids=(tag_id,),
                actor="test:engineer",
                base_revision=installed.configuration_revision,
            )

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_tags WHERE id=%s", (tag_id,))
                self.assertEqual(1, cursor.fetchone()[0])

    def test_deactivation_preserves_evidence_and_reactivation_reuses_entity_ids(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan
        from app.services.telemetry_store import get_connection

        service, repository, revision_id = self._service_and_revision("pcs-brand-a")
        install_plan = self._plan(service, revision_id)
        installed = service.apply(ApplyPointProcessingPlan(
            install_plan.id,
            install_plan.digest,
            "install-before-deactivation",
            "test:engineer",
        ))
        observed_at = datetime.now(UTC)
        observed_entity_id = installed.output_entity_instance_ids[0]
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM t_installed_point_processings "
                    "WHERE node_id=%s AND current=TRUE",
                    (NODE_ID,),
                )
                installed_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO t_l2_latest
                      (entity_instance_id,event_id,observed_at,received_at,
                       calculated_at,value_observed_at,value_float,quality,processing_revision_id,
                       configuration_revision,source_digest,source_order_key,
                       event_time_basis,frame_sequence)
                    VALUES(%s,%s,%s,%s,%s,%s,12.5,192,%s,%s,%s,'deactivation-evidence',
                           'received_at',0)
                    """,
                    (
                        observed_entity_id,
                        uuid4(),
                        observed_at,
                        observed_at,
                        observed_at,
                        observed_at,
                        revision_id,
                        installed.configuration_revision,
                        "a" * 64,
                    ),
                )
            connection.commit()

        stop_plan = service.preview_deactivation(
            node_id=NODE_ID,
            actor="test:engineer",
        )
        stopped = service.apply(ApplyPointProcessingPlan(
            stop_plan.id,
            stop_plan.digest,
            "deactivate-current",
            "test:engineer",
        ))
        replayed = service.apply(ApplyPointProcessingPlan(
            stop_plan.id,
            stop_plan.digest,
            "deactivate-current",
            "test:engineer",
        ))

        self.assertEqual(stopped, replayed)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current FROM t_installed_point_processings WHERE id=%s",
                    (installed_id,),
                )
                self.assertEqual((False,), cursor.fetchone())
                cursor.execute(
                    "SELECT count(*) FROM t_entity_instances "
                    "WHERE id=ANY(%s) AND active=FALSE",
                    (list(installed.output_entity_instance_ids),),
                )
                self.assertEqual(len(installed.output_entity_instance_ids), cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM t_point_processing_output_bindings "
                    "WHERE installed_processing_id=%s",
                    (installed_id,),
                )
                self.assertEqual(len(installed.output_entity_instance_ids), cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM t_l2_latest WHERE entity_instance_id=%s",
                    (observed_entity_id,),
                )
                self.assertEqual(1, cursor.fetchone()[0])

        reactivation_plan = self._plan(service, revision_id)
        reactivated = service.apply(ApplyPointProcessingPlan(
            reactivation_plan.id,
            reactivation_plan.digest,
            "reactivate-current",
            "test:engineer",
        ))

        self.assertEqual(installed.output_entity_instance_ids, reactivated.output_entity_instance_ids)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM t_entity_instances "
                    "WHERE id=ANY(%s) AND active=TRUE",
                    (list(installed.output_entity_instance_ids),),
                )
                self.assertEqual(len(installed.output_entity_instance_ids), cursor.fetchone()[0])

    def test_retiring_node_stops_its_current_processing_and_keeps_evidence(self) -> None:
        from app.services.node_tree_postgres import PostgresNodeTree
        from app.services.point_processing import ApplyPointProcessingPlan
        from app.services.telemetry_store import get_connection

        service, _repository, revision_id = self._service_and_revision("pcs-brand-a")
        install_plan = self._plan(service, revision_id)
        installed = service.apply(ApplyPointProcessingPlan(
            install_plan.id,
            install_plan.digest,
            "install-before-node-retirement",
            "test:engineer",
        ))
        legacy_entity_id = uuid4()
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM t_installed_point_processings "
                    "WHERE node_id=%s AND current=TRUE",
                    (NODE_ID,),
                )
                installed_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id,node_id,definition_id,display_name,data_type,direction,
                       freshness_seconds,active,source_kind)
                    VALUES(%s,%s,'test.legacy','Legacy entity','INT','R',30,TRUE,
                           'legacy_tag')
                    """,
                    (legacy_entity_id, NODE_ID),
                )
            connection.commit()

        result = PostgresNodeTree().retire(
            node_id=NODE_ID,
            actor="test:engineer",
            base_revision=installed.configuration_revision,
        )

        self.assertEqual(installed.configuration_revision + 1, result["configuration_revision"])
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current FROM t_installed_point_processings WHERE id=%s",
                    (installed_id,),
                )
                self.assertEqual((False,), cursor.fetchone())
                cursor.execute(
                    "SELECT count(*) FROM t_entity_instances "
                    "WHERE id=ANY(%s) AND active=FALSE",
                    (list(installed.output_entity_instance_ids),),
                )
                self.assertEqual(len(installed.output_entity_instance_ids), cursor.fetchone()[0])
                cursor.execute(
                    "SELECT active FROM t_entity_instances WHERE id=%s",
                    (legacy_entity_id,),
                )
                self.assertEqual((False,), cursor.fetchone())
                cursor.execute(
                    "SELECT count(*) FROM t_point_processing_output_bindings "
                    "WHERE installed_processing_id=%s",
                    (installed_id,),
                )
                self.assertEqual(len(installed.output_entity_instance_ids), cursor.fetchone()[0])

    def test_schema_057_stops_processing_left_active_by_legacy_node_retirement(self) -> None:
        from app.services.point_processing import ApplyPointProcessingPlan
        from app.services.telemetry_store import get_connection

        service, _repository, revision_id = self._service_and_revision("pcs-brand-a")
        install_plan = self._plan(service, revision_id)
        installed = service.apply(ApplyPointProcessingPlan(
            install_plan.id,
            install_plan.digest,
            "install-before-retirement-repair",
            "test:engineer",
        ))
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_nodes SET enabled=FALSE,retired_at=clock_timestamp() "
                    "WHERE id=%s",
                    (NODE_ID,),
                )
            connection.commit()

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_057.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_057.read_text(encoding="utf-8"))

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM t_installed_point_processings "
                    "WHERE node_id=%s AND current=TRUE",
                    (NODE_ID,),
                )
                self.assertEqual(0, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM t_entity_instances "
                    "WHERE id=ANY(%s) AND active=FALSE",
                    (list(installed.output_entity_instance_ids),),
                )
                self.assertEqual(len(installed.output_entity_instance_ids), cursor.fetchone()[0])
                cursor.execute(
                    "SELECT count(*) FROM t_point_processing_output_bindings "
                    "WHERE entity_instance_id=ANY(%s)",
                    (list(installed.output_entity_instance_ids),),
                )
                self.assertEqual(len(installed.output_entity_instance_ids), cursor.fetchone()[0])

    def test_schema_058_deactivates_every_entity_on_a_retired_node(self) -> None:
        from app.services.telemetry_store import get_connection

        legacy_entity_id = uuid4()
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_entity_instances
                      (id,node_id,definition_id,display_name,data_type,direction,
                       freshness_seconds,active,source_kind)
                    VALUES(%s,%s,'test.legacy','Legacy entity','INT','R',30,TRUE,
                           'legacy_tag')
                    """,
                    (legacy_entity_id, NODE_ID),
                )
                cursor.execute(
                    "UPDATE t_nodes SET enabled=FALSE,retired_at=clock_timestamp() "
                    "WHERE id=%s",
                    (NODE_ID,),
                )
            connection.commit()

        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_058.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_058.read_text(encoding="utf-8"))

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT active FROM t_entity_instances WHERE id=%s",
                    (legacy_entity_id,),
                )
                self.assertEqual((False,), cursor.fetchone())

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
                      (id,node_id,name,data_type,unit,read_write,enabled,
                       timestamp_trusted,freshness_seconds)
                    VALUES(%s,%s,'ReactivePowerRaw','FLOAT','W','R',TRUE,FALSE,60)
                    """,
                    (auxiliary_id, NODE_ID),
                )
                cursor.execute(
                    "UPDATE t_tags SET freshness_seconds=60 WHERE id=%s",
                    (active_id,),
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
                "freshness": "60s",
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
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE t_telemetry_latest "
                    "SET ts=clock_timestamp(),event_received_at=clock_timestamp() "
                    "WHERE tag_id=ANY(%s)",
                    ([active_id, auxiliary_id],),
                )
            connection.commit()
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
                       calculated_at,value_observed_at,value_float,quality,processing_revision_id,
                       configuration_revision,source_digest,source_order_key,
                       producing_runtime_instance_id,event_time_basis,frame_sequence)
                    VALUES(%s,%s,%s,%s,%s,%s,12.5,192,%s,0,%s,'trial-l2',%s,
                           'received_at',%s)
                    """,
                    (
                        source_entity_id,
                        event_id,
                        observed_at,
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
