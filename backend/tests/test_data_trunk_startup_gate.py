from __future__ import annotations

from contextlib import contextmanager
import inspect
import os
import unittest


os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")


class DataTrunkStartupGateTest(unittest.TestCase):
    def _connection(self, *, row_count: int = 1):
        calls: list[str] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, query):
                calls.append(" ".join(query.split()))

            def fetchall(self):
                return [(None,)] * row_count

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return Cursor()

        @contextmanager
        def connection():
            yield Connection()

        return connection, calls

    def test_gate_revalidates_every_entity_against_database_contract(self) -> None:
        from app.services.data_trunk_postgres import verify_data_trunk_contract_gate

        connection, calls = self._connection(row_count=3)

        self.assertEqual(verify_data_trunk_contract_gate(connection), 3)
        self.assertEqual(len(calls), 2)
        self.assertIn("t_configuration_state", calls[0])
        self.assertIn("t_configuration_revisions", calls[0])
        self.assertIn("t_configuration_audit", calls[0])
        self.assertIn("t_point_processing_expressions", calls[0])
        self.assertIn("t_point_processing_selector_members", calls[0])
        self.assertIn("t_point_processing_dependencies", calls[0])
        self.assertIn("t_point_processing_formula_runs", calls[0])
        self.assertIn("t_data_frames", calls[0])
        self.assertIn("t_data_frame_outbox", calls[0])
        self.assertIn("t_committed_frame_consumers", calls[0])
        self.assertIn("uq_committed_frame_consumer_sequence", calls[0])
        self.assertIn("chk_committed_frame_consumer_key", calls[0])
        self.assertIn("fk_committed_frame_consumer_frame", calls[0])
        self.assertIn("t_l2_stream_outbox", calls[0])
        self.assertIn("frame_sequence", calls[0])
        self.assertIn("processing_token", calls[0])
        self.assertIn("claim_token", calls[0])
        self.assertIn("ix_data_frames_claim", calls[0])
        self.assertIn("ix_data_frame_outbox_pending", calls[0])
        self.assertIn("payload_version", calls[0])
        self.assertIn("ix_data_frame_outbox_replay", calls[0])
        self.assertIn("l2_agg_1h", calls[0])
        self.assertIn("zizu_internal.retention_guard", calls[0])
        self.assertIn("prune_committed_frame_history", calls[0])
        self.assertIn("column_name = 'node_id'", calls[0])
        self.assertNotIn("t_cross_node_processing_acceptance_reports", calls[0])
        self.assertIn("assert_entity_instance_single_source(id)", calls[1])
        self.assertIn("FROM t_entity_instances", calls[1])

    def test_production_startup_invokes_gate_after_migrations(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertIn("verify_data_trunk_contract_gate", source)
        self.assertLess(
            source.index("run_migrations()"),
            source.index("verify_data_trunk_contract_gate"),
        )

    def test_removed_business_metric_projection_is_not_started(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertEqual(main._data_trunk_tasks, [])
        self.assertIsNone(main._data_trunk_stop)
        self.assertNotIn("business_metric_projection", source)
        self.assertNotIn("metric_projection.advance", source)

    def test_main_starts_only_frame_runtime_loops(self) -> None:
        from app import main

        source = inspect.getsource(main.lifespan)
        self.assertIn("data_frame_capture", source)
        self.assertIn("data_frame_processor", source)
        self.assertIn("data_frame_outbox", source)
        self.assertIn("register_committed_frame_consumer", source)
        self.assertNotIn("data_trunk_freshness", source)
        self.assertNotIn("data_trunk_typed_formulas", source)
        self.assertNotIn("run_formula_tick", source)
        self.assertNotIn("run_rule_tick", source)
        self.assertNotIn("run_aggregation_tick", source)

    def test_first_stage_release_blockers_are_explicit(self) -> None:
        from app.services.data_trunk_postgres import (
            data_frame_release_readiness_blockers,
        )

        self.assertEqual(
            frozenset(
                {
                    "COMMITTED_FRAME_CONSUMER_MISSING",
                    "DATA_FRAME_RETENTION_POLICY_UNRESOLVED",
                }
            ),
            data_frame_release_readiness_blockers(
                committed_frame_consumer=False,
                retention_policy_resolved=False,
            ),
        )
        self.assertEqual(
            frozenset(),
            data_frame_release_readiness_blockers(
                committed_frame_consumer=True,
                retention_policy_resolved=True,
            ),
        )

    def test_production_source_has_no_legacy_runtime_path(self) -> None:
        from app.services.data_trunk import DataTrunk
        from app.services.data_trunk_postgres import PostgresFrameRepository
        from app.services.pipeline import DataPipeline

        pipeline_source = inspect.getsource(DataPipeline)
        trunk_source = inspect.getsource(DataTrunk)
        postgres_source = inspect.getsource(PostgresFrameRepository)
        self.assertNotIn("_buffer", pipeline_source)
        self.assertNotIn("flush_now", pipeline_source)
        self.assertNotIn("evaluate_due_formulas", trunk_source)
        self.assertNotIn("mark_expired_outputs_stale", trunk_source)
        self.assertNotIn("t_l2_stream_outbox", postgres_source)
        self.assertNotIn("_select_history_observations", postgres_source)
        self.assertIn("FROM t_telemetry_latest AS latest", postgres_source)
        self.assertIn(
            "JOIN LATERAL (\n                        SELECT accepted_beat",
            postgres_source,
        )
        self.assertNotIn("candidate.frame_sequence <=", postgres_source)


if __name__ == "__main__":
    unittest.main()
