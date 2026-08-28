# 1 号机 v0.4.85-rc.11 HTTP 部署记录

## 结果

- 部署时间：2026-08-28（Asia/Shanghai）
- 源码：`31f4db64`（发布提交 `31f4db6`）
- GitHub Actions：`33137526715`，成功
- linux/arm64：`ghcr.io/taidai/zizu@sha256:a494e89a1b63a632877992dc6820c11f07e1ff237721f407f54f8cebd529891a`
- 容器 image ID：`sha256:e3afa1b82369153eda4bc698cef0bb602b4e628588c22489979ca282a1f03fc6`
- Schema：049
- 发布目录：`/opt/zizu-release-test-0.4.85-rc.11`

## 切换前保护

- rc.10 容器健康、restart count 0；根分区约 5.3 GB 可用，`/userdata` 约 5.3 GB 可用。
- 备份：`/opt/zizu-backups/pre-v0.4.85-rc.11-schema049/omnithings.dump`
- 大小：3,433,038 bytes
- SHA-256：`73b4127c7ecfb393e8fa5bf6523ba329609071955801c02d6a5deb4b43c29406`
- `sha256sum --check` 与现有 TimescaleDB 容器内 `pg_restore -l` 均通过。现场 Docker bridge
  不能创建临时 veth，因此没有额外启动校验容器。

## 切换边界

- 复用 rc.10 已验证 compose 与受保护的 runtime env，只替换固定 ARM64 镜像摘要。
- 只重建 `zizu-release-test-backend-1`；TimescaleDB、NanoMQ、Neuron 未重建。
- 保持 `network_mode: host`、`/dev/mqueue` tmpfs、`restart: unless-stopped` 和既有
  `zizu-release-test_zizu-data` 数据卷。
- 未启动 Caddy/TLS，未发布或修改告警规则，未执行自动策略、控制或设备写入。

## 验收证据

- 容器 `healthy`、restart count 0；实际 image ID 与固定摘要解析结果一致。
- `/api/v1/health/live` 返回 `alive / 0.4.85-rc.11`，本机和公网首页均为 HTTP 200；浏览器端
  ZiZu 登录页可真实加载。
- Schema 049；`t_data_frame_outbox` 未发布记录为 0；committed-frame 消费收据 179 条，
  最大 frame sequence 477。
- 管理员只读登录成功；告警事件 7 条，当前活动/未确认/严重均为 0；规则组 1 个；事件均提供
  `node_name / entity_name / alarm_name / duration_seconds` 可读字段。
- 启动后日志没有真正的 ERROR、CRITICAL、Traceback、启动失败或 Schema 049 门禁错误。

当前仍为 development HTTP 联网测试站，不代表正式生产网络安全验收。若需回滚，使用 rc.10 固定
摘要与原发布目录；Schema 仍为 049，除非数据库损坏，否则不恢复备份。禁止执行 `docker compose
down -v`，不得删除业务卷。
