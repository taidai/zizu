# 1 号机 v0.4.97 HTTP 部署与主干验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.97`
- 发布提交：`942a2ef710b5eec7c7cdf12299b8ac65eab86e86`
- 标签：`v0.4.97`
- GitHub Actions：`33296893256`，成功
- Schema：`053`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:3cd6c383c408a1d18b2cd80671031c20764e0f84fcbe58f7590897d29cb09889`
- 1 号机实际 image ID：`sha256:27613f5c87cec977ed9a78187d62454398a233b8e45bcf43644e196d08383d22`
- 发布目录：`/opt/zizu-release-test-0.4.97`

## 本轮修复

v0.4.96 重启后虽然 MQTT 消息继续进入，但数据帧处理停在旧 frame head。根因是实时黑板恢复了最后一份已
提交观测，却没有把它们记为当前活动配置修订已见过的完整基线；稀疏点位未在同一重启窗口全部重发时，系统
会一直停在 WARMING，后续帧无法继续。

v0.4.97 在 `RealtimeBlackboard.restore()` 中把同一活动修订的已提交观测恢复为可信基线。新鲜度推进仍会
把长期不更新的输入转成 STALE；缺少已提交必需观测时仍保持 WARMING，不会伪造齐全或 GOOD。回归测试
`test_restart_uses_complete_committed_baseline_without_waiting_for_sparse_inputs` 先在旧实现 RED，再在最小修复后
GREEN。

## 自动门禁与恢复保护

- 后端：370 tests 通过，147 项外部环境型 skip，0 failure。
- scripts：43/43；前端：49/49；TypeScript/Vite production build、compileall 和发布范围 diff check 通过。
- GitHub 双架构镜像流水线成功，release manifest 为 `0.4.97 / Schema 053 / amd64+arm64` 固定摘要。
- 切换前备份：`/opt/zizu-backups/pre-v0.4.97-schema053/omnithings.dump`，大小 `109,227,098` bytes，
  SHA-256 为 `f47d4f05e0d68bd2715a0bead27a4aa2839b5e3126cc4504d779add946a67848`；`pg_restore -l`
  可读，共 854 行目录。
- 只重建 backend；TimescaleDB、NanoMQ 和 easyread 未切换。继续保持 `network_mode: host`、
  `tmpfs /dev/mqueue`、`restart: unless-stopped`、`no-new-privileges`、`cap_drop: ALL` 和原业务卷。
- 1 号机拉取 GHCR 过慢时改为本机拉取精确 ARM64 摘要、校验后传输和加载。验收后已删除远端 98,250,098
  bytes 的临时传输包，备份和业务数据未删除；根分区可用空间约 4.3 GB。
- 未启动 Caddy/TLS，未发布实体、确认告警、启停规则、运行 JDM、下发控制或写设备。

## 运行证据

- 匿名 liveness 返回 `alive / 0.4.97`；backend 为 running/healthy、restart 0，实际 image ID 与发布镜像一致。
- 数据帧从部署后的 76111 持续推进到 77570；最终瞬时未完成帧 20 个，范围 77551..77570，属于约 20 秒的
  有界在线队尾；未发布 outbox 为 0。
- 最后 5 分钟为 `COMPLETE 276 / PENDING 19 / PROCESSING 1 / FAILED 0`，没有新增帧龄失败。
- Schema 053 的 claim 查询命中 `ix_data_frames_claim`，2 个 shared buffer hit，执行约 0.228 ms。
- 最近 30 分钟日志未发现 ERROR、Exception、Traceback 或 `t_release_locks` 错误；迁移显示 053 已应用且
  `errors=0`。

## Browser 主干只读验收

- 前后端均显示 `0.4.97`，Pipeline 运行中、MQTT connected，消息数、入库数和最后消息时间持续前进。
- 真实节点树可选择 `LVK → 工厂电站 → 储能 → 变流器/储能电表/电池簇`。
- L0：PCS 变流器显示 45 个原始点位，实时值、质量、数据时间和 Neuron 来源可见；持续更新点为正常，停更
  点明确显示超时。历史页选择 IGBT 温度后可查看逐秒明细和真实值变化。
- L1：在 L0 勾选原始点位后，“加工为实体”展开内联表单，可定义实体名称、直接使用、倍率与偏移、状态映射
  或公式计算、单位和类型，并先检查结果再发布。本轮只展开检查入口，没有发布。
- L2：PCS 节点有 2 个标准实体；PCS 有功功率实时为 `0 kW / 正常`，展开可查看历史趋势、原始来源和即时
  加工证据。IGBT 实体也能显示实时卡片和来源。
- 告警：当前活动告警为 0；规则页有 3 个既有规则卡片，其中 1 个启用，实体选择只列 L2 标准实体；本轮
  未编辑、启停或确认。
- JDM：规则引擎页面可访问，现场暂无规则。控制页可访问，但当前没有可控 L2 实体。固定 EMS 工作台可访问，
  PCS 有功功率正常；停更的 IGBT 和并网实体按 `ENTITY_DATA_STALE` 失败关闭。

## 结论与待办

v0.4.97 的重启恢复缺陷已关闭：在线帧持续前进、处理队尾有界、outbox 排空、最近窗口没有失败，固定摘要
部署和主干只读验收完成。L0 → 内联 L1 → L2 已具备可见、可检查的使用闭环。

验收同时发现一处下一轮应优先统一的展示差异：L2 标准实体卡片仍按数据库保存质量把较旧 IGBT 显示为
“正常”，而 EMS 工作台按当前新鲜度正确显示 `ENTITY_DATA_STALE`。应让所有人机页面复用同一份 L2 当前有效
质量投影，避免同一实体在不同页面出现不同结论。JDM 尚无规则、控制尚无可控实体，这两项是未配置状态，
不是本次部署故障。

启动日志仍提示 insecure development mode 和示例凭据。1 号机当前是公网 HTTP 测试部署，不能宣称达到
生产安全要求；本文不记录账号密码。
