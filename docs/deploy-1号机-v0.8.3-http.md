# v0.8.3 通知记录表格与永久删除

2026-09-04，用户确认将告警通知记录改为表格，并支持当前页选择、单条删除和批量删除后，发布到 1 号机。

## 发布

- 源码提交/tag：`2c432da` / `v0.8.3`。
- GitHub Actions `33858498413` 成功；发布清单为 Schema 061。
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:5e7af4856f08a998b831a138f5bc407085a3c0fa65750a6d91055ec758c4e9c2`。
- 1 号机应用容器 healthy、restart 0；继续使用 host 网络和 `/dev/mqueue` tmpfs。TimescaleDB、NanoMQ 未重启。

## 变更

- 通知记录改为表格，保留发送结果、目标、时间、详情和失败重试。
- 支持复选框、全选当前页、单条删除和批量删除；一次最多 200 条。
- 删除为不可恢复的物理删除，并由外键级联删除发送尝试和重试幂等记录。
- 仅 `delivered`、`failed`、`cancelled` 可删除；`pending`、`retry_wait` 在界面和后端均拒绝删除。
- 批量删除先锁定并校验全部目标，再在一个事务内删除；任一记录不存在或仍在发送则整批不删除。
- 删除最后一页后自动回到最后一个仍存在的页码。

## 验证

- 本地后端：403 passed、199 skipped、104 subtests passed；跳过项为需要外部 PostgreSQL 的环境型测试。
- 发布脚本 56/56、前端契约 93/93、生产构建退出 0；独立复审无 Critical/Important。
- 无头浏览器走通节点 → 91 个 L0 → L1 → 2 个 L2 → 当前/历史告警；通知表格、当前页全选和批量删除入口可用。
- 现场只创建两条命名为 `E2E单删-v083`、`E2E批删-v083` 的已取消测试记录，各带一条测试尝试；分别通过单删和批删完成，数据库复核通知 0、尝试 0，未删除业务记录。
- 近 60 秒 19 个数据帧均为 COMPLETE，未完成通知 0；应用日志未发现 ERROR、Traceback 或 Exception。

## 备份与回滚

- `/opt/zizu-release-test-0.5.0/backups/v0.8.3-pre/` 保存切换前 `release.env`、Compose 和告警数据。
- `alarm-data.dump` 为 5,532,194 字节、权限 600，SHA-256 `5f1ceb7e694df06218e865885075a7a3ccc12608801543839b0cd7c6bfe7d019`；已由容器内 `pg_restore -l` 完整读取。
- 软件回滚使用备份目录中的旧 `release.env` 重建应用容器；本版没有 Schema 变更，不执行数据库逆迁移。
- 部署后根分区可用 3.2G，`/userdata` 可用 2.1G；未启动 Caddy/TLS，未清理业务数据。
