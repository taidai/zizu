# 1 号机 v0.4.91 HTTP 部署与验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.91`
- 发布提交：`20f00cec18445d528b115b5f1490557d09a7cd22`
- 标签：`v0.4.91`
- GitHub Actions：`33270779281`，成功
- Schema：`052`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:d8d5e8b184e2718cf2e9039a2d8976484376cda107c563ce90923a9334726613`
- 1 号机实际 image ID：`sha256:1f6c4f02bf0e23998e2ad0a7e28746a4d4e86c6f2b740ccce24fb934784e6980`
- 发布目录：`/opt/zizu-release-test-0.4.91`

## 本轮交付

- JDM 正式运行时只消费不可变 committed L2 数据帧，不再扫描 L0 或 latest 表。
- 每帧 `jdm` 收据与全部规则执行事实在一个 PostgreSQL 事务中提交；失败时整体回滚，重放幂等。
- 规则创建、修改、删除统一经过配置栅栏，并绑定全局配置修订；旧 alarm/fault-map JDM 行只读。
- 删除旧 `rule_engine.py`、JDM 直写告警适配器和规则模板直接插入 `t_rules` 的绕过入口。
- 生产 fanout 固定为“告警 → JDM → committed 实时流”。规则页只读显示最近判断或拒绝原因，
  “已完成判断”不表示设备控制成功；本轮不执行动作。

## 自动门禁

- 真实 PostgreSQL JDM/Schema/API/outbox/startup 专项：37/37 通过。
- 后端完整测试：362 tests，142 项需要显式外部环境而跳过，0 failure。
- scripts：43/43 通过。
- 前端 Node 测试：49/49 通过。
- TypeScript 与 Vite production build：8186 modules，成功。
- `compileall`、六处版本一致性与本轮文件 `git diff --check` 通过。
- GitHub 发布清单确认版本 `0.4.91`、Schema `052`，同时包含 amd64/arm64 固定摘要。

## 切换与恢复保护

- 切换前备份：`/opt/zizu-backups/pre-v0.4.91-schema051/omnithings.dump`
- 备份大小：`118,232,614` bytes
- 备份 SHA-256：`7b35f4f4e96049cdac7e8d3eddeafb7990f9b0a477a3f625a9317060df2be914`
- `sha256sum --check` 通过；`pg_restore -l` 可读，共 826 项。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron 未重建。
- 沿用原 runtime env、业务卷、`network_mode: host`、`tmpfs /dev/mqueue`、
  `restart: unless-stopped`，并保留 v0.4.90 固定摘要作为应用级回滚镜像。
- 未启用 Caddy/TLS，未启用规则，未执行控制、设备写入或配置发布。

## 运行验收

- backend 为 healthy，restart count 0，架构 arm64；容器 image ID 与固定摘要解析结果一致。
- 本机和公网 `/api/v1/health/live` 返回 `alive / 0.4.91`，公网首页 HTTP 200。
- Schema 052；配置修订 28；数据帧 outbox 未发布数 0。
- 记录时 frame head 45789；`jdm` 收据 56 条并前进到 45789，证明生产 fanout 正在消费新帧。
- JDM 执行事实 0 条：现场没有启用的 control/linkage 规则，本轮没有为验收自动创建或启用策略。
- 真实错误日志匹配 `Traceback/PoolError/ERROR/CRITICAL` 为 0；迁移日志为 `errors=0`。
- 根分区约 4.9 GiB 可用；`/userdata` 约 1.3 GiB 可用。

## Browser 主干验收

当前状态：`INCOMPLETE`。

Browser 控制标签页没有继承用户原标签页的登录会话，仍显示登录表单。按照安全边界，没有通过
Browser 再次输入或传输账号密码，也没有改用 API 冒充页面验收。登录页已保留，用户在该控制页登录后，
继续验证：真实节点树、L0 实时/历史、L1 只读试算、L2 实时/历史/来源、告警中心，以及 JDM 页面最近
执行事实。不得启用规则、发布配置、下发控制或写设备。

## 当前结论

v0.4.91 已安全部署并通过服务器运行门禁；由于 Browser 尚未完成登录后的主干点击验收，本次发布结论
仍为 `INCOMPLETE`，不能声称完整交付验收通过。
