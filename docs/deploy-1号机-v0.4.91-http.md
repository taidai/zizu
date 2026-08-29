# 1 号机 v0.4.91 HTTP 部署与验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.91`
- 发布提交：`20f00cec18445d528b115b5f1490557d09a7cd22`
- 标签：`v0.4.91`
- GitHub Actions：`33270779281`，成功
- Schema：`052`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:d8d5e8b184e2718cf2e9039a2d8976484376cda107c563ce90923a9334726613`
- 1 号机实际 image ID：`sha256:1f6c4f02bf0e23998e2ad0a7e28746a4d4e86c6f2b740ccce24fb934784e6980`
- 发布目录：`/opt/zizu-release-test-0.4.91`

## 本轮交付

- JDM 正式运行时只消费不可变 committed L2 数据帧，不再扫描 L0 或 latest 表。
- 每帧 `jdm` 收据与全部规则执行事实在一个 PostgreSQL 事务中提交；失败时整体回滚，重放幂等。
- 规则创建、修改、删除统一经过配置栅栏，并绑定全局配置修订；旧 alarm/fault-map JDM 行只读。
- 删除旧 `rule_engine.py`、JDM 直写告警适配器和规则模板直接插入 `t_rules` 的绕过入口。
- 生产 fanout 固定为“告警 → JDM → committed 实时流”。规则页只读显示最近判断或拒绝原因，
  “已完成判断”不表示设备控制成功；本轮不执行动作。

## 自动门禁

- 真实 PostgreSQL JDM/Schema/API/outbox/startup 专项：37/37 通过。
- 后端完整测试：362 tests，142 项需要显式外部环境而跳过，0 failure。
- scripts：43/43 通过。
- 前端 Node 测试：49/49 通过。
- TypeScript 与 Vite production build：8186 modules，成功。
- `compileall`、六处版本一致性与本轮文件 `git diff --check` 通过。
- GitHub 发布清单确认版本 `0.4.91`、Schema `052`，同时包含 amd64/arm64 固定摘要。

## 切换与恢复保护

- 切换前备份：`/opt/zizu-backups/pre-v0.4.91-schema051/omnithings.dump`
- 备份大小：`118,232,614` bytes
- 备份 SHA-256：`7b35f4f4e96049cdac7e8d3eddeafb7990f9b0a477a3f625a9317060df2be914`
- `sha256sum --check` 通过；`pg_restore -l` 可读，共 826 项。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron 未重建。
- 沿用原 runtime env、业务卷、`network_mode: host`、`tmpfs /dev/mqueue`、
  `restart: unless-stopped`，并保留 v0.4.90 固定摘要作为应用级回滚镜像。
- 未启用 Caddy/TLS，未启用规则，未执行控制、设备写入或配置发布。

## 运行验收

- backend 为 healthy，restart count 0，架构 arm64；容器 image ID 与固定摘要解析结果一致。
- 本机和公网 `/api/v1/health/live` 返回 `alive / 0.4.91`，公网首页 HTTP 200。
- Schema 052；配置修订 28；数据帧 outbox 未发布数 0。
- 记录时 frame head 45789；`jdm` 收据 56 条并前进到 45789，证明生产 fanout 正在消费新帧。
- JDM 执行事实 0 条：现场没有启用的 control/linkage 规则，本轮没有为验收自动创建或启用策略。
- 真实错误日志匹配 `Traceback/PoolError/ERROR/CRITICAL` 为 0；迁移日志为 `errors=0`。
- 根分区约 4.9 GiB 可用；`/userdata` 约 1.3 GiB 可用。

## Browser 主干验收

当前状态：`FAILED`。

- 用户授权登录后，Browser 已完成无副作用主干点击；未发布实体、未启用或修改规则、未下发控制、
  未写设备。
- 真实节点树可选择 PCS“变流器”；L0 实时显示 45 个原始点位及值、质量、数据时间和 Neuron 来源。
  L0 历史可选择 45 个点位，`交流总有功功率` 的 1 小时趋势图已渲染。
- L1 内联加工入口可从 L0 选择点位，支持直接使用、倍率与偏移、状态映射和公式计算。只读试算
  `交流总有功功率` 返回 `0 / 正常`、1 个来源、帧序号和配置修订 28，未点击“发布实体”。
- L2 可看到 `PCS 有功功率` 与 `pcs.igbt`，实体历史趋势、来源和技术详情均可展开；技术详情能追到
  processing revision、配置修订、source digest 和观测/投影帧序号。
- 告警中心可查看当前告警和三条规则；数值实体选择包含储能电表与 PCS 的 L2。JDM 页面可打开，
  现场没有 control/linkage 规则，因此显示暂无规则；EMS 工作台只读取 L2。

验收期间发现运行红线失败：L2 与 EMS 反复在“正常”和 `ENTITY_DATA_STALE` 之间跳变，L2 曾明确显示
“本次点位加工失败”。只读数据库证据表明这不是前端假象：最近 20 分钟约有 500 个数据帧以
`FRAME_PROCESSING_FAILED` 终态结束，另有约 60 个未完成帧，最老帧龄约 60 秒；最近 30 秒新建 29 帧，
仅 14 COMPLETE、14 FAILED，处理器没有追上统一 1 秒节拍。FAILED 帧 `attempt_count=0`，说明它们尚未
进入业务处理就因 60 秒帧龄预算被终结。日志没有 Traceback/PoolError，连接池也没有耗尽；现场负载约为
backend 94% CPU、TimescaleDB 116% CPU、系统 load average 10.65，说明当前真实负载下帧处理吞吐不足。

## 当前结论

v0.4.91 已安全部署，服务器存活和自动门禁通过，但 Browser 主干业务验收失败，不能声称交付通过。
下一步只处理“统一 1 秒节拍下帧处理吞吐不足”这一根因；在 L2 连续稳定、无帧龄超限失败之前，不扩展
告警、JDM、控制或页面功能。
