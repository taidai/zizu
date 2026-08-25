"""
F2 规则引擎 — 告警/控制/联动策略

职责：
  - 每 tick 扫描启用的 t_rules
  - 从 t_telemetry_latest 构建上下文（tag_name -> value）
  - 对每条规则通过 GoRules zen-engine 求值
  - 触发动作：
      alarm     -> 提交统一告警观测，由 AlarmRuntime 维护事件生命周期
      control   -> 创建统一控制命令，由命令运行时完成下发与回读
      linkage   -> 更新指定虚拟点位的 sources 或触发另一规则（MVP 未实现）

求值层：
  - 优先使用 GoRules zen-engine（import zen）
  - zen-engine 不可用时自动 fallback 到安全 AST 求值器
  - 兼容简化格式 {when, actions} 和标准 JDM {nodes, edges, actions}
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from uuid import UUID

from loguru import logger

from app.services.gorules_adapter import evaluate_rule


def _entity_instance_context(instance_ids: set[str]) -> dict[str, dict[str, any]]:
    """通过 Registry.resolve 读取已确认来源，再从该来源取新鲜 GOOD 观测。"""
    if not instance_ids:
        return {}
    from app.api.entity_instances import get_entity_instance_runtime
    from app.services.entity_instance_registry import EntityInstanceError

    runtime = get_entity_instance_runtime()
    context: dict[str, dict[str, any]] = {}
    for raw_id in sorted(instance_ids):
        try:
            entity_instance_id = UUID(raw_id)
            observation = runtime.read_for_alarm(entity_instance_id)
        except (ValueError, EntityInstanceError) as exc:
            logger.warning("[RuleEngine] entity instance {} is unavailable: {}", raw_id, exc)
            continue
        context[raw_id] = {
            "value": observation.value,
            "entity_instance_id": observation.entity_instance_id,
            "observed_at": observation.observed_at.isoformat(),
            "quality": observation.quality,
            "fresh": observation.fresh,
            "max_observation_gap_seconds": observation.max_observation_gap_seconds,
            "is_entity_instance": True,
            **observation.source_evidence(),
        }
    return context


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


def _rule_context(cur, content: dict) -> dict[str, dict[str, any]]:
    config = content.get("_config", {})
    if not isinstance(config, dict):
        config = {}
    config_actions = config.get("actions", []) if isinstance(config, dict) else []
    top_level_actions = content.get("actions", [])
    if not isinstance(top_level_actions, list):
        top_level_actions = []
    has_declarative_control = any(
        isinstance(action, dict) and action.get("type") == "control"
        for action in [*config_actions, *top_level_actions]
    )
    source_node_ids = {
        str(item)
        for item in config.get("sourceNodeIds", [])
        if item
    }
    source_entity_instance_ids = {
        str(item)
        for item in config.get("sourceEntityInstanceIds", [])
        if item
    }
    raw_input_ids = {
        str(item)
        for item in config.get("inputMappings", {}).values()
        if item
    }
    input_entity_instance_ids = set()
    for raw_id in raw_input_ids:
        try:
            UUID(raw_id)
        except ValueError:
            continue
        input_entity_instance_ids.add(raw_id)
    legacy_entity_ids = {
        str(item)
        for item in config.get("sourceEntityIds", [])
        if item
    }
    requested_instances = source_entity_instance_ids | input_entity_instance_ids
    if requested_instances:
        return _entity_instance_context(requested_instances)
    if has_declarative_control:
        # New automatic control has an instance-only input contract.  It must
        # never fall back to tag/node context simply because configuration is
        # incomplete; persisted legacy content remains read-only compatible.
        return {}
    context = _build_context(cur, source_node_ids or None)
    if source_node_ids:
        context = {
            key: value for key, value in context.items()
            if str(value.get("node_id")) in source_node_ids
        }
    if legacy_entity_ids:
        return {
            key: value for key, value in context.items()
            if str(value.get("entity_id")) in legacy_entity_ids
        }
    return context


_LEGACY_CONTROL_FIELDS = frozenset({
    "node", "group", "tag", "topic", "payload", "command",
    "entity_id", "entity", "entity_name", "cooldown",
})


def _is_legacy_control_action(action: dict) -> bool:
    return bool(_LEGACY_CONTROL_FIELDS.intersection(action))


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


def _coerce_control_value(value: Any) -> Any:
    """把模板产生的字符串数字还原为配置控制值。"""
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


def _execute_control(
    rule_id: UUID,
    rule_version: int,
    action: dict,
    action_index: int,
    context: dict,
    outputs: dict | None = None,
) -> bool:
    """Create one automatic command; rules never know device addresses."""
    from app.api.control_commands import get_automated_control_commands
    from app.services.automated_control_commands import AutomatedControlCommandRequest

    try:
        entity_instance_id = UUID(str(action["entity_instance_id"]))
        value = _coerce_control_value(
            _resolve_value(action["value"], outputs, _context_values(context))
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("[RuleEngine] invalid declarative control action for rule {}: {}", rule_id, exc)
        return False
    action_key = action.get("id")
    if not isinstance(action_key, str) or not action_key.strip():
        logger.warning("[RuleEngine] control action without stable id for rule {}", rule_id)
        return False
    evidence = {
        "inputs": [
            {
                "field": field,
                "entity_instance_id": str(item["entity_instance_id"]),
                "value": item.get("value"),
                "observed_at": item.get("observed_at"),
                "quality": item.get("quality"),
                "fresh": item.get("fresh"),
                "max_observation_gap_seconds": item.get("max_observation_gap_seconds"),
            }
            for field, item in sorted(context.items())
            if item.get("is_entity_instance")
        ],
        "outputs": outputs or {},
    }
    command = get_automated_control_commands().submit(
        AutomatedControlCommandRequest(
            source_type="rule",
            subject_id=rule_id,
            subject_version=rule_version,
            action_key=action_key.strip(),
            entity_instance_id=entity_instance_id,
            value=value,
            trigger_evidence=evidence,
        )
    )
    if command.status == "rejected":
        logger.warning(
            "[RuleEngine] control command rejected for rule {}: {}",
            rule_id,
            command.code,
        )
        return False
    return True


_rule_alarm_adapter = None


def _default_rule_alarm_adapter():
    global _rule_alarm_adapter
    if _rule_alarm_adapter is None:
        from app.services.rule_alarm_adapter import build_postgres_rule_alarm_adapter

        _rule_alarm_adapter = build_postgres_rule_alarm_adapter()
    return _rule_alarm_adapter


def _execute_alarm(
    rule_id: UUID,
    rule_version: int,
    action: dict,
    context: dict,
    outputs: dict | None = None,
) -> bool:
    """Submit one configured rule observation; the rule never writes an alarm state."""
    from app.services.rule_alarm_adapter import RuleAlarmObservation

    try:
        entity_instance_id = UUID(str(action["entity_instance_id"]))
        action_id = str(action["id"]).strip()
        alarm_definition = str(action["alarm_definition"]).strip()
        if not action_id or not alarm_definition:
            raise ValueError("missing stable rule alarm reference")
        value = _resolve_value(action["value"], outputs, _context_values(context))
        evidence_inputs = [
            {
                "field": field,
                "entity_instance_id": str(item["entity_instance_id"]),
                "value": item.get("value"),
                "observed_at": item.get("observed_at"),
                "quality": item.get("quality"),
                "fresh": item.get("fresh"),
                "max_observation_gap_seconds": item.get("max_observation_gap_seconds"),
            }
            for field, item in sorted(context.items())
            if item.get("is_entity_instance")
        ]
        observations = [
            datetime.fromisoformat(item["observed_at"])
            for item in evidence_inputs
            if isinstance(item.get("observed_at"), str)
        ]
        qualities = [
            int(item["quality"])
            for item in evidence_inputs
            if item.get("quality") is not None
        ]
        if not observations or not qualities:
            raise ValueError("missing source observation timestamp or quality")
        # A rule may combine inputs.  The oldest sample and worst quality are
        # conservative lifecycle inputs: a delayed tick cannot fabricate a
        # continuous trigger/recovery interval from its execution time.
        observed_at = min(observations)
        quality = min(qualities) if all(item.get("fresh") is not False for item in evidence_inputs) else 0
        observation_gaps = [
            float(item["max_observation_gap_seconds"])
            for item in evidence_inputs
            if item.get("max_observation_gap_seconds") is not None
        ]
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("[RuleEngine] invalid declarative alarm action for rule {}: {}", rule_id, exc)
        return False
    outcomes = _default_rule_alarm_adapter().submit(
        RuleAlarmObservation(
            rule_id=rule_id,
            rule_version=rule_version,
            action_id=action_id,
            alarm_definition=alarm_definition,
            entity_instance_id=entity_instance_id,
            observed_at=observed_at,
            value=value,
            quality=quality,
            max_observation_gap_seconds=min(observation_gaps) if observation_gaps else None,
            evidence={
                "inputs": evidence_inputs,
                "outputs": _safe_alarm_evidence(outputs or {}),
            },
        )
    )
    return any(outcome.code == "ALARM_ACTIVATED" for outcome in outcomes)


def _safe_alarm_evidence(value: object) -> object:
    """Preserve decision proof without leaking physical routing details."""
    from app.services.automated_control_commands import _safe_trigger_evidence

    return _safe_trigger_evidence(value)



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




def _extract_jdm_inputs(content: dict) -> list[dict]:
    """从简化决策表或决策图节点中提取 inputs 定义。"""
    inputs = []
    if isinstance(content.get("inputs"), list):
        inputs = content["inputs"]
    elif "nodes" in content:
        for node in content.get("nodes", []):
            if node.get("type") in ("decisionTableNode", "decisionNode"):
                inputs = node.get("content", {}).get("inputs", [])
                break
    return inputs


def _build_eval_context(context: dict, content: dict) -> dict:
    """
    构造规则求值上下文。

    先应用 _config.inputMappings；再按 JDM inputs 定义自动补齐：
      - input.id 直接匹配
      - input.field 匹配（支持 tag.xxx / entity.xxx 前缀回退到 xxx）
    这样前端用 tag.temp / entity.pcs.activePower 作为字段时，
    能与真实 telemetry 上下文（键为 tag 名或实体名）自动对上。
    """
    input_mappings = content.get("_config", {}).get("inputMappings", {}) or {}
    eval_ctx = _apply_input_mappings(context, input_mappings)

    for inp in _extract_jdm_inputs(content):
        inp_id = inp.get("id")
        if not inp_id or inp_id in eval_ctx:
            continue
        field = inp.get("field") or inp_id
        candidates = [field]
        if field.startswith("tag."):
            candidates.append(field[4:])
        elif field.startswith("entity."):
            candidates.append(field[7:])
        for c in candidates:
            if c in context:
                eval_ctx[inp_id] = context[c]
                break
    return eval_ctx


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
                SELECT id, rule_type, jdm_content, enabled, version
                FROM t_rules
                WHERE enabled = TRUE
                """
            )
            rules = cur.fetchall()
            if not rules:
                return result

            for rule_id, rule_type, jdm_content, enabled, rule_version in rules:
                result["evaluated"] += 1
                try:
                    content = jdm_content if isinstance(jdm_content, dict) else json.loads(jdm_content)
                    context = _rule_context(cur, content)

                    eval_context = _build_eval_context(context, content)
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
                    for action_index, action in enumerate(actions):
                        a_type = action.get("type")
                        if a_type == "alarm":
                            if _execute_alarm(
                                rule_id,
                                rule_version,
                                action,
                                eval_context,
                                eval_result.get("outputs"),
                            ):
                                result["alarms"] += 1
                        elif a_type == "control":
                            if _is_legacy_control_action(action):
                                logger.warning(
                                    "[RuleEngine] legacy physical control action skipped for rule {}; migrate to entity_instance_id",
                                    rule_id,
                                )
                                continue
                            if _execute_control(
                                rule_id,
                                rule_version,
                                action,
                                action_index,
                                context,
                                eval_result.get("outputs"),
                            ):
                                result["controls"] += 1
                        elif a_type == "neuron_write":
                            logger.warning(
                                "[RuleEngine] legacy neuron_write action skipped for rule {}; migrate to entity_instance_id",
                                rule_id,
                            )
                        else:
                            logger.debug("[RuleEngine] unsupported action type: {}", a_type)
                except Exception as e:
                    result["errors"] += 1
                    logger.warning("[RuleEngine] rule {} evaluation failed: {}", rule_id, e)

            conn.commit()

    if any(v for k, v in result.items() if k != "evaluated"):
        logger.debug("[RuleEngine] tick result: {}", result)
    return result

def dry_run_rule(rule_id: str) -> dict:
    """对单条规则使用当前真实 telemetry 做试运行，不执行任何动作。"""
    from app.services.telemetry_store import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, rule_type, jdm_content, enabled FROM t_rules WHERE id = %s""",
                (UUID(rule_id),),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Rule not found: {rule_id}")
            rid, rule_type, jdm_content, enabled = row
            content = jdm_content if isinstance(jdm_content, dict) else json.loads(jdm_content)

            context = _rule_context(cur, content)

            eval_context = _build_eval_context(context, content)
            eval_result = evaluate_rule(content, eval_context)

            return {
                "rule_id": str(rid),
                "rule_type": rule_type,
                "enabled": enabled,
                "triggered": bool(eval_result.get("triggered")),
                "actions": eval_result.get("actions", []),
                "outputs": eval_result.get("outputs", {}),
                "error": eval_result.get("error"),
                "engine": eval_result.get("engine"),
                "context_keys": list(context.keys()),
                "eval_context_keys": list(eval_context.keys()),
                "eval_context_values": {k: v.get("value") if isinstance(v, dict) and "value" in v else v for k, v in eval_context.items()},
            }

