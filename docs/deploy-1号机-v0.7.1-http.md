# 1 号机 v0.7.1 HTTP 部署与告警发布验收记录

日期：2026-09-02

## 发布身份

- 版本：`0.7.1`
- 功能提交：`76aa74d`；发布提交与标签：`7f35dbcead5a002effbcd1d9e3dcaebd6397c3e4`
- GitHub Actions：`33588890868`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:fd698246c5aae9a16c517a2f90854e6220434039a7d478ffd3f334708e8b9164`
- 运行 image ID：
  `sha256:30049a82e1a2db82c0e58efa9af47b51c359a72586ca7a6e53ed72e0c674a5d1`
- Schema：`059`
- 公网入口：`http://e606.hlszh.com:9000/`

## 根因与修复

现网请求链显示，试算、规则集和发布计划都能成功，只有计划 apply 在约 30 秒后返回 422
`CONFIGURATION_RUNTIME_DRAIN_TIMEOUT`。根因是告警 apply 的异步接口直接同步调用配置栅栏；栅栏等待旧帧和
outbox 排空时阻塞了 FastAPI 事件循环，而负责完成排空的帧处理任务恰好运行在同一个事件循环，形成自锁。
前端没有该底层超时码的专用文案，最终只显示“告警配置请求未完成”。

修复把完整的 `configuration.apply(...)` 调用移入工作线程，与节点、JDM、点位等既有配置发布入口保持一致。
事务边界、配置栅栏、排空条件、告警规则和数据库结构均未改变。新增异步回归测试证明：栅栏等待期间事件循环
仍能运行并释放栅栏；旧实现会稳定返回 422，修复后返回成功。

## 切换记录

- 数据库备份：
  `/opt/zizu-release-test-0.5.0/backups/v0.7.1-pre/omnithings-20260902T0400Z.dump`
- 大小：`214,160,789` bytes
- SHA-256：`fb0b44716aed0b44ba385319de0f0f9df239b2b40f68070f946679eba7cbb6d5`
- 容器内 `pg_restore -l`：`1,078` 项
- 运行配置备份：
  `/opt/zizu-release-test-0.5.0/release.env.pre-v0.7.1-20260902T0400Z`
- 只重建 Compose `backend`；TimescaleDB 与 NanoMQ 均保持自 2026-08-27 起的原容器，未重启。
  继续使用 host 网络、`/dev/mqueue` tmpfs、HTTP 9000；未启动 Caddy/TLS。

## 验收证据

- TDD RED：新增真实异步接口测试在旧实现上得到 422
  `CONFIGURATION_RUNTIME_DRAIN_TIMEOUT`；GREEN 后告警配置与运行专项 `16/16`，0 失败。
- 告警前端模型专项 `6/6`，0 失败；TypeScript 与 Vite production build 成功，转换 8,191 个模块。
- GitHub 发布工作流完成 ARM64/AMD64 构建、固定清单校验和制品保存，结论 `success`。
- 公网无头 Browser 真实闭环：
  - 选择 `E2E验证 / 15V电源故障`，安全规则试算值为 `0`，页面显示“未命中触发条件，命中恢复条件”；
  - 生成预览后确认发布，页面显示“已发布，统一配置版本 451”；
  - 服务端依次记录 trial 200、rule set 201、plan 201、apply 200；
  - 随后停用临时规则，第二次 plan 201、apply 200，页面确认该组“已停用”；
  - 数据库最终统一配置版本为 452，当前告警定义中来自验收版本 451 的记录为 0；
  - 强制刷新后同时显示 `FE 0.7.1` 与 `v0.7.1`，浏览器控制台 error 为 0。
- 最终现场：backend healthy、restart 0；运行摘要、host 网络和 `/dev/mqueue` 与发布清单一致；启动日志显示
  Schema 059 全部跳过且 errors=0，MQTT、Pipeline、数据帧处理与 outbox 均启动成功。

## 边界与遗留

- 那条触发值和恢复值都为 `false` 的失败计划没有被应用；临时验收规则只保留停用的历史修订，不参与运行。
- 本轮没有确认告警、执行 JDM、控制或设备写。
- 1 号机仍为 HTTP 测试环境，不满足正式公网安全基线；根分区剩余约 2.4GB，本轮未清理旧镜像。
