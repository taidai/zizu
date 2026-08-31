# 1 号机 v0.6.2 HTTP 部署与验收记录

日期：2026-09-01

## 发布身份

- 最终版本：`0.6.2`
- 源码提交：`39b8420aa577d8b357c2d0bb609974eda6836229`
- GitHub Actions：`33442895637`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:4a0989613e731b1a832d0b7a9373883354de8cfe0ed852da81578d9d344f424d`
- Schema：`058`
- 公网入口：`http://e606.hlszh.com:9000/`

## 根因与修复

- 节点 CRUD 允许实施工程师自定义 `node_type`，但点位加工模板解析器仍把设备类别限制在
  `SITE/ENERGY/ESS/PV/GRID/METER/EVSE/PCS` 八类。界面会把所选节点的真实类型作为
  `deviceCategory` 提交，因此现场 `E2E_ROOT` 等合法自定义类型被错误拒绝为
  `POINT_PROCESSING_DEVICE_CATEGORY_UNSUPPORTED`。
- v0.6.2 删除这张与节点模型冲突的固定白名单。设备类别仍必须是非空字符串，模板的数据类型、单位、
  公式、输入输出、实体契约和不可变修订校验均保持不变。
- 回归测试先在 `E2E_ROOT` 上按预期失败，再经最小修复转绿；长期 Playwright 主干设备类型由 `PCS`
  改为白名单外的 `E2E_DEVICE`，以后每次节点验收都会覆盖自定义类型点位加工。

## 切换与回退

- 只重建 Compose `backend`；TimescaleDB、NanoMQ、Neuron 均未重启。
- 保持 `network_mode: host`、`tmpfs: /dev/mqueue`、`unless-stopped` 和既有运行环境。
- 本轮没有 Schema 变化，未重复制作数据库完整备份；切换前运行配置保存为
  `/opt/zizu-release-test-0.5.0/release.env.pre-v0.6.2-39b8420-20260901-055033`，需要回退时可恢复该
  文件并重建 backend。更早的 Schema 057/056 完整备份继续保留。

## 验收证据

- 聚合命令 `python scripts/verify_delivery.py --site-url http://e606.hlszh.com:9000` 返回
  `PASSED`、`missing=[]`：后端 406 项通过、160 项环境跳过，脚本 51 项、前端 55 项、生产构建和
  公网站点版本检查全部通过。
- 真实 PostgreSQL 点位加工专项 15/15 通过；回归和 E2E 清理辅助测试均先红后绿。
- 公网 Playwright 无头主干 6/6 通过，0 失败、0 跳过，229.3 秒。实际使用自定义类型
  `E2E_DEVICE`，走通节点 CRUD、Neuron 点位导入、L0 实时/历史、L1 检查/发布/模板升级/停用/恢复、
  L2 实时/历史/质量/来源及禁用无动作规则的指定与取消；没有执行自动策略、控制或设备写。
- 二次清理返回全部 0：活动测试节点、当前测试加工、活动测试实体、活动测试共享模板、测试规则和
  Neuron 测试节点均无残留。未发布 outbox 为 0；60 秒抽样为 COMPLETE 57、在线队尾 2、最大龄
  2.1 秒；backend `healthy`、restart 0，近 15 分钟无 ERROR/Traceback/迁移失败。

## 现场边界

- 当前活动节点仍只有空的 `E2E验证 / E2E_ROOT` 边界根，没有真实 PCS/光储充项目配置；本记录证明
  自定义节点类型的数据主干可配置，不代表真实站点已交付。
- 公网仍使用 HTTP，未启动 Caddy/TLS；当前 development/不安全凭据告警尚未消除，不能宣称为公网
  生产安全配置。
