# 1 号机 v0.4.90 HTTP 部署与主干验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.90`
- 修复提交：`317b92c`（snapshot freshness）
- 发布提交：`cfbbbf57b5e6ef962f65c36ae6212c76dddfc102`
- 标签：`v0.4.90`
- GitHub Actions：`33265724519`，成功
- Schema：`051`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:bca774c2c5b50df85ca33bdd2552002091439ccdfa4cb11a3ae9f976fe415b94`
- 1 号机实际 image ID：`sha256:e444717ca41319861c53fc9c5c826693540eed4ba7f7b5b2961ce93499820198`
- 发布目录：`/opt/zizu-release-test-0.4.90`

## 本轮修复

修复 committed-frame 重连或页面刷新后的质量重建：L0 snapshot 现在根据帧头节拍、
`accepted_beat` 与接收时间重新判定 STALE；旧数据或 WARMING 阶段失败关闭。L2 snapshot 根据实体
`freshness_seconds` 与观测时间重新判定 STALE。这样实时流、页面刷新、EMS 工作台和机器消费者不再对
同一份过期数据给出相反质量。

## 自动门禁

- 新增 3 条行为回归测试，RED 时均复现 `expected STALE(1), got GOOD(192)`；修复后专项 7/7 通过。
- 使用真实 PostgreSQL 的 snapshot 集成测试 12/12 通过，并以现场数据克隆确认旧 L0 变为 STALE、
  过期 L2 为 `INPUT_STALE`。
- 后端完整测试：345 tests，134 skipped，0 failure。
- scripts：43/43 通过。
- 前端 Node 测试：45/45 通过。
- TypeScript 与 Vite production build：8185 modules，成功。
- `compileall`、版本一致性与 `git diff --check` 均通过。
- GitHub Actions 发布清单同时包含 amd64/arm64 固定摘要，版本 0.4.90、Schema 051。

## 切换与恢复保护

- 切换前备份：`/opt/zizu-backups/pre-v0.4.90-schema051/omnithings.dump`
- 备份大小：`101,266,755` bytes
- 备份 SHA-256：`061220d6774c653ec6b265850d79e620dece2e7e92734b122a80e7c495b1b50a`
- `sha256sum --check` 通过；`pg_restore -l` 可读，共 792 项。
- 只重建 ZiZu backend；TimescaleDB、NanoMQ、Neuron 未重建。
- 沿用既有 runtime env、业务卷、`network_mode: host`、`tmpfs /dev/mqueue`、
  `restart: unless-stopped`。
- 未启用 Caddy/TLS，未执行 JDM 策略、控制、设备写入或配置发布。
- v0.4.89 固定摘要镜像仍保留，Schema 同为 051，可用于应用级回滚。

1 号机直拉镜像大层超时后，改由本机代理拉取、压缩并传输。压缩包 SHA-256
`03c09c733c997765de24c280ebe35210182c75fa0533d20983bc4bfc7dcda211` 在两端一致；加载后重新拉取
固定摘要只补仓库绑定，并再次核对 image ID、arm64 与版本标签。部署验收后删除服务器上的压缩包和
Docker tar，共释放约 403 MiB；没有删除业务数据或回滚镜像。

## 运行验收

- backend 为 running/healthy，restart count 0；实际 image ID 等于固定摘要解析结果。
- 公网和本机 `/api/v1/health/live` 均返回 `alive / 0.4.90`，公网首页 HTTP 200。
- Schema 051；数据帧 outbox 未发布数量 0；最终记录时 frame head 41233 且持续前进。
- 近 30 分钟日志中 `PoolError`、连接池耗尽、Traceback、CRITICAL、ERROR 为 0。
- 最终根分区约 5.0 GiB 可用，`/userdata` 约 1.6 GiB 可用。

## Browser 主干验收

使用维护账号在应用内 Browser 对公网 HTTP 站执行无副作用验收：

1. **真实节点树**：可展开 `LVK → 工厂电站 → 储能`，选择 PCS 变流器；PCS 显示 45 个 L0 点位。
2. **L0 原始点位**：实时点位的值、质量、时间和来源可见。当前更新点显示“正常”；IGBT、环境温度、
   电感温度及输出 A/B/C 相电流等旧观测显示“超时”。v0.4.89 遗留的刷新后假 GOOD 已关闭。
3. **L1 点位加工**：选择 `交流总有功功率`，仅点击“检查结果”；返回 `0 / 正常`、1 条来源证据、
   帧 40927、配置 28，未误取 IGBT 值，未点击“发布实体”。
4. **L2 标准实体**：PCS 实体显示 `0 kW / 正常`，IGBT 实体显示 `33 / 超时`；展开 PCS 可见
   1小时/6小时/24小时/7天历史入口、来源“交流总有功功率”和加工类型“即时”。EMS 工作台对过期实体
   同步显示 `ENTITY_DATA_STALE`。
5. **告警**：告警中心可读，活动/未确认均为 0；规则页显示 3 组规则，实体候选只列 L2 标准实体。
   未确认、恢复、启停、编辑、试算或发布告警规则。

## 结论与边界

v0.4.90 已部署到 1 号机并通过运行门禁与“节点 → L0 → L1 → L2 → 告警”Browser 主干验收。
本轮只关闭 snapshot freshness 缺陷；不宣称 TLS 网络安全验收、自动策略或真实设备控制已完成。
