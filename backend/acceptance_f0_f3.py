import json, os, time, urllib.request, urllib.error, paho.mqtt.client as mqtt, psycopg2
from app.core.secret_policy import validate_secret

API = os.environ.get('ZIZU_API', 'http://127.0.0.1:9000/api/v1')
DB_DSN = os.environ.get('ZIZU_DSN')
if not DB_DSN or not DB_DSN.strip():
    raise RuntimeError('ZIZU_DSN is required; no database credential default is provided')
validate_secret('database', psycopg2.extensions.parse_dsn(DB_DSN).get('password', ''))
MQTT_HOST = os.environ.get('ZIZU_MQTT', '127.0.0.1')
MQTT_PORT = int(os.environ.get('ZIZU_MQTT_PORT', '1883'))
AGG_POLL_INTERVAL = 2
AGG_POLL_MAX = 30

PASS = FAIL = 0

def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f'  [PASS] {name}')
    else:
        FAIL += 1; print(f'  [FAIL] {name}  {detail}')

def close_to(name, actual, expected, rel_tol=1e-4, abs_tol=0.01):
    if actual is None or expected is None:
        check(name, False, f'actual={actual} expected={expected}')
        return False
    ok = abs(actual - expected) <= max(rel_tol * max(abs(actual), abs(expected)), abs_tol)
    check(name, ok, f'actual={actual} expected={expected}')
    return ok

def http(method, path, body=None):
    h = {'Content-Type': 'application/json'}
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(f'{API}{path}', data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return r.status, {'raw': raw.decode('utf-8', errors='replace') if raw else ''}
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return e.code, {'raw': body.decode('utf-8', errors='replace') if body else ''}
    except Exception as e:
        return 0, {'error': str(e)}

def db(query, params=None):
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()
    cur.execute(query, params or ())
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

print('='*60)
print('ZiZu F0 + F1 + F2 + F3 Acceptance on e606')
print('='*60)

# ---------- F0: Health + pipeline metrics ----------
print('\n--- F0.1 Health API ---')
status, health = http('GET', '/health')
check('health reachable', status == 200, f'{status}: {health}')
if status == 200:
    check('pipeline RUNNING', health.get('pipeline', {}).get('status') == 'RUNNING')
    check('mqtt connected', health.get('components', {}).get('mqtt', {}).get('status') == 'connected')
    check('tsdb connected', health.get('components', {}).get('timescaledb', {}).get('status') == 'connected')
    before_received = health.get('pipeline', {}).get('messages_received', 0)
    before_written = health.get('pipeline', {}).get('points_written_db', 0)
else:
    before_received = before_written = 0

# ---------- F0: Publish mock messages ----------
print('\n--- F0.2 Publish mock Neuron messages ---')
now_ms = int(time.time() * 1000)
messages = [
    ('neuron/en9_meter/telemetry', {'node_name': 'en9_meter', 'timestamp': now_ms, 'tags': {'meter_p_act': 12.5, 'meter_voltage': 220.1}}),
    ('neuron/en9_bms/telemetry', {'node_name': 'en9_bms', 'timestamp': now_ms, 'tags': {'bms_current': 16500.0, 'bms_soc': 78.5}}),
]
client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id='acceptance-publisher')
client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
client.loop_start()
for topic, body in messages:
    info = client.publish(topic, json.dumps(body), qos=1)
    info.wait_for_publish(timeout=5)
    print(f'  published -> {topic}')
client.loop_stop(); client.disconnect()
print('  waiting 5s for pipeline flush...')
time.sleep(5)

# ---------- F0: Verify pipeline metrics & latest values ----------
print('\n--- F0.3 Verify metrics & latest values ---')
status, health = http('GET', '/health')
if status == 200:
    after_received = health.get('pipeline', {}).get('messages_received', 0)
    after_written = health.get('pipeline', {}).get('points_written_db', 0)
    check('messages_received increased', after_received > before_received, f'{before_received} -> {after_received}')
    check('points_written_db increased', after_written > before_written, f'{before_written} -> {after_written}')
    check('parse success rate 100%', health.get('validation', {}).get('message_parsing', {}).get('success_rate') == 100.0)
else:
    check('health after publish', False, f'{status}: {health}')

meter_p_act_id = None
status, tags = http('GET', '/tags?node_id=44444444-4444-4444-4444-444444444444&limit=10')
if status == 200:
    mp = next((t for t in tags.get('tags', []) if t['name'] == 'meter_p_act'), None)
    meter_p_act_id = mp.get('id') if mp else None
    check('meter_p_act has latest value', mp is not None and mp.get('eng_value') == 12.5, mp)
else:
    check('tags API', False, f'{status}: {tags}')

status, tags = http('GET', '/tags?node_id=55555555-5555-5555-5555-555555555555&limit=10')
bms_current_id = None
bms_current_expected = None
if status == 200:
    bc = next((t for t in tags.get('tags', []) if t['name'] == 'bms_current'), None)
    bms_current_id = bc.get('id') if bc else None
    scale = bc.get('scale_factor', 1.0) or 1.0
    offset = bc.get('value_offset', 0.0) or 0.0
    bms_current_expected = 16500.0 * scale + offset
    check('bms_current has latest value', bc is not None and bc.get('raw_value') is not None, bc)
    close_to('bms_current engineering value matches normalizer', bc.get('eng_value') if bc else None, bms_current_expected)

    # Verify t_telemetry_latest cache table mirrors the latest value
    if bms_current_id:
        latest_rows = db(
            'SELECT ts, COALESCE(value_float, value_int::float) AS value, is_virtual FROM t_telemetry_latest WHERE tag_id = %s',
            (bms_current_id,)
        )
        check('t_telemetry_latest has bms_current row', bool(latest_rows), latest_rows)
        if latest_rows:
            close_to('t_telemetry_latest bms_current value matches expected', latest_rows[0][1], bms_current_expected)
else:
    check('bms tags API', False, f'{status}: {tags}')

# ---------- F3: Node tree ----------
print('\n--- F3.1 Node tree structure ---')
status, tree = http('GET', '/nodes/11111111-1111-1111-1111-111111111111/tree')
if status == 200:
    root = tree.get('tree', {})
    check('test site root', root.get('name') == '测试场站')
    check('has station child', len(root.get('children', [])) >= 1)
    check('has device with tags', any(c.get('tag_count', 0) > 0 for c in root.get('children', [])[0].get('children', []) if c.get('layer') == 4))
else:
    check('tree API', False, f'{status}: {tree}')

# ---------- F3: Create aggregate logical tag and verify computed value ----------
print('\n--- F3.2 Create aggregate logical tag and verify value ---')
test_tag_name = f'_accept_total_current_{int(time.time())}'
tag_id = None
expected_aggregate = bms_current_expected

try:
    if not bms_current_id:
        check('bms_current source exists', False, 'cannot create aggregate without source')
    else:
        body = {
            'node_id': '33333333-3333-3333-3333-333333333333',
            'name': test_tag_name,
            'display_name': '验收总电流',
            'unit': 'A',
            'tag_type': 'LOGICAL',
            'data_type': 'FLOAT',
            'formula_type': 'aggregate',
            'aggregate_fn': 'SUM',
            'sources': [bms_current_id],
        }
        status, created = http('POST', '/tags', body)
        check('created logical tag', status in (200, 201), f'{status}: {created}')

        if status in (200, 201):
            tag_id = created.get('id')
            print(f'  tag_id={tag_id}, expected aggregate={expected_aggregate}')
            print(f'  polling /tags/{tag_id} for up to {AGG_POLL_MAX}s...')

            actual = None
            for i in range(0, AGG_POLL_MAX, AGG_POLL_INTERVAL):
                time.sleep(AGG_POLL_INTERVAL)
                status, latest = http('GET', f'/tags/{tag_id}')
                if status == 200 and latest.get('eng_value') is not None:
                    actual = latest.get('eng_value')
                    print(f'  [{i+AGG_POLL_INTERVAL}s] eng_value={actual}')
                    break
                print(f'  [{i+AGG_POLL_INTERVAL}s] no value yet (status={status})')

            close_to('aggregate value matches expected SUM', actual, expected_aggregate)

            rows = db(
                'SELECT is_virtual, value_float FROM t_telemetry WHERE tag_id = %s ORDER BY ts DESC LIMIT 1',
                (tag_id,)
            )
            check('virtual row written to t_telemetry', bool(rows) and rows[0][0] is True, rows)
            if rows:
                close_to('virtual row value matches expected', rows[0][1], expected_aggregate)
finally:
    if tag_id:
        print('  cleaning up test tag...')
        http('DELETE', f'/tags/{tag_id}')

# ---------- F1: SymPy expression + condition ----------
print('\n--- F1.1 SymPy expression logical tag ---')
expr_tag_id = None
condition_tag_id = None
try:
    if not (bms_current_id and meter_p_act_id):
        check('sources for expression', False, 'bms_current or meter_p_act missing')
    else:
        expr_name = f'_accept_expr_power_{int(time.time())}'
        expr_body = {
            'node_id': '33333333-3333-3333-3333-333333333333',
            'name': expr_name,
            'display_name': '验收功率合成',
            'unit': 'kW',
            'tag_type': 'LOGICAL',
            'data_type': 'FLOAT',
            'formula_type': 'expression',
            'formula': 's0 + s1',
            'sources': [bms_current_id, meter_p_act_id],
        }
        status, created = http('POST', '/tags', expr_body)
        check('created expression logical tag', status in (200, 201), f'{status}: {created}')

        if status in (200, 201):
            expr_tag_id = created.get('id')
            expected_expr = -1435.0 + 12.5
            print(f'  expr_tag_id={expr_tag_id}, expected={expected_expr}')
            print(f'  polling /tags/{expr_tag_id} for up to {AGG_POLL_MAX}s...')
            actual = None
            for i in range(0, AGG_POLL_MAX, AGG_POLL_INTERVAL):
                time.sleep(AGG_POLL_INTERVAL)
                status, latest = http('GET', f'/tags/{expr_tag_id}')
                if status == 200 and latest.get('eng_value') is not None:
                    actual = latest.get('eng_value')
                    print(f'  [{i+AGG_POLL_INTERVAL}s] eng_value={actual}')
                    break
                print(f'  [{i+AGG_POLL_INTERVAL}s] no value yet (status={status})')
            close_to('expression value matches expected', actual, expected_expr)

            rows = db(
                'SELECT is_virtual, value_float FROM t_telemetry WHERE tag_id = %s ORDER BY ts DESC LIMIT 1',
                (expr_tag_id,)
            )
            check('expression virtual row in t_telemetry', bool(rows) and rows[0][0] is True, rows)

            latest_rows = db(
                'SELECT is_virtual, value_float FROM t_telemetry_latest WHERE tag_id = %s',
                (expr_tag_id,)
            )
            check('expression virtual row in t_telemetry_latest', bool(latest_rows) and latest_rows[0][0] is True, latest_rows)

    print('\n--- F1.2 Condition logical tag ---')
    if not bms_current_id:
        check('source for condition', False, 'bms_current missing')
    else:
        cond_name = f'_accept_condition_{int(time.time())}'
        cond_body = {
            'node_id': '33333333-3333-3333-3333-333333333333',
            'name': cond_name,
            'display_name': '验收电流告警',
            'unit': '',
            'tag_type': 'LOGICAL',
            'data_type': 'BOOL',
            'formula_type': 'condition',
            'formula': 's0 > -1000',
            'sources': [bms_current_id],
        }
        status, created = http('POST', '/tags', cond_body)
        check('created condition logical tag', status in (200, 201), f'{status}: {created}')

        if status in (200, 201):
            condition_tag_id = created.get('id')
            print(f'  condition_tag_id={condition_tag_id}, expected=False (s0=-1435 > -1000 is false)')
            print(f'  polling /tags/{condition_tag_id} for up to {AGG_POLL_MAX}s...')
            actual_bool = None
            for i in range(0, AGG_POLL_MAX, AGG_POLL_INTERVAL):
                time.sleep(AGG_POLL_INTERVAL)
                status, latest = http('GET', f'/tags/{condition_tag_id}')
                if status == 200 and latest.get('raw_value') is not None:
                    actual_bool = latest.get('raw_value')
                    print(f'  [{i+AGG_POLL_INTERVAL}s] raw_value={actual_bool}')
                    break
                print(f'  [{i+AGG_POLL_INTERVAL}s] no value yet (status={status})')
            check('condition value matches expected FALSE', actual_bool is False, f'actual={actual_bool}')

            rows = db(
                'SELECT is_virtual, value_bool FROM t_telemetry WHERE tag_id = %s ORDER BY ts DESC LIMIT 1',
                (condition_tag_id,)
            )
            check('condition virtual row in t_telemetry', bool(rows) and rows[0][0] is True and rows[0][1] is False, rows)
finally:
    if expr_tag_id:
        print('  cleaning up expression tag...')
        http('DELETE', f'/tags/{expr_tag_id}')
    if condition_tag_id:
        print('  cleaning up condition tag...')
        http('DELETE', f'/tags/{condition_tag_id}')

# ---------- F2: Rules / Alarms / RPC ----------
print('\n--- F2.1 Alarm rule trigger and acknowledge ---')
rule_id = None
alarm_id = None
try:
    alarm_rule_body = {
        'name': f'_accept_alarm_rule_{int(time.time())}',
        'rule_type': 'alarm',
        'jdm_content': {
            'when': 'bms_current > -2000',
            'actions': [
                {'type': 'alarm', 'level': 'MAJOR', 'message': '验收：bms_current 超过阈值'}
            ]
        },
        'enabled': True,
    }
    status, rule_created = http('POST', '/rules', alarm_rule_body)
    check('created alarm rule', status in (200, 201), f'{status}: {rule_created}')
    rule_id = rule_created.get('id') if status in (200, 201) else None

    if rule_id:
        # 确保来源数据存在（已发布过）
        print(f'  rule_id={rule_id}, waiting for rule tick (up to {AGG_POLL_MAX}s)...')
        alarm_id = None
        for i in range(0, AGG_POLL_MAX, AGG_POLL_INTERVAL):
            time.sleep(AGG_POLL_INTERVAL)
            status, alarms = http('GET', '/alarms?active=true&level=MAJOR')
            if status == 200:
                found = next((a for a in alarms.get('alarms', []) if a.get('rule_id') == rule_id), None)
                if found:
                    alarm_id = found.get('id')
                    print(f'  [{i+AGG_POLL_INTERVAL}s] alarm_id={alarm_id}')
                    break
            print(f'  [{i+AGG_POLL_INTERVAL}s] no alarm yet (status={status})')

        check('alarm created by rule', alarm_id is not None, alarms)

        if alarm_id:
            status, ack = http('PUT', f'/alarms/{alarm_id}/acknowledge', {'ack_user': 'acceptance'})
            check('acknowledged alarm', status == 200, f'{status}: {ack}')

            # 确认后查 acknowledged=true 应该能查到已确认记录
            status, alarms = http('GET', '/alarms?acknowledged=true')
            found = next((a for a in alarms.get('alarms', []) if a.get('id') == alarm_id), None) if status == 200 else None
            check('alarm marked acknowledged', found is not None and found.get('acknowledged') is True, found)

    print('\n--- F2.2 Rule simulation endpoint ---')
    if rule_id:
        status, sim = http('POST', f'/rules/{rule_id}/simulate', {'context': {'bms_current': -500}})
        check('rule simulation true for -500', status == 200 and sim.get('triggered') is True, sim)
        status, sim = http('POST', f'/rules/{rule_id}/simulate', {'context': {'bms_current': -3000}})
        check('rule simulation false for -3000', status == 200 and sim.get('triggered') is False, sim)
finally:
    if rule_id:
        print('  cleaning up alarm rule...')
        http('DELETE', f'/rules/{rule_id}')

print('\n--- F2.3 RPC command writes audit log ---')
status, rpc_resp = http('POST', '/devices/55555555-5555-5555-5555-555555555555/rpc', {
    'command': 'test_breaker',
    'payload': {'tag': 'breaker', 'value': 1},
    'topic': 'neuron/en9_bms/write',
})
check('rpc endpoint accepted', status in (200, 201), f'{status}: {rpc_resp}')

audit_rows = db(
    'SELECT action, target_type, details FROM t_audit_log WHERE action = %s AND target_id = %s ORDER BY created_at DESC LIMIT 1',
    ('RPC', '55555555-5555-5555-5555-555555555555')
)
check('rpc audit log row exists', bool(audit_rows), audit_rows)
if audit_rows:
    details = audit_rows[0][2]
    check('rpc audit log contains command', 'test_breaker' in str(details), details)

print('\n' + '='*60)
print(f'RESULT: {PASS} passed, {FAIL} failed')
print('='*60)
