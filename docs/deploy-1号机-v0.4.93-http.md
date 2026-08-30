# 1 号机 v0.4.93 HTTP 部署与验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.93`
- 发布提交：`9b96a136c49d9e687d24565d270ee46d1a9d7e95`
- 标签：`v0.4.93`
- GitHub Actions：`33284415513`，成功
- Schema：`052`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:a0a6e21161b50819fcd3d916f090ea2fa012ad281e77a07d140f8a6ad13c76ea`
- 1 号机实际 image ID：`sha256:27e680b9b18570ca3678e89f92005273492c829fabed8cd4e74ece4f2d6ae37a`
- 发布目录：`/opt/zizu-release-test-0.4.93`

## 本轮修复

- v0.4.91 将 JDM 接入 committed L2 后，现场没有 JDM 规则时仍为每个终态帧执行一次活动模型查询并写空消费收据，
  形成数据库热路径；v0.4.92 先停止空收据写入。
- v0.4.93 再按 `configuration_revision` 缓存“当前修订没有活动 JDM 模型”的事实。同一配置修订的后续帧
  不再打开 PostgreSQL 事务；配置修订变化会重新查询，存在活动模型时仍逐帧执行修订校验、原子收据和执行事实。
- 现场零规则期间 JDM 收据保持 `16,158` 条、最大帧序号 `61,891`，没有继续随帧写空收据。

## 自动门禁

- 后端完整测试：366 tests，144 项需要显式外部环境而跳过，0 failure。
- 真实 PostgreSQL JDM 专项：11/11 通过；scripts：43/43；前端：49/49；生产构建成功。
- v0.4.93 GitHub 发布流水线成功；下载后的 `release.json` 经 `release_preflight.py` 复核，版本 `0.4.93`、
  Schema `052`、amd64/arm64 固定摘要完整。
- 部署后重新运行 JDM 单元测试 10/10 通过，其中覆盖同一空修订只加载一次、修订变化重新加载、
  重放幂等、失败回滚和 fail-closed 执行事实。

## 切换与恢复保护

- 切换前备份：`/opt/zizu-backups/pre-v0.4.93-schema052/omnithings.dump`
- 备份大小：`92,722,591` bytes
- 备份 SHA-256：`c1efad0fe17ee8f579100bacffe4c97ade7f9442f209c79677f3dec84be2280f`
- `pg_restore -l` 可读，共 766 项。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron/easyread 未重建。
- 保持既有 runtime env、`network_mode: host`、`tmpfs /dev/mqueue` 和 `restart: unless-stopped`。
- 未启用 Caddy/TLS，未发布实体或配置，未启用 JDM/告警策略，未执行控制或设备写入。

## 运行证据

- 匿名 `/api/v1/health/live` 返回 `alive / 0.4.93`；backend 为 healthy、restart 0、arm64，镜像摘要和
  image ID 对应，真实错误日志匹配 `Traceback/PoolError/ERROR/CRITICAL` 为 0。
- 部署后从 01:05:11 UTC 到 01:10:40 UTC 连续 11 次、约 30 秒一采样：未完成帧均为 0、最老未完成帧龄为 0、
  最近 60 秒新失败均为 0、未发布 outbox 均为 0，JDM 收据数量与最大序号保持不变。
- 切换时遗留的超龄帧没有伪装成成功：共有 93 帧在当前运行实例启动后按 60 秒帧龄预算结算为
  `FRAME_PROCESSING_FAILED`，最后一条结束于 01:05:00 UTC，`attempt_count=0`。这是 v0.4.92 已形成的积压事实，
  v0.4.93 保留了失败证据。
- 01:03:26 UTC 后现场没有创建新数据帧；最后 5 分钟 `t_telemetry` 也没有新增观测。因此本轮证明了
  “队列已收口、空 JDM 热写已停止、空闲运行稳定”，但没有得到持续 1 秒新帧负载下的生产吞吐证据。
- 一次资源快照：backend 约 68% CPU、TimescaleDB 约 58%、easyread 约 88%、NanoMQ 约 4%；该瞬时值
  只用于定位，不作为性能承诺。

## Browser 主干验收

- Browser 确认前后端均为 `0.4.93`，Pipeline 运行中、MQTT connected，控制台 error 为 0。
- 真实节点树可选择 PCS“变流器”；L0 实时可见 45 个原始点位及值、质量、时间和 Neuron 来源。
  现场 PCS 数据停更后这些点位按设计显示“超时”，没有伪造 GOOD；L0 历史可选择单点并查看明细。
- 从 L0 选择“交流总有功功率”后，L1 内联加工表单可选择直接使用、倍率与偏移、状态映射或公式计算；
  “检查结果”可完成试算。本轮没有点击“发布实体”。
- L2 可见 PCS 有功功率与 IGBT 温度两个标准实体，并可展开历史、来源和技术详情；当前质量为超时，
  PCS 有功功率明确显示 `FRAME_PROCESSING_FAILED` 来源原因，没有把失败值伪装为正常值。
- 告警中心当前活动告警为 0，既有三条告警规则可查看；JDM 页面当前无规则；固定 EMS 工作台只读 L2，
  因实体超时明确显示 `ENTITY_DATA_STALE`。未修改或启停任何规则，未进入控制动作。

## 当前结论

v0.4.93 已按固定摘要安全部署，自动门禁、容器健康、零 outbox、空 JDM 收据停止增长和 Browser 主干只读
验收均有证据。吞吐根因修复的生产验收仍标记为 `INCOMPLETE`：现场在观察窗口内没有新的有效点位观测，
无法证明持续 1 秒新帧负载下仍无积压。最短补证是在真实点位恢复变化后连续观察至少 5 分钟，要求 frame head
持续前进、未完成帧和最老帧龄不增长、无新增 `FRAME_PROCESSING_FAILED`，再把结论升级为通过。
