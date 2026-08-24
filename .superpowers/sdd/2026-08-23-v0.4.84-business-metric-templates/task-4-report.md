# Task 4 报告：PostgreSQL 运行投影、封窗与迟到修正

日期：2026-08-24
分支：`ticket/v0.4.84-business-metric-templates`

## 范围

本任务实现普通在线运行投影、自动范围内迟到修正、DataTrunk 提交后提示，以及一秒恢复扫描。Task 5 的审批式审计重算状态机不在本任务实现范围；`recompute()` 只保留明确拒绝的接口结果。

实现没有启用自动策略、没有发送设备写命令、没有连接或部署 1 号机，也没有新增依赖。

## Carry-over：累计量窗口边界

Task 3 五轮后留下的真实 load-bearing finding 是累计量事件过滤和累计量采样过滤没有共享窗口谓词，导致 `aligned_daily` 的右边界事件可能污染前一日。

### RED

先在 `backend/tests/test_metric_projection.py` 增加独立手算边界测试，覆盖日窗 `end-1µs`、`end`、`end+1µs` 下的非法事件 ID、错误 unit、重复冲突和 evidence，并补充 rolling 右端点行为。原实现出现 4 个失败：`observed_at == end` 的事件进入了日窗。

### GREEN

在 `metric_projection.py` 增加共享 `_counter_range_contains(window, instant)`，并让 `_project_relevant_events` 与 `_counter_relevant_samples` 共同使用。结果语义固定为：

- `aligned_daily`：`[start, end)`，排除 `observed_at == end`；
- right-closed rolling：`(start, end]`，包含 `observed_at == end`。

聚焦边界测试 4/4 通过；Task 3 全套扩展到 98/98 通过。该修复与测试随 Task 4 提交。

## PostgreSQL 运行时

新增 `MetricProjection.observe_committed/advance/recompute`：

- `observe_committed(event_ids)` 仅作为提交后 wake-up；权威输入始终由 PostgreSQL 扫描得到，提示丢失只会延迟到下一次一秒 tick；
- 每安装使用 `pg_advisory_xact_lock`，只读取安装时冻结的 source binding；
- 可信独立 `observed_at` 优先，否则回退 `received_at`，并把 `timeBasis`、有效时间和事件身份写入规范来源摘要；
- L2 输入 unit 来自冻结 binding 的稳定实体定义，BAD/空值事件仍携带 unit；
- provisional 只 upsert `t_business_metric_projections`，不写 L2 历史；
- completed/corrected 在一个事务内写 L2 observation/latest、`t_l2_observation_sources`、outbox、window result 和 projection checkpoint；
- invalid 只写 window result，不写 L2；
- L2 event ID 由 installation、window 和 result revision 生成 UUID5；内容摘要不含进程级 runtime instance ID，因此重复 tick、重启和同一迟到事件重放幂等；
- 范围内迟到事件新增 corrected revision，旧 L2 不覆盖；
- 自动修正范围按当前枚举实现 rolling 6h、daily 7d；本版本无 monthly 窗口枚举，因此没有虚构 monthly 路径；
- 冻结 counter 异常 fail closed 为质量下降或 invalid，不会切换到未冻结的 power source。

`main.py` 把一秒循环接入既有 lifespan：安全启动、stop event、任务 gather 后关闭；模块 import 不启动线程。时钟可由 runtime 构造参数注入。

## DataTrunk 提交后提示

`DataTrunk` 只在 repository transaction 成功返回以后向 observer 传递已提交 L2 event IDs。observer 异常被隔离，不能回滚事实、改写 receipt 或把 `ingest()` 标记为失败。PostgreSQL builder 与 formula trunk 都使用惰性 runtime singleton，避免 import 时建立连接或启动线程。

## TDD 记录

### Task 4 RED

- `tests.test_metric_projection_postgres` 首次运行因 `metric_projection_postgres` 模块不存在而失败；
- DataTrunk post-commit 测试首次运行因构造器不接受 `projection_observer` 而失败；
- 空历史窗口最初错误生成 8 个 invalid 结果；通过仅枚举已有结果或存在冻结来源事件的窗口修正；
- current projection 推进到下一日时首次触发 Schema 043 projection immutable guard；新增真实 PostgreSQL 回归后，收窄 guard 为只冻结 installation identity，允许 checkpoint 恢复更新；
- 更换 runtime instance 后重启重放首次错误生成 corrected revision；从内容摘要移除 runtime instance identity 后恢复幂等；
- received-at fallback 的正式 evidence 首次记录成 `observed_at`；收紧可信判定后记录为 `received_at`。

### Task 4 GREEN 覆盖

`backend/tests/test_metric_projection_postgres.py` 当前包含 9 个真实 PostgreSQL 行为测试：

1. provisional projection 不产生 L2 history；
2. 重启与重复 tick 幂等；
3. runtime instance 变化仍不产生伪 correction；
4. checkpoint 可推进下一日窗口；
5. 范围内 late event 新增 corrected revision，重放不新增；
6. received-at fallback 写入正式 source summary；
7. 冻结 counter BAD 不切换 power source；
8. invalid 封窗只写 ledger，不写 L2；
9. L2、result、source evidence、outbox、checkpoint 五个注入点分别证明整笔回滚。

`backend/tests/test_data_trunk_postgres.py` 增加提交后可见性和 observer 失败隔离测试。

## 事务证据

五种故障注入均在同一安装事务内抛出，测试随后从独立连接断言以下状态全部没有部分写入：正式 L2、latest、来源 evidence、outbox、window result、projection checkpoint。正常 completed/corrected 路径则同时满足 Schema 043 的 method/source/time/runtime 强绑定和 L2 复合外键。

## 数据库迁移兼容修正

运行时必须滚动 projection 的 window/checkpoint。原 Schema 043 guard 禁止改变 window start/end，使下一窗口恢复必然失败。新增 PostgreSQL RED 后，将 guard 收窄为禁止改变 `installed_metric_id`，继续禁止 delete/truncate，同时允许受限 checkpoint update；migration fingerprint 同步更新。既有非法 identity update、delete 和 truncate 门禁继续由 Task 2 回归覆盖。

## 验证记录

已完成的阶段性验证：

- carry-over 聚焦边界：4/4 PASS；
- Task 3 全套：98/98 PASS；
- DataTrunk PostgreSQL：20/20 PASS；
- DataTrunk in-memory：1/1 PASS；
- Task 1/2 内存相关组合：37/37 PASS；
- Schema 043 migration replay/损坏函数聚焦：2/2 PASS；
- Task 4 PostgreSQL + DataTrunk PostgreSQL fresh 联合门禁：29/29 PASS（193.168s）；
- Task 2 migration/business metric/point PostgreSQL fresh 回归：72/72 PASS（3413.192s）；
- 无数据库 fresh 联合门禁：126/126 PASS（5.022s），其中 DataTrunk in-memory 1、Task 3 全套 98、Task 2 内存交付 9、Task 1 编译回归 18；
- `py_compile` 覆盖 5 个改动生产模块和 3 个改动测试模块：PASS；
- `git diff --check`：PASS。

无数据库门禁首次调用遗漏 `PYTHONTZPATH`，因此本机 `ZoneInfo` 无法加载 `Asia/Shanghai` 并产生环境级联失败；未改代码，补齐任务指定 tzdata 路径后同一命令 126/126 通过。目标提交信息为 `feat(metrics): persist projections and immutable results`。

## 明确未做

- Task 5 审计重算的 requested/approved/running/completed|failed 状态机、角色审批和超范围重算；
- REST/UI、验收报告与性能固定数据集（后续任务）；
- monthly 自动修正路径（当前版本没有 monthly 窗口枚举）；
- 自动策略、设备写、现场部署和依赖安装。

## 风险与后续

- Schema 043 冻结 binding 尚未持久化 counter 位宽、明确 reset/rollover 契约；本任务用存储类型上界和“下降即歧义”的 fail-closed 规则，安全但可能把现场合法复位判为 invalid。完整工业 counter 契约应在后续 schema/安装计划中冻结。
- `maximum_sample_gap_seconds` 当前从 source entity freshness 读取，而 freshness 尚未复制进 metric binding；来源定义被不当修改时可能改变运行质量判断。后续应把它冻结进安装配置。
- 当前恢复模型按安装扫描来源，正确性优先；100 个统计实体的一秒 p95 性能由 Task 8 固定数据集门禁最终确认。
