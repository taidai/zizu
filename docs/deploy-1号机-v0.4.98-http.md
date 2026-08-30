# 1 号机 v0.4.98 HTTP 部署与主干验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.98`
- 发布提交：`5db740518a295303a2c86cf9bc3796ab4d06edc6`
- 标签：`v0.4.98`
- GitHub Actions：`33302239329`，成功
- Schema：`054`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:940643f9ba8a07cf9f3d7d2b5ec1ec3f035e9ab0dccf008f976b4b481c3662dd`
- 1 号机实际 image ID：`sha256:99b3d90b9a014663203c8f42362f7a2901361eb06279c4c935856261d6878aed`
- 发布目录：`/opt/zizu-release-test-0.4.98`

## 本轮修复

v0.4.97 的 L2 当前质量存在续鲜错误：`t_telemetry_latest.frame_sequence` 会随每个空帧推进，旧实现又把
该帧的 `capture_beat` 当成原始点位真正被接收的节拍，导致停止上报的 L0 仍可能被 L1/L2 判为 GOOD，
而 EMS 的新鲜度投影已经判为 STALE。

Schema 054 为 `t_telemetry_latest` 增加非空、非负的 `accepted_beat`，并从真实遥测事实一次性回填；无法
证明来源的历史 latest 以 0 失败关闭。事务 B、处理快照、统一 outbox 和共享新鲜度函数都改为携带原始
观测真正被接收的节拍，不再用后续空帧替旧样本续鲜，也没有在每拍增加历史表查询。

## 自动门禁与恢复保护

- 后端完整发现：375 tests 通过，151 项明确的外部 PostgreSQL 环境型 skip，0 failure。
- 真实 PostgreSQL 专项：44/44；scripts：43/43；前端：49/49。
- TypeScript/Vite production build、Python compileall 和发布范围 diff check 通过；独立复审无
  Critical/Important，结论 Ready。
- GitHub 双架构镜像流水线成功，release manifest 为 `0.4.98 / Schema 054 / amd64+arm64` 固定摘要。
- 切换前备份：`/opt/zizu-backups/pre-v0.4.98-schema053/omnithings.dump`，大小 `152,082,125` bytes，
  SHA-256 为 `a67763e4b3579a83ce47ae2a99a5939cc446a839f818079dc6120d20b2f89958`；
  `pg_restore -l` 可读，共 911 行目录。
- 只重建 backend；TimescaleDB、NanoMQ 和 easyread 未切换。继续保持 `network_mode: host`、
  `tmpfs /dev/mqueue`、`restart: unless-stopped`、`no-new-privileges`、`cap_drop: ALL` 和原业务卷。
- 未启动 Caddy/TLS，未发布实体、确认告警、启停规则、运行 JDM、下发控制或写设备。

## 运行证据

- 匿名 liveness 返回 `alive / 0.4.98`；backend 为 running/healthy、restart 0，实际 image ID 与固定
  ARM64 发布镜像一致。
- Schema 054 已应用且 `errors=0`；`accepted_beat` 为 NOT NULL，并有 `accepted_beat >= 0` 检查约束。
- 数据帧从部署后持续推进到验收时 `capture_beat=190141 / frame_sequence=84859`；部署后 COMPLETE
  1298、FAILED 0，瞬时在线队尾仅 1 帧。未发布 outbox 从 frame sequence 84874 在 3 秒内排空，证明
  dispatcher 正常消费而非固定积压。
- IGBT 验收时 L0 为 `quality=STALE(1)`，真实 `accepted_beat=189491`，当前 head 为 190141；L2 为
  `quality=STALE(1) / INPUT_STALE`。旧样本没有被空帧续鲜。
- 启动后日志未发现 ERROR、Exception 或 Traceback；根分区仍有约 4.1 GB 可用空间。

## Browser 主干只读验收

- 前后端均显示 `0.4.98`，Pipeline 运行中、MQTT connected，消息、入库和最后消息时间持续前进。
- 真实节点树可选择 `LVK → 工厂电站 → 储能 → 变流器/储能电表/电池簇`。
- L0：PCS 变流器显示 45 个原始点位；实时值、质量、时间和 Neuron 来源可见；历史页可以选择任一
  原始点位，并提供 1 小时、24 小时、7 天及趋势/明细入口。
- L1：在 L0 勾选 IGBT 温度后，内联“加工为实体”可定义实体名称、直接使用、倍率与偏移、状态映射、
  公式计算、单位和类型，并先检查结果再发布。本轮只展开检查，没有发布。
- L2：PCS 节点显示 2 个标准实体；实时卡片、历史趋势、原始来源和技术证据可见。IGBT 在 L0 与 L2
  同时显示“超时”，EMS 同时失败关闭为 `ENTITY_DATA_STALE`，v0.4.97 的页面质量矛盾已关闭。
- 告警：当前活动告警 0；规则页 3 个既有规则，实体选择仅列 L2 标准实体；未确认、启停或编辑。
- JDM：规则引擎页面可访问，现场暂无规则。控制页可访问，当前没有可控 L2 实体。固定 EMS 工作台
  可访问，PCS 有功功率正常，停更实体按 `ENTITY_DATA_STALE` 失败关闭。

## 结论

v0.4.98 已按固定摘要部署并完成只读主干验收。原始观测的接收节拍现在可持久化、可恢复并贯穿 L1/L2，
L0、L2 和 EMS 对同一停更实体给出一致结论。1 号机仍是公网 HTTP 测试部署，启动日志中的开发模式与
示例凭据风险没有因本次功能修复而消失，不能宣称达到生产安全要求。
