# 1 号机 v0.4.85-rc.12 HTTP 部署记录

## 结果

- 部署时间：2026-08-28（Asia/Shanghai）
- 源码与发布提交：`175359dfb2f7365d8f72f79134863b9ef19e4e09`
- GitHub Actions：`33150903078`，成功
- linux/arm64：`ghcr.io/taidai/zizu@sha256:8aa018672bdefef962b4d8d3c5c8e1b1780fba7534aa4f3adb7129532afaf5f7`
- 容器 image ID：`sha256:b77399e80dca96fc9f1aa384b90e95821eb30cb45ba95e7659ff27886d5de15e`
- Schema：049
- 发布目录：`/opt/zizu-release-test-0.4.85-rc.12`

## 切换前保护

- rc.11 容器健康、restart count 0；根分区约 5.2 GB 可用，`/userdata` 约 5.0 GB 可用。
- 备份：`/opt/zizu-backups/pre-v0.4.85-rc.12-schema049/omnithings.dump`
- 大小：3,552,364 bytes
- SHA-256：`fbb5678bf1144530787dc5e962cc4a0f7170b975d23c6465887541b265163137`
- `sha256sum --check` 与现有 TimescaleDB 容器内 `pg_restore -l` 均通过。

## 切换边界

- 复用 rc.11 已验证 Compose 与 `/opt/zizu-release-test-0.4.80/runtime.env`，只替换固定 ARM64 镜像摘要。
- 只重建 `zizu-release-test-backend-1`；TimescaleDB、NanoMQ、Neuron 未重建。
- 保持 `network_mode: host`、`/dev/mqueue` tmpfs、`restart: unless-stopped` 和既有
  `zizu-release-test_zizu-data` 数据卷。
- 未启动 Caddy/TLS，未发布配置，未执行自动策略、控制或设备写入。

## 验收证据

- backend 为 `healthy`、restart count 0；固定摘要、实际 image ID 和 arm64 架构一致。
- `/api/v1/health/live` 在1号机本机与公网均返回 `alive / 0.4.85-rc.12`；公网首页 HTTP 200。
- Schema 049；`t_data_frame_outbox` 未发布记录为 0；committed-frame 消费收据 179 条。
- NanoMQ 与 TimescaleDB 保持原容器连续运行且健康；切换后精确日志筛选真实错误为 0。
- 本轮未输入业务账号做认证后页面操作，因此不把双角色浏览器 smoke 冒充为已完成。

当前仍为 development HTTP 联网测试站，不代表正式生产网络安全验收。若需回滚，使用 rc.11 固定
摘要 `sha256:a494e89a1b63a632877992dc6820c11f07e1ff237721f407f54f8cebd529891a`
与原发布目录；Schema 仍为 049，除非数据库损坏，否则不恢复备份。禁止执行 `docker compose down -v`，
不得删除业务卷。
