# 1 号机 v0.6.7 HTTP 部署与验收记录

日期：2026-09-01

## 发布身份

- 最终版本：`0.6.7`
- 发布提交：`2c3c0dec4bd3f504f6fc027cf4fd83d96e52f3c6`
- GitHub Actions：`33491291281`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:f5159d3802a310d6234e0b5192f84df21a821829896baa92712e50e33d681a35`
- 现场 image ID：`sha256:a9d5549532a28775ee8618fbe32c1c682d569bb29ed55218b911c1074ed20755`
- Schema：`058`（本轮无数据库结构变更）
- 公网入口：`http://e606.hlszh.com:9000/`

## 根因与修复

- L0 点名 `15V电源故障` 被内联点位加工规范为内部输入名 `15v`。BOOL 的“直接使用”内部采用强类型
  恒等公式，Python 公式语法不允许变量以数字开头，因此在检查阶段返回
  `Formula syntax is invalid`。
- 新版本只调整内联加工的内部输入身份：非字母开头或命中公式关键字时自动增加 `point_` 前缀，
  因而示例变为 `point_15v`。界面名称、L0 来源键、L2 业务标识和数据结构均不改变；重名仍按既有
  稳定后缀消歧。
- 独立复审发现 `and`、`in` 等公式关键字存在同类风险，发布前一并纳入前缀规则和回归测试，没有只
  针对截图中的单一名称打补丁。

## 验证证据

- TDD：真实 `15V电源故障 + BOOL + 直接使用` 先复现 `15v` 错误，再验证生成
  `point_15v`；公式关键字 `and` 同样先红后绿。
- 前端专项 `12/12`、前端全量 `61/61`、发布/版本专项 `15/15`、`git diff --check` 全部通过；本地
  TypeScript/Vite 生产构建通过（8191 modules）。GitHub Actions 在干净环境成功构建并验证
  v0.6.7 的 AMD64/ARM64 不可变镜像和发布清单。
- 两轮独立复审最终为 Critical 0、Important 0；数字、全中文、符号、公式关键字、重名消歧和
  numeric/state/formula 路径均无阻断。
- 应用内 Browser 新页两次未能附着，外部浏览器连接也不可用；未伪报插件验收成功。随后按已确认的
  快速测试方式，用现有本机 Chromium 对公网执行一次性无头短验收：登录后选择现有节点
  `E2E验证` 的 `15V电源故障`，选择“直接使用”并点击“检查结果”，页面显示“检查通过，可以发布”，
  未再出现公式错误；没有点击“发布实体”，没有改变运行配置。
- 最终现场：近 60 秒 `25` 帧 COMPLETE，outbox 待发 `0`，未完成帧 `0`；backend healthy、restart
  `0`，保持 host 网络和 `/dev/mqueue`，近 10 分钟无 ERROR、CRITICAL、Traceback 或 tick failure。

## 切换与边界

- 只重建 Compose `backend`；TimescaleDB、NanoMQ、Neuron 均未重启。运行配置备份为
  `/opt/zizu-release-test-0.5.0/release.env.pre-v0.6.7-2c3c0de-20260901T0919Z`。
- 未启动 Caddy/TLS，未执行控制、设备写、自动策略或实体发布。公网仍是 HTTP 测试/维护环境，不能
  宣称满足正式公网生产安全基线。
