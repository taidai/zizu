"""Isolated Neuron and MQTT fixture for the Node Management browser acceptance."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

import paho.mqtt.client as mqtt


REQUIRED_ROOT = "E2E验证"
POINT_PROCESSING_DEVICE_CATEGORY = "E2E_DEVICE"
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class FixtureNames:
    platform_node: str
    neuron_node: str
    neuron_group: str
    neuron_tag: str = "e2e_active_power"
    bit_tag: str = "e2e_fault_bit"


def build_resource_names(root: str, run_id: str) -> FixtureNames:
    if root != REQUIRED_ROOT:
        raise ValueError(f"write root must be exactly {REQUIRED_ROOT}")
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("run id may contain only ASCII letters, digits, underscore or hyphen")
    neuron_run_id = run_id.replace("-", "_")
    return FixtureNames(
        platform_node=f"{root}-设备-{run_id}",
        neuron_node=f"zizu_e2e_{neuron_run_id}",
        # Neuron 2.10 rejects long/uppercase group identifiers with error 1002.
        # A group is scoped by its unique fixture node, so a short fixed name is safe.
        neuron_group="e2e_data",
    )


def build_telemetry_payload(
    names: FixtureNames,
    *,
    value: int | float | str | bool,
    point_key: str | None = None,
    timestamp_ms: int | None = None,
) -> tuple[str, dict[str, Any]]:
    if not names.neuron_node.startswith("zizu_e2e_"):
        raise ValueError("refusing to publish a non-E2E Neuron source")
    observed_at = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    selected = names.neuron_tag if point_key is None else point_key
    allowed = {item["name"] for item in build_neuron_tags(names)}
    if selected not in allowed:
        raise ValueError("refusing to publish a non-E2E point")
    tags: dict[str, int | float | str | bool] = {
        names.neuron_tag: 0.0,
        names.bit_tag: 0,
    }
    tags.update({f"e2e_spare_{index:03d}": 0.0 for index in range(1, 51)})
    tags[selected] = value
    return (
        f"/neuron/{names.neuron_node}/telemetry",
        {
            "node_name": names.neuron_node,
            "group": names.neuron_group,
            "timestamp": observed_at,
            "tags": tags,
        },
    )


def build_neuron_tags(names: FixtureNames) -> list[dict[str, Any]]:
    tags = [
        {
            "name": names.neuron_tag,
            "address": "1!400001",
            "attribute": 1,
            "type": 9,
        },
        {
            "name": names.bit_tag,
            "address": "1!400002",
            "attribute": 1,
            "type": 11,
        },
    ]
    tags.extend(
        {
            "name": f"e2e_spare_{index:03d}",
            "address": f"1!{400002 + index}",
            "attribute": 1,
            "type": 9,
        }
        for index in range(1, 51)
    )
    return tags


def _environment_names() -> FixtureNames:
    return build_resource_names(
        os.environ.get("ZIZU_E2E_WRITE_ROOT", ""),
        os.environ.get("ZIZU_E2E_RUN_ID", ""),
    )


def _base_url() -> str:
    raw = os.environ.get("ZIZU_E2E_BASE_URL", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("ZIZU_E2E_BASE_URL must be an explicit HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("ZIZU_E2E_BASE_URL must not contain credentials, query or fragment")
    return raw


def _mqtt_host() -> str:
    return (
        os.environ.get("ZIZU_E2E_MQTT_HOST", "").strip()
        or os.environ.get("ZIZU_E2E_SSH_HOST", "").strip()
        or urllib.parse.urlsplit(_base_url()).hostname
        or ""
    )


def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: Any | None = None,
) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{_base_url()}/api/v1{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ZiZu API {method} {path} failed with HTTP {error.code}: {detail}") from error


def _token() -> str:
    username = os.environ.get("ZIZU_E2E_USERNAME", "").strip()
    password = os.environ.get("ZIZU_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("ZIZU_E2E_USERNAME and ZIZU_E2E_PASSWORD are required")
    response = _request(
        "POST",
        "/auth/login",
        body={"username": username, "password": password},
    )
    token = str(response.get("access_token", "")).strip()
    if not token:
        raise RuntimeError("ZiZu login response did not contain an access token")
    return token


def preflight() -> dict[str, Any]:
    token = _token()
    health = _request("GET", "/health", token=token)
    neuron = _request("GET", "/neuron/nodes", token=token)
    if neuron.get("error"):
        raise RuntimeError(f"Neuron catalog unavailable: {neuron['error']}")
    return {
        "status": "ready",
        "pipeline": health.get("pipeline", {}).get("status"),
        "neuron_nodes": neuron.get("total", 0),
    }


def setup() -> dict[str, Any]:
    names = _environment_names()
    _token()
    # The remote operation is idempotent and also completes a prior partial setup
    # (node exists but its group or tags do not).
    _setup_neuron_via_ssh(names)
    return {"status": "ready", "resources": asdict(names)}


def _safe_dispatch_jdm(target: int | float) -> dict[str, Any]:
    rows = []
    for key, start, end, action in (
        ("charge-1", 0, 360, "CHARGE"),
        ("discharge-1", 600, 720, "DISCHARGE"),
        ("charge-2", 720, 840, "CHARGE"),
        ("discharge-2", 1080, 1320, "DISCHARGE"),
    ):
        rows.append({
            "_id": key,
            "site_local_minute": f"site_local_minute >= {start} && site_local_minute < {end}",
            "soc": "soc >= 0 && soc <= 100",
            "action_id": json.dumps("power-target"),
            "target": target,
            "matched_rule": json.dumps(key),
            "_description": action,
        })
    rows.append({
        "_id": "other-time",
        "site_local_minute": "1 == 1",
        "soc": "1 == 1",
        "action_id": json.dumps("power-target"),
        "target": target,
        "matched_rule": json.dumps("other-time"),
        "_description": "HOLD",
    })
    return {
        "nodes": [
            {"id": "input", "type": "inputNode", "name": "Input"},
            {
                "id": "schedule",
                "type": "decisionTableNode",
                "name": "2充2放",
                "content": {
                    "hitPolicy": "first",
                    "inputs": [
                        {"id": "site_local_minute", "name": "场站本地分钟", "type": "expression", "field": "site_local_minute"},
                        {"id": "soc", "name": "SOC", "type": "expression", "field": "soc"},
                    ],
                    "outputs": [
                        {"id": "action_id", "name": "动作标识", "type": "expression", "field": "action_id"},
                        {"id": "target", "name": "功率目标", "type": "expression", "field": "target"},
                        {"id": "matched_rule", "name": "命中行", "type": "expression", "field": "matched_rule"},
                    ],
                    "rules": rows,
                },
            },
            {"id": "output", "type": "outputNode", "name": "Output"},
        ],
        "edges": [
            {"id": "input-schedule", "sourceId": "input", "targetId": "schedule", "type": "edge"},
            {"id": "schedule-output", "sourceId": "schedule", "targetId": "output", "type": "edge"},
        ],
    }


def ensure_strategy() -> dict[str, Any]:
    names = _environment_names()
    token = _token()
    strategy_name = f"E2E调度-{os.environ['ZIZU_E2E_RUN_ID']}"
    strategies = _request("GET", "/dispatch-strategies", token=token).get("strategies", [])
    existing = next((item for item in strategies if item.get("name") == strategy_name), None)
    if existing is not None:
        if existing.get("enabled"):
            _request("POST", f"/dispatch-strategies/{existing['id']}/disable", token=token)
        return {"status": "existing", "strategy_id": str(existing["id"]), "strategy_name": strategy_name}

    edited_node_name = f"{names.platform_node}-已编辑"
    entities = _request("GET", "/entity-instances", token=token).get("items", [])
    inputs = [
        entity for entity in entities
        if entity.get("node_display_name") == edited_node_name
        and str(entity.get("data_type", "")).upper() in {"FLOAT", "INT"}
        and entity.get("direction") in {"R", "RW"}
        and entity.get("confirmed") is True
    ]
    if len(inputs) != 1:
        raise RuntimeError(
            f"expected exactly one numeric E2E input for {edited_node_name}, found {len(inputs)}"
        )
    output_id = os.environ.get("ZIZU_E2E_STRATEGY_OUTPUT_ID", "").strip()
    outputs = [entity for entity in entities if str(entity.get("id")) == output_id]
    if len(outputs) != 1 or outputs[0].get("direction") not in {"W", "RW"} or outputs[0].get("confirmed") is not True:
        raise RuntimeError("ZIZU_E2E_STRATEGY_OUTPUT_ID must name one confirmed controllable L2 entity")
    output_realtime = _request("GET", f"/entity-instances/{output_id}/realtime", token=token)
    if output_realtime.get("fresh") is not True or output_realtime.get("quality_good") is not True:
        raise RuntimeError("strategy fixture output must have fresh GOOD readback")
    target = output_realtime.get("value")
    if isinstance(target, bool) or not isinstance(target, (int, float)):
        raise RuntimeError("strategy fixture output readback must be numeric")
    created = _request(
        "POST",
        "/dispatch-strategies",
        token=token,
        body={"name": strategy_name, "starter": "two_charge_two_discharge"},
    )
    strategy_id = str(created["id"])
    draft = created.get("draft") or {}
    input_entity = inputs[0]
    output_entity = outputs[0]
    saved = _request(
        "PUT",
        f"/dispatch-strategies/{strategy_id}/draft",
        token=token,
        body={
            "expected_digest": draft["content_digest"],
            "name": strategy_name,
            "trigger_kind": "FIXED_TICK",
            "site_timezone": "Asia/Shanghai",
            "base_configuration_revision": draft["base_configuration_revision"],
            "jdm_content": _safe_dispatch_jdm(target),
            "bindings": [
                {
                    "direction": "INPUT",
                    "binding_key": "soc",
                    "ordinal": 0,
                    "entity_instance_id": str(input_entity["id"]),
                    "expected_data_type": str(input_entity["data_type"]).upper(),
                    "unit": input_entity.get("unit"),
                    "freshness_seconds": input_entity["freshness_seconds"],
                },
                {
                    "direction": "OUTPUT",
                    "binding_key": "power-target",
                    "ordinal": 0,
                    "entity_instance_id": output_id,
                    "expected_data_type": str(output_entity["data_type"]).upper(),
                    "unit": output_entity.get("unit"),
                    "freshness_seconds": output_entity["freshness_seconds"],
                },
            ],
        },
    )
    saved_draft = saved["draft"]
    simulation = _request(
        "POST",
        f"/dispatch-strategies/{strategy_id}/simulate",
        token=token,
        body={"revision_id": saved_draft["id"], "overrides": {"soc": 50}},
    )
    if simulation.get("status") != "EVALUATED" or simulation.get("proposed_intents") != []:
        raise RuntimeError("strategy fixture refuses enable unless simulation is a no-op")
    published = _request(
        "POST",
        f"/dispatch-strategies/{strategy_id}/publish",
        token=token,
        body={
            "expected_digest": saved_draft["content_digest"],
            "configuration_revision": saved_draft["base_configuration_revision"],
        },
    )
    _request(
        "POST",
        f"/dispatch-strategies/{strategy_id}/enable",
        token=token,
        body={"revision_id": published["id"]},
    )
    _request("POST", f"/dispatch-strategies/{strategy_id}/disable", token=token)
    return {"status": "verified", "strategy_id": strategy_id, "strategy_name": strategy_name}


def publish(point_key: str, value: int | float | str | bool) -> dict[str, Any]:
    names = _environment_names()
    topic, payload = build_telemetry_payload(names, point_key=point_key, value=value)
    if all(
        os.environ.get(key, "").strip()
        for key in (
            "ZIZU_E2E_SSH_HOST",
            "ZIZU_E2E_SSH_USER",
            "ZIZU_E2E_SSH_PASSWORD",
        )
    ):
        _publish_via_ssh(topic, payload)
        return {
            "status": "published",
            "topic": topic,
            "point_key": point_key,
            "value": value,
            "transport": "ssh",
        }
    host = _mqtt_host()
    if not host:
        raise RuntimeError("ZIZU_E2E_MQTT_HOST is required")
    port = int(os.environ.get("ZIZU_E2E_MQTT_PORT", "1883"))
    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"zizu-node-e2e-{names.neuron_group}"[:120],
        )
        mqtt_username = os.environ.get("ZIZU_E2E_MQTT_USERNAME", "")
        if mqtt_username:
            client.username_pw_set(mqtt_username, os.environ.get("ZIZU_E2E_MQTT_PASSWORD", ""))
        client.connect(host, port, keepalive=30)
        client.loop_start()
        try:
            receipt = client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
            receipt.wait_for_publish(timeout=10)
            if not receipt.is_published():
                raise RuntimeError("MQTT publish did not complete")
        finally:
            client.loop_stop()
            client.disconnect()
        transport = "mqtt"
    except OSError:
        _publish_via_ssh(topic, payload)
        transport = "ssh"
    return {
        "status": "published",
        "topic": topic,
        "point_key": point_key,
        "value": value,
        "transport": transport,
    }


def _publish_via_ssh(topic: str, payload: dict[str, Any]) -> None:
    """Publish inside the server because NanoMQ is intentionally not public."""
    remote_script = (
        "import json\n"
        "import time\n"
        "from paho.mqtt import publish\n"
        f"topic = {topic!r}\n"
        f"payload = json.loads({json.dumps(json.dumps(payload, ensure_ascii=False))})\n"
        "for _ in range(16):\n"
        "    payload['timestamp'] = int(time.time() * 1000)\n"
        "    publish.single(topic, json.dumps(payload, ensure_ascii=False), "
        "hostname='127.0.0.1', port=1883, qos=1)\n"
        "    time.sleep(0.25)\n"
    )
    _run_backend_script_via_ssh(remote_script)


def _setup_neuron_via_ssh(names: FixtureNames) -> None:
    desired_tags = json.dumps(build_neuron_tags(names), ensure_ascii=True)
    remote_script = f"""
import json
from app.services.neuron_client import get_neuron_client

client = get_neuron_client()
node = {names.neuron_node!r}
group = {names.neuron_group!r}
tag = {names.neuron_tag!r}
desired_tags = json.loads({desired_tags!r})
params = {{
    'connection_mode': 0,
    'host': '127.0.0.1',
    'port': 1,
    'timeout': 3000,
    'interval': 20,
}}
if node not in {{str(item.get('name', '')) for item in client.get_nodes()}}:
    client._request('POST', '/api/v2/node', json={{'name': node, 'plugin': 'Modbus TCP'}})
client._request('POST', '/api/v2/node/setting', json={{'node': node, 'params': params}})
if group not in {{str(item.get('name', '')) for item in client.get_groups(node)}}:
    client._request(
        'POST',
        '/api/v2/gtags',
        json={{
            'node': node,
            'groups': [{{'group': group, 'interval': 1000, 'tags': desired_tags}}],
        }},
    )
else:
    existing_tags = {{str(item.get('name', '')) for item in client.get_tags(node, group)}}
    missing_tags = [item for item in desired_tags if item['name'] not in existing_tags]
    if missing_tags:
        client.add_tags(node, group, missing_tags)
""".lstrip()
    _run_backend_script_via_ssh(remote_script)


def _cleanup_neuron_via_ssh(names: FixtureNames) -> None:
    remote_script = f"""
from app.services.neuron_client import get_neuron_client

client = get_neuron_client()
node = {names.neuron_node!r}
if node in {{str(item.get('name', '')) for item in client.get_nodes()}}:
    client._request('DELETE', '/api/v2/node', json={{'name': node}})
""".lstrip()
    _run_backend_script_via_ssh(remote_script)


def _run_backend_script_via_ssh(remote_script: str) -> None:
    try:
        import paramiko
    except ImportError as error:
        raise RuntimeError("private server access requires the existing Paramiko deployment dependency") from error

    host = os.environ.get("ZIZU_E2E_SSH_HOST", "").strip()
    if not host:
        host = urllib.parse.urlsplit(_base_url()).hostname or ""
    user = os.environ.get("ZIZU_E2E_SSH_USER", "").strip()
    password = os.environ.get("ZIZU_E2E_SSH_PASSWORD", "")
    sudo_password = os.environ.get("ZIZU_E2E_SUDO_PASSWORD", password)
    if not host or not user or not password:
        raise RuntimeError(
            "private server access requires ZIZU_E2E_SSH_USER and ZIZU_E2E_SSH_PASSWORD"
        )
    port = int(os.environ.get("ZIZU_E2E_SSH_PORT", "13122"))
    remote_command = (
        "sudo -S -p '' sh -c '"
        "container=$(docker ps --filter label=com.docker.compose.service=backend "
        "--format \"{{.Names}}\" | head -n 1); "
        "[ -n \"$container\" ] || exit 42; "
        "exec docker exec -i \"$container\" python -'"
    )
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            host,
            port=port,
            username=user,
            password=password,
            allow_agent=False,
            look_for_keys=False,
            timeout=15,
            banner_timeout=20,
            auth_timeout=20,
        )
        stdin, stdout, stderr = client.exec_command(remote_command, timeout=30)
        stdin.write(f"{sudo_password}\n")
        stdin.write(remote_script)
        stdin.flush()
        stdin.channel.shutdown_write()
        error_output = stderr.read().decode("utf-8", errors="replace").strip()
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise RuntimeError(
                f"remote backend fixture failed with exit {exit_status}: {error_output}"
            )
    finally:
        client.close()


def _retire_run_templates(token: str, run_id: str) -> int:
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("run id may contain only ASCII letters, digits, underscore or hyphen")
    asset_id = f"e2e.template.{run_id.replace('-', '_')}"
    items = _request(
        "GET",
        (
            "/point-processing-templates?device_category="
            f"{POINT_PROCESSING_DEVICE_CATEGORY}"
        ),
        token=token,
    ).get("items", [])
    matches = [item for item in items if item.get("asset_id") == asset_id]
    if not matches:
        return 0
    current = max(matches, key=lambda item: int(item.get("revision", 0)))
    revision_id = str(current.get("revision_id", "")).strip()
    if not revision_id:
        raise RuntimeError(f"E2E template {asset_id} has no revision id")
    content = _request(
        "GET",
        f"/point-processing-templates/{revision_id}/export",
        token=token,
    )
    revision = content.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise RuntimeError(f"E2E template {asset_id} has an invalid revision")
    if content.get("status") == "retired":
        return 0
    retired = dict(content)
    retired["revision"] = revision + 1
    retired["status"] = "retired"
    _request(
        "POST",
        "/point-processing-templates/import",
        token=token,
        body=retired,
    )
    return 1


def cleanup() -> dict[str, Any]:
    names = _environment_names()
    token = _token()
    retired_templates = _retire_run_templates(
        token,
        os.environ["ZIZU_E2E_RUN_ID"],
    )
    disabled_strategies = 0
    strategy_name = f"E2E调度-{os.environ['ZIZU_E2E_RUN_ID']}"
    for strategy in _request("GET", "/dispatch-strategies", token=token).get("strategies", []):
        if strategy.get("name") == strategy_name and strategy.get("enabled"):
            _request("POST", f"/dispatch-strategies/{strategy['id']}/disable", token=token)
            disabled_strategies += 1

    nodes = _request("GET", "/nodes", token=token).get("nodes", [])
    by_id = {str(item["id"]): item for item in nodes}
    accepted_names = {names.platform_node, f"{names.platform_node}-已编辑"}
    targets = [
        item for item in nodes
        if item.get("name") in accepted_names
        and by_id.get(str(item.get("parent_id")), {}).get("name") == REQUIRED_ROOT
    ]
    for item in targets:
        _request("DELETE", f"/nodes/{item['id']}", token=token)

    current = _request("GET", "/neuron/nodes", token=token).get("nodes", [])
    existing_names = {str(item.get("name", "")) for item in current}
    if names.neuron_node in existing_names:
        _cleanup_neuron_via_ssh(names)
    return {
        "status": "clean",
        "neuron_node": names.neuron_node,
        "retired_nodes": len(targets),
        "disabled_strategies": disabled_strategies,
        "retired_templates": retired_templates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("preflight", "setup", "publish", "ensure-strategy", "cleanup"),
    )
    parser.add_argument("--point-key", default="e2e_active_power")
    parser.add_argument("--value-json", default="12.5")
    arguments = parser.parse_args()
    if os.environ.get("ZIZU_E2E_ALLOW_LIVE_WRITES") != "1":
        raise RuntimeError("ZIZU_E2E_ALLOW_LIVE_WRITES must be 1")
    action = {
        "preflight": preflight,
        "setup": setup,
        "publish": lambda: publish(arguments.point_key, _parse_scalar(arguments.value_json)),
        "ensure-strategy": ensure_strategy,
        "cleanup": cleanup,
    }[arguments.command]
    print(json.dumps(action(), ensure_ascii=False))
    return 0


def _parse_scalar(raw: str) -> int | float | str | bool:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("--value-json must contain one JSON scalar") from error
    if value is None or not isinstance(value, (int, float, str, bool)):
        raise ValueError("--value-json must contain one JSON scalar")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
