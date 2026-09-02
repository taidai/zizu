# Alarm HTTP Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 ZiZu 告警增加可配置、可测试、提交后异步投递且可追踪的 HTTP 通知闭环，同时保证告警状态不受通知成败影响。

**Architecture:** 继续以 `AlarmRuntime` 为告警状态唯一写者，在状态转换同一事务内写入现有通知 outbox；新增深模块 `AlarmHttpNotifications` 负责配置、模板、密钥、HTTP、领取、重试和脱敏。规则只保存一个可空 HTTP 配置引用，前端分别在“系统工具”“告警规则”“告警中心”暴露配置、绑定和投递记录，不引入新微服务、Redis、Kafka 或第二套规则引擎。

**Tech Stack:** Python 3.11、FastAPI、psycopg2、httpx、cryptography/Fernet（现有 Python 运行依赖）、PostgreSQL/TimescaleDB、React 18、TypeScript、Playwright、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-09-02-alarm-http-notification-design.md`

## Global Constraints

- 数据主干保持 `真实节点树 → L0 → L1 → L2 → 告警`；通知只由 committed L2 触发的已提交告警状态转换产生。
- `AlarmRuntime` 仍是告警事件、转换和通知任务的唯一写入协调者；通知结果不得修改或回滚告警状态。
- 一个告警规则最多绑定一个可空 HTTP 通知配置；省略或 `null` 等于不通知。
- 发生通知受现有 `notification_throttle_seconds` 约束；只有已生成发生通知的事件才生成配对恢复通知；确认不通知。
- 首次立即发送；失败后 5 秒、30 秒、5 分钟重试，共最多四次；2xx 成功，3xx 不跟随。
- 待发送和重试任务每次尝试读取当前配置；停用时等待且不消耗尝试次数；删除时解除绑定并取消未完成任务。
- URL、敏感查询参数和敏感请求头使用独立 `HTTP_NOTIFICATION_ENCRYPTION_KEY` 加密；接口、日志和尝试记录永不返回明文。
- 不支持脚本、表达式、请求链、响应提取、响应驱动规则/JDM/控制、历史告警补发或内置邮件。
- 不增加第三方依赖，不更换技术栈；继续使用现有 `httpx` 与 `python-jose[cryptography]` 带入的加密运行库。
- 本次目标版本为 `v0.7.4`，数据库 Schema 目标为 `060`；版本号按“逢十进一”规则维护。
- 1 号机保持 `network_mode: host`、`tmpfs: /dev/mqueue`，以 ARM64 固定镜像摘要部署；不得执行控制、设备写或自动策略。

## File Map

| 文件 | 单一职责 |
|---|---|
| `init-db/migration_060_alarm_http_notifications.sql` | 配置、绑定、任务、尝试、租约和重发幂等 Schema |
| `backend/app/services/alarm_http_notifications.py` | HTTP 通知领域类型、模板、安全校验、密钥和 dispatcher 深模块 |
| `backend/app/services/alarm_http_notification_postgres.py` | 配置与投递的 PostgreSQL 持久化、领取和状态推进 |
| `backend/app/api/alarm_http_notifications.py` | 系统管理员的 HTTP 通知配置 API |
| `backend/app/api/alarm_notification_deliveries.py` | 告警通知记录和手工重发 API |
| `backend/app/services/alarm_configuration.py` | 在规则领域和计划校验中携带可空通知绑定 |
| `backend/app/services/alarm_configuration_postgres.py` | 在既有告警配置 apply 事务内发布绑定 |
| `backend/app/services/alarm_runtime.py` | 从发生/恢复转换生成通知意图，不执行 HTTP |
| `backend/app/services/alarm_postgres.py` | 在告警事务中原子保存转换和通知任务 |
| `backend/app/core/config.py`、`.env.example`、`scripts/bootstrap_runtime_secrets.py` | 独立加密密钥的加载、说明和生成 |
| `frontend/src/components/admin/AlarmHttpNotificationPanel.tsx` | 系统工具中的配置、测试、启停和删除界面 |
| `frontend/src/components/admin/alarmHttpNotificationModel.ts` | 配置表单、脱敏预览和稳定错误中文模型 |
| `frontend/src/pages/MinimalAlarmRulesPage.tsx` | 告警规则可空单选通知配置 |
| `frontend/src/components/alarm-center/AlarmNotificationRecords.tsx` | 通知历史、尝试明细和手工重发界面 |
| `frontend/src/components/alarm-center/alarmNotificationModel.ts` | 投递状态和重发资格纯模型 |
| `frontend/src/api/client.ts` | 三处界面共享的 HTTP 通知 API 类型和客户端 |
| `backend/scripts/alarm_http_test_receiver.py` | 1 号机临时、无控制能力的 HTTP 验收接收端 |
| `backend/scripts/alarm_http_notification_e2e_fixture.py` | 只管理 E2E 前缀通知资源、接收器和重试时钟的安全夹具 |
| `frontend/e2e/alarm-http-notification.spec.ts` | 无头浏览器告警 HTTP 通知闭环 |
| `docs/deploy-1号机-v0.7.4-http.md` | 版本、备份、固定摘要、测试和清理证据 |

---

### Task 1: 建立 Schema 060 通知持久化契约

**Files:**
- Create: `init-db/migration_060_alarm_http_notifications.sql`
- Create: `backend/tests/test_alarm_http_notifications_migration_postgres.py`

**Interfaces:**
- Consumes: 现有 `t_alarm_definitions`、`t_alarm_transitions`、`t_alarm_notification_outbox`。
- Produces: 配置表、定义绑定表、可领取通知任务字段、逐次尝试表，以及 `transition_id` 的部分唯一约束。

- [ ] **Step 1: 写失败的 PostgreSQL 迁移契约测试**

```python
MIGRATION_060 = Path(__file__).resolve().parents[2] / "init-db" / "migration_060_alarm_http_notifications.sql"

class AlarmHttpNotificationMigrationSourceTest(unittest.TestCase):
    def test_060_declares_config_binding_delivery_and_attempt_contracts(self):
        sql = MIGRATION_060.read_text(encoding="utf-8")
        for fragment in (
            "CREATE TABLE IF NOT EXISTS public.t_alarm_http_notification_configs",
            "CREATE TABLE IF NOT EXISTS public.t_alarm_http_notification_bindings",
            "CREATE TABLE IF NOT EXISTS public.t_alarm_notification_attempts",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_alarm_notification_transition",
            "LEGACY_NOTIFICATION_NOT_REPLAYED",
        ):
            self.assertIn(fragment, sql)

@unittest.skipUnless(os.environ.get("ZIZU_POSTGRES_TEST") == "1", "set ZIZU_POSTGRES_TEST=1")
class AlarmHttpNotificationMigrationPostgresTest(unittest.TestCase):
    def test_060_is_replayable_and_never_replays_legacy_rows(self):
        with psycopg2.connect(**self.connection_kwargs) as connection, connection.cursor() as cursor:
            legacy_id = self.insert_legacy_notification(cursor)
            cursor.execute(MIGRATION_060.read_text(encoding="utf-8"))
            connection.commit()
            cursor.execute(MIGRATION_060.read_text(encoding="utf-8"))
            cursor.execute(
                "SELECT status,last_error_code FROM t_alarm_notification_outbox WHERE id=%s",
                (legacy_id,),
            )
            self.assertEqual(cursor.fetchone(), ("cancelled", "LEGACY_NOTIFICATION_NOT_REPLAYED"))
```

测试类沿用现有迁移测试的 `setUpClass()` 规则：只接受 `ZIZU_POSTGRES_TEST=1` 且 `DB_NAME` 以 `_test` 结尾，先执行当前基础迁移到 059，再插入一条旧 outbox 行。另一个查询 `information_schema.columns` 的测试必须逐项确认本任务列清单。

- [ ] **Step 2: 运行测试确认 RED**

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_http_notifications_migration_postgres`

Expected: FAIL，提示 `migration_060_alarm_http_notifications.sql` 或目标表不存在。

- [ ] **Step 3: 编写幂等迁移**

```sql
CREATE TABLE IF NOT EXISTS public.t_alarm_http_notification_configs (
  id uuid PRIMARY KEY,
  name text NOT NULL UNIQUE CHECK (btrim(name) <> ''),
  description text,
  method text NOT NULL CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')),
  encrypted_url text NOT NULL,
  url_display text NOT NULL,
  public_query_params jsonb NOT NULL DEFAULT '[]'::jsonb,
  encrypted_secret_query_params text,
  public_headers jsonb NOT NULL DEFAULT '[]'::jsonb,
  encrypted_secret_headers text,
  content_type text NOT NULL,
  body_template text NOT NULL DEFAULT '',
  timeout_seconds integer NOT NULL DEFAULT 5 CHECK (timeout_seconds BETWEEN 1 AND 30),
  current_digest char(64) NOT NULL,
  tested_digest char(64),
  tested_at timestamptz,
  last_test_status jsonb,
  enabled boolean NOT NULL DEFAULT false,
  created_by text NOT NULL,
  updated_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.t_alarm_http_notification_bindings (
  definition_id uuid PRIMARY KEY REFERENCES public.t_alarm_definitions(id) ON DELETE CASCADE,
  configuration_id uuid NOT NULL REFERENCES public.t_alarm_http_notification_configs(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL
);

ALTER TABLE public.t_alarm_notification_outbox
  ADD COLUMN IF NOT EXISTS transition_id uuid REFERENCES public.t_alarm_transitions(id),
  ADD COLUMN IF NOT EXISTS transition_code text,
  ADD COLUMN IF NOT EXISTS configuration_id uuid REFERENCES public.t_alarm_http_notification_configs(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS configuration_name_snapshot text,
  ADD COLUMN IF NOT EXISTS context_snapshot jsonb,
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cycle_attempt_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS lease_owner text,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_target_display text,
  ADD COLUMN IF NOT EXISTS last_http_status integer,
  ADD COLUMN IF NOT EXISTS last_error_code text,
  ADD COLUMN IF NOT EXISTS last_error_detail text,
  ADD COLUMN IF NOT EXISTS last_response_excerpt text,
  ADD COLUMN IF NOT EXISTS cancelled_at timestamptz,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

UPDATE public.t_alarm_notification_outbox
SET status = CASE WHEN delivered_at IS NULL THEN 'cancelled' ELSE 'delivered' END,
    cancelled_at = CASE WHEN delivered_at IS NULL THEN now() ELSE cancelled_at END,
    last_error_code = CASE WHEN delivered_at IS NULL THEN 'LEGACY_NOTIFICATION_NOT_REPLAYED' ELSE last_error_code END
WHERE transition_id IS NULL;

DO $block$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='chk_alarm_notification_context'
      AND conrelid='public.t_alarm_notification_outbox'::regclass
  ) THEN
    ALTER TABLE public.t_alarm_notification_outbox
      ADD CONSTRAINT chk_alarm_notification_context
      CHECK (transition_id IS NULL OR context_snapshot IS NOT NULL) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='chk_alarm_notification_status'
      AND conrelid='public.t_alarm_notification_outbox'::regclass
  ) THEN
    ALTER TABLE public.t_alarm_notification_outbox
      ADD CONSTRAINT chk_alarm_notification_status
      CHECK (status IN ('pending','retry_wait','delivered','failed','cancelled'));
  END IF;
END
$block$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_alarm_notification_transition
  ON public.t_alarm_notification_outbox(transition_id)
  WHERE transition_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_alarm_notification_claim
  ON public.t_alarm_notification_outbox(status,next_attempt_at,lease_expires_at)
  WHERE status IN ('pending','retry_wait');

CREATE TABLE IF NOT EXISTS public.t_alarm_notification_attempts (
  id uuid PRIMARY KEY,
  notification_id uuid NOT NULL REFERENCES public.t_alarm_notification_outbox(id) ON DELETE CASCADE,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  attempted_at timestamptz NOT NULL,
  method text NOT NULL,
  target_display text NOT NULL,
  duration_ms integer NOT NULL CHECK (duration_ms >= 0),
  outcome text NOT NULL CHECK (outcome IN ('delivered','rejected','timeout','network_error','render_error')),
  http_status integer,
  error_code text,
  error_detail text,
  response_excerpt text,
  UNIQUE(notification_id,attempt_no)
);

CREATE TABLE IF NOT EXISTS public.t_alarm_notification_retry_idempotency (
  actor text NOT NULL,
  idempotency_key text NOT NULL,
  notification_id uuid NOT NULL REFERENCES public.t_alarm_notification_outbox(id) ON DELETE CASCADE,
  response jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(actor,idempotency_key)
);
```

在同一迁移中为 outbox 增加状态 CHECK：只允许 `pending`、`retry_wait`、`delivered`、`failed`、`cancelled`。迁移脚本保持项目既有做法，不自行写 `schema_migrations`；owner migration runner 成功提交后登记 `060`。

- [ ] **Step 4: 运行迁移测试确认 GREEN**

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_http_notifications_migration_postgres`

Expected: PASS，且没有 skip。

- [ ] **Step 5: 提交 Schema 契约**

```bash
git add init-db/migration_060_alarm_http_notifications.sql backend/tests/test_alarm_http_notifications_migration_postgres.py
git commit -m "feat(alarm): add HTTP notification schema"
```

---

### Task 2: 实现模板、安全校验和密钥深模块

**Files:**
- Create: `backend/app/services/alarm_http_notifications.py`
- Create: `backend/tests/test_alarm_http_notifications.py`
- Modify: `backend/app/core/config.py:20-150`
- Modify: `.env.example:1-100`
- Modify: `scripts/bootstrap_runtime_secrets.py:110-230`
- Modify: `scripts/test_bootstrap_runtime_secrets.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`、`cryptography.fernet.Fernet`、`Settings`。
- Produces: `HttpNotificationError`、`RequestField`、`HttpNotificationDraft`、`NotificationContext`、`SecretCodec`、`normalize_draft()`、`render_request()`、`send_http_request()`。

- [ ] **Step 1: 写纯模块和 Secret 生成失败测试**

```python
class AlarmHttpNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_json_template_uses_typed_values(self):
        rendered = render_request(
            valid_draft(body_template='{\"value\":{{entity.value}},\"id\":{{notification.id}}}'),
            TEST_CONTEXT,
            SecretCodec(TEST_FERNET_KEY),
        )
        self.assertEqual(json.loads(rendered.body), {"value": 12.5, "id": str(TEST_NOTIFICATION_ID)})

    def test_unknown_variable_is_rejected(self):
        with self.assertRaisesRegex(HttpNotificationError, "HTTP_NOTIFICATION_INVALID_TEMPLATE"):
            normalize_draft(valid_draft(body_template="{{system.password}}"))

    def test_link_local_metadata_target_is_rejected(self):
        with self.assertRaisesRegex(HttpNotificationError, "HTTP_NOTIFICATION_INVALID_URL"):
            normalize_draft(valid_draft(url="http://169.254.169.254/latest/meta-data"))

    async def test_redirect_is_not_followed(self):
        response = await send_http_request(RENDERED, transport=redirect_transport())
        self.assertEqual(response.error_code, "HTTP_NOTIFICATION_DELIVERY_REJECTED")
        self.assertEqual(response.http_status, 302)

    async def test_response_excerpt_is_sanitized_and_bounded(self):
        response = await send_http_request(RENDERED, transport=text_transport("ok\u0000" + "x" * 5000))
        self.assertNotIn("\u0000", response.response_excerpt)
        self.assertLessEqual(len(response.response_excerpt.encode("utf-8")), 4096)
```

并在 `scripts/test_bootstrap_runtime_secrets.py` 断言首次 bootstrap 生成非空 `HTTP_NOTIFICATION_ENCRYPTION_KEY`，重复运行不轮换该值。

- [ ] **Step 2: 运行测试确认 RED**

Run: `cd backend && python -m unittest tests.test_alarm_http_notifications -v && cd .. && python -m unittest scripts.test_bootstrap_runtime_secrets -v`

Expected: 第一条命令 FAIL，提示模块不存在；第二条 FAIL，提示环境变量未生成。

- [ ] **Step 3: 建立固定类型与纯函数**

```python
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ALLOWED_VARIABLES = frozenset({
    "notification.id", "event.id", "event.type", "event.time",
    "alarm.name", "alarm.severity", "alarm.state", "alarm.definition_id", "alarm.rule_key",
    "node.id", "node.name", "node.path",
    "entity.id", "entity.key", "entity.name", "entity.value", "entity.unit",
    "entity.quality", "entity.observed_at",
})

class HttpNotificationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

@dataclass(frozen=True)
class RequestField:
    key: str
    value: str
    sensitive: bool = False

@dataclass(frozen=True)
class HttpNotificationDraft:
    name: str
    description: str | None
    method: str
    url: str
    query_params: Sequence[RequestField]
    headers: Sequence[RequestField]
    content_type: str
    body_template: str
    timeout_seconds: int = 5

@dataclass(frozen=True)
class NotificationContext:
    values: dict[str, object]

@dataclass(frozen=True)
class RenderedHttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes
    timeout_seconds: int
    target_display: str

@dataclass(frozen=True)
class HttpSendResult:
    delivered: bool
    http_status: int | None
    duration_ms: int
    error_code: str | None
    error_detail: str | None
    response_excerpt: str | None

@dataclass(frozen=True)
class StoredHttpNotificationConfig:
    id: UUID
    name: str
    description: str | None
    method: str
    url_display: str
    public_query_params: Sequence[RequestField]
    secret_query_param_names: Sequence[str]
    public_headers: Sequence[RequestField]
    secret_header_names: Sequence[str]
    content_type: str
    body_template: str
    timeout_seconds: int
    current_digest: str
    tested_digest: str | None
    tested_at: datetime | None
    last_test_status: dict[str, object] | None
    enabled: bool

@dataclass(frozen=True)
class ResolvedHttpNotificationConfig:
    id: UUID
    draft: HttpNotificationDraft
    current_digest: str
    tested_digest: str | None
    enabled: bool

def public_config(config: StoredHttpNotificationConfig) -> dict[str, object]:
    return {
        "id": str(config.id), "name": config.name, "description": config.description,
        "method": config.method, "url_display": config.url_display,
        "query_params": public_fields(config.public_query_params, config.secret_query_param_names),
        "headers": public_fields(config.public_headers, config.secret_header_names),
        "content_type": config.content_type, "body_template": config.body_template,
        "timeout_seconds": config.timeout_seconds, "current_digest": config.current_digest,
        "tested_digest": config.tested_digest,
        "tested_at": config.tested_at.isoformat() if config.tested_at else None,
        "last_test_status": config.last_test_status, "enabled": config.enabled,
    }

def public_fields(values: Sequence[RequestField], secret_names: Sequence[str]) -> list[dict[str, object]]:
    visible = [{"key": item.key, "value": item.value, "sensitive": False} for item in values]
    hidden = [{"key": key, "sensitive": True, "configured": True} for key in secret_names]
    return visible + hidden
```

`normalize_draft()` 必须规范化方法和字段键、拒绝重复/保留请求头、检查 1～30 秒、只接收 HTTP(S) 绝对 URL、拒绝 URL userinfo 和文字形式的 link-local/metadata 主机；`render_request()` 必须自动写入 `Idempotency-Key` 与 `X-ZiZu-Notification-Id`，JSON 模板变量按 JSON 值编码，文本模板按 UTF-8 字符串替换；`send_http_request()` 使用 `httpx.AsyncClient(follow_redirects=False, trust_env=False)`，只把 2xx 判为成功，响应摘要清理控制字符并截断至 4096 字符。

网络异常映射固定为：超时→`HTTP_NOTIFICATION_DELIVERY_TIMEOUT`；3xx/4xx/5xx→`HTTP_NOTIFICATION_DELIVERY_REJECTED`；DNS/连接失败→同一 rejected 码并使用不含 URL/凭据的分类详情。配置和模板异常固定使用规格第 9 节列出的 `NOT_FOUND`、`DISABLED`、`NOT_TESTED`、`TEST_STALE`、`INVALID_URL`、`INVALID_TEMPLATE`、`SECRET_KEY_NOT_CONFIGURED`、`DELIVERY_CANCELLED`；不能把原始 exception 字符串直接返回或写日志。

- [ ] **Step 4: 加入独立密钥配置和 bootstrap**

```python
# backend/app/core/config.py
http_notification_encryption_key: str | None = None

# scripts/bootstrap_runtime_secrets.py，bootstrap() 内
http_key = env.get("HTTP_NOTIFICATION_ENCRYPTION_KEY", "") or Fernet.generate_key().decode("ascii")
env_text = replace_env_value(env_text, "HTTP_NOTIFICATION_ENCRYPTION_KEY", http_key)
```

在 `.env.example` 的 JWT 后加入 `HTTP_NOTIFICATION_ENCRYPTION_KEY=` 和中文恢复提示。由于完整 URL 始终加密，`SecretCodec(None)` 只允许列出已脱敏的公开配置；创建、修改、测试、启用或正式投递均抛出 `HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED`，绝不降级明文。

- [ ] **Step 5: 运行纯模块和 Secret 测试确认 GREEN**

Run: `cd backend && python -m unittest tests.test_alarm_http_notifications -v && cd .. && python -m unittest scripts.test_bootstrap_runtime_secrets -v`

Expected: 两组 PASS；测试输出和断言消息不出现测试 URL token 或敏感请求头值。

- [ ] **Step 6: 提交深模块**

```bash
git add backend/app/services/alarm_http_notifications.py backend/tests/test_alarm_http_notifications.py backend/app/core/config.py .env.example scripts/bootstrap_runtime_secrets.py scripts/test_bootstrap_runtime_secrets.py
git commit -m "feat(alarm): add safe HTTP notification core"
```

---

### Task 3: 实现 HTTP 通知配置 CRUD、测试和生命周期 API

**Files:**
- Create: `backend/app/services/alarm_http_notification_postgres.py`
- Create: `backend/app/api/alarm_http_notifications.py`
- Create: `backend/tests/test_alarm_http_notification_public_api.py`
- Create: `backend/tests/test_alarm_http_notification_postgres.py`
- Modify: `backend/app/main.py:290-390`

**Interfaces:**
- Consumes: Task 1 表、Task 2 类型/渲染/发送函数、`SYSTEM_MANAGE` 权限。
- Produces: `AlarmHttpNotifications` 服务及 `/api/v1/admin/alarm-http-notifications` 七个管理端点。

- [ ] **Step 1: 写 API 与 Postgres 生命周期失败测试**

```python
def test_material_edit_disables_configuration_and_invalidates_test(self):
    created = self.create_and_test_config()
    self.enable(created["id"])
    response = self.client.put(
        f"/api/v1/admin/alarm-http-notifications/{created['id']}",
        headers=self.admin_headers,
        json={**self.valid_payload, "timeout_seconds": 6},
    )
    self.assertEqual(response.status_code, 200)
    self.assertFalse(response.json()["enabled"])
    self.assertIsNone(response.json()["tested_at"])

def test_secret_values_are_never_returned(self):
    payload = self.valid_payload | {
        "url": "https://receiver.invalid/hook?token=hidden",
        "headers": [{"key": "Authorization", "value": "Bearer hidden", "sensitive": True}],
    }
    response = self.client.post("/api/v1/admin/alarm-http-notifications", headers=self.admin_headers, json=payload)
    self.assertNotIn("hidden", response.text)
    self.assertEqual(response.json()["headers"][0], {"key": "Authorization", "sensitive": True, "configured": True})

def test_database_does_not_contain_plaintext_url_or_secrets(self):
    self.create_secret_config(url="https://receiver.invalid/hook?token=hidden", authorization="Bearer hidden")
    serialized = self.dump_notification_config_row_as_text()
    self.assertNotIn("token=hidden", serialized)
    self.assertNotIn("Bearer hidden", serialized)

def test_delete_detaches_bindings_and_cancels_unfinished_tasks_atomically(self):
    config_id, definition_id, notification_ids = self.seed_bound_delivery_states()
    self.repository.delete_config(config_id, actor="admin")
    self.assertEqual(self.binding_count(definition_id), 0)
    self.assertEqual(self.delivery_states(notification_ids), ["cancelled", "cancelled", "cancelled", "delivered"])
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `cd backend && python -m unittest tests.test_alarm_http_notification_public_api -v`

Expected: FAIL，提示新路由返回 404。

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_http_notification_postgres`

Expected: FAIL，提示 `PostgresAlarmHttpNotificationRepository` 不存在。

- [ ] **Step 3: 实现服务和仓储接口**

```python
class AlarmHttpNotificationRepository(Protocol):
    def list_configs(self) -> Sequence[StoredHttpNotificationConfig]: raise NotImplementedError
    def get_config(self, config_id: UUID) -> StoredHttpNotificationConfig | None: raise NotImplementedError
    def resolve_config(self, config_id: UUID) -> ResolvedHttpNotificationConfig | None: raise NotImplementedError
    def create_config(self, draft: HttpNotificationDraft, actor: str) -> StoredHttpNotificationConfig: raise NotImplementedError
    def update_config(self, config_id: UUID, patch: HttpNotificationDraft, actor: str) -> StoredHttpNotificationConfig: raise NotImplementedError
    def record_test(self, config_id: UUID, digest: str, result: HttpSendResult, actor: str) -> StoredHttpNotificationConfig: raise NotImplementedError
    def set_enabled(self, config_id: UUID, enabled: bool, actor: str) -> StoredHttpNotificationConfig: raise NotImplementedError
    def delete_config(self, config_id: UUID, actor: str) -> None: raise NotImplementedError

class AlarmHttpNotifications:
    def list(self) -> Sequence[dict[str, object]]: return tuple(public_config(item) for item in self._repository.list_configs())
    def create(self, draft: HttpNotificationDraft, actor: str) -> dict[str, object]: return public_config(self._repository.create_config(normalize_draft(draft), actor))
    def update(self, config_id: UUID, draft: HttpNotificationDraft, actor: str) -> dict[str, object]: return public_config(self._repository.update_config(config_id, normalize_draft(draft), actor))
    async def test(self, config_id: UUID, actor: str) -> dict[str, object]: return await self._test_current_config(config_id, actor)
    def enable(self, config_id: UUID, actor: str) -> dict[str, object]: return public_config(self._repository.set_enabled(config_id, True, actor))
    def disable(self, config_id: UUID, actor: str) -> dict[str, object]: return public_config(self._repository.set_enabled(config_id, False, actor))
    def delete(self, config_id: UUID, actor: str) -> None: self._repository.delete_config(config_id, actor)
```

Postgres 仓储在一次事务内完成删除：删除 bindings；把 `pending`、`retry_wait`、`failed` 更新为 `cancelled`、清空 `configuration_id`、写 `HTTP_NOTIFICATION_DELIVERY_CANCELLED`；最后删除 config。启用时必须同时满足 `tested_digest == current_digest`，否则稳定抛出 `HTTP_NOTIFICATION_NOT_TESTED` 或 `HTTP_NOTIFICATION_TEST_STALE`。材料字段变化后始终 `enabled=false` 并清空测试结果；只修改名称/说明不使测试失效。`resolve_config()` 是唯一解密入口，只供测试发送和正式 dispatcher 使用；测试使用固定 `event.type=TEST` 上下文，不写告警事件、转换或 outbox。

- [ ] **Step 4: 暴露七个受保护端点并注册路由**

```python
router = APIRouter(prefix="/admin/alarm-http-notifications")

@router.get("", **protected(SYSTEM_MANAGE))
def list_configs(): return get_alarm_http_notifications().list()

@router.post("", status_code=201, openapi_extra=capability_metadata(SYSTEM_MANAGE))
def create_config(command: HttpNotificationRequest, principal: Principal = Depends(principal_for(SYSTEM_MANAGE))):
    return get_alarm_http_notifications().create(command.domain(), principal.actor)

@router.put("/{config_id}", openapi_extra=capability_metadata(SYSTEM_MANAGE))
def update_config(config_id: UUID, command: HttpNotificationRequest, principal: Principal = Depends(principal_for(SYSTEM_MANAGE))):
    return get_alarm_http_notifications().update(config_id, command.domain(), principal.actor)

@router.post("/{config_id}/test", openapi_extra=capability_metadata(SYSTEM_MANAGE))
async def test_config(config_id: UUID, principal: Principal = Depends(principal_for(SYSTEM_MANAGE))):
    return await get_alarm_http_notifications().test(config_id, principal.actor)

@router.post("/{config_id}/enable", openapi_extra=capability_metadata(SYSTEM_MANAGE))
def enable_config(config_id: UUID, principal: Principal = Depends(principal_for(SYSTEM_MANAGE))):
    return get_alarm_http_notifications().enable(config_id, principal.actor)

@router.post("/{config_id}/disable", openapi_extra=capability_metadata(SYSTEM_MANAGE))
def disable_config(config_id: UUID, principal: Principal = Depends(principal_for(SYSTEM_MANAGE))):
    return get_alarm_http_notifications().disable(config_id, principal.actor)

@router.delete("/{config_id}", status_code=204, openapi_extra=capability_metadata(SYSTEM_MANAGE))
def delete_config(config_id: UUID, principal: Principal = Depends(principal_for(SYSTEM_MANAGE))):
    get_alarm_http_notifications().delete(config_id, principal.actor)
    return Response(status_code=204)
```

响应只包含 `url_display`、普通字段、敏感字段名和 `configured` 标记。把 `HttpNotificationError.code` 原样放入项目现有 `{detail:{code,message}}` 错误结构；在 `main.py` 以 `/api/v1` 前缀注册。

- [ ] **Step 5: 运行 API、Postgres 和既有权限测试确认 GREEN**

Run: `cd backend && python -m unittest tests.test_alarm_http_notification_public_api -v`

Expected: PASS。

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_http_notification_postgres`

Expected: PASS，且没有 skip。

- [ ] **Step 6: 提交配置后端**

```bash
git add backend/app/services/alarm_http_notification_postgres.py backend/app/api/alarm_http_notifications.py backend/tests/test_alarm_http_notification_public_api.py backend/tests/test_alarm_http_notification_postgres.py backend/app/main.py
git commit -m "feat(alarm): manage HTTP notification configs"
```

---

### Task 4: 将通知配置纳入告警规则计划与原子 apply

**Files:**
- Modify: `backend/app/services/alarm_configuration.py:20-230`
- Modify: `backend/app/services/alarm_configuration_postgres.py:35-430`
- Modify: `backend/app/api/alarm_configurations.py:45-120`
- Modify: `backend/tests/test_alarm_configuration_l2.py`
- Modify: `backend/tests/test_alarm_configuration_l2_postgres.py`

**Interfaces:**
- Consumes: `t_alarm_http_notification_configs`、`t_alarm_http_notification_bindings`。
- Produces: `AlarmRule.http_notification_config_id: UUID | None`，计划摘要和 apply 均携带相同绑定。

- [ ] **Step 1: 写可空绑定、阻断和原子发布失败测试**

```python
def test_rule_without_http_notification_remains_valid(self):
    plan = self.configuration.plan(valid_command(http_notification_config_id=None))
    self.assertEqual(plan.status, "ready")

def test_plan_blocks_disabled_or_stale_http_notification(self):
    plan = self.configuration.plan(valid_command(http_notification_config_id=DISABLED_CONFIG_ID))
    self.assertEqual(plan.status, "blocked")
    self.assertIn("HTTP_NOTIFICATION_DISABLED", plan.blockers)

def test_apply_writes_definition_and_http_binding_in_one_transaction(self):
    result = self.apply_bound_plan(ENABLED_TESTED_CONFIG_ID)
    self.assertEqual(self.binding_for(result.definition_ids[0]), ENABLED_TESTED_CONFIG_ID)
```

- [ ] **Step 2: 运行专项确认 RED**

Run: `cd backend && python -m unittest tests.test_alarm_configuration_l2 -v`

Expected: FAIL，提示 `AlarmRule` 或请求模型不接受 `http_notification_config_id`。

- [ ] **Step 3: 扩展规则类型、序列化和计划校验**

```python
@dataclass(frozen=True)
class AlarmRule:
    id: str
    name: str
    severity: Severity
    trigger: dict[str, Any]
    trigger_duration_seconds: float
    recovery: dict[str, Any]
    recovery_duration_seconds: float
    notification_throttle_seconds: float
    unit: str | None = None
    fault_map_id: UUID | None = None
    http_notification_config_id: UUID | None = None

class AlarmConfigurationRepository(Protocol):
def http_notification_status(self, config_id: UUID) -> tuple[bool, bool] | None: raise NotImplementedError
```

`AlarmRuleRequest` 同步增加 `http_notification_config_id: UUID | None = None`；`_rule_from_json()`、`_json_value()`、`_rule()` 和当前配置响应必须保留该字段。计划时：不存在追加 `HTTP_NOTIFICATION_NOT_FOUND`，停用追加 `HTTP_NOTIFICATION_DISABLED`，摘要失效追加 `HTTP_NOTIFICATION_TEST_STALE`。

- [ ] **Step 4: 在现有 apply 事务内写绑定**

```python
if rule.get("http_notification_config_id"):
    cursor.execute(
        """
        INSERT INTO t_alarm_http_notification_bindings
          (definition_id,configuration_id,created_by)
        VALUES (%s,%s,%s)
        ON CONFLICT(definition_id) DO UPDATE
        SET configuration_id=EXCLUDED.configuration_id,
            created_by=EXCLUDED.created_by,
            created_at=now()
        """,
        (definition_id, UUID(rule["http_notification_config_id"]), actor),
    )
```

内容摘要必须包含绑定 ID；`preserve` 继续引用旧 definition 和旧 binding；`add/update` 写新 definition 后立即写 binding，任一失败回滚整个配置发布。

- [ ] **Step 5: 运行纯服务、公开 API 和 PostgreSQL 测试确认 GREEN**

Run: `cd backend && python -m unittest tests.test_alarm_configuration_l2 -v`

Expected: PASS。

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_configuration_l2_postgres`

Expected: PASS，且没有 skip。

- [ ] **Step 6: 提交规则绑定**

```bash
git add backend/app/services/alarm_configuration.py backend/app/services/alarm_configuration_postgres.py backend/app/api/alarm_configurations.py backend/tests/test_alarm_configuration_l2.py backend/tests/test_alarm_configuration_l2_postgres.py
git commit -m "feat(alarm): bind rules to HTTP notifications"
```

---

### Task 5: 从发生与恢复状态转换创建唯一通知任务

**Files:**
- Modify: `backend/app/services/alarm_runtime.py:80-730`
- Modify: `backend/app/services/alarm_postgres.py:500-590`
- Modify: `backend/tests/test_alarm_runtime.py`
- Create: `backend/tests/test_alarm_runtime_postgres.py`

**Interfaces:**
- Consumes: 定义绑定、Task 1 outbox 字段。
- Produces: 每个 `ALARM_ACTIVATED`/`ALARM_RECOVERED` 转换最多一个稳定 outbox 任务；确认和重复采样不创建。

- [ ] **Step 1: 写状态机通知语义失败测试**

```python
def test_activation_and_recovery_create_paired_notifications(self):
    activated = self.runtime.submit(triggering_observation())
    acknowledged = self.runtime.acknowledge(ack_command(activated.event_id))
    recovered = self.runtime.submit(recovery_observation())
    self.assertFalse(acknowledged.notification_created)
    self.assertTrue(recovered.notification_created)
    self.assertEqual(
        [item.transition_code for item in self.repository.notifications()],
        ["ALARM_ACTIVATED", "ALARM_RECOVERED"],
    )

def test_recovery_without_activation_notification_does_not_create_orphan(self):
    self.catalog.unbind_http_notification(DEFINITION_ID)
    event_id = self.activate_alarm()
    self.assertFalse(self.recover_alarm(event_id).notification_created)

def test_duplicate_transition_id_does_not_duplicate_outbox(self):
    transition = saved_activation_transition()
    self.repository.enqueue_notification(notification_for(transition))
    self.repository.enqueue_notification(notification_for(transition))
    self.assertEqual(len(self.repository.notifications()), 1)
```

- [ ] **Step 2: 运行状态机测试确认 RED**

Run: `cd backend && python -m unittest tests.test_alarm_runtime -v`

Expected: FAIL，恢复没有通知且 `AlarmNotification` 没有转换字段。

- [ ] **Step 3: 给转换稳定 ID，并让 runtime 只写通知意图**

```python
@dataclass(frozen=True)
class AlarmTransition:
    event_id: UUID
    from_state: str | None
    to_state: str
    occurred_at: datetime
    code: str
    evidence: dict[str, Any] | None = None
    actor: str | None = None
    note: str | None = None
    audit_event_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)

@dataclass(frozen=True)
class AlarmNotification:
    id: UUID
    transition_id: UUID
    transition_code: str
    event_id: UUID
    definition_id: UUID
    entity_instance_id: UUID
    configuration_id: UUID
    configuration_name: str
    context_snapshot: dict[str, object]
    created_at: datetime
```

Repository protocol 增加：

```python
def notification_configuration(self, definition_id: UUID) -> tuple[UUID, str] | None: raise NotImplementedError
def has_activation_notification(self, event_id: UUID) -> bool: raise NotImplementedError
```

发生路径把同一个 `AlarmTransition` 实例先 `append_transition()` 再交给 `_notify_activation_if_allowed()`；恢复路径先保存 `ALARM_RECOVERED` 转换，再仅在 `has_activation_notification(event.id)` 且 binding 仍存在时 enqueue。`context_snapshot` 在该事务内由 event、definition 和 transition evidence 生成，包含规格白名单字段；后续重试只替换 HTTP 配置，绝不重新读取变化后的 L2 值。Postgres `append_transition()` 显式插入 `transition.id`；outbox 使用 `ON CONFLICT(transition_id) DO NOTHING`，`next_attempt_at=created_at`。`last_notification_at()` 只统计 `transition_code='ALARM_ACTIVATED'`，恢复任务不能延长下一次发生通知的节流窗口。

- [ ] **Step 4: 运行状态机和 PostgreSQL 测试确认 GREEN**

Run: `cd backend && python -m unittest tests.test_alarm_runtime -v`

Expected: PASS。

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_runtime_postgres`

Expected: PASS，且没有 skip；通知插入与告警转换处于同一事务。

- [ ] **Step 5: 提交状态转换通知意图**

```bash
git add backend/app/services/alarm_runtime.py backend/app/services/alarm_postgres.py backend/tests/test_alarm_runtime.py backend/tests/test_alarm_runtime_postgres.py
git commit -m "feat(alarm): enqueue transition notifications"
```

---

### Task 6: 实现提交后领取、投递、重试和重启恢复

**Files:**
- Modify: `backend/app/services/alarm_http_notifications.py`
- Modify: `backend/app/services/alarm_http_notification_postgres.py`
- Modify: `backend/tests/test_alarm_http_notifications.py`
- Modify: `backend/tests/test_alarm_http_notification_postgres.py`
- Modify: `backend/app/main.py:160-280`
- Create: `backend/tests/test_alarm_http_notification_startup.py`

**Interfaces:**
- Consumes: committed outbox、当前 config、Task 2 `render_request()`/`send_http_request()`。
- Produces: `AlarmHttpNotificationDispatcher.run_once(now) -> int`，单独后台循环，固定重试和租约恢复。

- [ ] **Step 1: 写领取、动态配置、停用和重试失败测试**

```python
async def test_retry_schedule_and_final_failure(self):
    dispatcher = dispatcher_with_results(500, 500, 500, 500)
    await dispatcher.run_once(NOW)
    self.assert_delivery("retry_wait", cycle=1, next_at=NOW + timedelta(seconds=5))
    await dispatcher.run_once(NOW + timedelta(seconds=5))
    self.assert_delivery("retry_wait", cycle=2, next_at=NOW + timedelta(seconds=35))
    await dispatcher.run_once(NOW + timedelta(seconds=35))
    self.assert_delivery("retry_wait", cycle=3, next_at=NOW + timedelta(seconds=335))
    await dispatcher.run_once(NOW + timedelta(seconds=335))
    self.assert_delivery("failed", cycle=4, next_at=None)

async def test_retry_reads_current_configuration(self):
    await self.dispatcher.run_once(NOW)
    self.repository.update_target(CONFIG_ID, "https://new.invalid/hook")
    await self.dispatcher.run_once(NOW + timedelta(seconds=5))
    self.assertEqual(self.transport.targets, ["https://old.invalid/hook", "https://new.invalid/hook"])

async def test_disabled_configuration_waits_without_attempt(self):
    self.repository.disable(CONFIG_ID)
    self.assertEqual(await self.dispatcher.run_once(NOW), 0)
    self.assertEqual(self.repository.delivery(NOTIFICATION_ID).attempt_count, 0)
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `cd backend && python -m unittest tests.test_alarm_http_notifications tests.test_alarm_http_notification_startup -v`

Expected: FAIL，提示 dispatcher 不存在或没有告警通知后台任务。

- [ ] **Step 3: 实现领取与状态落库协议**

```python
class AlarmDeliveryRepository(Protocol):
    def claim_due(self, *, worker_id: str, now: datetime, lease_seconds: int = 30) -> DeliveryClaim | None: raise NotImplementedError
    def current_config(self, config_id: UUID) -> ResolvedHttpNotificationConfig | None: raise NotImplementedError
    def complete_attempt(self, claim: DeliveryClaim, result: HttpSendResult, now: datetime) -> None: raise NotImplementedError
    def release_lease(self, notification_id: UUID, worker_id: str) -> None: raise NotImplementedError

class AlarmHttpNotificationDispatcher:
    RETRY_DELAYS = (5, 30, 300)

    async def run_once(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        claim = self._repository.claim_due(worker_id=self._worker_id, now=current)
        if claim is None:
            return 0
        config = self._repository.current_config(claim.configuration_id)
        if config is None:
            self._repository.cancel_missing_config(claim, current)
            return 1
        if not config.enabled or config.tested_digest != config.current_digest:
            self._repository.release_lease(claim.id, self._worker_id)
            return 0
        result = await self._sender(render_request(config.draft, claim.context, self._codec))
        self._repository.complete_attempt(claim, result, current)
        return 1
```

`DeliveryClaim` 在同一模块定义为固定快照，只承载告警事实，不承载 HTTP 配置：

```python
@dataclass(frozen=True)
class DeliveryClaim:
    id: UUID
    transition_id: UUID
    transition_code: str
    event_id: UUID
    configuration_id: UUID
    context: NotificationContext
    attempt_count: int
    cycle_attempt_count: int
    lease_owner: str
```

dispatcher 使用 `config.draft` 调用 `render_request()`；不得调用不存在的 `to_draft()`。动态配置语义只替换请求配置，`DeliveryClaim.context` 中已提交的告警、节点、实体和值证据不可变。

`claim_due()` 用单事务 `FOR UPDATE SKIP LOCKED`，只领取配置仍存在、启用、测试摘要有效且到期的 `pending/retry_wait`；租约过期可被另一个进程重新领取。`complete_attempt()` 在一个事务追加 attempt、累加总数和本轮次数、清除租约；成功写 `delivered`，前三次失败按固定延迟写 `retry_wait`，第四次写 `failed`。渲染错误也计为一次失败。尝试表只写脱敏目标和清理后的结果。

- [ ] **Step 4: 把 dispatcher 接入现有 lifespan**

```python
alarm_http_dispatcher = build_postgres_alarm_http_notification_dispatcher()

async def _alarm_http_notification_loop() -> None:
    while not _data_trunk_stop.is_set():
        try:
            delivered = await alarm_http_dispatcher.run_once()
        except Exception as error:
            logger.warning("[AlarmHTTP] delivery tick failed: {}", type(error).__name__)
            delivered = 0
        if delivered == 0:
            await _wait_or_stop(0.5)

_data_trunk_tasks.append(
    asyncio.create_task(_alarm_http_notification_loop(), name="alarm_http_notification")
)
```

日志只能包含任务 ID、稳定错误码和脱敏目标；不可打印 exception 字符串、完整 URL、请求头或请求体。生产缺少加密密钥时只令需要解密的配置失败关闭，不得阻止采集与告警主链启动。

- [ ] **Step 5: 运行纯模块、启动和 PostgreSQL 测试确认 GREEN**

Run: `cd backend && python -m unittest tests.test_alarm_http_notifications tests.test_alarm_http_notification_startup -v`

Expected: PASS。

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_http_notification_postgres`

Expected: PASS，且没有 skip；并发 worker 只产生一次 attempt，过期租约可恢复。

- [ ] **Step 6: 提交 dispatcher**

```bash
git add backend/app/services/alarm_http_notifications.py backend/app/services/alarm_http_notification_postgres.py backend/tests/test_alarm_http_notifications.py backend/tests/test_alarm_http_notification_postgres.py backend/app/main.py backend/tests/test_alarm_http_notification_startup.py
git commit -m "feat(alarm): deliver HTTP notifications asynchronously"
```

---

### Task 7: 暴露通知记录和手工重发 API

**Files:**
- Create: `backend/app/api/alarm_notification_deliveries.py`
- Create: `backend/tests/test_alarm_notification_deliveries_public_api.py`
- Modify: `backend/app/services/alarm_http_notification_postgres.py`
- Modify: `backend/app/main.py:330-390`

**Interfaces:**
- Consumes: 通知 outbox、attempts、告警展示查询、`RUNTIME_READ` 和告警配置写权限。
- Produces: `GET /api/v1/alarms/notification-deliveries`、`POST /api/v1/alarms/notification-deliveries/{id}/retry`。

- [ ] **Step 1: 写脱敏列表和手工重发失败测试**

```python
def test_operator_can_list_redacted_delivery_history(self):
    response = self.client.get("/api/v1/alarms/notification-deliveries", headers=self.operator_headers)
    self.assertEqual(response.status_code, 200)
    self.assertNotIn("secret-token", response.text)
    self.assertEqual(response.json()["items"][0]["status"], "failed")

def test_manual_retry_reopens_failed_delivery_without_changing_alarm(self):
    before = self.event_state(EVENT_ID)
    response = self.client.post(
        f"/api/v1/alarms/notification-deliveries/{DELIVERY_ID}/retry",
        headers={**self.configurer_headers, "Idempotency-Key": "retry-once"},
    )
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["status"], "pending")
    self.assertEqual(self.event_state(EVENT_ID), before)
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `cd backend && python -m unittest tests.test_alarm_notification_deliveries_public_api -v`

Expected: FAIL，两个路由返回 404。

- [ ] **Step 3: 实现分页查询和重发命令**

```python
router = APIRouter(prefix="/alarms/notification-deliveries")

@router.get("", **protected(RUNTIME_READ))
def list_deliveries(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    return get_alarm_http_notifications().list_deliveries(page=page, page_size=page_size)

@router.post("/{notification_id}/retry", openapi_extra=capability_metadata(CONFIGURATION_WRITE))
def retry_delivery(
    notification_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
):
    return get_alarm_http_notifications().retry(notification_id, principal.actor, idempotency_key)
```

列表返回 `id,event_id,event_type,alarm_name,severity,node_name,entity_name,configuration_name,target_display,status,attempt_count,last_http_status,last_error_code,last_error_detail,last_response_excerpt,created_at,delivered_at,cancelled_at,attempts`。手工重发只允许 `failed` 且 configuration 仍存在：保留 `attempt_count`，把 `cycle_attempt_count=0,status='pending',next_attempt_at=now()`；同一 actor/idempotency key 重放返回同一结果。配置被删时返回 `HTTP_NOTIFICATION_NOT_FOUND`。

- [ ] **Step 4: 注册路由并运行权限测试确认 GREEN**

Run: `cd backend && python -m unittest tests.test_alarm_notification_deliveries_public_api -v`

Expected: PASS；操作员只能查看，实施工程师可重发，未授权用户为 403。

- [ ] **Step 5: 提交通知记录 API**

```bash
git add backend/app/api/alarm_notification_deliveries.py backend/app/services/alarm_http_notification_postgres.py backend/app/main.py backend/tests/test_alarm_notification_deliveries_public_api.py
git commit -m "feat(alarm): expose notification delivery history"
```

---

### Task 8: 在系统工具提供 HTTP 通知配置界面

**Files:**
- Modify: `frontend/src/api/client.ts:1-1650`
- Create: `frontend/src/components/admin/alarmHttpNotificationModel.ts`
- Create: `frontend/src/components/admin/alarmHttpNotificationModel.test.mjs`
- Create: `frontend/src/components/admin/AlarmHttpNotificationPanel.tsx`
- Modify: `frontend/src/components/AdminPanel.tsx:1-300`

**Interfaces:**
- Consumes: Task 3 七个管理 API。
- Produces: 系统工具“HTTP 通知”卡片，完整 CRUD、测试、启停、脱敏编辑、变量插入和预览。

- [ ] **Step 1: 写表单状态与中文错误失败测试**

```javascript
test('material edits disable and require a fresh test', () => {
  const next = applyHttpNotificationEdit(enabledTestedDraft, { timeout_seconds: 6 })
  assert.equal(next.enabled, false)
  assert.equal(next.tested_digest, null)
})

test('stable backend errors have actionable Chinese copy', () => {
  assert.equal(
    describeHttpNotificationError('HTTP_NOTIFICATION_TEST_STALE'),
    '请求内容已修改，请重新发送测试，成功后再启用。',
  )
})
```

- [ ] **Step 2: 运行模型测试确认 RED**

Run: `cd frontend && node --test src/components/admin/alarmHttpNotificationModel.test.mjs`

Expected: FAIL，提示模型文件或导出不存在。

- [ ] **Step 3: 实现前端契约与纯模型**

```typescript
export interface HttpNotificationField {
  key: string
  value?: string
  sensitive: boolean
  configured?: boolean
  clear?: boolean
}

export interface AlarmHttpNotificationConfig {
  id: string
  name: string
  description: string | null
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  url_display: string
  query_params: HttpNotificationField[]
  headers: HttpNotificationField[]
  content_type: string
  body_template: string
  timeout_seconds: number
  current_digest: string
  tested_digest: string | null
  tested_at: string | null
  last_test_status: HttpNotificationTestResult | null
  enabled: boolean
}
```

在 `client.ts` 增加 `fetch/create/update/test/enable/disable/deleteAlarmHttpNotification`。纯模型固定暴露 `HTTP_NOTIFICATION_VARIABLES`、`applyHttpNotificationEdit()`、`buildMaskedPreview()`、`describeHttpNotificationError()`；预览不得把 `configured=true` 的敏感值还原。

- [ ] **Step 4: 实现专用面板并嵌入 AdminPanel**

```tsx
<section className="neu-card p-4" aria-label="HTTP 通知">
  <header className="flex items-center justify-between">
    <div>
      <h3 className="text-sm font-bold text-gray-800">HTTP 通知</h3>
      <p className="text-xs text-gray-500">告警发生或恢复后，向指定地址发送 HTTP 请求。</p>
    </div>
    <button onClick={startCreate}>新增通知</button>
  </header>
  <NotificationConfigList />
  {editing && <NotificationRequestEditor />}
</section>
```

编辑器按规格依次呈现基础字段、查询参数、请求头、请求体模板/变量按钮、脱敏预览、测试结果；敏感输入留空保持、明确“清除”才删除。测试成功后才允许启用；删除显示“将解除规则绑定并取消未完成通知”的二次确认。所有失败显示稳定中文提示，不显示泛化“请求未完成”。

- [ ] **Step 5: 运行模型测试与生产构建确认 GREEN**

Run: `cd frontend && node --test src/components/admin/alarmHttpNotificationModel.test.mjs && npm run build`

Expected: 模型测试 PASS；TypeScript 和 Vite build PASS。

- [ ] **Step 6: 提交系统工具 UI**

```bash
git add frontend/src/api/client.ts frontend/src/components/admin/alarmHttpNotificationModel.ts frontend/src/components/admin/alarmHttpNotificationModel.test.mjs frontend/src/components/admin/AlarmHttpNotificationPanel.tsx frontend/src/components/AdminPanel.tsx
git commit -m "feat(ui): manage alarm HTTP notifications"
```

---

### Task 9: 在告警规则中选择一个通知配置

**Files:**
- Modify: `frontend/src/api/client.ts:1460-1580`
- Modify: `frontend/src/components/alarm-configuration/alarmConfigurationContracts.ts`
- Modify: `frontend/src/components/alarm-configuration/alarmConfigurationContracts.test.mjs`
- Modify: `frontend/src/pages/MinimalAlarmRulesPage.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/admin/alarm-http-notifications` 和扩展后的告警规则 API。
- Produces: 规则草稿完整保存 `http_notification_config_id`，下拉只列启用且测试有效配置。

- [ ] **Step 1: 写新建、编辑和序列化保持绑定失败测试**

```javascript
test('editing a rule preserves its HTTP notification binding', () => {
  const draft = ruleToDraft(Object.assign({}, savedRule, { http_notification_config_id: 'config-1' }))
  assert.equal(draft.http_notification_config_id, 'config-1')
  assert.equal(draftToRequest(draft).http_notification_config_id, 'config-1')
})

test('new rules default to no notification', () => {
  assert.equal(createEmptyRuleDraft().http_notification_config_id, null)
})
```

- [ ] **Step 2: 运行契约测试确认 RED**

Run: `cd frontend && node --test src/components/alarm-configuration/alarmConfigurationContracts.test.mjs`

Expected: FAIL，草稿中没有 `http_notification_config_id`。

- [ ] **Step 3: 扩展 TS 规则契约并加入下拉框**

```tsx
<label className="space-y-1">
  <span className="text-xs text-gray-600">HTTP 通知（可选）</span>
  <select
    value={draft.http_notification_config_id ?? ''}
    onChange={(event) => updateDraft({
      http_notification_config_id: event.target.value || null,
    })}
  >
    <option value="">不发送 HTTP 通知</option>
    {availableHttpNotifications.map((config) => (
      <option key={config.id} value={config.id}>{config.name}</option>
    ))}
  </select>
</label>
```

页面加载时获取配置并筛选 `enabled && tested_digest === current_digest`。后端返回 `NOT_FOUND/DISABLED/TEST_STALE` 时在规则行显示可执行中文原因；选择通知仍走现有试算→计划→摘要→apply，不添加直接保存按钮。

- [ ] **Step 4: 运行契约测试和构建确认 GREEN**

Run: `cd frontend && node --test src/components/alarm-configuration/alarmConfigurationContracts.test.mjs && npm run build`

Expected: PASS。

- [ ] **Step 5: 提交规则选择 UI**

```bash
git add frontend/src/api/client.ts frontend/src/components/alarm-configuration/alarmConfigurationContracts.ts frontend/src/components/alarm-configuration/alarmConfigurationContracts.test.mjs frontend/src/pages/MinimalAlarmRulesPage.tsx
git commit -m "feat(ui): bind alarm rules to HTTP notifications"
```

---

### Task 10: 在告警中心展示通知记录并支持重发

**Files:**
- Create: `frontend/src/components/alarm-center/alarmNotificationModel.ts`
- Create: `frontend/src/components/alarm-center/alarmNotificationModel.test.mjs`
- Create: `frontend/src/components/alarm-center/AlarmNotificationRecords.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/AlarmCenterPage.tsx:220-280`

**Interfaces:**
- Consumes: Task 7 的列表和重发 API。
- Produces: “通知记录”页签、状态/错误中文说明、尝试历史和最终失败重发。

- [ ] **Step 1: 写状态展示和重发资格失败测试**

```javascript
test('delivery states have plain Chinese labels', () => {
  assert.equal(describeDeliveryStatus('retry_wait'), '等待重试')
  assert.equal(describeDeliveryStatus('delivered'), '已送达')
  assert.equal(describeDeliveryStatus('cancelled'), '已取消')
})

test('only failed deliveries with an existing config can retry', () => {
  assert.equal(canRetryDelivery({ status: 'failed', configuration_exists: true }), true)
  assert.equal(canRetryDelivery({ status: 'failed', configuration_exists: false }), false)
  assert.equal(canRetryDelivery({ status: 'retry_wait', configuration_exists: true }), false)
})
```

- [ ] **Step 2: 运行模型测试确认 RED**

Run: `cd frontend && node --test src/components/alarm-center/alarmNotificationModel.test.mjs`

Expected: FAIL，模型导出不存在。

- [ ] **Step 3: 实现记录组件和页签**

```tsx
const [tab, setTab] = useState<'events' | 'notifications' | 'rules'>('events')

<button onClick={() => setTab('notifications')} className={tabClass('notifications')}>
  通知记录
</button>

{tab === 'events' && <CurrentAlarmView />}
{tab === 'notifications' && <AlarmNotificationRecords canRetry={canConfigure} />}
{tab === 'rules' && canConfigure && <MinimalAlarmRulesPage key={actorId} />}
```

记录表显示告警名称、发生/恢复、节点、实体、脱敏目标、状态、尝试次数、HTTP 状态、稳定错误和时间；展开行显示每次尝试。只有 `failed && configuration_exists && canRetry` 显示“重新发送”。重发使用新 UUID 作为 `Idempotency-Key`，成功后重新加载，不改告警事件展示。

- [ ] **Step 4: 运行模型测试和构建确认 GREEN**

Run: `cd frontend && node --test src/components/alarm-center/alarmNotificationModel.test.mjs && npm run build`

Expected: PASS。

- [ ] **Step 5: 提交通知记录 UI**

```bash
git add frontend/src/components/alarm-center/alarmNotificationModel.ts frontend/src/components/alarm-center/alarmNotificationModel.test.mjs frontend/src/components/alarm-center/AlarmNotificationRecords.tsx frontend/src/api/client.ts frontend/src/pages/AlarmCenterPage.tsx
git commit -m "feat(ui): show alarm notification deliveries"
```

---

### Task 11: 增加无头闭环和临时 HTTP 接收器

**Files:**
- Create: `backend/scripts/alarm_http_test_receiver.py`
- Create: `backend/scripts/alarm_http_notification_e2e_fixture.py`
- Create: `backend/tests/test_alarm_http_test_receiver.py`
- Create: `backend/tests/test_alarm_http_notification_e2e_fixture.py`
- Create: `frontend/e2e/alarm-http-notification.spec.ts`
- Create: `frontend/e2e/support/alarmHttpNotificationFixture.ts`
- Modify: `frontend/package.json`

**Interfaces:**
- Consumes: 现有安全 E2E 根 `E2E验证`、节点/L0/L1/L2 fixture、通知配置/规则/记录 UI。
- Produces: 无设备写的告警发生→HTTP 收到→现场恢复→HTTP 收到闭环证据和清理机制。

- [ ] **Step 1: 写接收器和 E2E 静态发现失败测试**

```python
def test_receiver_deduplicates_by_idempotency_key(self):
    first = self.post_json("/hook", {"type": "ALARM_ACTIVATED"}, key="same-id")
    second = self.post_json("/hook", {"type": "ALARM_ACTIVATED"}, key="same-id")
    self.assertEqual(first.status, 204)
    self.assertEqual(second.status, 204)
    self.assertEqual(len(self.receiver.records()), 1)

def test_force_due_refuses_non_e2e_notification(self):
    with self.assertRaisesRegex(ValueError, "refusing non-E2E notification"):
        validate_force_due_candidate(
            notification_id=uuid4(),
            alarm_name="现场真实告警",
            target_display="https://business.example/hook",
            expected_run_id="run-1",
        )
```

```typescript
test('发生和恢复各产生一条 HTTP 通知', async ({ page, request }) => {
  await publishRawPoint(fixtureNames().bitTag, 1)
  await expect.poll(() => notificationDeliveryTypes(request)).toContain('ALARM_ACTIVATED')
  await publishRawPoint(fixtureNames().bitTag, 0)
  await expect.poll(() => notificationDeliveryTypes(request)).toContain('ALARM_RECOVERED')
})
```

- [ ] **Step 2: 运行测试确认 RED**

Run: `cd backend && python -m unittest tests.test_alarm_http_test_receiver tests.test_alarm_http_notification_e2e_fixture -v`

Expected: FAIL，接收器模块不存在。

Run: `cd frontend && npx playwright test e2e/alarm-http-notification.spec.ts --list`

Expected: FAIL，spec 不存在。

- [ ] **Step 3: 实现标准库临时接收器与 fixture 命令**

```python
class ReceiverHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        key = self.headers.get("Idempotency-Key")
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.record_once(key, self.path, body)
        self.send_response(self.server.response_status)
        self.end_headers()
```

接收器只绑定显式 `--host/--port`，支持 `/records` 查询和 `--response-status/--delay-seconds` 故障注入，不落盘、不打印 body。独立 fixture 提供 `setup`、`receiver-status`、`force-due` 和 `cleanup`：复用现有节点 E2E fixture，只在 `E2E验证` 根下建立临时 L2/规则/配置，清理配置、规则绑定、临时节点和接收记录；不调用控制或 Neuron 写接口。`force-due` 先验证 notification UUID 属于本轮 E2E 规则且配置目标是 `127.0.0.1:19091`，然后只把该任务 `next_attempt_at` 推到数据库当前时间，不改状态、次数、结果或告警；这样现场验收固定的 5 分钟重试语义而不实际空等 5 分钟。

- [ ] **Step 4: 完成无头用例与脚本入口**

```json
{
  "test:e2e:alarm-http": "playwright test e2e/alarm-http-notification.spec.ts",
  "test:e2e:alarm-http:list": "playwright test e2e/alarm-http-notification.spec.ts --list"
}
```

用例必须覆盖：创建→测试→启用配置；规则绑定并 apply；发布安全模拟 L0 触发和恢复；接收器看到相同 event 的 `ALARM_ACTIVATED/ALARM_RECOVERED` 且幂等键不同；确认动作不产生通知；模拟 500 和超时可见重试/failed；修改 URL 后先证明自动停用，再测试并重新启用，下一次重试命中新接收路径；手工重发后 delivered；删除配置后规则下拉解绑、未完成任务 cancelled；finally 二次清理。

- [ ] **Step 5: 运行接收器测试、E2E 静态发现和前端构建确认 GREEN**

Run: `cd backend && python -m unittest tests.test_alarm_http_test_receiver tests.test_alarm_http_notification_e2e_fixture -v`

Expected: PASS。

Run: `cd frontend && npm run test:e2e:alarm-http:list && npm run build`

Expected: Playwright 列出全部 HTTP 通知用例；build PASS。

- [ ] **Step 6: 提交验收工具**

```bash
git add backend/scripts/alarm_http_test_receiver.py backend/scripts/alarm_http_notification_e2e_fixture.py backend/tests/test_alarm_http_test_receiver.py backend/tests/test_alarm_http_notification_e2e_fixture.py frontend/e2e/alarm-http-notification.spec.ts frontend/e2e/support/alarmHttpNotificationFixture.ts frontend/package.json
git commit -m "test(alarm): cover HTTP notification delivery"
```

---

### Task 12: 完整回归、文档、v0.7.4 发布和 1 号机验收

**Files:**
- Modify: `README.md`
- Create: `docs/deploy-1号机-v0.7.4-http.md`
- Modify: `VERSION`
- Modify: `backend/app/VERSION`
- Modify: `backend/pyproject.toml`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Consumes: Tasks 1～11 完整功能。
- Produces: 可恢复的 v0.7.4 固定摘要部署、公开接口/密钥说明、真实无头验收证据。

- [ ] **Step 1: 写公开配置和恢复说明**

```markdown
### 告警 HTTP 通知

系统工具 → HTTP 通知维护请求；告警规则可选一个已测试、已启用配置。告警发生和现场恢复在数据库提交后异步发送，HTTP 失败不改变告警状态。

`HTTP_NOTIFICATION_ENCRYPTION_KEY` 必须随数据库备份独立备份并一同恢复。丢失该密钥后，ZiZu 不会明文降级；管理员必须重新录入 URL 和敏感字段、重新测试并启用配置。
```

README 同时列出 `/api/v1/admin/alarm-http-notifications` 和 `/api/v1/alarms/notification-deliveries`，说明 2xx、固定重试、至少一次、幂等键、当前配置重试语义和删除取消语义。

- [ ] **Step 2: 运行新鲜完整门禁**

Run: `cd backend && python -m unittest discover -s tests -p "test_*.py" -v`

Expected: 全部非现场依赖测试 PASS；只有项目已记录的环境测试允许 skip，新通知专项不得 skip。

Run: `cd backend && python tests/run_postgres_group.py tests.test_alarm_http_notifications_migration_postgres tests.test_alarm_http_notification_postgres tests.test_alarm_configuration_l2_postgres tests.test_alarm_runtime_postgres`

Expected: 全部 PASS，0 skip。

Run: `python -m unittest discover -s scripts -p "test_*.py" -v`

Expected: PASS。

Run（PowerShell）:

```powershell
Set-Location frontend
$modelTests = Get-ChildItem src,e2e/support -Recurse -Filter *.test.mjs | ForEach-Object FullName
node --test $modelTests
npm run build
npm run test:e2e:node:list
npm run test:e2e:alarm-http:list
```

Expected: 所有模型测试 PASS；production build PASS；两个 Playwright 集合均可发现且没有 `.skip`。

- [ ] **Step 3: 升版并提交发布候选**

Run: `python scripts/bump_version.py`

Expected: `VERSION`、`backend/app/VERSION`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/package-lock.json` 均为 `0.7.4`。

```bash
git add README.md VERSION backend/app/VERSION backend/pyproject.toml frontend/package.json frontend/package-lock.json
git commit -m "chore(release): prepare v0.7.4"
```

- [ ] **Step 4: 推送、等待 CI 并锁定 ARM64 镜像摘要**

Run: `git push origin HEAD:main && gh workflow run release-images.yml -f platform_version=0.7.4 -f edge_proxy_image=caddy@sha256:5f5ec15bf0e3973236fb0b3202d17e7919e0e445cf3a3dbe24f27a0c8a04d206`

Expected: push 成功并启动一次 `Build immutable release images` 工作流。

Run（PowerShell）:

```powershell
$releaseRun = gh run list --workflow release-images.yml --branch main --event workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $releaseRun --exit-status
New-Item -ItemType Directory -Force .release-artifacts/v0.7.4 | Out-Null
gh run download $releaseRun --name zizu-release-0.7.4 --dir .release-artifacts/v0.7.4
python scripts/release_preflight.py verify --release .release-artifacts/v0.7.4/release.json --migrations-dir init-db
```

Expected: 工作流成功；`release.json` 精确记录 0.7.4、Schema 060 和单一 ARM64 digest。当前 HTTP/无 Caddy 测试站已硬切删除旧 `t_release_locks`，所以本次“发布锁定”以 CI 生成并校验的不可变 `release.json` 加部署证据完成，不调用仍依赖旧解决方案表与 HTTPS edge 的 `record_release_lock.py`。

- [ ] **Step 5: 备份并部署 1 号机**

先在 1 号机把数据库、release.env 和 `HTTP_NOTIFICATION_ENCRYPTION_KEY` 纳入同一恢复清单；用 `pg_restore -l` 验证 dump 可读。运行 Schema 060 owner migration，确认当前 Schema 为 060 后按现有 `scripts/deploy_1号机.py`/发布锁流程切换固定摘要。保持：

```yaml
network_mode: host
tmpfs:
  - /dev/mqueue
```

Expected: `/api/v1/health` healthy；版本 `0.7.4`；Schema `060`；容器 restart 0；TimescaleDB/NanoMQ 不因应用切换而重建。

- [ ] **Step 6: 用无头浏览器沿主干和通知闭环验收**

先通过现有已校验 SSH 通道把 `backend/scripts/alarm_http_test_receiver.py` 暂存为 1 号机 `/tmp/alarm_http_test_receiver.py`，并在 1 号机执行：

```bash
python3 /tmp/alarm_http_test_receiver.py --host 127.0.0.1 --port 19091
```

验收配置只指向 `http://127.0.0.1:19091/hook`；因为 backend 使用 host network，这个地址不会开放新的公网监听。

Run: `cd frontend && npm run test:e2e:node`

Environment: `ZIZU_E2E_BASE_URL=http://e606.hlszh.com:9000`、授权测试账号、`ZIZU_E2E_ALLOW_LIVE_WRITES=1`、`ZIZU_E2E_WRITE_ROOT=E2E验证`。

Expected: 现有节点管理主干全部 PASS，覆盖 `节点树 → L0 → L1 → L2 → 告警`。

Run: `cd frontend && npm run test:e2e:alarm-http`

Expected: HTTP 通知全部 PASS；临时接收器证明发生/恢复、幂等键、500/超时重试、最终失败和手工重发；确认不通知；告警状态不因投递失败改变；测试资源二次清理为 0。

最后在同一 SSH 通道执行 `curl -fsS http://127.0.0.1:19091/records` 保存脱敏接收摘要，终止临时接收器并删除 `/tmp/alarm_http_test_receiver.py`；这是明确的验收临时文件，不属于平台运行数据。

- [ ] **Step 7: 记录不可伪造的部署证据并提交**

`docs/deploy-1号机-v0.7.4-http.md` 必须写入 commit、workflow run、固定 digest、image ID、备份路径/大小/SHA-256/`pg_restore -l` 项数、Schema、health、restart、近 10 分钟错误日志计数、两个无头测试实际耗时/通过数/清理结果、未执行控制/设备写/自动策略声明。`CODEX_HANDOFF.md` 同步记录现状和下一步。

```bash
git add docs/deploy-1号机-v0.7.4-http.md CODEX_HANDOFF.md
git commit -m "docs(deploy): record v0.7.4 HTTP notification acceptance"
git push origin HEAD:main
```

Expected: 工作树仅保留用户原有未跟踪 `.release-artifacts/`；不把其中内容加入提交。
