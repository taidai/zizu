# v0.8.2 飞书送达校验与告警记录维护

2026-09-04，用户确认修复飞书告警通知，并让告警中心现有记录具备安全维护入口后部署到 1 号机。

## 发布

- 源码提交/tag：`3921cc3e4854d23799c2a99da9cb285b3dc16527` / `v0.8.2`。
- GitHub Actions `33840829220` 成功，发布清单经 `release_preflight.py` 校验为 Schema 061。
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:6cbb4bfebc1bfc2eb72ab64b9d1f396652d376b8c41f6cce0f6f686c3dedf878`。
- 1 号机应用容器 healthy、restart 0；继续使用 host 网络与 `/dev/mqueue` tmpfs。TimescaleDB、NanoMQ 未重启。

## 变更

- 飞书机器人必须同时满足 HTTP 2xx 与业务 `code=0` 或 `StatusCode=0` 才记为送达；缺失、非零或非 JSON 响应均记为失败。
- 增加 `{{entity.value_text}}`，保证实体值进入飞书卡片文本字段时始终为字符串；原生类型变量 `{{entity.value}}` 继续保留。
- HTTP 通知配置支持新建、查看、编辑、测试、启停、删除；告警规则只允许停用后软删除；告警事件只允许现场恢复后归档；通知记录保留详情，失败记录可重新入队。
- 所有删除/归档均保留历史证据。规则发布与归档锁同一规则组，归档前生成的旧计划不能重新启用已归档规则。

## 验证

- 本地后端：399 passed、197 skipped、104 subtests passed；跳过项为需要外部 PostgreSQL 的环境型测试。
- 发布脚本 56/56、前端契约 91/91、生产构建退出 0；独立复审无 Critical/Important。
- 现场飞书配置已从 `{{entity.value}}` 改为 `{{entity.value_text}}`，只发送一次测试；HTTP 200、飞书业务成功，配置重新启用且测试摘要与当前摘要一致。
- 无头浏览器只读走通节点 → 91/91 L0 → L1 → 2 个 L2 → 告警；HTTP 配置 CRUD 和文本变量入口可见。
- 现场恢复告警页 50 条记录显示归档入口；11 个规则组中 9 个停用规则可删除，启用规则禁止删除；通知页首屏 50 条记录可看详情。首屏没有失败记录，因此未在生产制造失败通知，失败重试由自动化用例覆盖。
- Schema 061 已生效，归档字段 4/4；部署后 60 秒抽样 18 个 COMPLETE、0 个未完成帧，outbox 随后归零；应用日志未发现 ERROR/Traceback。

## 备份与回滚

- `/opt/zizu-release-test-0.5.0/backups/v0.8.2-pre/` 保存切换前 release.env、Compose 和告警数据。
- `alarm-data.dump` 为 7,203,499 字节、权限 600，SHA-256 `4bd2dfb0e358c8c1e25900c7702eb707f81e0426dd602b1632512752f57f9200`，已由容器内 `pg_restore -l` 完整读取。
- 软件回滚使用备份目录的旧 release.env 重建应用容器；Schema 061 只增加归档列和约束，不执行数据库逆迁移。
- 部署后根分区可用 3.2G，`/userdata` 可用 2.3G；未启动 Caddy/TLS，未删除业务数据。

