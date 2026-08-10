"""
GoRules zen-engine 适配器 (F2 规则引擎)

职责：
  - 统一封装 GoRules zen-engine 的规则求值。
  - 当 zen-engine 不可用时，fallback 到原有安全 AST 求值器，保证系统可运行。
  - 同时兼容两类 jdm_content：
    1) 简化格式：{"when": "...", "actions": [...]}
    2) 标准 GoRules JDM：{"nodes": [...], "edges": [...], "actions": [...]}

注意：
  - e606 容器内 zen-engine 0.53.0 的 Python 包名为 ``zen``，不是 ``zen_engine``。
  - 核心 API：``zen.evaluate_expression(expr, ctx)`` / ``zen.compile_expression(expr)``。
  - 标准 JDM 使用 ``zen.ZenEngine().create_decision(jdm).evaluate(ctx)``。
"""
from __future__ import annotations

import ast
import copy
import operator
from typing import Any

from loguru import logger

# -- 尝试加载 GoRules zen-engine --
try:
    import zen as _zen  # noqa: N812

    _ZEN_AVAILABLE = True
    logger.info("[GoRules] zen-engine {} loaded", getattr(_zen, "__version__", "unknown"))
except Exception as _zen_err:  # pragma: no cover - 未安装时的 fallback
    _zen = None  # type: ignore[assignment]
    _ZEN_AVAILABLE = False
    logger.warning("[GoRules] zen-engine not available, fallback to AST evaluator: {}", _zen_err)


# ================================================================
# Fallback：原有安全 AST 求值器
# ================================================================

_ALLOWED_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_COMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ALLOWED_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}


def _eval_node(node: ast.AST, ctx: dict) -> float | bool | int | str:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ctx)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool, str)):
            return node.value
        raise ValueError(f"unsupported constant type: {type(node.value)}")

    if isinstance(node, ast.Num):  # py<3.8
        return node.n

    if isinstance(node, ast.Name):
        if node.id not in ctx:
            raise ValueError(f"unknown variable: {node.id}")
        return ctx[node.id]

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_BIN_OPS:
            raise ValueError(f"disallowed binary operator: {op_type.__name__}")
        left = _eval_node(node.left, ctx)
        right = _eval_node(node.right, ctx)
        return _ALLOWED_BIN_OPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_UNARY_OPS:
            raise ValueError(f"disallowed unary operator: {op_type.__name__}")
        operand = _eval_node(node.operand, ctx)
        return _ALLOWED_UNARY_OPS[op_type](operand)

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx)
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise ValueError("chained comparisons not supported")
        op_type = type(node.ops[0])
        if op_type not in _ALLOWED_COMP_OPS:
            raise ValueError(f"disallowed comparison operator: {op_type.__name__}")
        right = _eval_node(node.comparators[0], ctx)
        return _ALLOWED_COMP_OPS[op_type](left, right)

    if isinstance(node, ast.BoolOp):
        op_type = type(node.op)
        if op_type is ast.And:
            return all(_eval_node(v, ctx) for v in node.values)
        if op_type is ast.Or:
            return any(_eval_node(v, ctx) for v in node.values)
        raise ValueError(f"disallowed bool operator: {op_type.__name__}")

    raise ValueError(f"unsupported AST node: {type(node).__name__}")


def _eval_condition_ast(condition: str, context: dict[str, Any]) -> bool:
    """安全 AST 求值（fallback 用）。"""
    tree = ast.parse(condition, mode="eval")
    return bool(_eval_node(tree.body, context))


# ================================================================
# GoRules 适配器
# ================================================================



# 决策表 cell 常见简写操作符（如 "> 30"）需要补全为 "inputId > 30"
_COMPARISON_SHORTHAND_PREFIXES = (">=", "<=", "==", "!=", ">", "<")


def _is_shorthand_expression_cell(cell: str, input_id: str) -> bool:
    if not isinstance(cell, str):
        return False
    s = cell.strip()
    if not s or s == "1 == 1":
        return False
    # 已经是完整表达式（包含 input_id）则不处理
    if input_id in s:
        return False
    return any(s.startswith(prefix) for prefix in _COMPARISON_SHORTHAND_PREFIXES)


def _normalize_jdm_content(jdm_content: dict) -> dict:
    """
    把 jdm-editor 生成的简写决策表 cell 补齐为 zen-engine 可求值的完整表达式。
    例如 expression 类型输入 id=pcs_temp 的 cell "> 30" -> "pcs_temp > 30"。
    """
    content = copy.deepcopy(jdm_content)

    def _normalize_inputs_rules(inputs: list, rules: list) -> None:
        expr_inputs = [
            inp for inp in inputs
            if inp.get("type") == "expression" and inp.get("id")
        ]
        for rule in rules:
            for inp in expr_inputs:
                inp_id = inp["id"]
                cell = rule.get(inp_id)
                if _is_shorthand_expression_cell(cell, inp_id):
                    rule[inp_id] = f"{inp_id} {cell.strip()}"

    if isinstance(content.get("inputs"), list):
        _normalize_inputs_rules(content.get("inputs", []), content.get("rules", []))
    elif "nodes" in content:
        for node in content.get("nodes", []):
            if node.get("type") in ("decisionTableNode", "decisionNode"):
                node_content = node.get("content", {})
                _normalize_inputs_rules(
                    node_content.get("inputs", []),
                    node_content.get("rules", []),
                )
    return content


def _is_standard_jdm(content: dict) -> bool:
    """判断是否为标准 GoRules JDM（含 nodes 字段或 inputs/rules 决策表）。"""
    if not isinstance(content, dict):
        return False
    if "nodes" in content and isinstance(content.get("nodes"), list):
        return True
    if "inputs" in content and "rules" in content and isinstance(content.get("inputs"), list):
        return True
    return False


def _extract_triggered(outputs: Any) -> bool:
    """从 zen-engine 输出中提取是否触发。"""
    if isinstance(outputs, dict):
        if outputs.get("triggered") is not None:
            return bool(outputs["triggered"])
        if outputs.get("result") is not None:
            return bool(outputs["result"])
        # 控制命令 / 动作标记也视为触发
        if outputs.get("command") or outputs.get("neuron_write") or outputs.get("action_type"):
            return True
        # 决策表输出可能包含 level/message 等字符串，只看布尔/数字字段
        return any(
            bool(v)
            for k, v in outputs.items()
            if isinstance(v, (bool, int, float)) or (isinstance(v, str) and v.lower() in ("true", "yes", "1"))
        )
    return bool(outputs)


def _extract_actions(outputs: Any, jdm_content: dict) -> list[dict]:
    """从 JDM 输出和规则配置中聚合待执行动作。"""
    actions: list[dict] = []

    # 1. 顶层显式 actions（向后兼容）
    actions.extend(list(jdm_content.get("actions", [])))

    # 2. 决策图/表输出里携带的动作字段
    if isinstance(outputs, dict):
        cmd = outputs.get("command") or outputs.get("neuron_write")
        if isinstance(cmd, dict) and cmd.get("node") and cmd.get("group") and cmd.get("tag"):
            actions.append({"type": "neuron_write", **cmd})

        if any(k.startswith("command.") for k in outputs):
            action: dict[str, Any] = {"type": "neuron_write"}
            for k, v in outputs.items():
                if k.startswith("command."):
                    action[k.split(".", 1)[1]] = v
            if action.get("node") and action.get("group") and action.get("tag"):
                actions.append(action)

        action_type = outputs.get("action_type")
        level = outputs.get("level")
        message = outputs.get("message")
        if action_type or level or message:
            actions.append({
                "type": action_type or "alarm",
                "level": level or "WARNING",
                "message": message or "rule triggered",
            })

    # 3. _config 中配置的动作（前端控制动作面板）
    for a in jdm_content.get("_config", {}).get("actions", []):
        actions.append(dict(a))

    return actions


def _evaluate_expression_zen(expression: str, context: dict[str, Any]) -> bool:
    """使用 zen-engine 求值布尔表达式。"""
    if _zen is None:
        raise RuntimeError("zen-engine not available")
    result = _zen.evaluate_expression(expression, context)
    return bool(result)


def _evaluate_jdm_zen(jdm_content: dict, context: dict[str, Any]) -> dict:
    """使用 zen-engine 评估标准 JDM 决策图/决策表。"""
    if _zen is None:
        raise RuntimeError("zen-engine not available")
    engine = _zen.ZenEngine()
    clean_content = _normalize_jdm_content(jdm_content)
    # 移除前端私有配置，并兼容旧版 jdm-editor 节点类型命名
    clean_content = {k: v for k, v in clean_content.items() if k not in ("_config", "actions")}
    if isinstance(clean_content.get("nodes"), list):
        for node in clean_content["nodes"]:
            node_type = node.get("type")
            if node_type == "startNode":
                node["type"] = "inputNode"
            elif node_type == "endNode":
                node["type"] = "outputNode"
            elif node_type == "decisionNode":
                node["type"] = "decisionTableNode"
    decision = engine.create_decision(clean_content)
    outputs = decision.evaluate(context)
    return outputs if isinstance(outputs, dict) else {"result": outputs}


def evaluate_rule(jdm_content: dict, context: dict[str, Any]) -> dict:
    """
    统一规则求值入口。

    Returns:
        {
            "triggered": bool,
            "actions": list[dict],
            "outputs": dict,
            "error": str | None,
            "engine": "zen" | "ast" | "error",
        }
    """
    if not isinstance(jdm_content, dict):
        return {
            "triggered": False,
            "actions": [],
            "outputs": {},
            "error": "jdm_content must be a dict",
            "engine": "error",
        }

    # -- 标准 GoRules JDM --
    if _is_standard_jdm(jdm_content):
        if not _ZEN_AVAILABLE:
            return {
                "triggered": False,
                "actions": [],
                "outputs": {},
                "error": "standard JDM requires zen-engine, which is not installed",
                "engine": "error",
            }
        try:
            # 标准 JDM 同样只需要 value 上下文
            if context and isinstance(context, dict) and any(isinstance(v, dict) and "value" in v for v in context.values()):
                jdm_ctx = {k: v["value"] for k, v in context.items() if isinstance(v, dict) and "value" in v}
            else:
                jdm_ctx = context
            normalized_content = _normalize_jdm_content(jdm_content)
            outputs = _evaluate_jdm_zen(normalized_content, jdm_ctx)
            triggered = _extract_triggered(outputs)
            if triggered:
                actions = _extract_actions(outputs, jdm_content)
            else:
                actions = []
            return {
                "triggered": triggered,
                "actions": actions,
                "outputs": outputs,
                "error": None,
                "engine": "zen",
            }
        except Exception as e:
            logger.warning("[GoRules] JDM evaluation failed: {}", e)
            return {
                "triggered": False,
                "actions": [],
                "outputs": {},
                "error": str(e),
                "engine": "error",
            }

    # -- 简化格式 {when, actions} --
    when = jdm_content.get("when")
    actions = jdm_content.get("actions", [])

    # 兼容两种 context：{tag: value} 和 {tag: {value, tag_id, node_id}}
    if context and isinstance(context, dict) and any(isinstance(v, dict) and "value" in v for v in context.values()):
        ctx_values = {k: v["value"] for k, v in context.items() if isinstance(v, dict) and "value" in v}
    else:
        ctx_values = context

    if not when:
        return {
            "triggered": False,
            "actions": [],
            "outputs": {},
            "error": None,
            "engine": "zen" if _ZEN_AVAILABLE else "ast",
        }

    engine_used = "zen" if _ZEN_AVAILABLE else "ast"

    try:
        if _ZEN_AVAILABLE:
            triggered = _evaluate_expression_zen(when, ctx_values)
        else:
            triggered = _eval_condition_ast(when, ctx_values)
    except Exception as e_zen:
        # zen 对中文变量名 / 带点实体名可能解析失败，fallback 到 AST 求值器
        try:
            triggered = _eval_condition_ast(when, ctx_values)
            engine_used = "ast"
        except Exception as e_ast:
            logger.warning("[GoRules] expression evaluation failed (zen: {}; ast: {})", e_zen, e_ast)
            return {
                "triggered": False,
                "actions": [],
                "outputs": {},
                "error": f"zen: {e_zen}; ast: {e_ast}",
                "engine": "error",
            }

    return {
        "triggered": bool(triggered),
        "actions": actions if triggered else [],
        "outputs": {"triggered": triggered},
        "error": None,
        "engine": engine_used,
    }
