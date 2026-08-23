"""Safe, strongly typed compiler and evaluator for point-processing formulas."""
from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import operator
from typing import Any

from pint import UnitRegistry

from app.services.data_trunk_contracts import CompiledFormula, FormulaSource, ValueKind


_UNITS = UnitRegistry()
_NUMERIC = {ValueKind.FLOAT, ValueKind.INT}
_BINOPS = {
    ast.Add: ("+", operator.add),
    ast.Sub: ("-", operator.sub),
    ast.Mult: ("*", operator.mul),
    ast.Div: ("/", operator.truediv),
}
_COMPARE = {
    ast.Eq: ("==", operator.eq),
    ast.NotEq: ("!=", operator.ne),
    ast.Lt: ("<", operator.lt),
    ast.LtE: ("<=", operator.le),
    ast.Gt: (">", operator.gt),
    ast.GtE: (">=", operator.ge),
}


class FormulaCompileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FormulaEvaluationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Type:
    kind: ValueKind
    unit: str | None
    cardinality: str = "one"


@dataclass(frozen=True)
class _Node:
    ast: dict[str, Any]
    type: _Type


def compile_formula(
    text: str,
    *,
    sources: Sequence[FormulaSource],
    result_type: ValueKind,
    result_unit: str | None,
) -> CompiledFormula:
    source_by_name = {source.name: source for source in sources}
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(source_by_name) != len(sources)
        or result_type not in {ValueKind.FLOAT, ValueKind.INT, ValueKind.BOOL}
    ):
        raise _invalid("Formula contract is invalid")
    try:
        parsed = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise _invalid("Formula syntax is invalid") from exc
    compiled = _compile(parsed.body, source_by_name)
    if compiled.type.cardinality != "one" or compiled.type.kind is not result_type:
        raise FormulaCompileError(
            "POINT_PROCESSING_TYPE_MISMATCH",
            "Formula result type does not match the declared output",
        )
    if not _same_unit(compiled.type.unit, result_unit):
        raise FormulaCompileError(
            "POINT_PROCESSING_UNIT_MISMATCH",
            "Formula result unit does not match the declared output",
        )
    canonical = json.dumps(
        compiled.ast,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return CompiledFormula(
        text=text.strip(),
        ast=compiled.ast,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        result_kind=result_type,
        result_unit=result_unit,
    )


def evaluate_compiled_formula(
    compiled: CompiledFormula,
    values: Mapping[str, float | int | bool | Sequence[float | int | bool]],
) -> float | int | bool:
    try:
        result = _evaluate(compiled.ast, values)
    except ZeroDivisionError as exc:
        raise FormulaEvaluationError("DIVIDE_BY_ZERO", "Formula divided by zero") from exc
    except OverflowError as exc:
        raise FormulaEvaluationError("OVERFLOW", "Formula overflowed") from exc
    except FormulaEvaluationError:
        raise
    except Exception as exc:
        raise FormulaEvaluationError(
            "POINT_PROCESSING_FORMULA_INVALID",
            "Formula could not be evaluated",
        ) from exc
    if isinstance(result, float) and not math.isfinite(result):
        raise FormulaEvaluationError("INVALID_NUMBER", "Formula produced a non-finite number")
    if compiled.result_kind is ValueKind.FLOAT:
        if not _is_number(result):
            raise FormulaEvaluationError("TYPE_MISMATCH", "Formula result is not numeric")
        return float(result)
    if compiled.result_kind is ValueKind.INT:
        if not isinstance(result, int) or isinstance(result, bool):
            raise FormulaEvaluationError("TYPE_MISMATCH", "Formula result is not an integer")
        return result
    if not isinstance(result, bool):
        raise FormulaEvaluationError("TYPE_MISMATCH", "Formula result is not boolean")
    return result


def _compile(node: ast.AST, sources: Mapping[str, FormulaSource]) -> _Node:
    if isinstance(node, ast.Name):
        source = sources.get(node.id)
        if source is None:
            raise _invalid("Formula references an undeclared input")
        return _Node(
            {"input": source.name},
            _Type(source.data_type, source.unit, source.cardinality),
        )
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return _Node({"const": node.value}, _Type(ValueKind.BOOL, None))
        if isinstance(node.value, int):
            return _Node({"const": node.value}, _Type(ValueKind.INT, None))
        if isinstance(node.value, float) and math.isfinite(node.value):
            return _Node({"const": node.value}, _Type(ValueKind.FLOAT, None))
        raise _invalid("Formula constant is unsupported")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _compile(node.left, sources), _compile(node.right, sources)
        _require_numeric(left, right)
        symbol = _BINOPS[type(node.op)][0]
        if symbol in {"+", "-"}:
            _require_same_unit(left.type.unit, right.type.unit)
            unit = left.type.unit
        elif symbol == "*":
            unit = _combine_units(left.type.unit, right.type.unit, operator.mul)
        else:
            unit = _combine_units(left.type.unit, right.type.unit, operator.truediv)
        kind = (
            ValueKind.FLOAT
            if symbol == "/" or ValueKind.FLOAT in {left.type.kind, right.type.kind}
            else ValueKind.INT
        )
        return _Node({"op": symbol, "args": [left.ast, right.ast]}, _Type(kind, unit))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
        operand = _compile(node.operand, sources)
        if isinstance(node.op, ast.Not):
            _require_bool(operand)
            return _Node({"unary": "not", "arg": operand.ast}, _Type(ValueKind.BOOL, None))
        _require_numeric(operand)
        return _Node(
            {"unary": "+" if isinstance(node.op, ast.UAdd) else "-", "arg": operand.ast},
            operand.type,
        )
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        values = [_compile(item, sources) for item in node.values]
        for item in values:
            _require_bool(item)
        return _Node(
            {"bool": "and" if isinstance(node.op, ast.And) else "or", "args": [item.ast for item in values]},
            _Type(ValueKind.BOOL, None),
        )
    if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
        left, right = _compile(node.left, sources), _compile(node.comparators[0], sources)
        entry = _COMPARE.get(type(node.ops[0]))
        if entry is None or left.type.cardinality != "one" or right.type.cardinality != "one":
            raise _invalid("Formula comparison is unsupported")
        if left.type.kind in _NUMERIC and right.type.kind in _NUMERIC:
            _require_same_unit(left.type.unit, right.type.unit)
        elif left.type.kind is not right.type.kind:
            raise FormulaCompileError("POINT_PROCESSING_TYPE_MISMATCH", "Comparison types differ")
        return _Node({"compare": entry[0], "args": [left.ast, right.ast]}, _Type(ValueKind.BOOL, None))
    if isinstance(node, ast.IfExp):
        condition, yes, no = _compile(node.test, sources), _compile(node.body, sources), _compile(node.orelse, sources)
        _require_bool(condition)
        if yes.type != no.type:
            raise FormulaCompileError("POINT_PROCESSING_TYPE_MISMATCH", "Conditional branch types differ")
        return _Node({"if": condition.ast, "then": yes.ast, "else": no.ast}, yes.type)
    if isinstance(node, ast.Call):
        return _compile_call(node, sources)
    raise _invalid(f"Formula node {type(node).__name__} is not allowed")


def _compile_call(node: ast.Call, sources: Mapping[str, FormulaSource]) -> _Node:
    if not isinstance(node.func, ast.Name) or node.keywords:
        raise _invalid("Only whitelisted function calls are allowed")
    name = node.func.id
    if name == "convert":
        if (
            len(node.args) != 2
            or not isinstance(node.args[1], ast.Constant)
            or not isinstance(node.args[1].value, str)
            or not node.args[1].value.strip()
        ):
            raise _invalid("convert requires a value and target unit")
        value = _compile(node.args[0], sources)
        _require_numeric(value)
        target = node.args[1].value.strip()
        annotated_constant = value.type.unit is None and "const" in value.ast
        if not annotated_constant and not _compatible_units(value.type.unit, target):
            raise FormulaCompileError("POINT_PROCESSING_UNIT_MISMATCH", "Units cannot be converted")
        return _Node(
            {
                "call": "convert",
                "args": [value.ast],
                "from_unit": target if annotated_constant else value.type.unit,
                "to_unit": target,
            },
            _Type(value.type.kind, target),
        )
    args = [_compile(item, sources) for item in node.args]
    if name in {"sum", "avg", "min_of", "max_of", "count"}:
        if len(args) != 1 or args[0].type.cardinality != "many":
            raise _invalid(f"{name} requires one collection input")
        if name == "count":
            return _Node({"call": name, "args": [args[0].ast]}, _Type(ValueKind.INT, None))
        if args[0].type.kind not in _NUMERIC:
            raise FormulaCompileError("POINT_PROCESSING_TYPE_MISMATCH", f"{name} requires numeric values")
        kind = ValueKind.FLOAT if name == "avg" or args[0].type.kind is ValueKind.FLOAT else ValueKind.INT
        return _Node({"call": name, "args": [args[0].ast]}, _Type(kind, args[0].type.unit))
    if name == "weighted_sum":
        if len(args) != 2 or any(item.type.cardinality != "many" for item in args):
            raise _invalid("weighted_sum requires value and weight collections")
        if args[0].type.kind not in _NUMERIC or args[1].type.kind not in _NUMERIC or args[1].type.unit is not None:
            raise FormulaCompileError("POINT_PROCESSING_TYPE_MISMATCH", "weighted_sum contracts are invalid")
        return _Node({"call": name, "args": [item.ast for item in args]}, _Type(ValueKind.FLOAT, args[0].type.unit))
    if name == "abs":
        if len(args) != 1:
            raise _invalid("abs requires one argument")
        _require_numeric(args[0])
        return _Node({"call": name, "args": [args[0].ast]}, args[0].type)
    if name in {"min", "max"}:
        if len(args) < 2:
            raise _invalid(f"{name} requires at least two arguments")
        _require_numeric(*args)
        for item in args[1:]:
            _require_same_unit(args[0].type.unit, item.type.unit)
        kind = ValueKind.FLOAT if any(item.type.kind is ValueKind.FLOAT for item in args) else ValueKind.INT
        return _Node({"call": name, "args": [item.ast for item in args]}, _Type(kind, args[0].type.unit))
    if name == "clamp":
        if len(args) != 3:
            raise _invalid("clamp requires value, minimum, and maximum")
        _require_numeric(*args)
        _require_same_unit(args[0].type.unit, args[1].type.unit)
        _require_same_unit(args[0].type.unit, args[2].type.unit)
        return _Node({"call": name, "args": [item.ast for item in args]}, args[0].type)
    if name == "if_else":
        if len(args) != 3:
            raise _invalid("if_else requires condition, true value, and false value")
        _require_bool(args[0])
        if args[1].type != args[2].type:
            raise FormulaCompileError("POINT_PROCESSING_TYPE_MISMATCH", "Conditional branch types differ")
        return _Node({"call": name, "args": [item.ast for item in args]}, args[1].type)
    raise _invalid("Formula function is not whitelisted")


def _evaluate(node: Mapping[str, Any], values: Mapping[str, Any]) -> Any:
    if "input" in node:
        if node["input"] not in values:
            raise FormulaEvaluationError("REQUIRED_INPUT_MISSING", "Formula input is missing")
        return values[node["input"]]
    if "const" in node:
        return node["const"]
    if "op" in node:
        left, right = (_evaluate(item, values) for item in node["args"])
        operation = next(value[1] for value in _BINOPS.values() if value[0] == node["op"])
        return operation(left, right)
    if "unary" in node:
        value = _evaluate(node["arg"], values)
        return {"+": operator.pos, "-": operator.neg, "not": operator.not_}[node["unary"]](value)
    if "bool" in node:
        evaluated = [_evaluate(item, values) for item in node["args"]]
        return all(evaluated) if node["bool"] == "and" else any(evaluated)
    if "compare" in node:
        left, right = (_evaluate(item, values) for item in node["args"])
        operation = next(value[1] for value in _COMPARE.values() if value[0] == node["compare"])
        return operation(left, right)
    if "if" in node:
        return _evaluate(node["then"] if _evaluate(node["if"], values) else node["else"], values)
    if "call" in node:
        name = node["call"]
        args = [_evaluate(item, values) for item in node["args"]]
        if name == "convert":
            source_unit = node["from_unit"] or _UNITS.dimensionless
            return _UNITS.Quantity(args[0], source_unit).to(node["to_unit"]).magnitude
        if name == "sum":
            return sum(args[0])
        if name == "avg":
            if not args[0]:
                raise FormulaEvaluationError("REQUIRED_INPUT_MISSING", "Average input is empty")
            return sum(args[0]) / len(args[0])
        if name in {"min_of", "max_of"}:
            if not args[0]:
                raise FormulaEvaluationError("REQUIRED_INPUT_MISSING", "Collection input is empty")
            return (min if name == "min_of" else max)(args[0])
        if name == "count":
            return len(args[0])
        if name == "weighted_sum":
            if len(args[0]) != len(args[1]):
                raise FormulaEvaluationError("TYPE_MISMATCH", "Weighted collections differ in size")
            return sum(value * weight for value, weight in zip(args[0], args[1], strict=True))
        if name == "abs":
            return abs(args[0])
        if name == "min":
            return min(args)
        if name == "max":
            return max(args)
        if name == "clamp":
            return min(max(args[0], args[1]), args[2])
        if name == "if_else":
            return args[1] if args[0] else args[2]
    raise FormulaEvaluationError("POINT_PROCESSING_FORMULA_INVALID", "Canonical formula AST is invalid")


def _invalid(message: str) -> FormulaCompileError:
    return FormulaCompileError("POINT_PROCESSING_FORMULA_INVALID", message)


def _require_numeric(*nodes: _Node) -> None:
    if any(node.type.cardinality != "one" or node.type.kind not in _NUMERIC for node in nodes):
        raise FormulaCompileError("POINT_PROCESSING_TYPE_MISMATCH", "Formula requires scalar numeric values")


def _require_bool(node: _Node) -> None:
    if node.type.cardinality != "one" or node.type.kind is not ValueKind.BOOL:
        raise FormulaCompileError("POINT_PROCESSING_TYPE_MISMATCH", "Formula requires a boolean value")


def _same_unit(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return _UNITS.Unit(left) == _UNITS.Unit(right)
    except Exception:
        return False


def _require_same_unit(left: str | None, right: str | None) -> None:
    if not _same_unit(left, right):
        raise FormulaCompileError("POINT_PROCESSING_UNIT_MISMATCH", "Formula units differ; use convert explicitly")


def _compatible_units(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    try:
        _UNITS.Quantity(1, left).to(right)
        return True
    except Exception:
        return False


def _combine_units(left: str | None, right: str | None, operation) -> str | None:
    left_unit = _UNITS.Unit(left) if left else _UNITS.dimensionless
    right_unit = _UNITS.Unit(right) if right else _UNITS.dimensionless
    combined = operation(left_unit, right_unit)
    return None if combined.dimensionless else str(combined)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
