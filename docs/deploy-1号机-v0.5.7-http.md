# 1 号机 v0.5.7 HTTP 部署记录

日期：2026-08-31

## 发布身份

- 运行版本：`0.5.7`
- 运行功能提交：`1574462dd6f238e780e5b6616bb65b65cb30ecd6`
- 验收脚本后续修订：`324bb62`
- GitHub Actions：`33385471679`，成功
- ARM64 固定镜像：`ghcr.io/taidai/zizu@sha256:5cc9734b2959655dcbb8cf98e3a48b20efc7eb379fccb0d8c4669a6a535bdbea`
- 运行 image ID：`sha256:d3fe79d1e3ff2121ce2f0a07e22664066251f4cdd470d77621718f069268d0cc`
- Schema：`056`

`324bb62` 只调整无头验收对配置栅栏有界等待的断言，不进入运行容器；运行功能代码与
`1574462` 一致。

## 本轮修复

- 点位加工停用计划只含删除候选，不再错误执行输出值试算，修复停用时的 `KeyError`/HTTP 500。
- 配置栅栏仍先暂停旧修订拍照并排空旧帧/outbox，但把异常上限从 5 秒调整为 30 秒；正常排空后立即
  返回。1 号机实测冷启动队列曾需 26.6 秒，旧 5 秒上限会误报
  `CONFIGURATION_RUNTIME_DRAIN_TIMEOUT`。
- 无头验收在每次配置修订后发布一条新样本，确认新修订真正产生 committed L2，避免用旧数据误判。

## 切换与回滚

- 仅拉取 ARM64 固定摘要并重建 Compose `backend` 服务；TimescaleDB、NanoMQ、Neuron、easyread 未重建。
- 保持 `network_mode: host`、`tmpfs: /dev/mqueue` 和原运行环境。
- v0.5.7 切换前配置备份：
  `/opt/zizu-release-test-0.5.0/release.env.pre-v0.5.7-1574462-20260831T111700Z`。
- Schema 未变化；沿用切换前已验证备份：
  `/opt/zizu-backups/pre-v0.5.4-schema056-20260831-180256/omnithings.dump`，
  SHA-256 `97958f9cef66cc8fd403a0d4b64d828f0ccfa4d4632352a231cca31633fa0eb4`。

## 验证证据

- 后端完整测试：397 项通过，153 项按环境跳过，0 失败。
- 发布脚本测试：51 项通过，0 失败；TypeScript 检查通过。
- 公网无头浏览器主干验收：6/6 通过，0 失败、0 跳过，耗时 209.9 秒。
- 页面旅程实际覆盖：登录；节点创建、编辑、搜索、刷新、选择和退役；Neuron 点位预览导入；L0
  实时、历史、筛选和分页；L1 检查、发布、模板保存、升级、停用和恢复；L2 实时、历史、质量、来源；
  禁用且无动作的规则绑定与解绑；读取失败可见并恢复。
- 最终运行：`alive / 0.5.7`，Pipeline `RUNNING`；TimescaleDB、MQTT、Neuron 均 connected；消息解析
  成功率 100%，数据库写入错误 0；backend healthy、restart 0、host 网络、`/dev/mqueue`。
- 抽样时未完成帧 1 个、帧龄约 1.1 秒，未发布 outbox 0；发布后日志未发现
  ERROR/CRITICAL/Traceback 或数据帧 tick 失败。

## 测试资源清理

- 最终只保留空测试边界根节点 `E2E验证`。
- 活动临时设备节点 0、活动 E2E 规则 0、E2E Neuron 节点 0、活动 E2E 共享模板 0。
- 清理走正式 API/退役语义，没有直接删除数据库记录。

## 边界

- 1 号机继续使用公网 HTTP；未启动 Caddy、未申请或配置 TLS。
- 未执行自动策略、JDM 动作、设备控制或真实设备写入。
- 可见 Browser 已打开登录页；新浏览器会话没有登录态，需用户确认后才能把账号凭据提交到网页并补做
  可见只读抽查。无头浏览器已完成全部主干验收。
