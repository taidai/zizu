# 1 号机 v0.4.86 HTTP 部署记录

## 结果

- 部署时间：2026-08-29（Asia/Shanghai）
- 版本：`0.4.86`，最终标签：`v0.4.86-hotfix.6`
- 源码：`bca5e33c3a4e8e64a07270823f5b73f8d6eceac0`
- GitHub Actions：`33214331065`，成功
- linux/arm64：`ghcr.io/taidai/zizu@sha256:07396b534b8c44f0947517aa00cbc29e45ed920a48567da65fd704ad4f7b183d`
- 容器 image ID：`sha256:e1b8f6cbba010e2a6b55fc0cb6478c978f71d5ec5b7d1cdf0e082bc2c5a996c8`
- Schema：050
- 发布目录：`/opt/zizu-release-test-0.4.86`

## 切换前保护

- 备份：`/opt/zizu-backups/pre-v0.4.86-schema049/omnithings.dump`
- 大小：4,165,420 bytes
- SHA-256：`858b75d6d656667c6143834b0e78eb804bf9d4c2ad9742497db28ab89c72a3ac`
- `sha256sum` 与 `pg_restore -l` 已验证，归档清单 1,235 行。
- Schema 050 已成功应用；未删除配置、终态帧、失败证据或业务历史。

## 交付功能

- 节点树支持创建、编辑、移动和安全退役，层级由服务端根据父节点计算。
- Neuron 点位导入支持多组选择、零副作用预览、冲突阻断、摘要确认和原子应用。
- PCS 节点可查看全部 committed L0 原始点位及历史，不再只显示 L1 已绑定输入。
- 管理员可维护版本化 L1 点位加工模板；工程师可选择、绑定、预览并发布。
- L2 全局实体提供 committed 实时值、质量、来源与历史；上层继续只读 L2。

## 现场发现并修复

- 旧版本长期积压帧使配置栅栏超时。没有清空账本，而是按 60 秒预算逐帧形成 FAILED、失败事实和 outbox；
  过期 L0 不再晋升 latest，时间窗口限制历史检索，相同 L2 STALE 不再重复写历史。旧积压最终归零。
- Neuron 导入预览成功但应用 500 的根因是新建 `t_tags` 漏写必填 `tag_type`。现已明确写入
  `PHYSICAL`，真实 PostgreSQL 创建与更新路径均有回归测试。

## 测试与现场验收

- 完整后端：329 项通过，130 项环境型跳过。
- 真实 PostgreSQL：数据帧 16 项通过；Neuron 导入创建/更新 2 项通过。
- 本轮前端既有门禁：34 项通过，TypeScript 与生产构建通过；发布脚本 37 项通过。
- 临时验收树完成：根节点创建、PCS 子节点创建、移动到根、移回并改名、预览 `en9_pcs/cmd`
  的 16 个点位、真实应用 16 个点位、整树退役。验收前后活动节点均为 6，临时节点残留为 0。
- PCS committed 快照为 45 个 L0，45 个均有值，其中 42 GOOD、3 STALE；原始点位历史查询成功。
- PCS 模板 6 个可见；L2 实时“PCS 有功功率”为 GOOD，实体历史查询成功。
- 10 秒稳定性窗口内 frame max 前进 10、terminal max 前进 12，未完成帧由 3 降为 1，最大帧龄
  0.48 秒；未发布 outbox 为 0，活动来源重复为 0。
- 最终容器 running、restart count 0；日志未出现 `ERROR`、`CRITICAL`、Traceback 或 ASGI 异常。
- 根分区约 5.2 GB 可用，`/userdata` 约 2.5 GB 可用。

## 运行约束

- 保持 `network_mode: host`、`/dev/mqueue` tmpfs、`restart: unless-stopped`。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron 未重建。
- 未启动 Caddy/TLS，未验证自动策略，未执行控制或设备写入。

当前仍是 development HTTP 联网测试站，运行日志会提示不安全开发模式，不能视为正式网络安全验收。
回滚优先切回部署目录中保存的 hotfix.5 固定摘要；若必须回退 Schema，再使用上述 Schema 049 备份，
不得删除或改写终态帧和业务历史来伪造回滚。
