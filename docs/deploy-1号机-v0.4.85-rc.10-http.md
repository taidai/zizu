# 1 号机 v0.4.85-rc.10 HTTP 部署记录

## 结果

- 部署时间：2026-08-28（Asia/Shanghai）
- 源码：`909e7fc566842e8b1ee0a75f714ef677fb8310b1`
- GitHub Actions：`33124140499`，成功
- linux/arm64：`ghcr.io/taidai/zizu@sha256:5e92e7efef2cb645cee96c41d5136ae45ce683c2b38d89b64d413767c5478544`
- 容器 image ID：`sha256:bf5ceb447b2650aa0d850efd78a306ba18c9f52edb93f54f07bd7694d8aac067`
- Schema：049
- 发布目录：`/opt/zizu-release-test-0.4.85-rc.10`

## 切换前保护

- Schema 048、未发布 outbox 为 0；根分区约 5.3 GB 可用，`/userdata` 约 5.4 GB 可用。
- 备份：`/opt/zizu-backups/pre-v0.4.85-rc.10-schema048/omnithings.dump`
- 大小：3,258,020 bytes
- SHA-256：`b5895963c1dd611ec851c1f1b9b440e3b2d1a477caf934dfe437b42a94d122b8`
- `sha256sum --check` 与 `pg_restore -l` 均通过。

## 迁移与容器约束

- Schema 049 由数据库 owner 执行，`BEGIN / DO / COMMIT` 成功；`schema_migrations` 已记录 049。
- `t_committed_frame_consumers` 的主键、三条检查约束、帧外键共 5 条约束存在。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron 未重建。
- 保持 `network_mode: host`、`/dev/mqueue` tmpfs、`restart: unless-stopped`。
- 未启动 Caddy/TLS，未执行自动策略、控制或设备写入。

## 验收证据

- 容器 `healthy`、restart count 0；启动日志显示迁移 005—049 全部可识别且 errors=0。
- MQTT 已连接，97 条点位规则加载，capture/processor/outbox 三个循环启动。
- 公网首页 200；`/api/v1/health/live` 返回 `alive / 0.4.85-rc.10`；登录成功。
- 告警事件 API 返回 `model_version=v1`、7 条事件；告警配置修订为 7、当前定义 1 条。
- 告警 committed L2 消费收据 2 条，最新 frame sequence 300、配置修订 7；未发布 outbox 为 0。
- 部署后日志未出现 `ERROR`、`CRITICAL`、Traceback、告警帧修订错误或 Schema 049 门禁错误。

当前仍是 development HTTP 联网测试站，运行日志会明确提示不安全开发模式；不得把本记录解释为正式
生产网络安全验收。回滚时使用 rc.9 固定摘要与原发布目录，并按备份记录决定是否恢复 Schema 048；
不得删除或手工改写业务历史来伪造回滚。
