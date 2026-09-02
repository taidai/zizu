"""Guarded live fixture for alarm HTTP notification acceptance.

Every mutable resource is scoped to the exact ``E2E验证`` root and current run
identifier. The fixture publishes simulated telemetry only; it never calls a
control or Neuron write endpoint.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import UUID

try:
    from scripts import node_management_e2e_fixture as node_fixture
except ModuleNotFoundError:  # Direct execution adds backend/scripts to sys.path.
    import node_management_e2e_fixture as node_fixture


RECEIVER_HOST = "127.0.0.1"
RECEIVER_PORT = 19091
REQUIRED_ROOT = "E2E验证"
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_force_due_candidate(
    *,
    notification_id: UUID,
    alarm_name: str | None,
    target_display: str | None,
    expected_run_id: str,
) -> None:
    expected_name = f"E2E通知-{expected_run_id}"
    parsed = urllib.parse.urlsplit(target_display or "")
    if (
        not SAFE_RUN_ID.fullmatch(expected_run_id)
        or alarm_name != expected_name
        or parsed.hostname != RECEIVER_HOST
        or parsed.port != RECEIVER_PORT
    ):
        raise ValueError(f"refusing non-E2E notification {notification_id}")


def _environment() -> tuple[str, str]:
    root = os.environ.get("ZIZU_E2E_WRITE_ROOT", "")
    run_id = os.environ.get("ZIZU_E2E_RUN_ID", "")
    if os.environ.get("ZIZU_E2E_ALLOW_LIVE_WRITES") != "1":
        raise RuntimeError("ZIZU_E2E_ALLOW_LIVE_WRITES must be 1")
    if root != REQUIRED_ROOT:
        raise RuntimeError(f"ZIZU_E2E_WRITE_ROOT must be exactly {REQUIRED_ROOT}")
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise RuntimeError("ZIZU_E2E_RUN_ID is invalid")
    return root, run_id


def _api_request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: Any | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    base_url = os.environ.get("ZIZU_E2E_BASE_URL", "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("ZIZU_E2E_BASE_URL must be an explicit HTTP(S) URL")
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url}/api/v1{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"ZiZu API {method} {path} failed with HTTP {error.code}: {detail}"
        ) from error


def _token() -> str:
    username = os.environ.get("ZIZU_E2E_USERNAME", "").strip()
    password = os.environ.get("ZIZU_E2E_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("ZIZU_E2E_USERNAME and ZIZU_E2E_PASSWORD are required")
    result = _api_request(
        "POST", "/auth/login", body={"username": username, "password": password}
    )
    token = str(result.get("access_token", ""))
    if not token:
        raise RuntimeError("ZiZu login did not return an access token")
    return token


def _find_or_create_node(
    token: str,
    name: str,
    node_type: str,
    parent_id: str | None,
) -> dict[str, Any]:
    nodes = _api_request("GET", "/nodes", token=token).get("nodes", [])
    matches = [
        item
        for item in nodes
        if item.get("name") == name and (item.get("parent_id") or None) == parent_id
    ]
    if len(matches) > 1:
        raise RuntimeError(f"multiple E2E nodes named {name}")
    if matches:
        return matches[0]
    result = _api_request(
        "POST",
        "/nodes",
        token=token,
        body={
            "name": name,
            "node_type": node_type,
            "parent_id": parent_id,
            "sort_order": 0,
            "config": {},
        },
    )
    return result.get("node", result)


def _processing_content(run_id: str) -> dict[str, Any]:
    safe_run = run_id.replace("-", "_")
    return {
        "schemaVersion": "zizu.point-processing/v1alpha1",
        "id": f"inline.e2e_fault_{safe_run}",
        "kind": "point_processing_template",
        "displayName": f"E2E故障状态-{run_id}",
        "deviceCategory": "E2E_DEVICE",
        "brand": "ZiZu",
        "model": "E2E",
        "revision": 1,
        "status": "active",
        "inputs": [
            {
                "id": "fault_raw",
                "sourceKind": "l0",
                "sourceKey": "e2e_fault_bit",
                "aliases": [],
                "dataType": "INT",
                "unit": None,
                "required": True,
            }
        ],
        "outputs": [
            {
                "id": "fault_state",
                "entityDefinition": f"e2e.fault_state_{safe_run}",
                "dataType": "BOOL",
                "unit": None,
                "freshness": "30s",
                "transform": {
                    "kind": "boolean_map",
                    "input": "fault_raw",
                    "trueWhen": 1,
                },
            }
        ],
    }


def setup() -> dict[str, Any]:
    root, run_id = _environment()
    names = node_fixture.build_resource_names(root, run_id)
    node_fixture.setup()
    token = _token()
    root_node = _find_or_create_node(token, root, "Site", None)
    node = _find_or_create_node(
        token,
        names.platform_node,
        "E2E_DEVICE",
        str(root_node["id"]),
    )
    node_id = str(node["id"])

    preview = _api_request(
        "POST",
        "/tags/import-neuron/preview",
        token=token,
        body={
            "node_id": node_id,
            "neuron_node": names.neuron_node,
            "neuron_groups": [names.neuron_group],
        },
    )
    _api_request(
        "POST",
        "/tags/import-neuron",
        token=token,
        body={
            "node_id": node_id,
            "neuron_node": names.neuron_node,
            "neuron_groups": [names.neuron_group],
            "preview_digest": preview["preview_digest"],
        },
    )
    tags = _api_request(
        "GET",
        f"/tags?node_id={urllib.parse.quote(node_id)}&page=1&page_size=100",
        token=token,
    ).get("tags", [])
    bit_matches = [item for item in tags if item.get("name") == names.bit_tag]
    if len(bit_matches) != 1:
        raise RuntimeError(f"expected one E2E BIT tag, found {len(bit_matches)}")
    bit_tag = bit_matches[0]

    trunk = _api_request("GET", f"/nodes/{node_id}/data-trunk", token=token)
    expected_key = f"e2e.fault_state_{run_id.replace('-', '_')}"
    entity_id = next(
        (
            str(item["entity_instance_id"])
            for item in trunk.get("l2", [])
            if item.get("definition_key") == expected_key
        ),
        None,
    )
    if entity_id is None:
        plan = _api_request(
            "POST",
            f"/nodes/{node_id}/point-processing-drafts/plan",
            token=token,
            body={
                "content": _processing_content(run_id),
                "input_selections": {"fault_raw": str(bit_tag["id"])},
            },
        )
        if plan.get("blockers"):
            raise RuntimeError(f"E2E point-processing plan blocked: {plan['blockers']}")
        _api_request(
            "POST",
            f"/point-processing-plans/{plan['id']}/apply",
            token=token,
            headers={"Idempotency-Key": f"alarm-http-l1-{run_id}"},
            body={"plan_digest": plan["digest"]},
        )

    node_fixture.publish(names.bit_tag, 0)
    entities = _api_request("GET", "/entity-instances", token=token).get("items", [])
    matches = [
        item
        for item in entities
        if item.get("node_id") == node_id and item.get("definition_id") == expected_key
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one E2E entity, found {len(matches)}")
    return {
        "status": "ready",
        "node_id": node_id,
        "node_name": names.platform_node,
        "tag_id": str(bit_tag["id"]),
        "tag_key": names.bit_tag,
        "entity_id": str(matches[0]["id"]),
        "entity_name": f"E2E故障状态-{run_id}",
        "alarm_name": f"E2E通知-{run_id}",
        "config_name": f"E2E HTTP-{run_id}",
        "rule_set_key": f"e2e-http-{run_id}",
    }


def _ssh_connection():
    try:
        import paramiko
    except ImportError as error:
        raise RuntimeError("Paramiko is required for private receiver access") from error
    host = os.environ.get("ZIZU_E2E_SSH_HOST", "").strip()
    if not host:
        host = urllib.parse.urlsplit(os.environ["ZIZU_E2E_BASE_URL"]).hostname or ""
    user = os.environ.get("ZIZU_E2E_SSH_USER", "").strip()
    password = os.environ.get("ZIZU_E2E_SSH_PASSWORD", "")
    if not host or not user or not password:
        raise RuntimeError("private receiver access requires SSH credentials")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        host,
        port=int(os.environ.get("ZIZU_E2E_SSH_PORT", "13122")),
        username=user,
        password=password,
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
    )
    return client


def _remote_backend_python(source: str) -> str:
    password = os.environ.get(
        "ZIZU_E2E_SUDO_PASSWORD",
        os.environ.get("ZIZU_E2E_SSH_PASSWORD", ""),
    )
    command = (
        "sudo -S -p '' sh -c 'container=$(docker ps --filter "
        "label=com.docker.compose.service=backend --format \"{{.Names}}\" | head -n 1); "
        "[ -n \"$container\" ] || exit 42; exec docker exec -i \"$container\" python -'"
    )
    client = _ssh_connection()
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=40)
        stdin.write(f"{password}\n{source}")
        stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8", errors="replace")
        error_output = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise RuntimeError(
                f"remote backend command failed ({status}): {error_output.strip()}"
            )
        return output.strip()
    finally:
        client.close()


def start_receiver(
    response_status: int = 204,
    delay_seconds: float = 0,
) -> dict[str, Any]:
    _environment()
    source = Path(__file__).with_name("alarm_http_test_receiver.py").read_bytes()
    encoded = base64.b64encode(source).decode("ascii")
    launcher = f"""
import base64, socket, subprocess, sys, time
path = '/tmp/zizu_alarm_http_test_receiver.py'
open(path, 'wb').write(base64.b64decode({encoded!r}))
probe = socket.socket()
probe.settimeout(0.25)
try:
    listening = probe.connect_ex(({RECEIVER_HOST!r}, {RECEIVER_PORT})) == 0
finally:
    probe.close()
if not listening:
    subprocess.Popen(
        [sys.executable, path, '--host', {RECEIVER_HOST!r}, '--port', {str(RECEIVER_PORT)!r},
         '--response-status', {str(response_status)!r}, '--delay-seconds', {str(delay_seconds)!r}],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.5)
print('ready')
""".lstrip()
    _remote_backend_python(launcher)
    return receiver_status()


def receiver_status() -> dict[str, object]:
    _environment()
    output = _remote_backend_python(
        f"""
import urllib.request
with urllib.request.urlopen('http://{RECEIVER_HOST}:{RECEIVER_PORT}/records', timeout=5) as response:
    print(response.read().decode('utf-8'))
""".lstrip()
    )
    payload = json.loads(output.splitlines()[-1])
    return {"status": "ready", "records": payload.get("items", [])}


def clear_receiver() -> dict[str, object]:
    _environment()
    _remote_backend_python(
        f"""
import urllib.request
request = urllib.request.Request('http://{RECEIVER_HOST}:{RECEIVER_PORT}/records', method='DELETE')
with urllib.request.urlopen(request, timeout=5) as response:
    response.read()
""".lstrip()
    )
    return {"status": "cleared"}


def force_due(notification_id: UUID) -> dict[str, Any]:
    _, run_id = _environment()
    source = f"""
from uuid import UUID
from app.services.telemetry_store import get_connection
notification_id = UUID({str(notification_id)!r})
with get_connection() as connection, connection.cursor() as cursor:
    cursor.execute('''
        SELECT task.context_snapshot->'alarm'->>'name', task.target_display, task.status
        FROM t_alarm_http_notification_tasks task WHERE task.id=%s
    ''', (notification_id,))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError('notification not found')
    if row[0] != {f'E2E通知-{run_id}'!r} or not str(row[1]).startswith('http://127.0.0.1:19091/') or row[2] != 'retry_wait':
        raise RuntimeError('refusing non-E2E notification')
    cursor.execute('UPDATE t_alarm_http_notification_tasks SET next_attempt_at=NOW() WHERE id=%s', (notification_id,))
    connection.commit()
print('ready')
""".lstrip()
    _remote_backend_python(source)
    return {"status": "due", "notification_id": str(notification_id)}


def cleanup() -> dict[str, Any]:
    _, run_id = _environment()
    token = _token()
    rule_key = f"e2e-http-{run_id}"
    rule_sets = _api_request("GET", "/alarm-rule-sets", token=token).get("items", [])
    matching = [item for item in rule_sets if item.get("key") == rule_key]
    if matching:
        current = max(matching, key=lambda item: int(item["revision"]))
        if current.get("rules"):
            empty = _api_request(
                "POST",
                f"/alarm-rule-sets/{current['rule_set_id']}/revisions",
                token=token,
                body={"rules": []},
            )
            entities = _api_request(
                "GET", "/entity-instances", token=token
            ).get("items", [])
            definition_id = f"e2e.fault_state_{run_id.replace('-', '_')}"
            selected = [
                str(item["id"])
                for item in entities
                if item.get("definition_id") == definition_id
            ]
            if selected:
                plan = _api_request(
                    "POST",
                    "/alarm-configuration-plans",
                    token=token,
                    body={
                        "selection": {
                            "entity_instance_ids": selected,
                            "node_ids": [],
                            "entity_definition_ids": [],
                        },
                        "rule_set_id": empty["rule_set_id"],
                        "rule_set_revision": empty["revision"],
                    },
                )
                _api_request(
                    "POST",
                    f"/alarm-configuration-plans/{plan['id']}/apply",
                    token=token,
                    headers={"Idempotency-Key": f"alarm-http-cleanup-{run_id}"},
                    body={"plan_digest": plan["digest"]},
                )

    config_name = f"E2E HTTP-{run_id}"
    configs = _api_request("GET", "/admin/alarm-http-notifications", token=token)
    for config in configs if isinstance(configs, list) else []:
        if config.get("name") == config_name:
            _api_request(
                "DELETE",
                f"/admin/alarm-http-notifications/{config['id']}",
                token=token,
            )

    result = node_fixture.cleanup()
    try:
        clear_receiver()
    except Exception:
        pass
    return {"status": "clean", **result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "setup",
            "start-receiver",
            "receiver-status",
            "clear-receiver",
            "force-due",
            "cleanup",
        ),
    )
    parser.add_argument("--notification-id")
    parser.add_argument("--response-status", type=int, default=204)
    parser.add_argument("--delay-seconds", type=float, default=0)
    arguments = parser.parse_args()
    actions = {
        "setup": setup,
        "start-receiver": lambda: start_receiver(
            arguments.response_status, arguments.delay_seconds
        ),
        "receiver-status": receiver_status,
        "clear-receiver": clear_receiver,
        "cleanup": cleanup,
    }
    if arguments.command == "force-due":
        if not arguments.notification_id:
            parser.error("--notification-id is required")
        result = force_due(UUID(arguments.notification_id))
    else:
        result = actions[arguments.command]()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
