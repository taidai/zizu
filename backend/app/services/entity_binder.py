"""
Entity Auto Binder — 根据映射表自动把国标实体绑定到点位。

设计原则：
  - 幂等：已存在的绑定不会重复创建
  - 可扩展：映射表在 app.core.tag_entity_mappings 维护
  - 安全：只绑定 enabled 的 entity/tag/node
"""
from __future__ import annotations

from loguru import logger

from app.core.tag_entity_mappings import lookup_entity_name
from app.services.telemetry_store import get_connection


def auto_bind_standard_entities(dry_run: bool = False) -> dict:
    """
    扫描所有 enabled tag，按 TAG_ENTITY_MAP 自动创建实体绑定。

    Args:
        dry_run: 为 True 时只返回预览，不写入 DB。

    Returns:
        {"created": int, "skipped": int, "preview": [(entity_name, tag_name, node_name), ...]}
    """
    from app.services.telemetry_store import get_connection

    created = 0
    skipped = 0
    preview: list[dict] = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id AS tag_id, t.name AS tag_name, t.node_id,
                       n.name AS node_name, n.node_type
                FROM t_tags t
                JOIN t_nodes n ON n.id = t.node_id
                WHERE t.enabled = TRUE AND n.enabled = TRUE
            """)
            columns = [desc[0] for desc in cur.description]
            tags = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute("SELECT id, name FROM t_entities WHERE enabled = TRUE")
            entity_map = {name: str(eid) for eid, name in cur.fetchall()}

            cur.execute("""
                SELECT entity_id, tag_id FROM t_entity_bindings
                WHERE enabled = TRUE
            """)
            existing = {(str(row[0]), str(row[1])) for row in cur.fetchall()}

            for tag in tags:
                tag_id = str(tag["tag_id"])
                tag_name = tag["tag_name"]
                node_type = tag.get("node_type")
                node_name = tag.get("node_name")
                node_id = tag["node_id"]

                entity_name = lookup_entity_name(node_type, tag_name)
                if not entity_name:
                    skipped += 1
                    continue

                entity_id = entity_map.get(entity_name)
                if not entity_id:
                    logger.warning("[AutoBind] entity {} not found or disabled", entity_name)
                    skipped += 1
                    continue

                if (entity_id, tag_id) in existing:
                    skipped += 1
                    continue

                preview.append({
                    "entity_name": entity_name,
                    "tag_name": tag_name,
                    "node_name": node_name,
                    "node_type": node_type,
                })

                if not dry_run:
                    cur.execute("""
                        INSERT INTO t_entity_bindings
                        (entity_id, tag_id, node_id, binding_type, priority, enabled)
                        VALUES (%s, %s, %s, %s, %s, TRUE)
                        ON CONFLICT (entity_id, tag_id) DO UPDATE SET
                            binding_type = EXCLUDED.binding_type,
                            priority = EXCLUDED.priority,
                            enabled = TRUE,
                            updated_at = now()
                    """, (entity_id, tag_id, node_id, "PHYSICAL", 1))
                    created += 1

            if not dry_run and created > 0:
                conn.commit()

    logger.info("[AutoBind] created={}, skipped={}, dry_run={}", created, skipped, dry_run)
    return {"created": created, "skipped": skipped, "preview": preview}
