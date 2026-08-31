# 1 号机 v0.6.1 HTTP 部署与验收记录

日期：2026-09-01

## 发布身份

- 最终版本：`0.6.1`
- 源码提交：`7961563a5b23dc5efd269897da6b4dee1985fc82`
- GitHub Actions：`33426123305`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:6a181ea4cb9f3d4d745cf9db4312bde5198c287b322add62f2ddef18e57827f2`
- Schema：`058`
- 公网入口：`http://e606.hlszh.com:9000/`

## 根因与修复

- v0.5.9 现场配置修改出现 `CONFIGURATION_RUNTIME_DRAIN_TIMEOUT`。数据库证据显示，已退役的 E2E 和
  旧设备节点仍有 17 个 L1 加工实例保持 `current=TRUE`，每一拍都继续执行，长期占用帧处理能力。
- v0.6.0 / Schema 057 让节点退役同时停止其 L1 当前加工并停用对应 L2，迁移清理了遗留的 17 个实例；
  绑定、历史、latest 和来源证据均保留。
- 现场继续发现退役节点下还有 1 个旧式 L2 实体保持活动。v0.6.1 / Schema 058 将语义收口为：节点及
  子树退役时停用其全部 L2 实体，不区分来源类型；迁移清理现网遗留行。最终退役节点活动实体和当前加工
  均为 0。
- 无头验收另修正了两处测试竞态：同步确认停用对话框；点位改名后按精确单元格和配置栅栏等待，避免把
  来源路径中的旧名称误判为页面已更新。

## 切换与备份

- 只重建 Compose `backend`；TimescaleDB、NanoMQ、Neuron 均未重启。
- 保持 `network_mode: host`、`tmpfs: /dev/mqueue`、`unless-stopped` 和既有运行环境。
- Schema 056 切换前完整备份：
  `/opt/zizu-backups/pre-v0.6.0-schema056-20260901-0220/omnithings.dump`，
  100,295,281 bytes，SHA-256
  `0bcf6174884daa79a32f72947ac156a2ba8d2f8a9121706cd5b3ee49302ba711`。
- Schema 057 切换前完整备份：
  `/opt/zizu-backups/pre-v0.6.1-schema057-20260901-0243/omnithings.dump`，
  100,211,616 bytes，SHA-256
  `beb52c7e0aa55e4b562b57f9de469b1a7a3d3d0a21f8cd51bfbf5e615ede5378`。
- 两个备份均通过 `pg_restore -l` 目录读取验证；对应 `release.env` 同目录保存。

## 验收证据

- 最终聚合命令 `python scripts/verify_delivery.py --site-url http://e606.hlszh.com:9000` 返回
  `PASSED`，`missing=[]`；后端 406 项通过、160 项环境跳过，脚本 51 项通过，前端契约与生产构建、
  公网站点只读检查全部通过。
- 真实 PostgreSQL 节点与加工专项 20/20 通过；新增退役实体用例按 TDD 先失败后通过。
- 公网 Playwright 无头主干 6/6 通过，0 失败、0 跳过，216 秒。覆盖登录、节点 CRUD、Neuron 点位预览
  与导入、L0 实时/历史/筛选/分页、L1 检查/发布/模板升级/停用/恢复、L2 实时/历史/质量/来源，以及
  禁用无动作规则的指定与取消；没有执行控制或自动策略。
- 最终容器 `healthy`、restart 0、固定摘要正确；抽样 60 秒内 COMPLETE 58，未完成帧 2、最大帧龄
  2 秒，未发布 outbox 0，近 15 分钟无 ERROR/Traceback/迁移失败。

## 测试资源与边界

- 活动临时节点、当前 E2E 加工、退役节点活动实体、当前 E2E 告警、活动 E2E 共享模板、Neuron 测试节点
  均为 0。节点私有加工修订作为不可变历史证据保留，不是当前运行加工或共享模板。
- 当前现场只保留空的活动边界根 `E2E验证`，没有真实活动设备节点。因此本记录证明平台配置主干可用，
  不代表真实 PCS/光储充站点配置已经交付；真实站点仍需按正式项目重新建立节点、导入点位并发布实体。
- 继续使用公网 HTTP，未启动 Caddy/TLS。当前为 development 模式且仍有不安全凭据告警，不能宣称为
  公网生产安全配置。
