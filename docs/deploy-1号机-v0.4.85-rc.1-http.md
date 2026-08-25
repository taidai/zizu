# 1 号机 v0.4.85-rc.1 HTTP 部署记录

部署时间：2026-08-26（Asia/Shanghai）

## 最终运行身份

- 源码提交：`3184064a195c33c656992a7a75832be8555ba2e1`
- GitHub Actions：`32872105720`
- 平台版本：`0.4.85-rc.1`
- 数据库 Schema：`044`
- linux/arm64 镜像：`ghcr.io/taidai/zizu@sha256:d024a8301a16943783e8c7683b5d114db97f90615a65ca860538ec4669bf3a03`
- 主机镜像 ID：`sha256:e7ffca230c60d2e42456cd41c44c5e3dee5bd1d604212c03d9e2c992656e9b7c`
- 发布目录：`/opt/zizu-release-test-0.4.85-rc.1`
- 公网入口：`http://e606.hlszh.com:9000/`

本次仅替换 backend，继续使用 `network_mode: host`、`/dev/mqueue` tmpfs、
`restart: unless-stopped` 和既有具名数据卷；没有启动 Caddy、申请 TLS、重建
TimescaleDB/NanoMQ，也没有执行自动策略验证或设备控制写入。

## 数据保护与迁移

切换前完整备份：

```text
/opt/zizu-backups/zizu-pre-0.4.85-rc.1-20260825.dump
/opt/zizu-backups/zizu-pre-0.4.85-rc.1-20260825.dump.sha256
```

SHA-256 文件校验与 `pg_restore -l` 目录读取均通过。首次切换发现生产
`t_telemetry` 已启用 TimescaleDB columnstore，Schema 043 的非恒定默认值 DDL
被数据库拒绝；新 backend 随即退出验收并恢复旧固定摘要，旧数据管道重新加载
97 个点位规则。随后新增同构 PostgreSQL 回归测试，把历史行默认值改为迁移时刻
常量、新写入默认值仍为 `now()`，并重新生成不可变镜像。最终切换从已兼容的 042
继续应用 043、044，日志记录 `errors=0`。

## 最终验收

- 容器 liveness 返回 `alive / 0.4.85-rc.1`。
- Schema 精确为 044；`t_configuration_state` 存在，`t_solution_packages` 不存在。
- 两个数据库 L2 实体均获得节点归属；旧告警验证实体依据其唯一活动 L0 绑定归属
  到“变流器”，告警历史未删除。
- F0 管道加载 97 个点位规则和 97 个 Neuron source-path 映射；NanoMQ 连接成功。
- 登录、健康、节点、实体、EMS 工作台、告警配置和 JDM 规则只读 API 均返回 200。
- 节点数据主干显示储能电表 55 个 L0、变流器 42 个 L0 和 1 个 L2 输出。
- OpenAPI 不再暴露 solution、device-template、acceptance、ems-policy 或
  business-metric 路由。

当前唯一运行时告警是 `pcs.active_power` 实体被正确标为 `ENTITY_DATA_STALE`：其
L0 来源“交流总有功功率”质量为 192，但现场出现约 60–90 秒上报空档，超过实体
30 秒 freshness。平台没有转换或数据库错误，也没有放宽 freshness 掩盖断流；
后续应从 Neuron/设备采集链路排查这一上游间歇性空档。

当前仍启用了公开 HTTP 与 development Secret 兼容模式，只可作为已确认的联网
测试/维护部署，不能宣称满足正式生产安全基线。
