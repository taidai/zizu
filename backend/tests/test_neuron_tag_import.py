from __future__ import annotations

import os
import unittest
from uuid import UUID

import psycopg2

from app.services.neuron_point_processing_catalog import ScannedPoint


NODE_ID = UUID("00000000-0000-0000-0000-000000000101")
TAG_ID = UUID("00000000-0000-0000-0000-000000000201")


def point(
    name: str,
    *,
    group: str = "data",
    address: str = "1!1",
    wire_data_type: str = "INT16",
    value_data_type: str = "INT",
) -> ScannedPoint:
    return ScannedPoint(
        group=group,
        group_interval_ms=1000,
        name=name,
        address=address,
        wire_data_type=wire_data_type,
        value_data_type=value_data_type,
        decimal=0.0,
        read_only=True,
    )


class NeuronTagImportPlanTest(unittest.TestCase):
    def test_bit_import_keeps_protocol_type_and_integer_value_contract(self) -> None:
        from app.services.neuron_tag_import import plan_neuron_tag_import

        preview = plan_neuron_tag_import(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("error1",),
            points=(
                point(
                    "15V电源故障",
                    group="error1",
                    wire_data_type="BIT",
                    value_data_type="INT",
                ),
            ),
            existing=(),
            base_configuration_revision=7,
        )

        self.assertEqual("BIT", preview.items[0].wire_data_type)
        self.assertEqual("INT", preview.items[0].value_data_type)

    def test_multi_group_preview_classifies_source_owned_changes(self) -> None:
        from app.services.neuron_tag_import import ExistingNeuronTag, plan_neuron_tag_import

        existing = (
            ExistingNeuronTag(
                id=TAG_ID,
                node_id=NODE_ID,
                name="有功功率",
                display_name="旧名称",
                data_type="INT",
                wire_data_type="INT16",
                source_path="EN9-PCS/data/有功功率",
                source_address="1!1",
                decimal=0.0,
                read_write="R",
                enabled=True,
                l1_bound=False,
            ),
        )

        preview = plan_neuron_tag_import(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("status", "data"),
            points=(
                point("故障码", group="status", address="1!2"),
                point("有功功率", group="data", address="1!1"),
            ),
            existing=existing,
            base_configuration_revision=7,
        )

        self.assertEqual(("data", "status"), preview.selected_groups)
        self.assertEqual({"create": 1, "update": 1}, preview.counts)
        self.assertEqual(
            [(item.source_path, item.action) for item in preview.items],
            [
                ("EN9-PCS/data/有功功率", "update"),
                ("EN9-PCS/status/故障码", "create"),
            ],
        )

    def test_bound_l1_input_rejects_an_incompatible_wire_contract(self) -> None:
        from app.services.neuron_tag_import import ExistingNeuronTag, plan_neuron_tag_import

        existing = ExistingNeuronTag(
            id=TAG_ID,
            node_id=NODE_ID,
            name="有功功率",
            display_name="有功功率",
            data_type="INT",
            wire_data_type="INT16",
            source_path="EN9-PCS/data/有功功率",
            source_address="1!1",
            decimal=0.0,
            read_write="R",
            enabled=True,
            l1_bound=True,
        )

        preview = plan_neuron_tag_import(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("data",),
            points=(point("有功功率", wire_data_type="FLOAT", value_data_type="FLOAT"),),
            existing=(existing,),
            base_configuration_revision=7,
        )

        self.assertEqual({"conflict": 1}, preview.counts)
        self.assertEqual("ACTIVE_L1_CONTRACT_CONFLICT", preview.items[0].reason)

    def test_preview_digest_is_deterministic_and_bound_to_configuration_revision(self) -> None:
        from app.services.neuron_tag_import import plan_neuron_tag_import

        kwargs = dict(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("status", "data"),
            points=(
                point("故障码", group="status", address="1!2"),
                point("有功功率", group="data", address="1!1"),
            ),
            existing=(),
            base_configuration_revision=7,
        )
        first = plan_neuron_tag_import(**kwargs)
        reordered = plan_neuron_tag_import(
            **(kwargs | {"selected_groups": ("data", "status"), "points": tuple(reversed(kwargs["points"]))})
        )
        revised = plan_neuron_tag_import(**(kwargs | {"base_configuration_revision": 8}))

        self.assertEqual(first.digest, reordered.digest)
        self.assertNotEqual(first.digest, revised.digest)
        self.assertEqual(64, len(first.digest))

    def test_unchanged_source_is_not_rewritten(self) -> None:
        from app.services.neuron_tag_import import ExistingNeuronTag, plan_neuron_tag_import

        existing = ExistingNeuronTag(
            id=TAG_ID,
            node_id=NODE_ID,
            name="有功功率",
            display_name="有功功率",
            data_type="INT",
            wire_data_type="INT16",
            source_path="EN9-PCS/data/有功功率",
            source_address="1!1",
            decimal=0.0,
            read_write="R",
            enabled=True,
            l1_bound=False,
        )

        preview = plan_neuron_tag_import(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("data",),
            points=(point("有功功率"),),
            existing=(existing,),
            base_configuration_revision=7,
        )

        self.assertEqual({"unchanged": 1}, preview.counts)
        self.assertIsNone(preview.items[0].after_id)


class _Gate:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def begin_configuration_publish(self, revision: int) -> None:
        self.events.append(f"begin:{revision}")

    def cancel_configuration_publish(self) -> None:
        self.events.append("cancel")

    def reconcile_configuration_runtime(self) -> None:
        self.events.append("reconcile")


class _Repository:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def apply(self, preview, *, actor: str):
        self.events.append(f"apply:{actor}")
        return {"configuration_revision": preview.base_configuration_revision + 1}


class NeuronTagImportApplyTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def preview(*, revision: int = 7, bound: bool = False, changed_type: bool = False):
        from app.services.neuron_tag_import import ExistingNeuronTag, plan_neuron_tag_import

        existing = ExistingNeuronTag(
            id=TAG_ID,
            node_id=NODE_ID,
            name="有功功率",
            display_name="有功功率",
            data_type="INT",
            wire_data_type="INT16",
            source_path="EN9-PCS/data/有功功率",
            source_address="1!1",
            decimal=0.0,
            read_write="R",
            enabled=True,
            l1_bound=bound,
        )
        incoming = point(
            "有功功率",
            wire_data_type="FLOAT" if changed_type else "INT16",
            value_data_type="FLOAT" if changed_type else "INT",
        )
        return plan_neuron_tag_import(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("data",),
            points=(incoming,),
            existing=(existing,),
            base_configuration_revision=revision,
        )

    async def test_digest_mismatch_is_zero_write(self) -> None:
        from app.services.neuron_tag_import import NeuronTagImportError, apply_neuron_tag_import

        events: list[str] = []
        preview = self.preview()

        with self.assertRaisesRegex(NeuronTagImportError, "NEURON_IMPORT_PREVIEW_STALE"):
            await apply_neuron_tag_import(
                preview,
                preview_digest="0" * 64,
                actor="user:engineer",
                repository=_Repository(events),
                runtime_gate=_Gate(events),
                reload_runtime=lambda: None,
            )

        self.assertEqual([], events)

    async def test_conflict_is_zero_write(self) -> None:
        from app.services.neuron_tag_import import NeuronTagImportError, apply_neuron_tag_import

        events: list[str] = []
        preview = self.preview(bound=True, changed_type=True)

        with self.assertRaisesRegex(NeuronTagImportError, "NEURON_IMPORT_CONFLICT"):
            await apply_neuron_tag_import(
                preview,
                preview_digest=preview.digest,
                actor="user:engineer",
                repository=_Repository(events),
                runtime_gate=_Gate(events),
                reload_runtime=lambda: None,
            )

        self.assertEqual([], events)

    async def test_apply_reloads_before_runtime_reopens(self) -> None:
        from app.services.neuron_tag_import import apply_neuron_tag_import, plan_neuron_tag_import

        events: list[str] = []
        preview = plan_neuron_tag_import(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("data",),
            points=(point("有功功率"),),
            existing=(),
            base_configuration_revision=7,
        )

        async def reload_runtime() -> None:
            events.append("reload")

        result = await apply_neuron_tag_import(
            preview,
            preview_digest=preview.digest,
            actor="user:engineer",
            repository=_Repository(events),
            runtime_gate=_Gate(events),
            reload_runtime=reload_runtime,
        )

        self.assertEqual(
            ["begin:7", "apply:user:engineer", "reload", "reconcile"],
            events,
        )
        self.assertEqual(8, result["configuration_revision"])


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run PostgreSQL import tests",
)
class NeuronTagImportPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_name = os.environ.get("DB_NAME", "")
        if not cls.db_name.endswith("_test"):
            raise RuntimeError("Neuron import tests require a *_test database")
        cls.connection_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ["DB_PORT"]),
            "dbname": cls.db_name,
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
        }

    def setUp(self) -> None:
        from tests import test_data_trunk_migration_postgres
        from tests.test_node_data_trunk_hard_cut_migration_postgres import MIGRATION_044

        migration = test_data_trunk_migration_postgres.DataTrunkMigrationPostgresTest
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                migration._reset_through_041(cursor)
                migration._apply_042(cursor)
                migration._apply_043(cursor)
                cursor.execute(MIGRATION_044.read_text(encoding="utf-8"))

    def test_source_upsert_preserves_tag_identity_and_history(self) -> None:
        from app.services.neuron_tag_import import PostgresNeuronTagImports

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) VALUES (%s,'PCS','Device',TRUE)",
                    (str(NODE_ID),),
                )
                cursor.execute(
                    """
                    INSERT INTO t_tags
                      (id,node_id,name,display_name,data_type,source_type,
                       source_path,source_address,wire_data_type,value_data_type,
                       decimal,read_write,enabled)
                    VALUES (%s,%s,'有功功率','旧名称','INT','neuron',
                            'EN9-PCS/data/有功功率','1!1','INT16','INT',0,'R',TRUE)
                    """,
                    (str(TAG_ID), str(NODE_ID)),
                )
                cursor.execute(
                    """
                    INSERT INTO t_telemetry
                      (ts,node_id,tag_id,value_int,is_virtual,quality)
                    VALUES (now(),%s,%s,12,FALSE,192)
                    """,
                    (str(NODE_ID), str(TAG_ID)),
                )
            connection.commit()

        repository = PostgresNeuronTagImports(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        preview = repository.preview(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("data",),
            points=(point("有功功率"),),
        )
        result = repository.apply(preview, actor="user:engineer")

        self.assertEqual(1, result["configuration_revision"])
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id,display_name FROM t_tags WHERE source_path=%s",
                    ("EN9-PCS/data/有功功率",),
                )
                self.assertEqual((str(TAG_ID), "有功功率"), cursor.fetchone())
                cursor.execute("SELECT count(*) FROM t_telemetry WHERE tag_id=%s", (str(TAG_ID),))
                self.assertEqual(1, cursor.fetchone()[0])

    def test_source_create_is_a_physical_tag(self) -> None:
        from app.services.neuron_tag_import import PostgresNeuronTagImports

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                # The legacy migration fixture predates the production
                # PHYSICAL/LOGICAL discriminator. Recreate the live contract
                # here so the Neuron create path cannot silently rely on a
                # database default that production does not have.
                cursor.execute(
                    "ALTER TABLE t_tags ADD COLUMN tag_type TEXT NOT NULL "
                    "CHECK (tag_type IN ('PHYSICAL','LOGICAL'))"
                )
                cursor.execute(
                    "INSERT INTO t_nodes(id,name,node_type,enabled) "
                    "VALUES (%s,'PCS','Device',TRUE)",
                    (str(NODE_ID),),
                )
            connection.commit()

        repository = PostgresNeuronTagImports(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )
        preview = repository.preview(
            node_id=NODE_ID,
            neuron_node="EN9-PCS",
            selected_groups=("data",),
            points=(point("新有功功率"),),
        )

        result = repository.apply(preview, actor="user:engineer")

        self.assertEqual("applied", result["status"])
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT tag_type,source_type,source_path FROM t_tags "
                    "WHERE node_id=%s",
                    (str(NODE_ID),),
                )
                self.assertEqual(
                    ("PHYSICAL", "neuron", "EN9-PCS/data/新有功功率"),
                    cursor.fetchone(),
                )


if __name__ == "__main__":
    unittest.main()
