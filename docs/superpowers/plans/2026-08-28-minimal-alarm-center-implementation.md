# 最简告警中心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复用现有 committed L2 告警内核，把告警收敛为一个入口、两个视图，使实施工程师能完成“选实体—配规则—试算—发布”，操作员能完成“看活动告警—确认—等待自然恢复”。

**Architecture:** 不新建第二套告警引擎。后端只补公开条件匹配 seam、无副作用试算、面向界面的事件/规则组读模型；发布继续使用现有不可变规则修订、配置计划、配置栅栏和幂等 apply。前端合并“告警中心/告警配置”，按 L2 数据类型生成表单；CODE_SET 每个故障码编译为一条独立告警规则。

**Tech Stack:** FastAPI、Pydantic、PostgreSQL、React、TypeScript、现有原生 Node 测试、unittest

**Spec:** `docs/superpowers/specs/2026-08-28-minimal-alarm-center-design.md`

## Global Constraints

- 只消费 committed L2；不读取 L0，不修改控制链、JDM 执行语义或设备写入。
- 不新增 Redis、Kafka、WebSocket、通知系统、自由表达式、新依赖或新数据库表。
- 数据库提交前，页面状态、事件、通知和下游均不可见。
- 所有改动先写失败测试；每项任务通过定向测试后再进入下一项。
- 下列命令均从工作树根目录开始执行；进入子目录的任务结束后先返回根目录。
- 前端只保留一个“告警中心”入口；角色仅控制“告警规则”是否可编辑。
- 最后只构建一次 ARM64 发布镜像，并以固定 digest 部署到 1 号机。

---

## Task 1：统一告警条件匹配，并支持 CODE_SET

**Files:**

- Modify: `backend/app/services/alarm_runtime.py`
- Modify: `backend/app/services/alarm_configuration.py`
- Modify: `backend/app/api/alarm_configurations.py`
- Test: `backend/tests/test_alarm_runtime.py`
- Test: `backend/tests/test_alarm_configuration_l2.py`

- [ ] **RED：固定公开 matcher 的行为**

  在 `test_alarm_runtime.py` 增加用例，要求公开函数：

  ```python
  match_alarm_condition({"operator": "contains", "value": "E30"}, ["E30", "E42"]) is True
  match_alarm_condition({"operator": "contains", "value": "E99"}, ["E30", "E42"]) is False
  match_alarm_condition({"operator": "not_contains", "value": "E30"}, ["E42"]) is True
  ```

  同时保留 `eq/ne/gt/gte/lt/lte` 的既有结果，非法操作符必须返回 `False`，不得抛出并中断帧消费。

- [ ] **RED：固定数据类型门禁**

  在 `test_alarm_configuration_l2.py` 增加：`contains/not_contains` 只允许绑定 `CODE_SET`；有序比较只允许数值 L2；单位不一致仍阻止发布。

- [ ] **运行失败测试**

  ```powershell
  cd backend
  python -m unittest tests.test_alarm_runtime tests.test_alarm_configuration_l2 -v
  ```

- [ ] **GREEN：抽出唯一 matcher**

  将 `alarm_runtime.py` 的私有 `_matches` 改为公开且纯函数：

  ```python
  def match_alarm_condition(condition: dict[str, Any], value: Any) -> bool:
      """Match one typed alarm condition without I/O or state mutation."""
  ```

  运行态只调用这个函数。`contains/not_contains` 仅接受非空字符串阈值和字符串集合值；其他组合返回 `False`。

- [ ] **GREEN：扩展配置契约**

  `AlarmConditionRequest.operator` 增加 `contains/not_contains`；配置领域增加 `_MEMBERSHIP`，并在 `_binding_issues` 对 CODE_SET 做强类型校验。

- [ ] **回归并提交**

  ```powershell
  python -m unittest tests.test_alarm_runtime tests.test_alarm_configuration_l2 -v
  git add backend/app/services/alarm_runtime.py backend/app/services/alarm_configuration.py backend/app/api/alarm_configurations.py backend/tests/test_alarm_runtime.py backend/tests/test_alarm_configuration_l2.py
  git commit -m "feat(alarm): support typed code-set conditions"
  ```

## Task 2：增加“试算”，且保证零持久化

**Files:**

- Modify: `backend/app/services/alarm_configuration.py`
- Modify: `backend/app/api/alarm_configurations.py`
- Test: `backend/tests/test_alarm_configuration_l2.py`

- [ ] **RED：定义试算契约**

  增加测试，调用：

  ```python
  result = configuration.trial(
      entity_instance_id=entity.id,
      rule=rule,
      value=["E30"],
      quality=192,
  )
  ```

  断言返回 `trigger_matches=True`、`recovery_matches=False`、自然语言说明；质量非 GOOD 时两个结果都为 `False`。断言 fake repository 的修订、计划、应用和审计记录数量完全不变。

- [ ] **RED：定义 HTTP API**

  `POST /api/v1/alarm-configurations/trials` 请求只包含：

  ```json
  {
    "entity_instance_id": "uuid",
    "rule": {"id":"e30","name":"压缩机故障","severity":"MAJOR","trigger":{"operator":"contains","value":"E30"},"trigger_duration_seconds":0,"recovery":{"operator":"not_contains","value":"E30"},"recovery_duration_seconds":3,"notification_throttle_seconds":300},
    "value": ["E30"],
    "quality": 192
  }
  ```

  未解析实体返回既有稳定错误码；不创建 rule set、plan 或 definition。

- [ ] **GREEN：复用同一 matcher**

  新增不可变结果 `AlarmRuleTrial`，`AlarmConfiguration.trial()` 先用现有 `resolve_entities` 与 `_binding_issues` 校验绑定，再调用 `match_alarm_condition`。不得复制比较逻辑。

- [ ] **回归并提交**

  ```powershell
  python -m unittest tests.test_alarm_configuration_l2 tests.test_alarm_runtime -v
  git add backend/app/services/alarm_configuration.py backend/app/api/alarm_configurations.py backend/tests/test_alarm_configuration_l2.py
  git commit -m "feat(alarm): add side-effect-free rule trial"
  ```

## Task 3：为页面提供可读告警和活动摘要

**Files:**

- Modify: `backend/app/services/alarm_postgres.py`
- Modify: `backend/app/api/alarm_events.py`
- Test: `backend/tests/test_alarm_event_public_api.py`
- Test: `backend/tests/test_alarm_configuration_postgres.py`

- [ ] **RED：当前告警只统计活动事件**

  扩展 API 测试：已恢复历史不得计入总数；摘要只返回 `active`、`unacknowledged`、`critical`。列表默认先显示未恢复，再按发生时间倒序。

- [ ] **RED：事件必须让人看懂**

  公开事件增加以下只读字段，并保证内部 UUID、frame/revision/evidence 不出现在列表响应：

  ```json
  {
    "node_name": "储能系统",
    "entity_name": "PCS-01 运行状态",
    "alarm_name": "压缩机故障",
    "duration_seconds": 42
  }
  ```

  缺少展示名称时按 `definition_key` 降级，不能显示“实体实例 UUID”。

- [ ] **GREEN：在 PostgreSQL 查询侧组装读模型**

  在现有 `alarm_postgres.py` 增加一条只读 JOIN 查询：事件 → 当前/历史告警定义 → L2 实体 → 节点；告警名称优先读取已提交观测证据中的规则名，失败时使用稳定 definition key。不得改事件状态机和持久化表。

- [ ] **GREEN：保留单条确认语义**

  现有确认接口继续记录 actor/time；无批量确认、无手工恢复。详情接口可返回来源摘要，但页面默认不展开内部证据。

- [ ] **回归并提交**

  ```powershell
  python -m unittest tests.test_alarm_event_public_api tests.test_alarm_configuration_postgres -v
  git add backend/app/services/alarm_postgres.py backend/app/api/alarm_events.py backend/tests/test_alarm_event_public_api.py backend/tests/test_alarm_configuration_postgres.py
  git commit -m "feat(alarm): expose operator-friendly event views"
  ```

## Task 4：提供规则组摘要和安全启停

**Files:**

- Modify: `backend/app/services/alarm_configuration.py`
- Modify: `backend/app/services/alarm_configuration_postgres.py`
- Modify: `backend/app/api/alarm_configurations.py`
- Test: `backend/tests/test_alarm_configuration_l2.py`
- Test: `backend/tests/test_alarm_configuration_l2_postgres.py`

- [ ] **RED：规则列表只返回页面需要的信息**

  为 `GET /api/v1/alarm-rule-groups` 定义：

  ```json
  {
    "items": [{
      "rule_set_id": "uuid",
      "key": "pcs_fault_codes",
      "name": "PCS 故障码",
      "latest_revision": 3,
      "last_non_empty_revision": 2,
      "entity_instance_ids": ["uuid"],
      "enabled_entity_instance_ids": ["uuid"],
      "device_count": 4,
      "rule_count": 18,
      "highest_severity": "CRITICAL"
    }]
  }
  ```

  数据来自现有不可变 rule-set revisions、历史 definitions 与 current pointers，不新增表。

- [ ] **RED：停用后仍可重新启用**

  PostgreSQL 测试覆盖：应用空规则修订后 current pointer 被删除，但规则组仍携带原实体集合与 `last_non_empty_revision`；重用该非空修订发布后恢复启用。配置 revision 单调递增，apply 重放不重复创建定义。

- [ ] **GREEN：实现只读规则组聚合**

  在 repository 增加 `list_rule_groups()`，查询历史定义保留曾应用实体，查询 current pointer 判定当前启用实体。设备数按不同 `node_id` 计数；最高等级顺序固定为 `CRITICAL > MAJOR > WARNING > INFO`。

- [ ] **GREEN：复用现有发布链完成启停**

  不增加 `enabled` 可变字段。停用 = 创建空规则修订 → plan → 幂等 apply；启用 = 取 `last_non_empty_revision` → plan → 幂等 apply。UI 单击期间锁按钮，结果只在 apply 数据库提交后改变。

- [ ] **回归并提交**

  ```powershell
  python -m unittest tests.test_alarm_configuration_l2 tests.test_alarm_configuration_l2_postgres -v
  git add backend/app/services/alarm_configuration.py backend/app/services/alarm_configuration_postgres.py backend/app/api/alarm_configurations.py backend/tests/test_alarm_configuration_l2.py backend/tests/test_alarm_configuration_l2_postgres.py
  git commit -m "feat(alarm): add rule group summaries"
  ```

## Task 5：建立前端纯模型，先证明中文配置可用

**Files:**

- Create: `frontend/src/components/alarm-center/alarmCenterModel.ts`
- Create: `frontend/src/components/alarm-center/alarmCenterModel.test.mjs`
- Modify: `frontend/src/api/client.ts`

- [ ] **RED：故障码粘贴和规则编译**

  原生 Node 测试覆盖三列粘贴：

  ```text
  E30\t压缩机故障\tMAJOR
  E42\t直流母线过压\tCRITICAL
  ```

  解析后每行生成一条规则：触发 `contains(code)`、恢复 `not_contains(code)`；重复 code、空名称和非法等级给出具体行号错误。

- [ ] **RED：中文预览与默认值**

  数值默认“持续 3 秒触发 / 持续 3 秒恢复”；状态默认“立即触发 / 持续 3 秒恢复”。预览必须生成类似：`PCS-01 有功功率 ≥ 100 kW 持续 3 秒，产生严重告警；≤ 95 kW 持续 3 秒后恢复。`

- [ ] **GREEN：实现四个纯函数**

  ```ts
  parseFaultCodePaste(text: string): FaultCodeRow[]
  compileFaultCodeRules(rows: FaultCodeRow[]): AlarmRule[]
  defaultAlarmDraft(dataType: L2DataType): AlarmDraft
  describeAlarmDraft(draft: AlarmDraft, entity: L2EntityOption): string
  ```

  文件内不访问网络和 DOM，保证能独立测试。`client.ts` 只补 Task 2–4 的强类型 API 客户端。

- [ ] **运行测试并提交**

  ```powershell
  cd ..\frontend
  node --experimental-strip-types src/components/alarm-center/alarmCenterModel.test.mjs
  git add src/components/alarm-center/alarmCenterModel.ts src/components/alarm-center/alarmCenterModel.test.mjs src/api/client.ts
  git commit -m "feat(alarm): add minimal configuration model"
  ```

## Task 6：合并为一个“告警中心”入口

**Files:**

- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/AlarmCenterPage.tsx`
- Modify: `frontend/src/pages/AlarmConfigurationPage.tsx`
- Modify: `frontend/src/components/alarm-configuration/RuleSetEditor.tsx`
- Modify: `frontend/src/pages/RuleEnginePage.tsx`
- Modify: `frontend/src/api/client.ts`
- Test: `frontend/src/components/alarm-center/alarmCenterModel.test.mjs`

- [ ] **RED：锁定产品入口**

  在纯模型测试中读取菜单/规则类型导出：断言只有一个“告警中心”，不存在“告警配置”；JDM 新建类型不包含 `alarm` 和 `fault_map`。

- [ ] **GREEN：当前告警视图**

  顶部仅显示活动、未确认、紧急三项；表格仅显示等级、节点/设备、故障、发生时间、持续时间、状态和确认。默认只看活动，可切到历史；确认成功后刷新当前数据。无手工恢复和批量确认。

- [ ] **GREEN：告警规则视图**

  工程师/管理员看到“告警规则”页签；操作员只看当前告警。规则列表仅显示名称、设备数、规则数/最高等级、启用状态、编辑、复制、启停。

- [ ] **GREEN：三步配置**

  1. 批量选择类型兼容的 L2 全局实体；
  2. 数值、状态、CODE_SET 使用各自表单；CODE_SET 支持三列粘贴；
  3. 调用无副作用试算，展示中文解释和影响设备，再 plan/apply 发布。

  发布中锁定按钮；未知结果使用现有 apply 查询恢复，禁止盲目重试。编辑/复制都产生不可变新修订。

- [ ] **GREEN：删除重复入口，不删除底层兼容数据**

  从 `App.tsx` 删除 `alarm-config` 菜单和路由状态；从 `RuleEnginePage.tsx` 删除 `alarm/fault_map` 新建选项。旧表和旧 API 暂不物理删除，避免把“打磨现有功能”扩大成迁移项目。

- [ ] **前端验证并提交**

  ```powershell
  node --experimental-strip-types src/components/alarm-center/alarmCenterModel.test.mjs
  npm run build
  git add src/App.tsx src/pages/AlarmCenterPage.tsx src/pages/AlarmConfigurationPage.tsx src/components/alarm-configuration/RuleSetEditor.tsx src/pages/RuleEnginePage.tsx src/api/client.ts src/components/alarm-center/alarmCenterModel.ts src/components/alarm-center/alarmCenterModel.test.mjs
  git commit -m "feat(alarm): deliver one simple alarm center"
  ```

## Task 7：纵向验收、发布并部署 1 号机

**Files:**

- Modify: `VERSION`
- Modify: `backend/app/main.py`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md`
- Modify: `CODEX_HANDOFF.md`
- Test: alarm modules, full backend, frontend build, real PostgreSQL

- [ ] **专项验收**

  使用真实 committed L2 输入验证：数值越限、状态相等、同一 CODE_SET 同时包含两个故障码、恢复、确认、重复 delivery、BAD/STALE 质量。断言每个 code 独立事件、重放不重复、非 GOOD 不产生假活动告警。

- [ ] **真实 PostgreSQL 门禁**

  使用名称明确、数据库名含 `_test` 的一次性 PostgreSQL/TimescaleDB 容器运行：

  ```powershell
  cd backend
  python -m unittest tests.test_alarm_configuration_postgres tests.test_alarm_configuration_l2_postgres tests.test_committed_l2_alarm_consumer -v
  ```

  完成后只删除经 `docker inspect` 核对的该测试容器和匿名卷，不触碰本地正式库。

- [ ] **完整本地门禁**

  ```powershell
  python -m unittest discover -s tests -v
  python -m compileall app
  cd ..\frontend
  node --experimental-strip-types src/components/alarm-center/alarmCenterModel.test.mjs
  npm run build
  cd ..
  git diff --check
  ```

- [ ] **准备单一发布候选**

  将版本统一提升为下一个未占用的 `v0.4.85-rc.*`；更新 README 当前状态和 `CODEX_HANDOFF.md`，记录准确测试数、已知限制与部署回滚点。不得改历史 release 记录。

- [ ] **构建一次 ARM64 镜像并固定 digest**

  复用仓库现有 GitHub Actions/发布脚本，等待构建成功后解析 `linux/arm64` digest。部署清单禁止使用浮动 `latest`。

- [ ] **部署 1 号机**

  部署前检查磁盘、当前 digest、容器配置和数据库备份；保持旧容器约束：

  ```yaml
  network_mode: host
  tmpfs:
    - /dev/mqueue
  ```

  只停止旧 ZiZu 容器并切换到新固定 digest，不启动 Caddy、不申请 TLS、不修改 NanoMQ/Neuron 配置。

- [ ] **部署后验收**

  验证容器 `healthy`、restart=0、Schema 正确；在网页完成一条数值规则和一条双故障码规则的“选择—试算—发布—触发—确认—自然恢复”。不得进行设备写入或自动控制试验。

- [ ] **完成提交**

  ```powershell
  git add VERSION backend/app/main.py frontend/package.json frontend/package-lock.json README.md CODEX_HANDOFF.md
  git commit -m "chore(release): prepare minimal alarm center"
  git status --short
  ```

## Completion Gate

- [ ] 左侧只有一个“告警中心”，操作员没有配置入口。
- [ ] 数值、状态、CODE_SET 三类规则均可通过同一流程配置和试算。
- [ ] 多故障码同帧产生独立事件，重复/坏质量没有假告警。
- [ ] 当前告警只统计活动事件，名称和位置可读，确认/恢复语义明确。
- [ ] 启停仍经过不可变修订、配置栅栏、幂等 apply 和提交后可见边界。
- [ ] 无新依赖、无新基础设施、无第二套 matcher、无 JDM 告警入口。
- [ ] 完整后端、真实 PostgreSQL、前端 build 和 1 号机 smoke 全部通过。
