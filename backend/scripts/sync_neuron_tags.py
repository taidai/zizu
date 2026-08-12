#!/usr/bin/env python3
"""
sync_neuron_tags.py — Neuron API → ZiZu t_nodes/t_tags 自动同步

从 Neuron REST API 拉取 DRIVER 节点的 group/tag 配置，
生成 ZiZu 节点树 + 点位映射（幂等, 可重复执行）。

用法 (服务器上):
    python3 /home/zizu/backend/scripts/sync_neuron_tags.py
    python3 /home/zizu/backend/scripts/sync_neuron_tags.py --dry-run

类型映射 (Neuron type → ZiZu data_type):
    1-8 整数系 → INT | 9/10 浮点 → FLOAT | 11/12 → BOOL | 13/14 → STRING

缩放策略 (关键!):
    Neuron decimal ≠ 0  → Neuron 端已缩放, payload 是工程值 → scale=1.0 (不重复缩放)
    Neuron decimal = 0  → 原始值 → scale=1.0 保留 (后续人工校准)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import psycopg2

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.secret_policy import validate_secret

NEURON = os.environ.get("NEURON_API_URL", "http://127.0.0.1:7000").rstrip("/") + "/api/v2"
NEURON_USER = os.environ.get("NEURON_USERNAME", "admin")
NEURON_PASS = os.environ.get("NEURON_PASSWORD", "").strip()
DB_DSN = os.environ.get("ZIZU_DSN", "").strip()

# Neuron type code → ZiZu data_type
TYPE_MAP = {
    1: "INT", 2: "INT", 3: "INT", 4: "INT",   # INT8/UINT8/INT16/UINT16
    5: "INT", 6: "INT", 7: "INT", 8: "INT",   # INT32/UINT32/INT64/UINT64
    9: "FLOAT", 10: "FLOAT",                   # FLOAT/DOUBLE
    11: "BOOL", 12: "BOOL",                    # BIT/BOOL
    13: "STRING", 14: "STRING",                # STRING/BYTES
}

# 节点类型推断 (按 plugin + 名字)
def guess_node_type(name: str, plugin: str) -> str:
    n = name.lower()
    if "db" in n or "meter" in n:
        return "METER"
    if "bms" in n:
        return "BMS"
    if "pcs" in n:
        return "PCS"
    if "inv" in n:
        return "INVERTER"
    return "DEVICE"


def api(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{NEURON}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def login() -> str:
    req = urllib.request.Request(
        f"{NEURON}/login",
        data=json.dumps({"name": NEURON_USER, "pass": NEURON_PASS}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())["token"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印不写入 DB")
    args = ap.parse_args()

    try:
        validate_secret("neuron", NEURON_PASS)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not DB_DSN:
        raise RuntimeError("ZIZU_DSN is required")
    try:
        validate_secret("database", psycopg2.extensions.parse_dsn(DB_DSN).get("password", ""))
    except (ValueError, psycopg2.ProgrammingError) as exc:
        raise RuntimeError(f"ZIZU_DSN is not secure: {exc}") from exc

    print("=" * 60)
    print("Neuron → ZiZu tag 同步")
    print("=" * 60)

    token = login()
    print("[1/4] Neuron login OK")

    # DRIVER 节点 (type=1)
    nodes = api(token, "/node?type=1")["nodes"]
    print(f"[2/4] DRIVER nodes: {[n['name'] for n in nodes]}")

    conn = None
    if not args.dry_run:
        conn = psycopg2.connect(DB_DSN)
        conn.autocommit = False
        cur = conn.cursor()
        # 节点树骨架 (幂等)
        cur.execute("""
            INSERT INTO t_nodes (id, name, parent_id, layer, node_type, config)
            VALUES
              ('10000000-0000-0000-0000-000000000001','宇泰en9测试场',NULL,1,'SITE','{}'),
              ('10000000-0000-0000-0000-000000000002','配电房','10000000-0000-0000-0000-000000000001',2,'STATION','{}'),
              ('10000000-0000-0000-0000-000000000003','设备层','10000000-0000-0000-0000-000000000002',3,'STATION','{}')
            ON CONFLICT (id) DO NOTHING;
        """)

    total_tags = 0
    for node in nodes:
        name, plugin = node["name"], node["plugin"]
        ntype = guess_node_type(name, plugin)

        # 节点 (幂等 upsert by name)
        node_id = None
        if not args.dry_run:
            cur.execute("SELECT id FROM t_nodes WHERE name = %s", (name,))
            row = cur.fetchone()
            if row:
                node_id = row[0]
            else:
                cur.execute(
                    """INSERT INTO t_nodes (name, parent_id, layer, node_type, config)
                       VALUES (%s, '10000000-0000-0000-0000-000000000003', 4, %s, %s)
                       RETURNING id""",
                    (name, ntype, json.dumps({"plugin": plugin, "source": "neuron-sync"})),
                )
                node_id = cur.fetchone()[0]

        # group → tags
        try:
            groups = api(token, f"/group?node={name}")["groups"]
        except Exception as e:
            print(f"  [SKIP] {name}: group query failed: {e}")
            continue

        for g in groups:
            gname = g["name"]
            try:
                tags = api(token, f"/tags?node={name}&group={gname}")["tags"]
            except Exception as e:
                print(f"  [SKIP] {name}/{gname}: tags query failed: {e}")
                continue

            print(f"[3/4] {name} ({ntype}) group={gname}: {len(tags)} tags, node_id={node_id}")

            for i, t in enumerate(tags):
                tname = t["name"]
                dtype = TYPE_MAP.get(t["type"], "FLOAT")
                decimal = t.get("decimal", 0.0) or 0.0
                bias = t.get("bias", 0.0) or 0.0
                addr = t.get("address", "")
                desc = t.get("description", "")
                unit = desc.rstrip("0123456789.") if desc else ""

                # 缩放策略: Neuron decimal ≠ 0 表示已缩放 → 我们 scale=1.0
                scale = 1.0

                if args.dry_run:
                    if i < 3:
                        print(f"    {tname} type={dtype} dec={decimal} addr={addr}")
                else:
                    # 幂等: (node_id, name) 唯一则跳过
                    cur.execute(
                        "SELECT id FROM t_tags WHERE node_id = %s AND name = %s",
                        (node_id, tname),
                    )
                    if cur.fetchone():
                        continue
                    cur.execute(
                        """INSERT INTO t_tags
                           (node_id, tag_type, data_type, name, display_name, unit,
                            source_type, source_path, scale_factor, value_offset,
                            description, sort_order, enabled)
                           VALUES (%s,'PHYSICAL',%s,%s,%s,%s,'NEURON',%s,%s,%s,%s,%s,true)""",
                        (node_id, dtype, tname, tname, unit,
                         f"{name}/{gname}/{addr}", scale, bias, desc or None, i),
                    )
                    total_tags += 1

    if not args.dry_run:
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM t_tags")
        db_total = cur.fetchone()[0]
        cur.close()
        conn.close()
        print(f"[4/4] 新插入 {total_tags} tags; DB 总计 {db_total} tags")
    else:
        print("[4/4] DRY-RUN 结束 (未写入)")

    print("=" * 60)
    print("完成. 重启 backend 重载规则: docker restart zizu")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
