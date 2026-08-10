"""
F2 规则引擎 — 告警/控制/联动策略

职责：
  - 每 tick 扫描启用的 t_rules
  - 从 t_telemetry_latest 构建上下文（tag_name -> value）
  - 对每条规则通过 GoRules zen-engine 求值
  - 触发动作：
      alarm     -> 写入 t_alarms（同一规则未恢复时不再重复创建）
      control   -> 经 MQTT 发布控制命令 + 写入 t_audit_log
      linkage   -> 更新指定虚拟点位的 sources 或触发另一规则（MVP 未实现）

求值层：
  - 优先使用 GoRules zen-engine（import zen）
  - zen-engine 不可用时自动 fallback 到安全 AST 求值器
  - 兼容简化格式 {when, actions} 和标准 JDM {nodes, edges, actions}
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import UUID

from loguru import logger

from app.services.gorules_adapter import evaluate_rule


def _build_context(cur, source_node_ids: set[str] | None = None) -> dict[str, dict[str, any]]:
    """从 t_telemetry_latest 构建 tag_name + entity_name -> {value, tag_id, node_id} 上下文。

    同时把全局实体名注入上下文，使规则引擎可直接使用业务语义变量（如 pcs.activePower）。
    若提供 source_node_ids，则只保留选中节点下的点位。
    """
    where = ""
    params = ()
    if source_node_ids:
        where = "WHERE t.node_id = ANY(%s)"
        params = (list(source_node_ids),)
    cur.execute(
        f"""
        SELECT t.id, t.node_id, t.name,
               l.value_float, l.value_int, l.value_bool, l.value_str
        FROM t_telemetry_latest l
        JOIN t_tags t ON t.id = l.tag_id
        {where}
        """,
        params,
    )
    ctx: dict[str, dict[str, any]] = {}
    for tag_id, node_id, name, value_float, value_int, value_bool, value_str in cur.fetchall():
        value: float | bool | int | str | None = None
        if value_bool is not None:
            value = value_bool
        elif value_str is not None:
            if value_str.lower() in ("true", "false"):
                value = value_str.lower() == "true"
            else:
                try:
                    if "." in value_str:
                        value = float(value_str)
                    else:
                        value = int(value_str)
                except ValueError:
                    value = value_str
        elif value_float is not None:
            value = value_float
        elif value_int is not None:
            value = value_int

        ctx[name] = {
            "value": value,
            "tag_id": tag_id,
            "node_id": node_id,
        }
    # 注入全局实体当前值
    try:
        cur.execute("""
            SELECT e.name, e.id AS entity_id, b.id AS binding_id, b.tag_id, b.node_id,
                   l.value_float, l.value_int, l.value_bool, l.value_str
            FROM t_entities e
            JOIN t_entity_bindings b ON b.entity_id = e.id
            JOIN t_telemetry_latest l ON l.tag_id = b.tag_id AND l.node_id = b.node_id
            WHERE e.enabled = TRUE
              AND b.enabled = TRUE
              AND b.priority = (
                  SELECT MIN(b2.priority)
                  FROM t_entity_bindings b2
                  WHERE b2.entity_id = e.id AND b2.enabled = TRUE
              )
        """)
        for name, entity_id, binding_id, tag_id, node_id, vf, vi, vb, vs in cur.fetchall():
            value = None
            if vb is not None:
                value = vb
            elif vs is not None:
                value = vs
            elif vf is not None:
                value = vf
            elif vi is not None:
                value = vi
            ctx[name] = {
                "value": value,
                "tag_id": tag_id,
                "node_id": node_id,
                "entity_id": entity_id,
                "binding_id": binding_id,
                "is_entity": True,
            }
    except Exception as e:
        logger.warning("[RuleEngine] failed to load entity context (non-fatal): {}", e)

    return ctx


def _has_active_alarm(cur, rule_id: UUID) -> bool:
    cur.execute(
        """
        SELECT 1 FROM t_alarms
        WHERE rule_id = %s AND resolved_at IS NULL
        LIMIT 1
        """,
        (rule_id,),
    )
    return cur.fetchone() is not None


def _create_alarm(
    cur,
    rule_id: UUID,
    node_id: UUID | None,
    tag_id: UUID | None,
    trigger_tag_name: str | None,
    trigger_value: float | int | bool | str | None,
    level: str,
    message: str,
) -> None:
    cur.execute(
        """
        INSERT INTO t_alarms (rule_id, node_id, tag_id, trigger_tag_name, trigger_value, level, message, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        """,
        (
            rule_id,
            node_id,
            tag_id,
            trigger_tag_name,
            float(trigger_value) if isinstance(trigger_value, (int, float)) else None,
            level,
            message,
        ),
    )


def _log_audit(cur, action: str, target_type: str, target_id: str | UUID | None, details: dict) -> None:
    cur.execute(
        """
        INSERT INTO t_audit_log (user_id, action, target_type, target_id, details, created_at)
        VALUES (%s, %s, %s, %s, %s, now())
        """,
        ("system", action, target_type, target_id, json.dumps(details)),
    )


# 内存级控制冷却，避免同一规则每秒都发命令
_last_control_ts: dict[UUID, datetime] = {}


def _control_cooldown_ok(rule_id: UUID, cooldown: int = 60) -> bool:
    last = _last_control_ts.get(rule_id)
    now = datetime.now(timezone.utc)
    if last is None or (now - last).total_seconds() >= cooldown:
        _last_control_ts[rule_id] = now
        return True
    return False


def _resolve_value(value: Any, outputs: dict | None, context_values: dict[str, Any]) -> Any:
    """解析 value 模板，如 {{pcs_setpoint}}。优先从决策输出取，其次从上下文取。"""
    if not isinstance(value, str):
        return value
    tmpl_re = re.compile(r"\{\{\s*([a-zA-Z0-9_\.]+)\s*\}\}")
    if not tmpl_re.search(value):
        return value

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if outputs and key in outputs and outputs[key] is not None:
            return str(outputs[key])
        if key in context_values and context_values[key] is not None:
            return str(context_values[key])
        return match.group(0)

    resolved = tmpl_re.sub(repl, value)
    single = tmpl_re.fullmatch(value)
    if single:
        try:
            if "." in resolved:
                return float(resolved)
            return int(resolved)
        except ValueError:
            return resolved
    return resolved


def _coerce_neuron_value(value: Any) -> Any:
    """把字符串数字转为数值，方便 Neuron 写入。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            pass
    return value


def _execute_neuron_write(cur, rule_id: UUID, action: dict, context: dict, outputs: dict | None = None) -> bool:
    """通过 Neuron REST API 下发写点位指令；支持直接写 entity。"""
    raw_value = action.get("value")
    if raw_value is None:
        logger.warning("[RuleEngine] neuron_write action missing value: {}", action)
        return False

    context_values = _context_values(context)
    value = _coerce_neuron_value(_resolve_value(raw_value, outputs, context_values))

    # 优先按 entity 写回：规则引擎输出可作用于全局实体
    entity_id = action.get("entity_id")
    entity_name = action.get("entity")
    if entity_id or entity_name:
        from app.services.entity_resolver import write_entity_value
        try:
            target = entity_id or entity_name
            result = write_entity_value(target, value)
            _log_audit(cur, "ENTITY_WRITE", "entity", result.get("entity_id"), {
                "rule_id": str(rule_id),
                "entity_name": result.get("entity_name"),
                "value": value,
                "context": {k: v for k, v in context.items() if isinstance(v, (int, float, bool, str))},
            })
            return True
        except Exception as e:
            logger.error("[RuleEngine] entity write failed for rule {}: {}", rule_id, e)
            return False

    node = action.get("node")
    group = action.get("group")
    tag = action.get("tag")
    if not node or not group or not tag:
        logger.warning("[RuleEngine] neuron_write action missing node/group/tag: {}", action)
        return False

    from app.services.neuron_client import get_neuron_client
    try:
        client = get_neuron_client()
        client.write_tag(node, group, tag, value)
    except Exception as e:
        logger.error("[RuleEngine] Neuron write failed for rule {}: {}", rule_id, e)
        return False

    _log_audit(cur, "NEURON_WRITE", "device", action.get("target_id"), {
        "rule_id": str(rule_id),
        "node": node,
        "group": group,
        "tag": tag,
        "value": value,
        "context": {k: v for k, v in context.items() if isinstance(v, (int, float, bool, str))},
    })
    return True


def _execute_control(cur, rule_id: UUID, action: dict, context: dict, outputs: dict | None = None) -> bool:
    """执行控制动作：优先走 Neuron 写点位，否则发布 MQTT 命令。"""
    a_type = action.get("type")
    if a_type == "neuron_write":
        return _execute_neuron_write(cur, rule_id, action, context, outputs)

    from app.services.mqtt_client import get_mqtt_client

    command = action.get("command", {})
    topic = command.get("topic")
    payload = command.get("payload")
    if not topic or payload is None:
        logger.warning("[RuleEngine] control action missing topic/payload: {}", action)
        return False

    mqtt_client = get_mqtt_client()
    if mqtt_client is None:
        logger.warning("[RuleEngine] MQTT client not available, control skipped")
        return False

    try:
        payload_str = json.dumps(payload, ensure_ascii=False)
        mqtt_client.publish(topic, payload_str)
    except Exception as e:
        logger.error("[RuleEngine] MQTT publish failed for control action: {}", e)
        return False

    _log_audit(cur, "RPC", "device", action.get("target_id"), {
        "rule_id": str(rule_id),
        "topic": topic,
        "payload": payload,
        "context": {k: v for k, v in context.items() if isinstance(v, (int, float, bool, str))},
    })
    return True



def _context_values(context: dict[str, dict[str, any]]) -> dict[str, float | bool | int | str]:
    """把带元信息的上下文展平为 tag_name -> value，供 GoRules 求值使用。"""
    return {k: v["value"] for k, v in context.items() if v.get("value") is not None}


def _extract_first_varname(expression: str | None) -> str | None:
    """从表达式中粗略提取第一个变量名（用于定位触发点位）。"""
    if not expression:
        return None
    import re
    match = re.search(r"[a-zA-Z_][a-zA-Z0-9_]*", expression)
    return match.group(0) if match else None

def _apply_input_mappings(context: dict, input_mappings: dict[str, str] | None) -> dict:
    """按 inputMappings 把真实 tag 名映射为决策表字段名。"""
    if not input_mappings:
        return context
    mapped = dict(context)
    for field, tag_name in input_mappings.items():
        if tag_name in context:
            mapped[field] = context[tag_name]
    return mapped


def run_rule_tick() -> dict[str, int]:
    """
    执行一次 F2 规则 tick。

    Returns:
        {"evaluated": N, "alarms": N, "controls": N, "errors": N}
    """

    from app.services.telemetry_store import get_connection

    result = {"evaluated": 0, "alarms": 0, "controls": 0, "errors": 0}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, rule_type, jdm_content, enabled
                FROM t_rules
                WHERE enabled = TRUE
                """
            )
            rules = cur.fetchall()
            if not rules:
                return result

            full_context = _build_context(cur)

            for rule_id, rule_type, jdm_content, enabled in rules:
                result["evaluated"] += 1
                try:
                    content = jdm_content if isinstance(jdm_content, dict) else json.loads(jdm_content)
                    source_node_ids = set(
                        str(nid) for nid in content.get("_config", {}).get("sourceNodeIds", []) if nid
                    )
                    source_entity_ids = set(
                        str(eid) for eid in content.get("_config", {}).get("sourceEntityIds", []) if eid
                    )
                    context = full_context
                    if source_node_ids:
                        context = {
                            k: v for k, v in context.items()
                            if str(v.get("node_id")) in source_node_ids
                        }
                    if source_entity_ids:
                        context = {
                            k: v for k, v in context.items()
                            if str(v.get("entity_id")) in source_entity_ids
                        }

                    input_mappings = content.get("_config", {}).get("inputMappings", {}) or {}
                    eval_context = _apply_input_mappings(context, input_mappings)
                    eval_result = evaluate_rule(content, eval_context)

                    if eval_result.get("error"):
                        logger.warning(
                            "[RuleEngine] rule {} evaluation error ({}): {}",
                            rule_id,
                            eval_result.get("engine"),
                            eval_result["error"],
                        )

                    if not eval_result.get("triggered"):
                        continue

                    actions = eval_result.get("actions", [])
                    for action in actions:
                        a_type = action.get("type")
                        if a_type == "alarm":
                            level = action.get("level", "WARNING")
                            message = action.get("message", f"rule {rule_id} triggered")
                            target_node_id = action.get("node_id")
                            # 定位触发点位（简化格式：从 when 表达式提取第一个变量）
                            when_expr = content.get("when", "") if isinstance(content, dict) else ""
                            trigger_tag_name = _extract_first_varname(when_expr)
                            trigger_ctx = context.get(trigger_tag_name) if trigger_tag_name else None
                            trigger_tag_id = trigger_ctx.get("tag_id") if trigger_ctx else None
                            trigger_value = trigger_ctx.get("value") if trigger_ctx else None
                            effective_node_id = target_node_id
                            if not effective_node_id and trigger_ctx:
                                effective_node_id = trigger_ctx.get("node_id")
                            if not _has_active_alarm(cur, rule_id):
                                _create_alarm(
                                    cur,
                                    rule_id,
                                    effective_node_id,
                                    trigger_tag_id,
                                    trigger_tag_name,
                                    trigger_value,
                                    level,
                                    message,
                                )
                                result["alarms"] += 1
                        elif a_type in ("control", "neuron_write"):
                            if _control_cooldown_ok(rule_id, action.get("cooldown", 60)):
                                if _execute_control(cur, rule_id, action, context, eval_result.get("outputs")):
                                    result["controls"] += 1
                        else:
                            logger.debug("[RuleEngine] unsupported action type: {}", a_type)
                except Exception as e:
                    result["errors"] += 1
                    logger.warning("[RuleEngine] rule {} evaluation failed: {}", rule_id, e)

            conn.commit()

    if any(v for k, v in result.items() if k != "evaluated"):
        logger.debug("[RuleEngine] tick result: {}", result)
    return result
