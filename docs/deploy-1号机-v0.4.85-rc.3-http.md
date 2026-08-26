# 1 号机 v0.4.85-rc.3 HTTP 部署记录

部署时间：2026-08-26（Asia/Shanghai）

## 最终运行身份

- 源码分支：`ticket/v0.4.85-node-data-trunk-hard-cut`
- 源码提交：`f357809205d136879851cb04c988873136dd5271`
- GitHub Actions：`32936642061`
- 平台版本：`0.4.85-rc.3`
- 数据库 Schema：`044`
- linux/arm64 镜像：`ghcr.io/taidai/zizu@sha256:7eb0b5650061ced14123be491a12b4dd97592e176adc4560205f6242e19b6e84`
- 发布目录：`/opt/zizu-release-test-0.4.85-rc.3`
- 公网入口：`http://e606.hlszh.com:9000/`

仅替换 backend，保留旧容器配置：`network_mode: host`、`/dev/mqueue`
tmpfs、`restart: unless-stopped` 和既有具名数据卷。没有启动 Caddy、申请
TLS、迁移或清空数据库，也没有执行自动策略验证和设备控制写入。

## 本次修复

- L0 历史、去重和 latest 从逐点 SQL 改为整批 SQL，避免数据库调用量随点位数暴涨。
- 同一 `(node_id, tag_id)` 用事务 advisory lock 串行推进 latest，并以 SQL
  `RETURNING` 复核实际推进结果，保持乱序与并发语义。
- pipeline 仅在缓冲区为空时等待；仍有积压时立即处理下一批，不再在批次之间空等 1 秒。
- 失败台账不可用时保留待写数据并等待 4 秒再试，避免数据库故障时形成忙循环。

## 现场验收

`v0.4.85-rc.2` 的首次 10 分钟观察曾出现 L0 延迟约 37.7 秒、L2 短暂
STALE，因此判定不通过，并继续修复批次间空等。`v0.4.85-rc.3` 重新计时验收：

- 连续 10 分钟、每 30 秒取样，共 21 次，全部通过。
- `交流总有功功率` L0 延迟约 1.16～11.12 秒，始终小于 30 秒。
- `pcs.active_power` L2 与 L0 每次时间戳一致，质量始终为 192，没有 STALE。
- 认证后的 L2 实时 API 返回 HTTP 200、`fresh=true`、`quality_good=true`，
  现场抽查 `age_ms=7683`。
- 容器状态 `running/healthy`，重启次数 0；启动后运行错误日志 0。
- 公网 liveness 返回 `alive / 0.4.85-rc.3`。

## 仍需处理

根分区当前约 91% 已用，仅余约 1.4GB；历史遥测表及去重表是主要占用。
这不影响本次实时链路验收，但应作为下一项运维工作处理容量、压缩和保留期，
避免磁盘写满。当前仍是公开 HTTP 与 development Secret 兼容模式，只适合已确认的
联网测试/维护环境，不代表正式生产安全基线。

