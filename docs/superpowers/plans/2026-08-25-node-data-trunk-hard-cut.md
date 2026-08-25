# 节点数据主干硬切实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次性删除“解决方案交付”、设备实例和统一验收等平行产品层，把 ZiZu 收敛为可直接配置交付光储充 EMS 的唯一主线：`节点 → L0 原始点位 → L1 点位加工 → L2 全局实体 → 告警 / JDM / 控制 / 固定 EMS 工作台`。

**Architecture:** 节点是 L0、已安装 L1 和 L2 的唯一归属；L1 以单个规范 JSON 模板完成预览和原子发布；L2 以稳定 UUID、质量、时间戳和来源证据作为所有上层模块的唯一数据接口。配置安全由无界面的通用配置修订/审计事务提供，不再由解决方案包、站点版本或验收报告承载。数据库通过单个 Schema 044 事务硬切，任何实体归属、模板还原或上层引用无法对账都在删除前失败。

**Tech Stack:** Python 3.13、FastAPI、Pydantic、PostgreSQL/TimescaleDB、React 18、TypeScript、Vite、Docker Compose/GHCR。

**Spec:** `docs/superpowers/specs/2026-08-25-solution-delivery-hard-cut-design.md`

## Global Constraints

- 实施前先保存当前 `v0.4.84-business-metric-templates` 未提交 WIP；不得覆盖或丢弃现有用户改动。保存仅用于可恢复，不把独立业务指标继续带入产品。
- 本计划不新增依赖，不建立通用 DI 容器、配置中心、验收框架、页面设计器或第二套规则引擎。
- Schema 044 只接受完整 Schema 043；fresh、043→044、044 replay 和 partial/mixed 拒绝都必须有真实 PostgreSQL 测试。
- 不修改既有 001–043 migration；硬切、数据搬迁和删除全部在 `migration_044_node_data_trunk_hard_cut.sql` 中原子完成。
- 必须保留节点、L0、L2、告警、JDM、控制、用户和系统配置的稳定 UUID/事实；L2 实体 UUID 在品牌/L1 修订切换后不变。
- 配置发布才增加配置修订；模板导入、遥测、告警状态变化、JDM 运行和控制执行不增加修订。
- L0 只供诊断和 L1 输入；告警、JDM、控制和工作台只消费 L2。
- 每项行为先 RED、后最小 GREEN；每个任务独立提交。删除类任务先以测试/静态扫描证明目标边界，再删除。
- 最终现场验证只读；不执行自动策略、不下发设备控制、不修改 Neuron、不启动 Caddy、不申请 TLS。

## Execution Prerequisite: 保存当前 WIP 并建立隔离分支

这一步由执行者完成，不混入产品提交：

```powershell
git status --short
git diff --binary | Out-File -Encoding utf8 ..\zizu-v0.4.84-business-metric-wip.patch
git diff --check
```

核对补丁文件非空且能在临时 clone 中 `git apply --check`。随后使用 `superpowers:using-git-worktrees` 从规格提交 `ca28a29` 建立独立工作树和 `ticket/v0.4.85-node-data-trunk-hard-cut` 分支；不要清理当前工作树。若执行时已存在更新的、明确包含规格但不含独立业务指标 WIP 的基线提交，则记录该提交摘要并以它为基线。

---

### Task 1: 用 Schema 044 建立直接节点归属和通用配置修订

**Files:**
- Create: `init-db/migration_044_node_data_trunk_hard_cut.sql`
- Create: `backend/tests/test_node_data_trunk_hard_cut_migration_postgres.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`
- Modify: `scripts/test_build_release_images.py`

**Interfaces:**
- Consumes: 完整 Schema 043。
- Produces: `t_configuration_state`、`t_configuration_revisions`、`t_configuration_audit`；`t_entity_instances.node_id`；去解决方案化的点位加工计划、安装与 L2 lineage。
- Rejects: `HARD_CUT_ENTITY_NODE_AMBIGUOUS`、`HARD_CUT_TEMPLATE_UNRECOVERABLE`、`HARD_CUT_UPPER_REFERENCE_LOSS`、`SCHEMA_044_PARTIAL_STRUCTURE`。

- [ ] **Step 1: 写迁移 RED，先锁定保留、转换和拒绝行为**

```python
def test_044_preserves_l2_uuid_and_moves_ownership_to_node(self):
    before = self.seed_043_point_processing_site()
    self.apply_044()
    self.cursor.execute(
        "SELECT id, node_id, definition_id FROM t_entity_instances WHERE id=%s",
        (before.entity_id,),
    )
    self.assertEqual(self.cursor.fetchone(), (
        before.entity_id, before.node_id, before.definition_id,
    ))
    self.assert_column_absent("t_entity_instances", "device_instance_id")

def test_044_rejects_entity_without_unique_node_before_any_drop(self):
    self.seed_ambiguous_043_entity()
    with self.assertRaisesRegex(Exception, "HARD_CUT_ENTITY_NODE_AMBIGUOUS"):
        self.apply_044()
    self.assertIsNotNone(self.regclass("t_solution_packages"))
    self.assert_migration_absent("044")

def test_044_removes_solution_device_acceptance_and_metric_tables(self):
    self.apply_044()
    for table in REMOVED_044_TABLES:
        self.assertIsNone(self.regclass(table), table)
```

测试同时记录迁移前后以下对账：节点数/L0 tag 数/L0 历史数/L0 latest 数/L2 实体 UUID 集/L2 历史数/L2 latest 数/告警定义与事件/JDM rule UUID/控制命令 UUID。任何减少都失败。

- [ ] **Step 2: 运行 RED**

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest tests.test_node_data_trunk_hard_cut_migration_postgres -v`

Expected: FAIL，因为 migration 044 尚不存在。

- [ ] **Step 3: 在单事务中实现先校验、后搬迁、最后删除**

迁移开头先锁定相关配置表并执行 fail-closed 预检；所有 `RAISE EXCEPTION` 必须发生在 DROP 之前。核心目标结构：

```sql
CREATE TABLE t_configuration_revisions (
  revision BIGINT PRIMARY KEY CHECK (revision >= 0),
  previous_revision BIGINT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  before_digest CHAR(64),
  after_digest CHAR(64) NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (previous_revision) REFERENCES t_configuration_revisions(revision)
);

CREATE TABLE t_configuration_state (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  current_revision BIGINT NOT NULL REFERENCES t_configuration_revisions(revision)
);

CREATE TABLE t_configuration_audit (
  id UUID PRIMARY KEY,
  configuration_revision BIGINT NOT NULL REFERENCES t_configuration_revisions(revision),
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  before_digest CHAR(64),
  after_digest CHAR(64) NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

把旧 `t_site_configuration_versions/state` 转换成等序号配置修订；旧解决方案审计只搬迁操作者、动作、资源和摘要，不复制包正文。给 `t_entity_instances` 增加 `node_id`，从 `t_device_instances.node_id` 唯一回填并设 NOT NULL/FK；建立 `UNIQUE(node_id, definition_id) WHERE active`。把存活表中的 `base_site_configuration_version/site_configuration_version` 重命名为 `base_configuration_revision/configuration_revision`，并重接通用修订 FK。

保留 Schema 043 对 L2 `value_numeric`、`event_time_basis`、commit sequence 和类型/append-only 门禁的增强；删除独立业务指标表及 `processing_scope='business_metric'` 行，把普通 L1 恢复为每节点一个 current。删除解决方案包/资产/计划/安装、设备实例/候选绑定、EMS policy activation、统一报告和各模块验收表。删除前用 FK/计数查询证明没有上层引用损失。

- [ ] **Step 4: 加 044 contract gate、fresh/replay/partial 测试并运行 GREEN**

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest tests.test_node_data_trunk_hard_cut_migration_postgres tests.test_data_trunk_startup_gate -v`

Run: `python -m unittest scripts.test_build_release_images scripts.test_release_preflight -v`

Expected: 043→044、fresh、044 replay 全 PASS；缺表、混合结构、无法映射实体、不可还原模板和引用损失全部稳定失败且不登记 044。

- [ ] **Step 5: 提交**

```powershell
git add init-db/migration_044_node_data_trunk_hard_cut.sql backend/tests/test_node_data_trunk_hard_cut_migration_postgres.py backend/app/services/data_trunk_postgres.py backend/tests/test_data_trunk_startup_gate.py scripts/test_build_release_images.py
git commit -m "feat(data): hard-cut schema 044 to node trunk"
```

---

### Task 2: 建立极薄的配置修订事务边界

**Files:**
- Create: `backend/app/services/configuration_revision.py`
- Create: `backend/app/services/configuration_revision_postgres.py`
- Create: `backend/tests/test_configuration_revision.py`
- Create: `backend/tests/test_configuration_revision_postgres.py`

**Interfaces:**
- `current(transaction=None) -> int`
- `publish(transaction, base_revision, actor, action, resource_kind, resource_id, before_digest, after_digest, details) -> int`
- Error: `CONFIGURATION_REVISION_STALE`。

- [ ] **Step 1: 写并发、追加审计和回滚 RED**

```python
def test_publish_requires_current_base_and_appends_audit(self):
    revision = self.subject.publish(
        transaction=self.conn,
        base_revision=0,
        actor="engineer-1",
        action="point_processing.publish",
        resource_kind="node",
        resource_id=str(NODE_ID),
        before_digest=None,
        after_digest="a" * 64,
        details={"template_revision_id": str(REVISION_ID)},
    )
    self.assertEqual(revision, 1)
    self.assertEqual(self.audit_count(), 1)

def test_stale_base_is_zero_write(self):
    self.publish_once(base_revision=0)
    with self.assertRaisesRegex(ConfigurationRevisionError, "CONFIGURATION_REVISION_STALE"):
        self.publish_once(base_revision=0)
    self.assertEqual(self.current_revision(), 1)
    self.assertEqual(self.audit_count(), 1)
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest tests.test_configuration_revision -v`

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest tests.test_configuration_revision_postgres -v`

- [ ] **Step 3: 实现一个 helper，不做通用配置 API**

公开端口固定为 `current(transaction=None) -> int` 和 `publish(*, transaction, base_revision, actor, action, resource_kind, resource_id, before_digest, after_digest, details) -> int`，不再扩展通用资源 CRUD。

PostgreSQL 实现必须在调用者已有事务中 `SELECT ... FOR UPDATE` 锁定 singleton，复核 base，插入 revision 与 audit，再更新 singleton；helper 不自行提交。内存实现只服务纯领域测试。

- [ ] **Step 4: 运行 GREEN、并发测试和提交**

Run: `python -m unittest tests.test_configuration_revision -v`

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest tests.test_configuration_revision_postgres -v`

```powershell
git add backend/app/services/configuration_revision.py backend/app/services/configuration_revision_postgres.py backend/tests/test_configuration_revision.py backend/tests/test_configuration_revision_postgres.py
git commit -m "feat(config): add atomic configuration revisions"
```

---

### Task 3: 把 L2 Registry 从设备实例改为节点直接归属

**Files:**
- Modify: `backend/app/services/entity_instance_catalog.py`
- Modify: `backend/app/services/entity_instance_registry.py`
- Modify: `backend/app/services/entity_instance_postgres.py`
- Modify: `backend/app/services/entity_instance_runtime.py`
- Modify: `backend/app/api/entity_instances.py`
- Modify: `backend/tests/test_entity_instance_catalog.py`
- Modify: `backend/tests/test_entity_instance_registry.py`
- Modify: `backend/tests/test_entity_instance_l2_runtime.py`
- Modify: `backend/tests/test_entity_delivery_public_api.py`

**Interfaces:** `EntityInstanceDescriptor` 对外返回 `node_id/node_type/node_display_name`，不再返回 `device_instance_id/slot_id/instance_key/device_*`。

- [ ] **Step 1: 写直接节点目录和稳定 UUID RED**

```python
def test_descriptor_exposes_node_ownership_only(self):
    item = self.catalog.list()[0].public_dict()
    self.assertEqual(item["node_id"], str(NODE_ID))
    self.assertEqual(item["node_type"], "pcs")
    self.assertNotIn("device_instance_id", item)
    self.assertNotIn("slot_id", item)

def test_republish_keeps_entity_uuid_for_node_and_definition(self):
    first = self.apply_template("brand-a")
    second = self.apply_template("brand-b")
    self.assertEqual(first.entity_ids, second.entity_ids)
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest tests.test_entity_instance_catalog tests.test_entity_instance_registry tests.test_entity_instance_l2_runtime -v`

- [ ] **Step 3: 最小改造 Registry/Postgres 查询**

```python
@dataclass(frozen=True)
class EntityInstanceDescriptor:
    id: UUID
    node_id: UUID
    node_type: str
    node_display_name: str
    definition_id: str
    display_name: str
    data_type: str
    unit: str | None
    direction: str
    freshness_seconds: float
    confirmed: bool = False
```

实体稳定 ID 只由 `node_id + definition_id` 决定；`resolve()`、`entity_instance_for_definition()`、catalog 和 failover 查询直接 JOIN `t_nodes`。删除 package-approved entity plan、设备实例创建、legacy device migration preview 等只为设备实例存在的路径；保留 L2 realtime/history/source failover 安全语义。

- [ ] **Step 4: 更新公共 API 合同并运行 GREEN**

Run: `python -m unittest tests.test_entity_instance_catalog tests.test_entity_instance_registry tests.test_entity_instance_l2_runtime tests.test_entity_delivery_public_api -v`

- [ ] **Step 5: 提交**

```powershell
git add backend/app/services/entity_instance_catalog.py backend/app/services/entity_instance_registry.py backend/app/services/entity_instance_postgres.py backend/app/services/entity_instance_runtime.py backend/app/api/entity_instances.py backend/tests/test_entity_instance_catalog.py backend/tests/test_entity_instance_registry.py backend/tests/test_entity_instance_l2_runtime.py backend/tests/test_entity_delivery_public_api.py
git commit -m "refactor(data): attach L2 entities directly to nodes"
```

---

### Task 4: 把点位加工模板变成独立单 JSON 资源

**Files:**
- Create: `backend/app/services/point_processing_templates.py`
- Create: `backend/tests/test_point_processing_templates.py`
- Modify: `backend/app/api/point_processings.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `backend/tests/test_point_processing_public_api.py`
- Delete: `backend/app/services/solution_point_processings.py`
- Create: `reference-point-processings/pcs-en9.zizu-point-processing.json`
- Create: `reference-point-processings/pcs-brand-a.zizu-point-processing.json`
- Create: `reference-point-processings/pcs-brand-b.zizu-point-processing.json`
- Create: `reference-point-processings/site-total-pcs-power.zizu-point-processing.json`

**Interfaces:**
- `POST /api/v1/point-processing-templates/import`
- `GET /api/v1/point-processing-templates`
- `GET /api/v1/point-processing-templates/{revision_id}/export`
- Schema: `zizu.point-processing/v1alpha1`。

- [ ] **Step 1: 写规范 JSON、不可变修订、导入不增配置修订 RED**

```python
def test_import_registers_immutable_revision_without_publishing_config(self):
    before = self.configuration_revision()
    response = self.client.post(
        "/api/v1/point-processing-templates/import",
        json=self.fixture("pcs-en9.zizu-point-processing.json"),
        headers=self.admin_headers,
    )
    self.assertEqual(response.status_code, 201)
    self.assertEqual(self.configuration_revision(), before)
    self.assertEqual(response.json()["content_digest"], canonical_digest(response.json()["content"]))

def test_same_key_revision_with_changed_content_is_rejected(self):
    self.import_template(self.template())
    response = self.import_template(self.template(display_name="tampered"))
    self.assertEqual(response.status_code, 409)
    self.assertEqual(response.json()["detail"]["code"], "POINT_PROCESSING_REVISION_IMMUTABLE")
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest tests.test_point_processing_templates tests.test_point_processing_public_api -v`

- [ ] **Step 3: 搬移纯 parser 并删除包语义**

保留原 `PointProcessingInput/Output/Asset` 强类型解析和 UUID5 生成逻辑；改名为 `PointProcessingTemplate`，只接受完整 JSON object。删除 `PackageImport`、ZIP、manifest、asset path、overlay 和 `DeliveryError`。导出返回数据库中原规范内容，并设置：

```python
headers = {
    "Content-Disposition": f'attachment; filename="{template.key}.zizu-point-processing.json"',
    "ETag": f'"{template.content_digest}"',
}
```

参考模板从旧 YAML 资产提取，但不得包含节点 UUID、现场凭据、验收或页面资源。

- [ ] **Step 4: 运行 GREEN、规范往返测试和提交**

Run: `python -m unittest tests.test_point_processing_templates tests.test_point_processing_public_api -v`

```powershell
git add backend/app/services/point_processing_templates.py backend/app/api/point_processings.py backend/app/services/point_processing_postgres.py backend/tests/test_point_processing_templates.py backend/tests/test_point_processing_public_api.py reference-point-processings
git rm backend/app/services/solution_point_processings.py
git commit -m "feat(data): make point processing a single-file template"
```

---

### Task 5: 让 L1 预览和发布完全脱离解决方案安装

**Files:**
- Modify: `backend/app/services/point_processing.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `backend/app/services/data_trunk.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/api/point_processings.py`
- Modify: `backend/tests/test_point_processing.py`
- Modify: `backend/tests/test_point_processing_postgres.py`
- Modify: `backend/tests/test_point_processing_public_api.py`
- Modify: `backend/tests/test_data_trunk_postgres.py`

**Interfaces:**
- Plan fields: `node_id/template_revision_id/base_configuration_revision/input_bindings/output_entities/blockers/digest`。
- Apply output: `application_id/configuration_revision/installed_processing_id/entity_instance_ids`。

- [ ] **Step 1: 写无解决方案字段、stale/阻断零写和 UUID 稳定 RED**

```python
def test_plan_contains_only_node_trunk_configuration(self):
    plan = self.subject.plan(NODE_ID, TEMPLATE_REVISION_ID, actor="engineer")
    payload = plan.public_dict()
    self.assertEqual(payload["base_configuration_revision"], 7)
    for removed in ("solution_installation_id", "entity_identity_installation_id", "package_digest"):
        self.assertNotIn(removed, payload)

def test_apply_is_atomic_with_configuration_revision(self):
    plan = self.ready_plan(base_configuration_revision=7)
    self.repository.fail_after_l2_bindings = True
    with self.assertRaises(RuntimeError):
        self.subject.apply(plan.id, actor="engineer", idempotency_key="apply-1")
    self.assertEqual(self.repository.current_configuration_revision(), 7)
    self.assertEqual(self.repository.installed_count(), 0)
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest tests.test_point_processing tests.test_point_processing_postgres tests.test_point_processing_public_api -v`

- [ ] **Step 3: 收缩服务和持久化**

把 `PointProcessingDelivery` 改为 `PointProcessingService`；删除 solution/package ownership 和 `_create_derived_solution_lineage()`。计划直接读取 template revision、节点 L0 和跨节点 L2；apply 在一个事务中重新核对模板摘要、输入集合、DAG 和 base revision，写 current installation/bindings/L2 entity，再调用 `ConfigurationRevisionRepository.publish(...)`。

移除 `DataTrunk.acceptance_evidence()`；节点主干查询直接组合 L0 latest/history、current L1、L2 latest/history/source evidence，不生成验收快照。

- [ ] **Step 4: 运行 GREEN、幂等/并发/回滚真实 PG 测试并提交**

Run: `python -m unittest tests.test_point_processing tests.test_point_processing_public_api -v`

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest tests.test_point_processing_postgres tests.test_data_trunk_postgres -v`

```powershell
git add backend/app/services/point_processing.py backend/app/services/point_processing_postgres.py backend/app/services/data_trunk.py backend/app/services/data_trunk_postgres.py backend/app/api/point_processings.py backend/tests/test_point_processing.py backend/tests/test_point_processing_postgres.py backend/tests/test_point_processing_public_api.py backend/tests/test_data_trunk_postgres.py
git commit -m "refactor(data): publish L1 directly to nodes"
```

---

### Task 6: 让告警与 JDM 使用同一配置修订；控制只消费 L2

**Files:**
- Modify: `backend/app/services/alarm_configuration_postgres.py`
- Modify: `backend/app/api/alarm_configurations.py`
- Modify: `backend/app/api/rules.py`
- Modify: `backend/app/services/rule_engine.py`
- Modify: `backend/app/services/control_commands.py`
- Modify: `backend/app/api/control_commands.py`
- Modify: `backend/tests/test_alarm_configuration_postgres.py`
- Create: `backend/tests/test_jdm_configuration_revision_public_api.py`
- Modify: `backend/tests/test_control_command_public_api.py`

- [ ] **Step 1: 写配置事件与运行事件分离 RED**

```python
def test_alarm_apply_advances_one_configuration_revision(self):
    before = self.configuration_revision()
    self.apply_alarm_plan()
    self.assertEqual(self.configuration_revision(), before + 1)
    self.assert_audit(before + 1, "alarm_configuration.publish")

def test_jdm_update_advances_revision_but_simulation_does_not(self):
    before = self.configuration_revision()
    self.update_jdm_rule()
    self.assertEqual(self.configuration_revision(), before + 1)
    self.simulate_jdm_rule()
    self.assertEqual(self.configuration_revision(), before + 1)

def test_control_execution_does_not_advance_configuration_revision(self):
    before = self.configuration_revision()
    self.submit_safe_manual_control()
    self.assertEqual(self.configuration_revision(), before)
```

- [ ] **Step 2: 运行 RED**

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest tests.test_alarm_configuration_postgres tests.test_jdm_configuration_revision_public_api tests.test_control_command_public_api -v`

- [ ] **Step 3: 接入通用修订并收紧 L2 引用**

告警 plan/apply 和 JDM create/update/delete 在自己的业务写事务中调用配置修订 helper；删除对 `t_solution_delivery_audit` 的写入。simulate/evaluate、告警事件、ack/recovery、控制命令和回读不增修订。JDM 保留现有 GoRules JDM editor/zen-engine 执行语义，不引入平行规则 DSL；规则输入、告警目标和控制目标继续验证 active L2 UUID，拒绝 L0/tag/node 物理地址。

- [ ] **Step 4: 运行 GREEN 和提交**

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest tests.test_alarm_configuration_postgres tests.test_jdm_configuration_revision_public_api tests.test_control_command_public_api -v`

```powershell
git add backend/app/services/alarm_configuration_postgres.py backend/app/api/alarm_configurations.py backend/app/api/rules.py backend/app/services/rule_engine.py backend/app/services/control_commands.py backend/app/api/control_commands.py backend/tests/test_alarm_configuration_postgres.py backend/tests/test_jdm_configuration_revision_public_api.py backend/tests/test_control_command_public_api.py
git commit -m "refactor(config): publish alarm and JDM revisions"
```

---

### Task 7: 删除解决方案、验收、设备模板、EMS policy 和独立业务指标运行面

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/entity_instances.py`
- Modify: `backend/app/api/alarm_events.py`
- Modify: `backend/app/api/control_commands.py`
- Modify: `backend/app/api/websocket.py`
- Create: `backend/tests/test_hard_cut_public_surface.py`
- Delete: `backend/app/api/solution_delivery.py`
- Delete: `backend/app/api/ems_policies.py`
- Delete: `backend/app/api/device_templates.py`
- Delete: `backend/app/services/solution_delivery.py`
- Delete: `backend/app/services/solution_delivery_contracts.py`
- Delete: `backend/app/services/solution_delivery_repository.py`
- Delete: `backend/app/services/solution_package_archive.py`
- Delete: `backend/app/services/solution_parameters.py`
- Delete: `backend/app/services/solution_policies.py`
- Delete: `backend/app/services/solution_workbench.py`
- Delete: `backend/app/services/ems_policy_runtime.py`
- Delete: `backend/app/services/data_trunk_acceptance.py`
- Delete: `backend/app/services/en9_point_processing_acceptance.py`
- Delete: `backend/app/services/cross_node_processing_acceptance.py`
- Delete: `backend/app/services/alarm_configuration_acceptance.py`
- Delete: `backend/app/services/alarm_configuration_acceptance_postgres.py`
- Delete: `backend/app/services/business_metric_contracts.py`
- Delete: `backend/app/services/business_metrics.py`
- Delete: `backend/app/services/business_metrics_postgres.py`
- Delete: `backend/app/services/solution_business_metrics.py`
- Delete: `backend/app/services/metric_projection.py`
- Delete: `backend/app/services/metric_projection_postgres.py`
- Delete: 对应 `backend/tests/test_*delivery*`、`test_*acceptance*`、`test_business_metrics*`、`test_metric_projection*`、`test_ems_policy_postgres.py`。

- [ ] **Step 1: 写运行装配和公开路由 RED**

```python
def test_removed_product_routes_are_not_registered(self):
    paths = {route.path for route in create_app().routes}
    self.assertFalse(any(path.startswith("/api/v1/solution-") for path in paths))
    self.assertNotIn("/api/v1/device-templates", paths)
    self.assertFalse(any("acceptance" in path for path in paths))

def test_main_starts_without_solution_or_metric_projection_imports(self):
    source = Path("backend/app/main.py").read_text(encoding="utf-8")
    for forbidden in ("solution_delivery", "ems_policy", "metric_projection"):
        self.assertNotIn(forbidden, source)
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest tests.test_hard_cut_public_surface -v`

- [ ] **Step 3: 把 singleton getter 下沉到所属模块，再删除文件**

实体 catalog/runtime/failover getter 放入 `api/entity_instances.py` 或实体服务；告警 runtime 放入 `api/alarm_events.py`；控制手动/自动服务和 compatibility getter 放入 `api/control_commands.py`。`rule_engine.py` 只从 control 模块取得自动控制端口。禁止新建大而全的 composition root。

`main.py` 删除 solution/device-template/EMS-policy 路由和 policy/metric scheduler，只保留 L0 pipeline、L1 typed formula、L2 freshness/outbox、JDM、聚合等实际主干任务。WebSocket 保留认证推送，删除 EN9/验收 receipt 记录。

删除告警配置和点位加工 API 中的 acceptance endpoints；删除独立业务指标 runtime 和表对应代码。Schema 043 中对通用 L2 精度、时间基准、commit sequence 和控制 fail-closed 有用的字段/约束已由 Task 1 保留，不保留无调用者的 capability/metric 代码。

- [ ] **Step 4: 运行静态零引用和回归 GREEN**

Run: `rg -n "solution_delivery|solution-packages|solution-installations|device_instance_id|acceptance-report|ems_policy|business_metric|metric_projection" backend/app frontend/src`

Expected: 无生产代码命中；允许历史 migration、ADR/spec/plan 中出现。

Run: `python -m unittest tests.test_hard_cut_public_surface tests.test_entity_observation_websocket tests.test_business_rest_authorization -v`

- [ ] **Step 5: 删除并提交**

先用 `git rm` 精确列出上述文件和对应过时测试，不使用目录递归删除。然后：

```powershell
git add backend/app/main.py backend/app/api backend/app/services backend/tests
git commit -m "refactor(core): remove parallel delivery products"
```

---

### Task 8: 固定 EMS 工作台直接由节点类型和 L2 语义生成

**Files:**
- Modify: `backend/app/services/ems_workbench.py`
- Modify: `backend/app/api/ems_workbench.py`
- Create: `backend/tests/test_fixed_ems_workbench.py`
- Create: `backend/tests/test_ems_workbench_public_api.py`

**Interfaces:** `GET /api/v1/ems-workbench` 返回固定导航、按节点类型分组的 L2、KPI/趋势/告警/质量；不接受 package/workbench asset。

- [ ] **Step 1: 写节点类型、L2 语义和缺失质量 RED**

```python
def test_workbench_is_derived_from_node_types_and_l2_semantics(self):
    view = self.subject.read()
    self.assertEqual(view["workbench_id"], "fixed-ems-v1")
    self.assertEqual([g["kind"] for g in view["groups"]], [
        "site", "pv", "storage", "charging", "load",
    ])
    self.assertNotIn("solution_installation_id", view)
    self.assertEqual(view["configuration_revision"], 12)

def test_missing_soc_is_explicit_not_zero(self):
    item = self.subject.read()["kpis"]["storage_soc"]
    self.assertIsNone(item["value"])
    self.assertEqual(item["quality"], "MISSING")
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest tests.test_fixed_ems_workbench tests.test_ems_workbench_public_api -v`

- [ ] **Step 3: 实现固定投影**

`EmsWorkbench` 仅依赖 `EntityInstanceCatalog`、`EntityInstanceRuntime`、告警只读端口和配置修订读取器。导航是代码内固定常量；分组依据 `node_type`，KPI 映射依据标准 `definition_id`。未知节点类型仍在“其他”组展示，不生成页面资产，不写配置。

- [ ] **Step 4: 运行 GREEN 和提交**

Run: `python -m unittest tests.test_fixed_ems_workbench tests.test_ems_workbench_public_api -v`

```powershell
git add backend/app/services/ems_workbench.py backend/app/api/ems_workbench.py backend/tests/test_fixed_ems_workbench.py backend/tests/test_ems_workbench_public_api.py
git commit -m "refactor(ems): derive fixed workbench from L2"
```

---

### Task 9: 把“节点与数据”界面固定为 L0/L1/L2 三层

**Files:**
- Modify: `frontend/src/pages/NodeTreePage.tsx`
- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/components/data-trunk/NodeTrunkOverview.tsx`
- Modify: `frontend/src/components/data-trunk/PointProcessingPlanPanel.tsx`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.ts`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: 写三层视图、实时/历史/来源和无验收文案 RED**

```javascript
test('node data workspace exposes the fixed L0 L1 L2 flow', () => {
  const view = buildDataTrunkViewModel(fixture)
  assert.deepEqual(view.layers.map(layer => layer.key), ['l0', 'l1', 'l2'])
  assert.equal(view.flow, '采集 → L0 → L1 → L2 → 上层应用')
  assert.equal(view.l2[0].sourceEvidence.processingRevisionId, REVISION_ID)
  assert.equal(JSON.stringify(view).includes('验收'), false)
  assert.equal(JSON.stringify(view).includes('解决方案'), false)
})
```

- [ ] **Step 2: 运行 RED**

Run: `node --test frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`

- [ ] **Step 3: 实现右侧固定三页签**

左侧节点树不变；右侧仅保留：

- `L0 原始点位`：搜索、实时值、质量、观测/接收时间、来源、历史趋势；
- `L1 点位加工`：模板选择/JSON 导入/导出、输入匹配、阻断、公式/映射、预览、发布、当前配置修订；
- `L2 全局实体`：实时值、质量、时间戳、历史趋势、来源证据和上层消费者。

顶部始终显示链路状态。删除 EN9 acceptance state/report/button、“交付证据”“站点配置版本”“先安装解决方案”等状态与文案；就地验证直接读取普通 L0/L2 facts。

- [ ] **Step 4: 运行 GREEN、类型和构建**

Run: `node --test frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`

Run: `cd frontend; npx tsc -b; npm run build`

- [ ] **Step 5: 提交**

```powershell
git add frontend/src/pages/NodeTreePage.tsx frontend/src/components/data-trunk frontend/src/api/client.ts
git commit -m "feat(ui): make node data the L0 L1 L2 workspace"
```

---

### Task 10: 收敛导航和前端公开功能面

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/EMSWorkbenchPage.tsx`
- Modify: `frontend/src/pages/RuleEnginePage.tsx`
- Modify: `frontend/src/pages/AlarmConfigurationPage.tsx`
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/navigationContract.test.mjs`
- Delete: `frontend/src/pages/SolutionDeliveryPage.tsx`
- Delete: `frontend/src/pages/DeviceTemplatePage.tsx`
- Delete: `frontend/src/pages/EntityManagerPage.tsx`
- Delete: `frontend/src/components/alarm-configuration/acceptanceRetryState.ts`
- Delete: `frontend/src/components/alarm-configuration/acceptanceRetryState.test.mjs`

- [ ] **Step 1: 写唯一导航和死链 RED**

```javascript
test('main navigation contains only the confirmed product entries', () => {
  assert.deepEqual(NAV_ITEMS.map(item => item.label), [
    'EMS 工作台', '节点与数据', '告警中心', '告警配置', 'JDM', '控制', '系统工具',
  ])
  for (const removed of ['解决方案交付', '设备模板', '实体管理']) {
    assert.equal(NAV_ITEMS.some(item => item.label === removed), false)
  }
})
```

- [ ] **Step 2: 运行 RED**

Run: `node --test frontend/src/navigationContract.test.mjs`

- [ ] **Step 3: 删除页面和客户端合同**

导航改名/排序并移除三个并行入口；“控制”进入现有安全控制工作区，不新建第二套控制服务；“JDM”继续复用现有 `RuleEnginePage` 和 `@gorules/jdm-editor`。告警配置删除验收 report/retry UI，保留预览、发布、运行事实和审计。`client.ts` 删除全部 solution/package/install/report/acceptance/device-template 类型和函数。

- [ ] **Step 4: 运行 GREEN、构建并做静态死链扫描**

Run: `node --test frontend/src/navigationContract.test.mjs frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`

Run: `cd frontend; npm run build`

Run: `rg -n "SolutionDeliveryPage|DeviceTemplatePage|EntityManagerPage|solution-packages|acceptance" frontend/src`

Expected: 无生产代码命中。

- [ ] **Step 5: 提交**

```powershell
git add frontend/src
git commit -m "refactor(ui): remove parallel delivery navigation"
```

---

### Task 11: 删除参考解决方案与过时发布面，更新操作文档

**Files:**
- Delete: `reference-deliveries/pv-storage-charging-ems/`
- Delete: `scripts/build_reference_delivery.py`
- Delete: 只服务解决方案 ZIP/manifest 的构建与测试文件
- Modify: `README.md`
- Create: `docs/deploy-1号机-v0.4.85-rc.1-http.md`
- Delete: `docs/deploy-1号机-v0.4.82-rc.2-http.md`
- Modify: `docs/adr/0013-node-data-trunk-replaces-solution-delivery.md`
- Modify: `CODEX_HANDOFF.md`

- [ ] **Step 1: 写发布输入静态 RED**

```python
def test_release_contains_no_solution_package_artifact(self):
    workflow = Path('.github/workflows/release.yml').read_text(encoding='utf-8')
    self.assertNotIn('reference-delivery', workflow)
    self.assertNotIn('.zizu.zip', workflow)
    self.assertIn('SCHEMA_VERSION=044', self.release_inputs())
```

- [ ] **Step 2: 运行 RED**

Run: `python -m unittest scripts.test_build_release_images scripts.test_release_preflight -v`

- [ ] **Step 3: 精确删除并更新文档**

README 只描述节点数据主干、直接模板流程、模块内验证和固定 EMS 工作台。部署文档必须包含：数据库全备份与恢复演练、旧固定摘要、044 migration dry-run clone、目标摘要、`network_mode: host`、`tmpfs: /dev/mqueue`、runtime secret、健康检查、只读对账及完整备份回滚；明确“不启动 Caddy/TLS、不自动策略、不设备写入”。历史 ADR 不删除，0013 标记 supersedes 的旧 ADR。

- [ ] **Step 4: 运行 GREEN、链接/术语扫描和提交**

Run: `python -m unittest scripts.test_build_release_images scripts.test_release_preflight -v`

Run: `rg -n "reference-deliveries|build_reference_delivery|solution package|解决方案交付" README.md docs scripts .github`

Expected: 只允许历史 ADR/spec/plan 的决策记录命中；现行 README、部署脚本和 workflow 无命中。

```powershell
git add README.md docs scripts .github CODEX_HANDOFF.md
git commit -m "docs(core): publish the node trunk operating model"
```

---

### Task 12: 全量验证、候选构建和 1 号机只读部署验收

**Files:**
- Modify: `VERSION`
- Modify: 候选发布 manifest/workflow 中的版本与 Schema 断言
- Create: `.superpowers/sdd/2026-08-25-node-data-trunk-hard-cut/final-verification.md`
- Modify: `CODEX_HANDOFF.md`

- [ ] **Step 1: 执行代码与数据库全量门禁**

Run: `python -m compileall backend/app backend/tests`

Run: `python -m unittest discover -s backend/tests -v`

Run: `$env:ZIZU_POSTGRES_TEST='1'; python -m unittest discover -s backend/tests -p '*postgres*.py' -v`

Run: `cd frontend; node --test src/navigationContract.test.mjs src/components/data-trunk/dataTrunkViewModel.test.mjs; npm run build`

Run: `git diff --check`

所有 skip 必须逐项解释；不得把缺失 PostgreSQL、前端依赖或认证环境当作通过。

- [ ] **Step 2: 执行 hard-cut 完成判据扫描**

```powershell
rg -n "solution_delivery|solution-packages|solution-installations|device_instance_id|ems_policy|business_metric|metric_projection|acceptance" backend/app frontend/src init-db/migration_044_node_data_trunk_hard_cut.sql
```

生产代码不得命中删除概念。随后用公开 API 测试证明被删路由返回 404；用迁移测试证明数据库无遗留解决方案 FK；用两套品牌模板证明 L2 UUID 与告警/JDM/控制引用不变。

- [ ] **Step 3: 构建固定摘要候选并在隔离数据库恢复演练**

把 VERSION 更新为 `0.4.85-rc.1`；若该版本在执行前已被占用，先修改本计划与发布断言，再使用下一个未占用版本，绝不移动既有 tag。构建 amd64/arm64 镜像并记录源码 commit、OCI digest、Schema=044。把 1 号机备份恢复到隔离 PostgreSQL，运行 044，保存迁移前后对账和被删表清单；再从完整备份启动旧固定摘要，证明回滚可用。

- [ ] **Step 4: 受控部署 1 号机**

维护窗内：停止旧 ZiZu 容器；不动 NanoMQ/Neuron；用固定 ARM64 摘要、旧容器等效配置、`network_mode: host` 和 `/dev/mqueue` tmpfs 启动新容器。Schema 044 任何预检失败立即停止，不手工删表或改数据，只恢复完整备份和旧固定摘要。

- [ ] **Step 5: 只读现场验收**

依次验证：health、登录、节点树、任一节点 L0 realtime/history、L1 current plan、L2 realtime/history/source evidence、告警查询、JDM simulate、控制权限/限值/联锁只读状态、固定 EMS 工作台、认证 WebSocket。禁止运行自动策略或提交设备命令。

在报告中记录实际命令、时间、HTTP 状态、关键计数、commit/digest 和任何未验证项；没有证据不得写“完成”。

- [ ] **Step 6: 请求独立 code review 并提交候选元数据**

使用 `superpowers:requesting-code-review` 检查规格完成判据、迁移安全、删除边界和现场只读约束；修复 Important 以上问题并重跑对应门禁。然后：

```powershell
git add VERSION .superpowers/sdd/2026-08-25-node-data-trunk-hard-cut/final-verification.md CODEX_HANDOFF.md
git commit -m "chore(release): verify node trunk hard cut"
```

## Final Evidence Checklist

- [ ] Schema 044 在 fresh、043 upgrade、replay、partial/mixed、阻断回滚上全部有真实 PostgreSQL 证据。
- [ ] 节点/L0/L2/告警/JDM/控制关键 UUID、数量和摘要迁移前后对账一致。
- [ ] 单 JSON 模板完成导入、预览、原子发布和 L0/L2 就地验证。
- [ ] 品牌切换保持 L2 UUID 和所有上层引用。
- [ ] 后端生产代码、前端、公开路由和数据库均无解决方案/设备实例/验收/独立业务指标运行入口。
- [ ] 前端只保留确认的七个入口，节点页固定显示 L0/L1/L2 实时、历史和来源。
- [ ] 完整后端、真实 PG、前端 build、迁移回放、重启和认证推送通过。
- [ ] 1 号机部署使用固定摘要、host network、`/dev/mqueue` tmpfs；现场验证全程只读。
- [ ] 完整数据库备份恢复旧摘要的回滚演练成功。
