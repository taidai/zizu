# 1号机 v0.4.85-rc.7 HTTP 部署记录

- 源码：`88383728392f86cd278a17a9259dddaf660f6976`
- GitHub Actions：`33083624288`（success）
- ARM64 镜像：`ghcr.io/taidai/zizu@sha256:e7fd5f92a37a0ab2d44ff3e842816c8724affa2b327f74ff07ffd054741f4303`
- 平台 / Schema：`0.4.85-rc.7` / `048`
- 备份：`/opt/zizu-backups/pre-v0.4.85-rc.5-schema045/omnithings.dump`
- 备份 SHA-256：`e34b274f5e2e5571d8bc8145bb4c7295d72a053b134f943d61956051ff69db3f`

## 现场约束

- 仅替换 backend；沿用 `network_mode: host`、`tmpfs: /dev/mqueue`、`restart: unless-stopped` 和
  `zizu-release-test_zizu-data` 数据卷。
- 不启用 Caddy/TLS，不验证自动策略，不执行任何设备控制写入。
- 本次从 rc.6 升级没有新迁移、没有删除业务数据。

## 验收结果

- 容器 `healthy`、重启次数 `0`，镜像版本标签为 `0.4.85-rc.7`。
- `/` 返回 200，`/api/v1/health/live` 返回 `alive`，登录可用。
- 连续读取全部 6 个节点后再次登录成功，日志无 `read-only transaction`、
  `DATA_FRAME_CLAIM_FAILED`、`DATA_FRAME_OUTBOX_UNAVAILABLE`、Traceback 或 ERROR。
- committed frame 序号为 2；储能电表快照含 55 个 L0，变流器快照含 42 个 L0 与 2 个 L2。
- WebSocket `/api/v1/ws/data-frames` 完成票据认证及节点订阅。
- 数据库保持 Schema 048；终态帧 `COMPLETE=1`，outbox 总数 1、未发布 0。

## 本轮修复

rc.6 的快照读取把连接池连接永久留在只读模式，连续查看节点后写事务失败。rc.7 将一致性快照的
`REPEATABLE READ READ ONLY` 限定在单次事务内，结束后连接仍可供 processor、outbox 和登录使用。
