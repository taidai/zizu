"""
V4 端到端验证: 发布模拟 Neuron 消息 → 验证管道消费 → 验证 TSDB 入库

用法: 设置 ZIZU_API 后运行 python test_f0_e2e.py；无 token 时交互登录
前置: backend uvicorn 已在 :9000 运行 + mosquitto @1883 + TSDB @5432
"""
import getpass
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt
import psycopg2
from app.core.secret_policy import validate_secret

MQTT_HOST, MQTT_PORT = "127.0.0.1", 1883


def require_safe_api_url() -> str:
    api = os.environ.get("ZIZU_API", "").strip().rstrip("/")
    if not api:
        raise RuntimeError(
            "ZIZU_API is required; set the explicit HTTPS API base, "
            "for example https://zizu.example/api/v1"
        )
    parsed = urllib.parse.urlsplit(api)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("ZIZU_API must not contain credentials, query, or fragment")
    if parsed.scheme == "https" and parsed.hostname:
        return api
    host = parsed.hostname or ""
    try:
        is_loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if (
        parsed.scheme == "http"
        and is_loopback
        and os.environ.get("ZIZU_ALLOW_INSECURE_LOCAL_HTTP", "").lower() == "true"
    ):
        return api
    raise RuntimeError(
        "ZIZU_API must use HTTPS. Loopback HTTP is allowed only with "
        "ZIZU_ALLOW_INSECURE_LOCAL_HTTP=true in an isolated development environment."
    )


def require_api_token(api: str) -> str:
    token = os.environ.get("ZIZU_API_TOKEN", "").strip()
    if token:
        return token
    if not sys.stdin.isatty():
        raise RuntimeError(
            "ZIZU_API_TOKEN is required for non-interactive runs; inject it from a secret manager"
        )
    username = os.environ.get("ZIZU_API_USERNAME", "").strip() or input("ZiZu username: ").strip()
    if not username:
        raise RuntimeError("ZiZu username is required")
    password = getpass.getpass("ZiZu password: ")
    try:
        request = urllib.request.Request(
            f"{api}/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ZiZu login failed with HTTP {exc.code}") from exc
    finally:
        password = ""
    token = str(payload.get("access_token", "")).strip()
    if not token:
        raise RuntimeError("ZiZu login response did not contain an access token")
    return token


API = require_safe_api_url()
API_TOKEN = require_api_token(API)
DB_DSN = os.environ.get("ZIZU_DSN")
if not DB_DSN or not DB_DSN.strip():
    raise RuntimeError("ZIZU_DSN is required; no database credential default is provided")
validate_secret("database", psycopg2.extensions.parse_dsn(DB_DSN).get("password", ""))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


print("=" * 60)
print("F0 End-to-End Validation")
print("=" * 60)

# ═══ 1. 发布模拟 Neuron 消息 ═══
print("\n--- 1. Publish mock Neuron messages to mosquitto ---")
now_ms = int(time.time() * 1000)
messages = [
    ("telemetry/en9_meter", {
        "node": "en9_meter", "timestamp": now_ms,
        "values": {"meter_p_act": 12.5, "meter_voltage": 220.1},
    }),
    ("telemetry/en9_bms", {
        "node": "en9_bms", "timestamp": now_ms,
        "values": {"bms_current": 16500, "bms_soc": 78.5},
    }),
]

client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                     client_id="e2e-publisher")
client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
client.loop_start()
for topic, body in messages:
    info = client.publish(topic, json.dumps(body), qos=1)
    info.wait_for_publish(timeout=5)
    print(f"  published → {topic}")
client.loop_stop()
client.disconnect()
print("  publish done, waiting 3s for pipeline flush ...")
time.sleep(3)

# ═══ 2. Health API: pipeline metrics ═══
print("\n--- 2. Health API pipeline metrics ---")
try:
    request = urllib.request.Request(
        f"{API}/health",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=5) as resp:
        health = json.loads(resp.read().decode())
    pipe = health.get("pipeline", {})
    print(f"  pipeline.status = {pipe.get('status')}")
    print(f"  messages_received = {pipe.get('messages_received')}")
    print(f"  messages_parsed_ok = {pipe.get('messages_parsed_ok')}")
    print(f"  points_written_db = {pipe.get('points_written_db')}")
    check("pipeline RUNNING", pipe.get("status") == "RUNNING")
    check("收到 ≥2 条消息", (pipe.get("messages_received") or 0) >= 2,
          f"got {pipe.get('messages_received')}")
    check("解析成功 ≥2", (pipe.get("messages_parsed_ok") or 0) >= 2)
    check("入库 ≥4 点 (2msg×2tag)", (pipe.get("points_written_db") or 0) >= 4,
          f"got {pipe.get('points_written_db')}")
    check("MQTT connected", health["components"]["mqtt"]["status"] == "connected")
    check("TSDB connected", health["components"]["timescaledb"]["status"] == "connected")
except Exception as e:
    check("Health API 可达", False, str(e))

# ═══ 3. TSDB 直查验证 ═══
print("\n--- 3. Direct TSDB query ---")
try:
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM t_telemetry")
    total = cur.fetchone()[0]
    check("t_telemetry 有数据", total >= 4, f"got {total}")

    # BMS 校准值验证: (16500 * 0.1) + (-1600) = 50.0
    cur.execute("""
        SELECT t2.value_float FROM t_telemetry t2
        JOIN t_tags tg ON t2.tag_id = tg.id
        WHERE tg.name = 'bms_current'
        ORDER BY t2.ts DESC LIMIT 1
    """)
    row = cur.fetchone()
    check("BMS校准值=50A", row is not None and abs(row[0] - 50.0) < 0.01,
          f"got {row[0] if row else None}")

    # 电表值
    cur.execute("""
        SELECT t2.value_float FROM t_telemetry t2
        JOIN t_tags tg ON t2.tag_id = tg.id
        WHERE tg.name = 'meter_p_act'
        ORDER BY t2.ts DESC LIMIT 1
    """)
    row = cur.fetchone()
    check("meter_p_act=12.5", row is not None and abs(row[0] - 12.5) < 0.01,
          f"got {row[0] if row else None}")

    cur.close()
    conn.close()
except Exception as e:
    check("TSDB 查询", False, str(e))

print("\n" + "=" * 60)
print(f"RESULT: {PASS} passed, {FAIL} failed")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
