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

`backend/tests/test_metric_projection_postgres.py` 首轮包含以下 9 个真实 PostgreSQL 行为测试；独立复审修正后已扩展到 21 个，新增边界见后文：

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

- 首轮报告中的 counter 契约与 freshness 未冻结风险已在本轮关闭：Schema 043 binding 现在冻结 producer digest、maximum sample gap、16/32/64-bit maximum 与 reset/rollover 规则，运行时不再猜测。
- 当前查询结构已从全历史/逐窗口 result 查询改为 checkpoint 后有效时间有界的来源扫描和一次批量 latest-result 查询；100 个统计实体的一秒 p95 数值门禁仍按计划留给 Task 8 固定数据集。
- 超出 rolling 6h / daily 7d 自动范围的迟到事实继续保留给 Task 5 审批式审计重算；本任务不会静默修正超范围结果。

## 首轮独立复审修正（进行中）

首轮独立复审判定 SPEC/QUALITY FAIL。确认的共同根因是 Schema 043 尚未把运行计算所需事实全部冻结：projection guard 只保护 installation ID，source binding 缺 producer/freshness/counter 契约，L2 缺显式 event-time basis；运行时因而读取可变 entity 元数据、猜测 counter 上界、全历史扫描并逐窗口读取 latest result，也没有使用 projection checkpoint 推导恢复范围。

本轮按 RED→GREEN 修正以下边界：

- projection 同窗恢复和合法下一窗滚动的数据库 fail-closed guard；
- producer output/freshness/counter 契约安装冻结与每事件精确复核；
- L2 显式 `event_time_basis`、原始 FK 身份与 effective event time 证据；
- checkpoint 驱动的首次结果补算、rolling 有界读取和批量 latest result；
- `observe_committed` 只排队 hint，不同步扫描所有安装；
- empty/current provisional、逐安装错误隔离、freshness post-commit 通知；
- canonical source summary 与 latest rollback 的直接断言。

Task 5 审批重算状态机仍不在修正范围内。

### 复审修正阶段性 RED/GREEN

- projection guard 恶意逐字段/回退/跨窗跳跃测试先出现 5 个失败，证明旧 guard 允许任意 checkpoint 改写；专用 trigger 校验同窗单调恢复、冻结 daily/rolling 合法边界及恰好下一窗后，聚焦测试 1/1 通过。
- counter source 强类型契约测试先因 parser 拒绝 `counter` 字段而 RED；新增 16/32/64 位上界、reset/rollover 互斥语义与 canonical digest 后聚焦 1/1 通过。
- 安装 preview 冻结 producer freshness/digest/counter 测试先因 candidate 不接受 producer contract 而 RED；扩展强类型 plan 后聚焦 1/1 通过。
- 显式 received-time basis 对“未来 observed timestamp”测试先因 `RawObservation` 无该字段而 RED；L0→L2 转换显式传播 basis、聚合输出 fail-closed 回退 received 后聚焦 1/1 通过，不再以时间大小猜可信性。

### 复审修正已闭环行为

1. **Projection guard**：Schema 043 trigger 现在校验安装冻结的 daily/rolling window contract；同窗只允许受表约束的恢复字段更新且 watermark/updated 不回退；跨窗只允许 daily 紧邻下一日或 rolling 恰好下一分钟，并要求携带与新窗口一致的重置 state。identity、delete、truncate、回退、任意跳窗和 carry-over state 全部 fail closed，函数 fingerprint 已同步。
2. **冻结 producer/counter/freshness**：模板和安装计划冻结 producer output/revision digest、source definition/data type/unit、maximum sample gap 以及 counter maximum/bit width/reset/rollover。每条 L2 通过 processing revision、output binding 和 entity identity 精确定位不可变 producer output；多输出 revision 不再误取排序首项。失配事件按其真实 producer metadata 记录并以 `SOURCE_CONTRACT_MISMATCH` fail closed。
3. **Checkpoint 恢复**：首次没有 checkpoint 时从最早冻结来源事件开始，daily 每 tick 最多 64 窗、rolling 最多 360 个一分钟推进窗；首次正式结果不受自动修正 horizon 截断。已有结果才应用 rolling 6h / daily 7d 修正 horizon。9 天停机后的首个日结果和重复重启均由真实 PG 测试锁定。
4. **显式时间可信性**：Schema 043 为 L2/latest/source evidence 增加 time basis；DataTrunk 从 parser 明确的 timestamp presence 传播 observed/received basis，freshness deadline 使用 effective event time。window result 同时保存原始 `(event_id, observed_at)` FK 与 first/last effective time，排序和范围约束只使用 effective time；未来原始 observed 配 received fallback 不再导致封窗回滚。
5. **Rolling 查询结构**：checkpoint 后 source bounds 查询限制为当前受影响有效时间或新接收事件；事件读取按最早受影响窗到最新窗有界；所有待处理窗口的 latest results 用单个 `DISTINCT ON` 查询批量读取。query spy 证明少量 rolling tick 只发一次 result 查询，不存在每窗 362 次查询。
6. **Current lifecycle**：没有任何相关冻结来源事件时不创建 projection；有来源的活动窗无论 GOOD/UNCERTAIN/BAD 都序列化为 `provisional`，原因和质量独立表达；`invalid` 只存在于已封闭 window-result ledger。
7. **故障隔离**：每 installation 独立 advisory-xact-lock/transaction；领域或单安装数据错误回滚该安装、增加 receipt error 并继续下一安装，连接级 `OperationalError/InterfaceError` 继续上抛。双安装首个故障、第二个完成以及并发双 tick 的真实 PG 测试均通过。
8. **Canonical source summary**：摘要保存有序冻结 source entity list、eventCount、首尾原始/有效时间、逐事件 timeBasis、事件身份/时间/value/quality/source digest/producer mismatch 的内容摘要；runtime instance identity 不进入 result content digest，重启不会制造 correction。
9. **Freshness observer 与主循环**：freshness repository 返回已提交 event IDs，和 ingest/formula 共用同一 post-commit observer seam，observer 失败隔离。主循环只在 lifespan 内创建任务，stop event 后 gather，import 不启动线程；`MetricProjection.advance()` 默认使用可注入 clock，循环保持一秒周期。

### 新增 RED→GREEN 证据

- carry-state 精确下一窗最初被旧 guard 接受；新增 window identity state 与跨窗 reset 校验后 GREEN。
- rolling 后续 tick 的 source-bounds SQL 最初没有有效时间下界；增加 checkpoint 下界或新接收事件条件后 query-count 测试 GREEN。
- 多输出 producer revision 最初按 output key 误取 decoy，产生 `SOURCE_CONTRACT_MISMATCH`；改为 processing revision + immutable output binding + entity identity 精确 join 后 GREEN。
- `t_l2_observation_sources` 最初没有 source time basis，freshness 对未来 untrusted observed 也按原始时间计算 deadline；Schema 043 扩展和 effective-time 读取后两个 PG 用例 GREEN。
- window evidence trigger 最初仍按原始 observed 排序，未来 untrusted observed 使合法 effective 顺序封窗回滚；trigger/acceptance 强绑定切换到 effective 范围后 GREEN。
- 主循环最初直接调用 `datetime.now()`，绕过 runtime 注入 clock；`advance(now=None)` 使用构造器 clock 且 main 不再自行取时后 GREEN。
- 16/32/64-bit rollover、reset、并发 tick、双安装隔离、`t_l2_latest=0` 故障回滚均新增真实 PG 锁定。
- Task 2 PostgreSQL 首次复跑暴露 5 个兼容问题（3 个旧 acceptance fixture 未填写 effective source time、1 个 point runtime fixture 未应用 043、1 个 revision-upgrade source plan 错判为 update）。修复后原 5 项以及 4 个补强的 window evidence 负向用例聚焦 9/9 GREEN；source plan loader 现在从上一安装读取完整 freshness/producer/counter 冻结合同，未变化合同恢复为 `preserve`，而不是放宽断言。
- Task 2 migration/business metric/point PostgreSQL 最终全量门禁：74/74 PASS（389.270s）。
- Task 4 projection + DataTrunk PostgreSQL 最终全量门禁：44/44 PASS（927.922s）；五阶段故障注入首测运行异常偏慢但持续推进，无 deadlock、OID 或数据库重建事件。
- 最终无数据库联合门禁：152/152 PASS（5.081s）；parser：7/7 PASS（0.30s）。
- 最终静态门禁：21 个改动 Python 文件 `py_compile` PASS，`git diff --check` PASS。
- counter reset 与 rollover 同时启用的资产合同先新增 RED（1 个失败），领域校验与 Schema 043 互斥约束对齐后聚焦 2/2 GREEN；point-processing source 选源对可空 unit 严格读取冻结 output，不回退到可变 entity 元数据，相关 plan/runtime counter PG 聚焦 3/3 GREEN。
