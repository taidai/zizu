# 1 号机部署记录与续跑清单（v0.4.82-rc.2 / HTTP）

> 当前状态：发布候选已经构建并验证，但尚未部署到 1 号机。
> 2026-08-23 12:05（Asia/Shanghai）复查时，SSH 端口拒绝连接，公网健康接口返回 HTTP 502。
> 故障发生在任何生产写操作之前；1 号机没有执行备份、迁移、镜像拉取、容器停止或替换。

## 固定发布身份

```text
版本：0.4.82
候选标签：v0.4.82-rc.2
源码提交：102ff8acba7fb17791b663b19cd805ab5de14a36
GitHub Actions：32616712942（成功）
目标架构：linux/arm64
Schema：041
镜像：ghcr.io/taidai/zizu@sha256:4c90d30e9a3e5b108f4a17c017277bfbc2a729b84ba999c193831ebe660a5e65
```

不要使用 `latest`，不要移动已经失败的 `v0.4.82-rc.1` 标签，也不要在 1 号机现场构建镜像。

## 已完成的发布验证

- 多架构镜像发布成功；ARM64 摘要与 `release.json` 一致。
- 前端构建改为在原生构建平台执行，最终运行镜像仍按目标平台生成，消除了 ARM64 QEMU 下 `npm ci` 长时间挂起的问题。
- L0 原始点位 → L1 点位加工 → L2 全局实体相关核心测试、PostgreSQL 迁移/运行时证据测试、WebSocket、outbox、公开验收缝和前端构建均通过。
- 独立代码审查结论为 Ready，没有 Critical 或 Important 阻断项。

## 切换前现场基线

离线前只读检查得到：1 号机为 `aarch64`，旧 backend 运行 v0.4.81 固定摘要，数据库 Schema 为 039；backend 使用 `network_mode: host`、`restart: unless-stopped` 和 `/dev/mqueue` tmpfs。部署必须保留这些约束，不启动 Caddy、不申请 TLS、不执行自动策略或设备控制。

## 主机恢复后的续跑顺序

1. 复查 SSH、站点、旧 backend、TimescaleDB、NanoMQ、Neuron/采集链路均可用，并再次记录旧镜像摘要、容器参数和 Schema。
2. 冻结配置变更，创建 PostgreSQL custom-format 全量备份；生成 SHA-256，并用 `pg_restore -l` 校验备份可读。
3. 在隔离数据库恢复备份，依次执行 owner migration 040、041，确认最新 Schema 为 041；以 ARM64 固定摘要启动隔离 backend。
4. 在隔离环境运行 EN9 扫描/应用，只有 blocker 为 0 才允许继续；对关键来源证据查询执行 `EXPLAIN (ANALYZE, BUFFERS)` 并保存结果。
5. 停止旧 backend，向生产库执行相同 owner migrations，确认 Schema 041；使用固定摘要重建 backend，并核验 `network_mode: host`、`/dev/mqueue`、restart policy 和实际 image ID。
6. 验证登录、节点树、L0 点位、L1 点位加工、L2 全局实体、实时数据、历史数据、告警和 WebSocket；重启一次 backend 验证运行时证据可恢复，再观察至少 30 分钟。
7. 任一迁移、健康、来源证据或业务验收失败，立即停止新 backend，恢复完整数据库备份并按记录的旧固定摘要恢复 v0.4.81。

部署过程中禁止自动策略验证和设备写命令；如确需控制验收，必须另开现场窗口并使用用户确认的小功率安全值。
