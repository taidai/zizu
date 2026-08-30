# 1 号机 v0.4.94 HTTP 部署与验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.94`
- 修复提交：`96d917e7`；发布提交：`e9e1d1ee61ed010979f43267bb8aaf55ef1c912d`
- 标签：`v0.4.94`
- GitHub Actions：`33287783502`，成功
- Schema：`052`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:f92214998ade4eea106db3c922d5c15edc60020487b9653b28759f303536255e`
- 1 号机实际 image ID：`sha256:9c3e7bdea20de95a86f28be063e22389db6228102cee848c291786e633e99365`
- 发布目录：`/opt/zizu-release-test-0.4.94`

## 本轮修复

数据帧每次完成时原来会两次通过 `t_data_frames.created_at = t_l0_observation_dedup.created_at`
重建 L0 latest 状态。该关联既依赖两个时间戳碰巧相等，又会在没有 `created_at` 索引的数据帧表上顺序扫描。
生产 `EXPLAIN ANALYZE` 的旧查询执行约 111.9 ms；系统本来已经在 `t_telemetry_latest.frame_sequence`
保存了唯一帧身份，因此 v0.4.94 改为按 `frame_sequence` 直接关联，现场同结构查询约 14.8 ms。

新增真实 PostgreSQL 回归测试：先完成帧，再故意让 dedup 与 frame 的 `created_at` 不相等；旧实现返回空
L0 状态而 RED，新实现仍按帧序号找到来源并 GREEN。修复没有新增表、迁移、缓存、服务或依赖。

## 自动门禁

- 数据帧真实 PostgreSQL 专项：17/17 通过。
- 后端完整测试：367 tests，145 项需显式外部环境而跳过，0 failure。
- scripts：43/43；前端：49/49；TypeScript/Vite production build 成功。
- `git diff --check` 对本轮文件通过；变更文件可编译。
- GitHub 双架构发布流水线成功；下载后的 `release.json` 经 `release_preflight.py` 复核为
  `0.4.94 / Schema 052 / amd64+arm64` 固定摘要。

## 切换与恢复保护

- 切换前备份：`/opt/zizu-backups/pre-v0.4.94-schema052/omnithings.dump`
- 备份大小：`99,606,431` bytes
- 备份 SHA-256：`017cfce59a9cbf1b1e406095ebb9f1e9290727849845ce44a399fb79d7d1445c`
- `pg_restore -l` 可读；容器内临时备份在复制和校验后已删除，固定主机备份保留。
- 第一次拉取镜像时 SSH 连接中断；当时尚未切换，旧 v0.4.93 backend 仍 healthy。重新连接核对后只重试拉取。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron/easyread 未重建。
- 保持既有 runtime env、`network_mode: host`、`tmpfs /dev/mqueue`、`restart: unless-stopped` 和业务卷。
- 未启用 Caddy/TLS，未发布实体、未启停告警/JDM，未执行控制或设备写入。

## 运行证据

- 匿名 liveness 返回 `alive / 0.4.94`；backend 为 healthy、restart 0、arm64，实际 image ID 与固定摘要一致。
- 最终帧头 `67,321`，未完成帧 0，最老未完成帧龄 0，未发布 outbox 0；JDM 收据仍为
  `16,158 / max frame 61,891`，没有重新出现空规则逐帧写入。
- v0.4.93 在切换前仍持续积压：02:32-02:42 UTC 各分钟约 23-62 帧，绝大多数在 60 秒预算后失败；
  v0.4.94 启动后把遗留队列按既定预算终态化，未保留半帧。
- 现场最后一条 telemetry 接收于 02:42:59 UTC。03:14 UTC 复核时最近 10 分钟没有新帧，因此本轮只能证明
  “切换后队列收口、outbox 为零、容器稳定”，尚不能证明持续 1 秒活负载下吞吐已经达标。
- 一次无活帧资源快照：backend 约 73% CPU、TimescaleDB 约 71%、easyread 约 83%、NanoMQ 约 5%；
  该瞬时值不作为性能承诺。

## Browser 主干验收

- Browser 强制刷新后，前后端均显示 `0.4.94`，Pipeline 运行中、MQTT connected，控制台日志为空。
- 真实节点树可选择 PCS“变流器”；L0 实时有 45 个原始点位，展示值、质量、时间和 Neuron 来源。
  点位停更后明确显示“超时”；L0 历史可选择单点和 1h/24h/7d 范围。
- 从 L0 选择“交流总有功功率”可打开内联 L1：直接使用、倍率与偏移、状态映射、公式计算均可选；
  “检查结果”返回值、质量、来源帧和配置修订。本轮没有点击“发布实体”。
- L2 有 PCS 有功功率与 IGBT 温度两个实体，均按当前时效显示“超时”；PCS 可展开历史、来源与技术证据，
  EMS 工作台同样 fail closed 为 `ENTITY_DATA_STALE`。
- 告警中心活动告警 0；三组规则和来自 L2 的实体选择入口可见。本轮没有启停或发布规则。
- JDM 页面当前无规则；未执行仿真、策略或控制。

## 发现的独立缺陷与结论

- L0“交流总有功功率”已经显示“超时”时，L1“当前试算结果”仍显示“正常”；L2 与 EMS 则正确显示超时。
  这说明候选配置试算没有按当前节拍重判来源质量。它不是本轮 SQL 热查询修复的回归，但违反“质量优先”，
  必须作为下一项主干 bug 修复，且在修复前不得把 L1 试算质量用于发布或控制判断。
- 启动日志仍提示公开可达的实例运行在 insecure development mode 并启用了示例凭据。本文不记录凭据；
  1 号机仍只可视为测试部署，不得宣称生产安全。

结论：v0.4.94 固定摘要部署、自动门禁、容器健康和 Browser 主干无副作用验收已经完成；持续活负载吞吐
验收仍为 `INCOMPLETE`，L1 试算质量一致性为已确认待修缺陷。最短下一步是先修 L1 试算 STALE 传播，
然后在真实点位恢复变化后连续观察至少 5 分钟，要求帧头持续前进、未完成帧和最老帧龄不增长、无新增
`FRAME_PROCESSING_FAILED`，再升级吞吐结论。
