# 1 号机 v0.7.0 HTTP 部署与验收记录

日期：2026-09-02

## 发布身份

- 版本：`0.7.0`
- 功能提交：`541ebf5`；发布提交与标签：`321b184c0c7209489cf24819ed871e83268255bf`
- GitHub Actions：`33583938581`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:fbd47acba11048c7834fd2bb88cdd97c644c943484f6274ed4344eeb4a0d55ea`
- 运行 image ID：
  `sha256:6246d51251a7a5ec6438374692b7fc09eabd7e0b9e1dceca924cdd793f9c2553`
- Schema：`059`
- 公网入口：`http://e606.hlszh.com:9000/`

## 根因与修复

现网抓包证明 BOOL 告警试算请求和后端 matcher 原本能正确区分 `false` 与 `true`。问题在展示层：旧结果
只返回“命中触发条件/未命中恢复条件”，没有显示试算对象、试算值、触发值和恢复值，所以不同选择即使
产生了正确计算，用户也无法确认选择是否生效。

新页面明确显示：

- 本次试算使用的节点与 L2 实体；
- 实际提交的试算值；
- “会触发某等级告警、会恢复、无变化或条件冲突”的直接结论；
- 触发与恢复的运算符、设定值及各自命中状态；
- 批量选择时明确说明试算以首个所选实体为准。

修复只格式化后端返回的结构化匹配结果，没有在前端复制判断逻辑，也没有改变告警运行或正式发布语义。

## 切换记录

- 数据库备份：
  `/opt/zizu-release-test-0.5.0/backups/v0.7.0-pre/omnithings-20260902T024519Z.dump`
- 大小：`188,591,543` bytes
- SHA-256：`1f26d4ba59bfbecd7d6d191489ae4b1a89b903312d4155ed7db2fc64d2147b55`
- 容器内 `pg_restore -l`：`1,045` 项
- 运行配置备份：
  `/opt/zizu-release-test-0.5.0/release.env.pre-v0.7.0-20260902T025129Z`
- 只重建 Compose `backend`；TimescaleDB、NanoMQ、Neuron 未重启。继续使用 host 网络、
  `/dev/mqueue` tmpfs、HTTP 9000；未启动 Caddy/TLS。

## 验收证据

- TDD RED：新增测试首先因 `describeAlarmTrialResult is not a function` 失败。
- GREEN：告警前端专项 `9/9`，0 失败；TypeScript 与 Vite production build 成功，转换 8,191 个模块。
- GitHub 发布工作流完成 ARM64/AMD64 构建、固定清单校验和制品保存，结论 `success`。
- 公网 Chromium 无头聚焦验收：
  - `试算值=false`，请求体为布尔 `false`，结果“会恢复”；
  - `试算值=true`，请求体为布尔 `true`，结果“会触发警告告警”；
  - 触发值改为 `false`、恢复值改为 `true` 后，请求体和页面条件同步反转；
  - 三次试算 API 均为 200，控制台 error 为 0。
- 最终现场：健康接口返回 0.7.0，Schema 059，backend healthy、restart 0；固定摘要、host 网络和
  `/dev/mqueue` 一致；最近 10 分钟无 ERROR、Traceback、CRITICAL、PoolError 或 tick failure。

## 边界

- 试算是无副作用操作。本轮未生成发布预览、未发布或启停告警规则、未确认告警、未执行 JDM、控制或
  设备写。
- 1 号机仍为 HTTP 测试环境，不满足正式公网安全基线；根分区剩余约 2.6GB，本轮未清理旧镜像。
