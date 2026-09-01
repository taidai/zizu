# 1 号机 v0.6.8 HTTP 部署与验收记录

日期：2026-09-02

## 发布身份

- 版本：`0.6.8`
- 产品提交与标签：`4dacdf4d8c8df325a9ad9729fc225f3829a9fc43`
- GitHub Actions：`33543992024`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:6bebbaabb06531d5338c6b3fb080b0fb0b1c721f82575c03f9bdcb2a6863b0b5`
- Schema：`059`
- 公网入口：`http://e606.hlszh.com:9000/`

## 本轮交付

- L0 保留设备原始标量。Neuron BIT 的 `0/1` 在 L0 保存为整数；超出协议范围的 `2` 仍保存为原值，
  同时标记 BAD 并给出原因，不再提前转成 `false/true`。
- L1 增加真正的直接使用与显式布尔映射。只有用户选定 `boolean_map` 后，`0/1` 才转成 L2 布尔值；
  非法原值不会生成假正常值。
- L2 使用“上次正常值 + 当前质量/状态”语义；当前 BAD/STALE 时，上层能看到当前不可用、上次正常值、
  状态时间和来源证据。
- 旧 BIT 恒等公式通过一次性硬切生成新的不可变加工修订和安装记录；旧证据保留，不做双写或运行期兼容。

## 切换记录

- 切换前备份：
  `/opt/zizu-release-test-0.5.0/backups/v0.6.8-pre/omnithings-20260901T181502Z.dump`
- 备份大小 `254,563,658` bytes，SHA-256
  `b7edc72f0f0f85faa84f8e5906d8c07930818656c985768212be5c4b0226ec57`；`pg_restore --list` 通过。
- 硬切预检：1 个可确定转换输出，0 个 blocker；Schema 058 迁移到 059，配置修订从 312 生成硬切修订 313。
- 按已确认的硬切要求清除 17 张运行表共 `3,232,454` 条旧测量/帧/告警/JDM 运行数据；清理前后
  59 个节点、1,684 个点位、87 个加工修订、25 个实体数量一致。
- 只重建 Compose `backend`；TimescaleDB、NanoMQ、Neuron 未重启。保持 host 网络、`/dev/mqueue`
  tmpfs、旧 runtime env 和 HTTP 9000；未启动 Caddy/TLS。

## 验收证据

- 真实 TimescaleDB 点位加工回归 `20/20`；新增覆盖独立连接把 UUID 解码为文本时的硬切预检。
- 公网 Chromium 无头主干最终 `7/7`、0 失败、0 跳过：登录；节点 CRUD；Neuron 点位预览/导入；
  L0 实时/历史/筛选/分页；L1 检查、发布、共享模板升级、停用与恢复；L2 实时/历史/质量/来源；
  BIT `0/1/2` 原值与显式布尔映射；告警实体选择；读取失败提示；点位删除；节点退役。
- 最终现场：backend healthy、restart 0、版本 0.6.8、固定 ARM64 摘要一致、Schema 059、
  `frames_failed=0`、`outbox_unpublished=0`、活动 E2E 临时节点 0、E2E Neuron 节点 0；近 30 分钟无
  Traceback、CRITICAL、PoolError、tick failure 或 ERROR。
- E2E 只在 `E2E验证` 隔离范围内写入并自动清理；规则只做绑定/取消，不执行 JDM、控制或设备写。

## 已知边界

- 1 号机仍以 development mode 和 HTTP 运行，会提示示例凭据不适合公网生产；它是测试/维护环境，
  不能宣称满足正式公网安全基线。
- 可见 Browser 已打开站点登录页；提交浏览器登录表单需要用户在动作前确认。无头验收已经完成全部业务主干。
