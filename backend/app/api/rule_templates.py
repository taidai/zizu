"""
ZiZu Rule Templates API — 可配置规则模板

规则模板把业务逻辑（光储充调度、心跳测试等）从代码中抽离，
存入数据库，用户可在前端选择模板后快速创建规则，
也可以通过 API 自定义/扩展模板，实现「不写死代码」。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter()

# ══════════════════════════════════════
# 默认模板（首次启动时自动写入数据库）
# ══════════════════════════════════════

_DEFAULT_TEMPLATES = [
    {
        "name": "光储充调度",
        "description": "PV + ESS + EVSE，根据 SOC / 光伏 / 电网功率 / 电价自动调度 PCS 与充电桩",
        "rule_type": "control",
        "graph": {
            "nodes": [
                {"id": "input-1", "type": "inputNode", "name": "Site Telemetry", "position": {"x": 70, "y": 250}},
                {
                    "id": "table-1",
                    "type": "decisionTableNode",
                    "name": "Energy Dispatch",
                    "position": {"x": 370, "y": 250},
                    "content": {
                        "hitPolicy": "first",
                        "inputs": [
                            {"id": "soc", "name": "SOC %", "field": "soc"},
                            {"id": "pv_power", "name": "PV Power kW", "field": "pv_power"},
                            {"id": "grid_power", "name": "Grid Power kW", "field": "grid_power"},
                            {"id": "tou_price", "name": "TOU Price", "field": "tou_price"},
                        ],
                        "outputs": [
                            {"id": "pcs_setpoint", "name": "PCS Setpoint kW", "field": "pcs_setpoint"},
                            {"id": "evse_current", "name": "EV Current A", "field": "evse_current"},
                            {"id": "strategy", "name": "Strategy", "field": "strategy"},
                        ],
                        "rules": [
                            {"_id": "r1", "soc": "< 10", "pv_power": "*", "grid_power": "*", "tou_price": "*", "pcs_setpoint": "0", "evse_current": "0", "strategy": '"电池亏电保护"'},
                            {"_id": "r2", "soc": "> 95", "pv_power": "*", "grid_power": "*", "tou_price": "*", "pcs_setpoint": "0", "evse_current": "16", "strategy": '"电池充满，光伏直供"'},
                            {"_id": "r3", "soc": "*", "pv_power": "> 80", "tou_price": "< 0.4", "grid_power": "*", "pcs_setpoint": "-min(pv_power - 80, 50)", "evse_current": "16", "strategy": '"光伏富余，低价储充"'},
                            {"_id": "r4", "soc": "*", "pv_power": "< 80", "tou_price": "> 0.8", "grid_power": "*", "pcs_setpoint": "min(80 - pv_power, 50)", "evse_current": "8", "strategy": '"高电价放电+限充"'},
                            {"_id": "r5", "soc": "*", "pv_power": "*", "grid_power": "*", "tou_price": "*", "pcs_setpoint": "pv_power - 80", "evse_current": "16", "strategy": '"默认自发自用"'},
                        ],
                    },
                },
                {"id": "output-1", "type": "outputNode", "name": "Dispatch Command", "position": {"x": 670, "y": 250}},
            ],
            "edges": [
                {"id": "e1", "sourceId": "input-1", "targetId": "table-1", "type": "edge"},
                {"id": "e2", "sourceId": "table-1", "targetId": "output-1", "type": "edge"},
            ],
        },
        "config": {
            "sourceNodeIds": [],
            "actions": [],
            "inputMappings": {
                "soc": "ess.soc",
                "pv_power": "pv.activePower",
                "grid_power": "grid.activePower",
                "tou_price": "billing.tariffPeakPrice",
            },
            "outputBindings": [
                {"field": "pcs_setpoint", "name": "PCS Setpoint kW", "node": "", "group": "", "tag": "", "cooldown": 60},
                {"field": "evse_current", "name": "EV Current A", "node": "", "group": "", "tag": "", "cooldown": 60},
            ],
            "template": "energy_dispatch",
        },
        "enabled": True,
        "is_default": True,
    },
    {
        "name": "防逆流保护",
        "description": "当检测到向电网反向送电时，降低 PCS 放电功率或切换为充电",
        "rule_type": "control",
        "graph": {
            "nodes": [
                {"id": "input-1", "type": "inputNode", "name": "Grid Telemetry", "position": {"x": 70, "y": 250}},
                {
                    "id": "table-1",
                    "type": "decisionTableNode",
                    "name": "Anti Reflux",
                    "position": {"x": 370, "y": 250},
                    "content": {
                        "hitPolicy": "first",
                        "inputs": [
                            {"id": "grid_power", "name": "Grid Power kW", "field": "grid_power"},
                            {"id": "soc", "name": "SOC %", "field": "soc"},
                        ],
                        "outputs": [
                            {"id": "pcs_setpoint", "name": "PCS Setpoint kW", "field": "pcs_setpoint"},
                            {"id": "strategy", "name": "Strategy", "field": "strategy"},
                        ],
                        "rules": [
                            {"_id": "r1", "grid_power": "< -5", "soc": "> 20", "pcs_setpoint": "max(grid_power + 5, -50)", "strategy": '"反向功率，减少放电"'},
                            {"_id": "r2", "grid_power": "< -10", "soc": "<= 20", "pcs_setpoint": "0", "strategy": '"SOC低，停机保护"'},
                            {"_id": "r3", "grid_power": ">= -5", "soc": "*", "pcs_setpoint": "grid_power", "strategy": '"正常范围"'},
                        ],
                    },
                },
                {"id": "output-1", "type": "outputNode", "name": "Command", "position": {"x": 670, "y": 250}},
            ],
            "edges": [
                {"id": "e1", "sourceId": "input-1", "targetId": "table-1", "type": "edge"},
                {"id": "e2", "sourceId": "table-1", "targetId": "output-1", "type": "edge"},
            ],
        },
        "config": {
            "sourceNodeIds": [],
            "actions": [],
            "inputMappings": {"grid_power": "grid.activePower", "soc": "ess.soc"},
            "outputBindings": [
                {"field": "pcs_setpoint", "name": "PCS Setpoint kW", "node": "", "group": "", "tag": "", "cooldown": 10},
            ],
            "template": "anti_reflux",
        },
        "enabled": True,
        "is_default": True,
    },
    {
        "name": "峰谷套利",
        "description": "根据分时电价：低谷充电、高峰放电",
        "rule_type": "control",
        "graph": {
            "nodes": [
                {"id": "input-1", "type": "inputNode", "name": "Price & SOC", "position": {"x": 70, "y": 250}},
                {
                    "id": "table-1",
                    "type": "decisionTableNode",
                    "name": "Peak Valley Arbitrage",
                    "position": {"x": 370, "y": 250},
                    "content": {
                        "hitPolicy": "first",
                        "inputs": [
                            {"id": "tou_price", "name": "Current Price 元/kWh", "field": "tou_price"},
                            {"id": "soc", "name": "SOC %", "field": "soc"},
                        ],
                        "outputs": [
                            {"id": "pcs_setpoint", "name": "PCS Setpoint kW", "field": "pcs_setpoint"},
                            {"id": "strategy", "name": "Strategy", "field": "strategy"},
                        ],
                        "rules": [
                            {"_id": "r1", "tou_price": "< 0.3", "soc": "< 90", "pcs_setpoint": "-50", "strategy": '"低谷充电"'},
                            {"_id": "r2", "tou_price": "> 0.8", "soc": "> 30", "pcs_setpoint": "50", "strategy": '"高峰放电"'},
                            {"_id": "r3", "tou_price": "*", "soc": "*", "pcs_setpoint": "0", "strategy": '"保持"'},
                        ],
                    },
                },
                {"id": "output-1", "type": "outputNode", "name": "Command", "position": {"x": 670, "y": 250}},
            ],
            "edges": [
                {"id": "e1", "sourceId": "input-1", "targetId": "table-1", "type": "edge"},
                {"id": "e2", "sourceId": "table-1", "targetId": "output-1", "type": "edge"},
            ],
        },
        "config": {
            "sourceNodeIds": [],
            "actions": [],
            "inputMappings": {"tou_price": "billing.tariffPeakPrice", "soc": "ess.soc"},
            "outputBindings": [
                {"field": "pcs_setpoint", "name": "PCS Setpoint kW", "node": "", "group": "", "tag": "", "cooldown": 60},
            ],
            "template": "peak_valley",
        },
        "enabled": True,
        "is_default": True,
    },
    {
        "name": "需量控制",
        "description": "当关口功率超过需量阈值时，调用储能放电削峰",
        "rule_type": "control",
        "graph": {
            "nodes": [
                {"id": "input-1", "type": "inputNode", "name": "Demand", "position": {"x": 70, "y": 250}},
                {
                    "id": "table-1",
                    "type": "decisionTableNode",
                    "name": "Demand Control",
                    "position": {"x": 370, "y": 250},
                    "content": {
                        "hitPolicy": "first",
                        "inputs": [
                            {"id": "grid_power", "name": "Grid Power kW", "field": "grid_power"},
                            {"id": "demand_limit", "name": "Demand Limit kW", "field": "demand_limit"},
                            {"id": "soc", "name": "SOC %", "field": "soc"},
                        ],
                        "outputs": [
                            {"id": "pcs_setpoint", "name": "PCS Setpoint kW", "field": "pcs_setpoint"},
                            {"id": "strategy", "name": "Strategy", "field": "strategy"},
                        ],
                        "rules": [
                            {"_id": "r1", "grid_power": "> demand_limit", "soc": "> 30", "demand_limit": "*", "pcs_setpoint": "min(grid_power - demand_limit, 100)", "strategy": '"削峰放电"'},
                            {"_id": "r2", "grid_power": "> demand_limit", "soc": "<= 30", "demand_limit": "*", "pcs_setpoint": "0", "strategy": '"SOC不足，无法削峰"'},
                            {"_id": "r3", "grid_power": "<= demand_limit", "soc": "*", "demand_limit": "*", "pcs_setpoint": "0", "strategy": '"未超需量"'},
                        ],
                    },
                },
                {"id": "output-1", "type": "outputNode", "name": "Command", "position": {"x": 670, "y": 250}},
            ],
            "edges": [
                {"id": "e1", "sourceId": "input-1", "targetId": "table-1", "type": "edge"},
                {"id": "e2", "sourceId": "table-1", "targetId": "output-1", "type": "edge"},
            ],
        },
        "config": {
            "sourceNodeIds": [],
            "actions": [],
            "inputMappings": {"grid_power": "grid.activePower", "soc": "ess.soc", "demand_limit": "ems.strategyPowerLimit"},
            "outputBindings": [
                {"field": "pcs_setpoint", "name": "PCS Setpoint kW", "node": "", "group": "", "tag": "", "cooldown": 30},
            ],
            "template": "demand_control",
        },
        "enabled": True,
        "is_default": True,
    },
    {
        "name": "心跳测试",
        "description": "固定写入心跳信号，验证控制链路是否打通",
        "rule_type": "control",
        "graph": {
            "nodes": [
                {"id": "input-1", "type": "inputNode", "name": "Trigger", "position": {"x": 70, "y": 250}},
                {
                    "id": "table-1",
                    "type": "decisionTableNode",
                    "name": "Heartbeat",
                    "position": {"x": 370, "y": 250},
                    "content": {
                        "hitPolicy": "first",
                        "inputs": [{"id": "trigger", "name": "Trigger", "field": "trigger"}],
                        "outputs": [{"id": "value", "name": "Value", "field": "value"}],
                        "rules": [{"_id": "r1", "trigger": "*", "value": "1"}],
                    },
                },
                {"id": "output-1", "type": "outputNode", "name": "Command", "position": {"x": 670, "y": 250}},
            ],
            "edges": [
                {"id": "e1", "sourceId": "input-1", "targetId": "table-1", "type": "edge"},
                {"id": "e2", "sourceId": "table-1", "targetId": "output-1", "type": "edge"},
            ],
        },
        "config": {
            "sourceNodeIds": [],
            "actions": [{"type": "neuron_write", "node": "", "group": "", "tag": "", "value": "1", "cooldown": 60}],
            "inputMappings": {},
            "outputBindings": [],
            "template": "heartbeat",
        },
        "enabled": True,
        "is_default": True,
    },
    {
        "name": "自定义",
        "description": "从空白决策图开始，自行拖拽节点",
        "rule_type": "control",
        "graph": {
            "nodes": [
                {"id": "input-1", "type": "inputNode", "name": "Request", "position": {"x": 70, "y": 250}},
                {"id": "output-1", "type": "outputNode", "name": "Response", "position": {"x": 670, "y": 250}},
            ],
            "edges": [],
        },
        "config": {"sourceNodeIds": [], "actions": [], "inputMappings": {}, "outputBindings": [], "template": "custom"},
        "enabled": True,
        "is_default": True,
    },
]


class RuleTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None)
    rule_type: str = Field(..., pattern="^(alarm|control|fault_map|linkage)$")
    graph: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False


class RuleTemplateUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    rule_type: str | None = Field(None, pattern="^(alarm|control|fault_map|linkage)$")
    graph: dict | None = None
    config: dict | None = None
    enabled: bool | None = None


def _ensure_table() -> None:
    """确保 t_rule_templates 表存在；首次创建后自动写入默认模板。"""
    from app.services.telemetry_store import get_connection

    create_sql = """
    CREATE TABLE IF NOT EXISTS t_rule_templates (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        rule_type TEXT NOT NULL CHECK (rule_type IN ('alarm', 'control', 'fault_map', 'linkage')),
        graph JSONB NOT NULL,
        config JSONB NOT NULL DEFAULT '{}',
        enabled BOOLEAN DEFAULT TRUE,
        is_default BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(create_sql)
            for tmpl in _DEFAULT_TEMPLATES:
                cur.execute("SELECT id FROM t_rule_templates WHERE name = %s", (tmpl["name"],))
                if cur.fetchone():
                    cur.execute(
                        """
                        UPDATE t_rule_templates
                        SET description = %s, rule_type = %s, graph = %s, config = %s, enabled = %s, is_default = %s, updated_at = now()
                        WHERE name = %s
                        """,
                        (
                            tmpl.get("description"), tmpl["rule_type"],
                            json.dumps(tmpl["graph"]), json.dumps(tmpl["config"]),
                            tmpl.get("enabled", True), tmpl.get("is_default", False),
                            tmpl["name"],
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO t_rule_templates
                        (name, description, rule_type, graph, config, enabled, is_default, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
                        """,
                        (
                            tmpl["name"],
                            tmpl.get("description"),
                            tmpl["rule_type"],
                            json.dumps(tmpl["graph"]),
                            json.dumps(tmpl["config"]),
                            tmpl.get("enabled", True),
                            tmpl.get("is_default", False),
                        ),
                    )
            conn.commit()


def _serialize(row: dict) -> dict:
    row = dict(row)
    row["id"] = str(row["id"])
    if isinstance(row.get("graph"), str):
        try:
            row["graph"] = json.loads(row["graph"])
        except Exception:
            pass
    if isinstance(row.get("config"), str):
        try:
            row["config"] = json.loads(row["config"])
        except Exception:
            pass
    if row.get("created_at"):
        row["created_at"] = row["created_at"].isoformat()
    if row.get("updated_at"):
        row["updated_at"] = row["updated_at"].isoformat()
    return row


@router.get("/rule-templates")
async def list_templates() -> dict:
    """列出所有规则模板。"""
    from app.services.telemetry_store import get_connection
    _ensure_table()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, description, rule_type, graph, config, enabled, is_default, created_at, updated_at "
                "FROM t_rule_templates ORDER BY is_default DESC, name"
            )
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    return {"templates": [_serialize(r) for r in rows], "total": len(rows)}


@router.get("/rule-templates/{template_id}")
async def get_template(template_id: UUID) -> dict:
    """获取单个规则模板。"""
    from app.services.telemetry_store import get_connection
    _ensure_table()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, description, rule_type, graph, config, enabled, is_default, created_at, updated_at "
                "FROM t_rule_templates WHERE id = %s",
                (template_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")
            columns = [desc[0] for desc in cur.description]
            return _serialize(dict(zip(columns, row)))


@router.post("/rule-templates")
async def create_template(req: RuleTemplateCreate) -> dict:
    """创建规则模板。"""
    from app.services.telemetry_store import get_connection
    _ensure_table()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_rule_templates
                    (name, description, rule_type, graph, config, enabled, is_default, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, name, description, rule_type, graph, config, enabled, is_default, created_at, updated_at
                    """,
                    (
                        req.name, req.description, req.rule_type, json.dumps(req.graph),
                        json.dumps(req.config), req.enabled, req.is_default,
                        datetime.now(timezone.utc), datetime.now(timezone.utc),
                    ),
                )
                row = dict(zip([desc[0] for desc in cur.description], cur.fetchone()))
                conn.commit()
        return _serialize(row)
    except Exception as e:
        logger.error("[API/rule-templates] create failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/rule-templates/{template_id}")
async def update_template(template_id: UUID, req: RuleTemplateUpdate) -> dict:
    """更新规则模板。"""
    from app.services.telemetry_store import get_connection
    _ensure_table()

    data = req.model_dump(exclude_none=True)
    if "graph" in data:
        data["graph"] = json.dumps(data["graph"])
    if "config" in data:
        data["config"] = json.dumps(data["config"])
    if not data:
        return await get_template(template_id)

    updates = []
    params: list = []
    for field, value in data.items():
        updates.append(f"{field} = %s")
        params.append(value)
    updates.append("updated_at = %s")
    params.append(datetime.now(timezone.utc))
    params.append(template_id)

    query = f"UPDATE t_rule_templates SET {', '.join(updates)} WHERE id = %s RETURNING id, name, description, rule_type, graph, config, enabled, is_default, created_at, updated_at"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Template not found")
                conn.commit()
                columns = [desc[0] for desc in cur.description]
                return _serialize(dict(zip(columns, row)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/rule-templates] update failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/rule-templates/{template_id}")
async def delete_template(template_id: UUID) -> dict:
    """删除规则模板。"""
    from app.services.telemetry_store import get_connection
    _ensure_table()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_rule_templates WHERE id = %s RETURNING id", (template_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")
            conn.commit()
    return {"status": "deleted", "id": str(template_id)}

class RuleTemplateApplyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="基于模板创建的新规则名称")
    enabled: bool = True


@router.post("/rule-templates/{template_id}/apply")
async def apply_template(template_id: UUID, req: RuleTemplateApplyRequest) -> dict:
    """基于规则模板快速创建一条可编辑的规则。"""
    from app.services.telemetry_store import get_connection

    tpl = await get_template(template_id)
    graph = tpl.get("graph") or {}
    config = tpl.get("config") or {}
    jdm_content = {**graph, "_config": config}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_rules (name, rule_type, jdm_content, enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, name, rule_type, jdm_content, version, enabled, created_at, updated_at
                    """,
                    (req.name, tpl.get("rule_type"), json.dumps(jdm_content), req.enabled,
                     datetime.now(timezone.utc), datetime.now(timezone.utc)),
                )
                columns = [desc[0] for desc in cur.description]
                row = dict(zip(columns, cur.fetchone()))
                conn.commit()
        from app.api.rules import _serialize_rule
        return {"rule": _serialize_rule(row), "status": "created"}
    except Exception as e:
        logger.error("[API/rule-templates] apply failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))
