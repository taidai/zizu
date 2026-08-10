"""
ZiZu Rules API - 规则引擎管理

规则以 GoRules JDM JSON 形式存储，支持 CRUD 与模拟测试。
后端使用 zen-engine 对 JDM 决策图/决策表进行真实评估。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter()

# zen-engine 为可选依赖；未安装时模拟接口回退到占位实现
ZEN_AVAILABLE = False
ZenEngine = None
ZenError = Exception
try:
    from zen import ZenEngine as _ZenEngine

    ZenEngine = _ZenEngine
    ZEN_AVAILABLE = True
except Exception as e:
    logger.warning("[API/rules] zen package not available: {}", e)


class RuleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    rule_type: str = Field(..., pattern="^(alarm|control|fault_map|linkage)$")
    jdm_content: dict = Field(default_factory=dict)
    enabled: bool = True


class RuleUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    rule_type: str | None = Field(None, pattern="^(alarm|control|fault_map|linkage)$")
    jdm_content: dict | None = None
    enabled: bool | None = None


class SimulateRequest(BaseModel):
    context: dict = Field(default_factory=dict)


class EvaluateRequest(BaseModel):
    content: dict = Field(default_factory=dict)
    context: dict = Field(default_factory=dict)


def _serialize_rule(row: dict) -> dict:
    row = dict(row)
    row["id"] = str(row["id"])
    if row.get("jdm_content") and isinstance(row["jdm_content"], str):
        try:
            row["jdm_content"] = json.loads(row["jdm_content"])
        except Exception:
            pass
    if row.get("created_at"):
        row["created_at"] = row["created_at"].isoformat()
    if row.get("updated_at"):
        row["updated_at"] = row["updated_at"].isoformat()
    return row


@router.get("/rules")
async def list_rules(enabled: bool | None = Query(None)) -> dict:
    """列出所有规则。"""
    from app.services.telemetry_store import get_connection

    conditions = []
    params: list = []
    if enabled is not None:
        conditions.append("enabled = %s")
        params.append(enabled)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
    SELECT id, name, rule_type, jdm_content, version, enabled, created_at, updated_at
    FROM t_rules
    {where}
    ORDER BY updated_at DESC
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                columns = [desc[0] for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        return {"rules": [_serialize_rule(r) for r in rows]}
    except Exception as e:
        logger.error("[API/rules] list failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rules")
async def create_rule(req: RuleCreateRequest) -> dict:
    """创建规则。"""
    from app.services.telemetry_store import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_rules (name, rule_type, jdm_content, enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, name, rule_type, jdm_content, version, enabled, created_at, updated_at
                    """,
                    (req.name, req.rule_type, json.dumps(req.jdm_content), req.enabled,
                     datetime.now(timezone.utc), datetime.now(timezone.utc)),
                )
                row = dict(zip([desc[0] for desc in cur.description], cur.fetchone()))
                conn.commit()
        return _serialize_rule(row)
    except Exception as e:
        logger.error("[API/rules] create failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: UUID) -> dict:
    """获取单个规则。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, rule_type, jdm_content, version, enabled, created_at, updated_at FROM t_rules WHERE id = %s",
                (rule_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Rule not found")
            columns = [desc[0] for desc in cur.description]
            return _serialize_rule(dict(zip(columns, row)))


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: UUID, req: RuleUpdateRequest) -> dict:
    """更新规则，版本号 +1。"""
    from app.services.telemetry_store import get_connection

    updates = []
    params: list = []
    data = req.model_dump(exclude_none=True)
    for field, value in data.items():
        if field == "jdm_content":
            updates.append("jdm_content = %s")
            params.append(json.dumps(value))
        else:
            updates.append(f"{field} = %s")
            params.append(value)

    if not updates:
        return await get_rule(rule_id)

    updates.append("version = version + 1")
    updates.append("updated_at = %s")
    params.append(datetime.now(timezone.utc))
    params.append(rule_id)

    query = f"UPDATE t_rules SET {', '.join(updates)} WHERE id = %s RETURNING id, name, rule_type, jdm_content, version, enabled, created_at, updated_at"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Rule not found")
                conn.commit()
                columns = [desc[0] for desc in cur.description]
                return _serialize_rule(dict(zip(columns, row)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API/rules] update failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: UUID) -> dict:
    """删除规则。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM t_rules WHERE id = %s RETURNING id", (rule_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Rule not found")
            conn.commit()
    return {"status": "deleted", "id": str(rule_id)}


@router.post("/rules/evaluate")
async def evaluate_rule(req: EvaluateRequest) -> dict:
    """直接评估决策图/表内容，不依赖数据库中的规则。"""
    logger.info("[API/rules] evaluate context_keys={}", list(req.context.keys()))
    try:
        if ZEN_AVAILABLE:
            evaluation = _evaluate_with_zen(req.content, req.context)
        else:
            evaluation = {
                "result": {"hit": True, "actions": [{"type": "log", "message": "zen 未安装，返回占位结果"}]},
                "trace": None,
                "performance": None,
            }
        return {
            "context": req.context,
            "evaluation": evaluation,
        }
    except Exception as e:
        logger.error("[API/rules] evaluate failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


def _normalize_table_cell(value: str | None, is_input: bool) -> str:
    """将决策表空单元格转换为 zen-engine 可识别的表达式。"""
    if value is None or value == "":
        return "1 == 1" if is_input else "null"
    return str(value)


def _table_to_graph(jdm_content: dict) -> dict:
    """将 jdm-editor 决策图/表转换为 zen-engine 可评估的决策图。"""
    if "nodes" not in jdm_content:
        # 纯决策表对象：包装成 zen-engine 决策图
        return _build_zen_graph(
            jdm_content.get("inputs", []),
            jdm_content.get("outputs", []),
            jdm_content.get("rules", []),
            jdm_content.get("hitPolicy", "first"),
        )

    # 决策图格式：转换节点类型并规范化决策表单元格
    node_type_map = {
        "startNode": "inputNode",
        "decisionNode": "decisionTableNode",  # 兼容旧版本 jdm-editor
        "endNode": "outputNode",
    }
    new_nodes = []
    for node in jdm_content.get("nodes", []):
        new_node = dict(node)
        new_node["type"] = node_type_map.get(node.get("type"), node.get("type"))
        if new_node["type"] == "decisionTableNode" and "content" in new_node:
            content = new_node["content"]
            new_node["content"] = _build_zen_table_content(
                content.get("inputs", []),
                content.get("outputs", []),
                content.get("rules", []),
                content.get("hitPolicy", "first"),
            )
        new_nodes.append(new_node)

    return {
        "nodes": new_nodes,
        "edges": jdm_content.get("edges", []),
    }


def _build_zen_table_content(inputs: list, outputs: list, rules: list, hit_policy: str) -> dict:
    """构建 zen-engine 决策表内容，并规范化空单元格。"""
    normalized_rules = []
    for rule in rules:
        new_rule: dict = {"_id": rule.get("_id", str(id(rule)))}
        for col in inputs:
            new_rule[col["id"]] = _normalize_table_cell(rule.get(col["id"]), is_input=True)
        for col in outputs:
            new_rule[col["id"]] = _normalize_table_cell(rule.get(col["id"]), is_input=False)
        if rule.get("_description"):
            new_rule["_description"] = rule["_description"]
        normalized_rules.append(new_rule)

    return {
        "hitPolicy": hit_policy,
        "inputs": inputs,
        "outputs": outputs,
        "rules": normalized_rules,
    }


def _build_zen_graph(inputs: list, outputs: list, rules: list, hit_policy: str) -> dict:
    """用决策表内容构建一个标准的 zen-engine 决策图。"""
    return {
        "nodes": [
            {"id": "input", "type": "inputNode", "name": "Input"},
            {
                "id": "table",
                "type": "decisionTableNode",
                "name": "决策表",
                "content": _build_zen_table_content(inputs, outputs, rules, hit_policy),
            },
            {"id": "output", "type": "outputNode", "name": "Output"},
        ],
        "edges": [
            {"id": "e1", "sourceId": "input", "targetId": "table", "type": "edge"},
            {"id": "e2", "sourceId": "table", "targetId": "output", "type": "edge"},
        ],
    }


def _evaluate_with_zen(jdm_content: dict, context: dict) -> dict:
    """使用 zen-engine 评估 JDM 决策图/决策表。"""
    if not ZEN_AVAILABLE or ZenEngine is None:
        raise RuntimeError("zen 未安装，无法评估规则")

    graph = _table_to_graph(jdm_content)
    engine = ZenEngine()
    decision = engine.create_decision(json.dumps(graph))
    response = decision.evaluate(context, {"trace": True})
    return {
        "result": response.get("result") if isinstance(response, dict) else getattr(response, "result", None),
        "trace": response.get("trace") if isinstance(response, dict) else getattr(response, "trace", None),
        "performance": response.get("performance") if isinstance(response, dict) else getattr(response, "performance", None),
    }


@router.post("/rules/{rule_id}/simulate")
async def simulate_rule(rule_id: UUID, req: SimulateRequest) -> dict:
    """
    规则模拟：加载 JDM 内容并使用 zen-engine 评估上下文。
    若 zen 未安装，返回占位结果以便 UI 联调。
    """
    rule = await get_rule(rule_id)
    logger.info("[API/rules] simulate rule={} context_keys={}", rule_id, list(req.context.keys()))

    try:
        if ZEN_AVAILABLE:
            evaluation = _evaluate_with_zen(rule.get("jdm_content", {}), req.context)
        else:
            evaluation = {
                "result": {"hit": True, "actions": [{"type": "log", "message": "zen 未安装，返回占位结果"}]},
                "trace": None,
                "performance": None,
            }
        return {
            "rule_id": str(rule_id),
            "rule_name": rule.get("name"),
            "context": req.context,
            "evaluation": evaluation,
        }
    except Exception as e:
        logger.error("[API/rules] simulate failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/rules/{rule_id}/dry-run")
async def dry_run_rule_endpoint(rule_id: UUID) -> dict:
    """用当前真实数据试运行单条规则，不执行告警/控制动作。"""
    from app.services.rule_engine import dry_run_rule
    try:
        return dry_run_rule(str(rule_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("[API/rules] dry-run failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))

