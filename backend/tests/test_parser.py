"""F0 Hook1 Neuron JSON 解析器单元测试。"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from app.models.schemas import RawMessage
from app.services.parser import parse_neuron_json


def _raw(body, topic="telemetry/HuaweiInverter_01") -> RawMessage:
    return RawMessage(topic=topic, payload=json.dumps(body).encode("utf-8"))


def test_parse_standard_format():
    """标准格式: node_name + timestamp + tags 全部提取正确。"""
    msg = parse_neuron_json(
        _raw({
            "node_name": "INV_01",
            "timestamp": 1721223400000,
            "tags": {"activePower": 45200, "running": True},
        })
    )
    assert msg is not None
    assert msg.node_name == "INV_01"
    assert msg.timestamp_ms == 1721223400000
    assert msg.tags == {"activePower": 45200, "running": True}


def test_parse_invalid_json_returns_none():
    """非法 JSON payload → None，不抛异常。"""
    assert parse_neuron_json(RawMessage(topic="t/x", payload=b"{not json")) is None


def test_parse_non_dict_body_returns_none():
    """body 是 list 而非 dict → None。"""
    assert parse_neuron_json(_raw([1, 2, 3])) is None


def test_parse_no_tags_returns_none():
    """无任何 tags → None。"""
    assert parse_neuron_json(_raw({"node_name": "INV_01", "timestamp": 1})) is None


def test_parse_node_name_fallback_from_topic():
    """body 缺 node_name 时从 topic 最后一段回退。"""
    msg = parse_neuron_json(
        _raw({"tags": {"p": 1}}, topic="telemetry/site/DEV_09")
    )
    assert msg is not None
    assert msg.node_name == "DEV_09"


def test_parse_timestamp_seconds_to_ms():
    """秒级时间戳自动 *1000 转毫秒。"""
    msg = parse_neuron_json(
        _raw({"node_name": "X", "ts": 1721223400, "tags": {"p": 1}})
    )
    assert msg is not None
    assert msg.timestamp_ms == 1721223400000


def test_parse_records_explicit_event_time_basis_without_time_comparison():
    received_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    explicit = parse_neuron_json(
        RawMessage(
            topic="telemetry/X",
            payload=json.dumps(
                {"node_name": "X", "timestamp": 2524608000000, "tags": {"p": 1}}
            ).encode("utf-8"),
            timestamp_recv=received_at,
        )
    )
    fallback = parse_neuron_json(
        RawMessage(
            topic="telemetry/X",
            payload=json.dumps({"node_name": "X", "tags": {"p": 1}}).encode(
                "utf-8"
            ),
            timestamp_recv=received_at,
        )
    )

    assert explicit is not None
    assert explicit.timestamp_ms == 2524608000000
    assert explicit.event_time_basis == "observed_at"
    assert fallback is not None
    assert fallback.timestamp_ms == 1787572800000
    assert fallback.event_time_basis == "received_at"
