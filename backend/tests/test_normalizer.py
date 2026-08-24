"""F0 Hook2 归一化器单元测试 (避开 pint 单位转换路径)。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.schemas import DataType, ParsedMessage, Quality
from app.services.normalizer import (
    TagNormalizationRule,
    _coerce_bool_or_str,
    _coerce_numeric,
    _infer_data_type,
    normalize,
)


def _parsed(tags: dict) -> ParsedMessage:
    return ParsedMessage(
        node_name="INV_01",
        timestamp_ms=1721223400000,
        event_time_basis="unknown",
        tags=tags,
    )


def test_scale_offset_formula():
    """工程值公式 value * scale_factor + offset。"""
    rule = TagNormalizationRule("power_kw", scale_factor=0.001, offset=2.0)
    out = normalize(_parsed({"power_kw": 45000}), {"power_kw": rule})
    assert len(out.points) == 1
    # 45000 * 0.001 + 2.0 = 47.0
    assert out.points[0].value == 47.0
    assert out.points[0].quality == Quality.GOOD


def test_range_out_of_bounds_uncertain():
    """超出 range_max → quality 降级为 UNCERTAIN。"""
    rule = TagNormalizationRule("temp", range_min=0.0, range_max=100.0)
    out = normalize(_parsed({"temp": 150.0}), {"temp": rule})
    assert out.points[0].quality == Quality.UNCERTAIN


def test_range_within_bounds_good():
    """区间内 → quality 保持 GOOD。"""
    rule = TagNormalizationRule("temp", range_min=0.0, range_max=100.0)
    out = normalize(_parsed({"temp": 50.0}), {"temp": rule})
    assert out.points[0].quality == Quality.GOOD


def test_auto_normalize_no_rule():
    """无规则时自动推断类型并保留原值。"""
    out = normalize(_parsed({"status": "normal", "running": True}), None)
    by_name = {p.tag_name: p for p in out.points}
    assert by_name["status"].data_type == DataType.STRING
    assert by_name["running"].data_type == DataType.BOOL


def test_coerce_numeric():
    """bool 返回 None; str 含点转 float 否则 int。"""
    assert _coerce_numeric(True) is None
    assert _coerce_numeric("3.14") == 3.14
    assert _coerce_numeric("42") == 42
    assert _coerce_numeric("abc") is None


def test_coerce_bool_or_str():
    assert _coerce_bool_or_str("on") is True
    assert _coerce_bool_or_str("off") is False
    assert _coerce_bool_or_str("normal") == "normal"


def test_infer_data_type():
    assert _infer_data_type(True) == DataType.BOOL
    assert _infer_data_type(1) == DataType.INT
    assert _infer_data_type(1.5) == DataType.FLOAT
    assert _infer_data_type("x") == DataType.STRING
