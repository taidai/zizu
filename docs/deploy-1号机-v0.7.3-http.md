# 1 号机 v0.7.3 HTTP 部署与告警规则启停验收记录

日期：2026-09-02

## 发布身份

- 版本：`0.7.3`
- 功能提交：`067545a`；发布提交与标签：`784f09c`
- GitHub Actions：`33610304919`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:cc06898b64d0f175a27923955f412fd9f1cc143cc1767ae947b4c477cef97e29`
- 运行 image ID：
  `sha256:f5a30f3d2f7f47f5d1ecacc36e0cce48f8e4f7bb56f86a22ff17bb009e359923`
- Schema：`059`
- 公网入口：`http://e606.hlszh.com:9000/`

## 根因与修复

现网在停用规则后再次启用同一修订时，计划生成成功，但 apply 返回 503
`ALARM_CONFIGURATION_PERSISTENCE_FAILED`。PostgreSQL 日志确认违反
`uq_alarm_definition_content`：停用只移除当前指针，正确地保留不可变历史定义；再次启用却尝试重新插入
相同 `(asset_id, entity_instance_id, content_digest_algorithm, content_digest)` 内容，因此发生唯一键冲突。

修复保持不可变历史语义：写入定义时遇到相同内容不再重复插入，而是按完整四列内容身份取回既有 definition
ID，再恢复 `t_alarm_definition_current` 指针。这样重新启用不会复制历史定义，也不会绕过配置修订、栅栏、事务
或审计。

## 测试与评审

- TDD RED：新增“启用 → 空修订停用 → 重新启用原修订”真实 PostgreSQL 回归测试，旧实现稳定触发上述唯一键
  冲突和 `ALARM_CONFIGURATION_PERSISTENCE_FAILED`。
- GREEN：L2 告警配置内存与 PostgreSQL 专项 `14/14`，0 失败；发布脚本 `56/56`，0 失败；Python
  `compileall` 与 `git diff --check` 通过。
- 独立代码评审未发现 Critical、Important 或 Minor 问题，确认四列内容身份、事务并发和不可变历史语义正确。
- 旧 `test_alarm_configuration_postgres` 大文件仍含硬切前构造器与已删除预览接口的历史测试债，不是本次回归；
  当前 L2 告警主链专项全部通过。

## 切换记录

- 运行配置备份：
  `/opt/zizu-release-test-0.5.0/release.env.pre-v0.7.3-20260902T0853Z`
- Schema 未变化，本轮复用当天已校验的数据库备份：
  `/opt/zizu-release-test-0.5.0/backups/v0.7.1-pre/omnithings-20260902T0400Z.dump`
  （`214,160,789` bytes，SHA-256
  `fb0b44716aed0b44ba385319de0f0f9df239b2b40f68070f946679eba7cbb6d5`，`pg_restore -l` 1,078 项）。
- 只重建 Compose `backend`；TimescaleDB 与 NanoMQ 均保持自 2026-08-27 起的原容器，未重启。
- 继续使用 host 网络、`/dev/mqueue` tmpfs、HTTP 9000；未启动 Caddy/TLS。

## 现场验收

- API 首先复用原失败计划启用专用规则，apply 从原 503 变为 200，统一配置版本从 453 升至 454；随后
  正式停用返回 200，版本升至 455。
- 公网 Browser 再做用户路径复验：告警中心 → 告警规则 →
  `E2E告警发布验证-20260902-1223`，点击“启用”后页面明确显示“已启用”，点击“停用”后恢复“已停用”；
  两次 plan 均为 201、apply 均为 200，统一配置版本最终为 457，浏览器控制台 error/warn 为 0。
- 数据库最终证明相同告警内容的不可变定义总数仍为 `1`，当前指针为 `0`；测试规则不参与现场运行。
- backend `healthy`、restart 0，`/api/v1/health/live` 返回版本 `0.7.3`；MQTT connected、Pipeline
  running。数据库和 MQTT 启动时间未变化，根分区剩余约 2.4GB。

## 边界

- 本轮没有确认告警、执行 JDM、控制或设备写。
- 1 号机仍是 HTTP 测试环境，不满足正式公网安全基线。
