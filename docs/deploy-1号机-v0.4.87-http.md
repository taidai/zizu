# 1 号机 v0.4.87 HTTP 部署记录

## 结果

- 部署时间：2026-08-29（Asia/Shanghai）
- 版本与标签：`0.4.87` / `v0.4.87`
- 发布源码：`ad914b45989882bd73eda064a5772563c009ae5d`
- GitHub Actions：[33248803913](https://github.com/taidai/zizu/actions/runs/33248803913)，成功
- linux/arm64：`ghcr.io/taidai/zizu@sha256:633726335628319c0507b820e84c010f71528a724415381d864f936caef23ca9`
- 容器 image ID：`sha256:0a39c3e8a89dab5e3286e8f16a09dd7ebb0184001ce9b4472cebaff8c285133c`
- Schema：051
- 发布目录：`/opt/zizu-release-test-0.4.87`
- 公网地址：`http://e606.hlszh.com:9000/`

## 切换前保护

- 切换前版本为 0.4.86、Schema 050；帧队列无积压，未发布 outbox 为 0。
- 备份：`/opt/zizu-backups/pre-v0.4.87-schema050/omnithings.dump`
- 大小：14,413,530 bytes
- SHA-256：`46986771d426348dc21a439d56aba68940aef5991ed0fdcd8cbda2cb758d94be`
- `pg_restore -l` 可读取 738 项归档清单。TimescaleDB 对连续聚合循环外键给出已知警告，但归档可列出。
- Schema 051 迁移文件 SHA-256：`495d46ff6aa166269753392bce7ee61127cc3c819e6e244605ed48a715651c0f`。
- 本次没有清空、改写或伪造遥测、帧、告警及配置历史。

## 本次交付

- 普通用户节点页收口为“原始数据”和“标准实体”两个主要视图；L0/L1/L2 保留为内部架构术语。
- PCS 原始数据可查看 committed 实时值、质量、数据时间、Neuron 来源及历史。
- 点位加工内嵌在标准实体的“数据来源与计算”中，可选一个或多个同节点原始点位，使用生产引擎只读试算后发布。
- 已有模板可复用；首次交付不以先建模板为前提。节点内联加工只绑定现有 L0，不会误触发 Neuron 扫描或重复导入。
- 标准实体提供 committed 实时值、历史与来源证据；告警消费者继续只消费 committed L2。

## 发布门禁

- 完整后端：270 passed、134 skipped、57 subtests。
- 发布专项：15 passed。
- 前端：42 passed；TypeScript 与 Vite 生产构建通过，仅保留既有大 chunk 警告。
- Actions 产物清单版本为 0.4.87、Schema 051；ARM64 固定摘要与现场容器架构、image ID 一致。

## 现场验收

- 公网首页和 `/api/v1/health/live` 均返回 HTTP 200，存活接口报告 0.4.87；登录页面正常渲染。
- 容器为 running/healthy、restart count 0；保持 host 网络、`/dev/mqueue` tmpfs、`unless-stopped`。
- Schema 051 已应用，作用域列及约束有效，非法作用域数据为 0；未发布 outbox 为 0。
- PCS 活动节点使用 `en9_pcs` 来源：45 个 L0 均有 latest，最终 45 GOOD、0 STALE。
- PCS L2 `pcs.active_power` 为 `0 / GOOD`，来源证据 1 条，L2 历史共 30,223 条；数据帧和告警消费者持续前进。
- 点位加工模板 5 个、应用 3 个、活动配置修订 7；告警定义 2 个，当前活动告警 0。
- 部署后日志未出现 `Traceback`、`CRITICAL`、`ERROR` 或未处理异常。
- 最终根分区约 5.2 GB 可用，`/userdata` 约 2.9 GB 可用。

## 边界与未完成项

- 本次保持 HTTP 测试站，不启动 Caddy，不申请或配置 TLS；因此不宣称正式网络安全验收或生产发布锁完成。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron 未重建。
- 未验证自动策略，未执行控制或设备写入。
- 当前现场登录凭据已经轮换；已停止继续尝试，也没有重置密码。因此本轮完成了公开页面、数据库和运行链路验收，尚缺使用有效账号进行一次登录后的页面点击复验。
- 旧 `t_release_locks` 表已经随硬切移除，现有 `record_release_lock.py` 仍依赖已删除的解决方案表和 HTTPS edge，不能用于本 HTTP 站。本文是部署证据，不冒充正式发布锁；脚本与发布锁语义需另行收口。

回退应优先切回上一版固定 ARM64 摘要；若必须回退 Schema，再使用上述 Schema 050 备份。不得通过删除或改写业务历史来伪造回滚成功。
