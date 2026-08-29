# 1 号机 v0.4.89 HTTP 部署与主干验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.89`
- 提交：`3567b54570755d1065d5d2936d2be6522b2e59b1`
- 标签：`v0.4.89`
- GitHub Actions：`33262237897`，成功
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:a2e2ccbc45f6a1e7c6a134574f4dca34be8f42594f7d32b6843d9f49f698f942`
- 1 号机实际 image ID：`sha256:2e911a94d330e31bb23505a6aa189294169e9308eba098f4532397152c355265`

## 切换与恢复证据

- 发布目录：`/opt/zizu-release-test-0.4.89`
- 切换前备份：`/opt/zizu-backups/pre-v0.4.89-schema051/omnithings.dump`
- 备份大小：`88,338,480` bytes
- 备份 SHA-256：`b52055043abdd83527f5fded4b1e7e6885366a2c8ec3b7b51c3c3e9a2114a9f5`
- `pg_restore -l`：通过，共 775 项
- 只重建 ZiZu backend；TimescaleDB 与 NanoMQ 未重建。
- 保留旧容器关键配置：`network_mode: host`、`tmpfs /dev/mqueue`。
- 未启用 Caddy/TLS，未执行 JDM 策略、控制或设备写入。

## 自动门禁

- 前端 Node 测试：45/45 通过。
- 前端 TypeScript 与 Vite production build：通过。
- 后端完整单测：342 tests，134 skipped，0 failure。
- scripts 完整单测：43/43 通过。
- L1 回归测试先复现“合并修订多输出时误取第一个输出”，修复后按目标
  `entity_definition_id` 选择试算结果。

## 1 号机运行复核

- 容器：healthy，restart count 0。
- 匿名存活探针：`alive / 0.4.89`。
- 数据库 Schema：051。
- 数据帧 outbox 未发布数量：0。
- 近 30 分钟日志中 `PoolError`、连接池耗尽、Traceback、CRITICAL、ERROR：0。
- 磁盘：根分区可用约 5.1 GiB；`/userdata` 可用约 2.0 GiB。

## Browser 主干验收

使用维护者已登录的应用内 Browser，在公网 HTTP 站点沿主干进行无副作用验收：

1. 真实节点树：可展开 `LVK → 工厂电站 → 储能`，可选择 PCS 变流器。
2. L0 原始点位：PCS 显示 45 个点位；可查看当前值、单位、质量、数据时间和来源。此前同一发布链已验证单点历史明细入口。
3. L1 点位加工：选择 `交流总有功功率`，实体名称自动为同名；只点击“检查结果”，返回当前值 `0`、质量“正常”、1 条来源证据、帧 36060、配置 28，数据时间与所选 L0 一致。未再误显示 IGBT 的 `33`。
4. L2 标准实体：显示 `PCS 有功功率 0 kW` 与 `IGBT温度 33`；展开 PCS 实体可见历史范围、来源 `交流总有功功率` 和加工类型“即时”。
5. 告警：当前告警页可用；规则页显示 3 个规则组，其中 `现场告警验收规则` 已启用；规则实体选择只列 L2 标准实体。

## 未关闭问题

- L0/L2 的超时质量仍不完全一致：例如部分数小时未更新的 L0 点位仍显示“正常”，而 IGBT 和另一些点位已显示“超时”。这说明页面可用，但逐点 STALE 判定尚需作为下一项独立缺陷处理；本次未借部署顺手扩大修改范围。
