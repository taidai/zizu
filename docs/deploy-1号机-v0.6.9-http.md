# 1 号机 v0.6.9 HTTP 部署与验收记录

日期：2026-09-02

## 发布身份

- 版本：`0.6.9`
- 产品提交与标签：`223dd8b261de5020ebafda1bdbae9e6b9b0b8d93`
- GitHub Actions：`33578491309`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:e6d4f4b6d70f088db710ce094806a52c6428d678a1ab95b4bc8db050b57e8fd9`
- 运行 image ID：
  `sha256:af33cb9829433ba485bae47d8b6caa481b117d0710b05a7e8f2ee3367b98a4b8`
- Schema：`059`
- 公网入口：`http://e606.hlszh.com:9000/`

## 本轮交付

- 告警规则试算在未选择实体、数值无效、故障码为空或 BOOL 值非法时就地阻止，并说明下一步。
- BOOL 告警的触发值、恢复值和试算值改为真实 `true/false` 选择，不再提交自由文本。
- 空数值不再被 JavaScript 静默转换为 `0`。
- 无头主干补充 BOOL 告警 `false` 恢复试算和 `true` 触发试算；只试算，不生成发布预览。

## 切换记录

- 切换前备份：
  `/opt/zizu-release-test-0.5.0/backups/v0.6.9-pre/omnithings-20260902T011420Z.dump`
- 备份大小 `177,943,629` bytes，SHA-256
  `f3debf541f538364023e255a2f3fc56343e240d81c4037554b989a98e338192b`；`pg_restore -l`
  返回 1,024 项，备份可读。
- 只重建 Compose `backend`；TimescaleDB、NanoMQ、Neuron 未重启。保持 host 网络、
  `/dev/mqueue` tmpfs、旧 runtime env 和 HTTP 9000；未启动 Caddy/TLS。

## 验收证据

- 告警前端专项 `8/8`、发布契约与版本门禁 `19/19`、TypeScript 和 Vite production build 通过，
  共转换 8,191 个模块。
- 公网 Chromium 无头验收完成节点 CRUD、Neuron 点位导入、L0 实时/历史/维护、L1 试算与发布、
  模板升级/停用/恢复、L2 实时/历史/质量/来源、BIT `0/1/2`、BOOL 告警试算、读取失败提示、
  点位永久删除和节点退役。
- 最终整套运行前 6 项全部通过；第 7 项已经实际完成 `raw_point.delete`（配置修订 448）和
  `node.retire`（配置修订 449），但 Playwright 旧的 600 秒全局上限先到，未写成单条 PASS。
  随后用独立无头 Chromium 补验 L0 读取失败与恢复，结果通过。验收上限已改为 900 秒，断言统一
  按条件等待最多 40 秒，条件满足立即继续。
- 调试确认两次早期失败均为验收脚本时序假设，不是产品丢数据：配置修订发布后 L0 `14.5` 和 L2
  `14.5` 已落在完整帧；点位维护推进配置修订后，L1 正确拒绝旧修订帧，脚本已改为发送新修订数据帧。
- 最终现场：backend healthy、restart 0、版本 0.6.9、Schema 059、固定 ARM64 摘要一致；最近 30 分钟
  `frames_failed=0`、`outbox_unpublished=0`，仅有 1 个约 3.8 秒的当前 PROCESSING 帧；活动 E2E
  平台节点 0、E2E Neuron 节点 0；日志无 Traceback、CRITICAL、PoolError、tick failure 或 ERROR。

## 已知边界

- 1 号机仍以 development mode 和 HTTP 运行，会提示示例凭据不适合公网生产；它是测试/维护环境，
  不能宣称满足正式公网安全基线。
- 本轮没有发布告警配置、没有启停现场规则、没有执行 JDM、控制或设备写。
