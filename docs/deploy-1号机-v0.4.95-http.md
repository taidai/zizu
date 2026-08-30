# 1 号机 v0.4.95 HTTP 部署与验收记录

日期：2026-08-30

## 发布身份

- 版本：`0.4.95`
- 发布提交：`141309b96badd3b0d655756cf9cfad34c4707e40`
- 标签：`v0.4.95`
- GitHub Actions：`33291009972`，成功
- Schema：`052`
- ARM64 固定摘要：`ghcr.io/taidai/zizu@sha256:0fcb2466e6c4311c49000db28de63c612d428b98601cf092e0b1f19d11503db9`
- 1 号机实际 image ID：`sha256:10689b5f38411d789f5195ac02659f3926846f20952f68c6cda5ca895a0bf4af4`
- 发布目录：`/opt/zizu-release-test-0.4.95`

## 本轮修复

v0.4.94 的 L0 实时页面会按当前节拍把过期点位显示为“超时”，但 L1 候选配置试算直接读取数据库里保存的
旧质量，因此同一个来源可能错误显示“正常”。根因是 L0 committed 投影和 L1 试算各自实现了一套质量判定。

v0.4.95 提取共享的 `effective_l0_quality`：统一根据帧节拍、点位 `accepted_beat` 和接收年龄计算当前有效
质量；L0 实时投影与 L1 试算共同使用。L1 试算同时读取真实来源点的 `accepted_beat`，因此过期来源会返回
`STALE / INPUT_STALE`，不再产生假 GOOD。没有新增服务、数据库迁移、依赖或兼容分支。

TDD 证据：新增的 `test_trial_marks_expired_l0_input_stale` 在旧实现中得到 `GOOD` 而按预期 RED；最小修复后
得到 `STALE` 而 GREEN。

## 自动门禁

- 相关真实 PostgreSQL 测试：19/19 通过。
- 后端完整测试：368 tests，146 项需显式外部环境而跳过，0 failure。
- scripts：43/43；前端：49/49；TypeScript/Vite production build 成功（8186 modules）。
- `compileall`、`py_compile` 和 `git diff --check` 通过。
- GitHub 双架构发布流水线成功；release manifest 为 `0.4.95 / Schema 052 / amd64+arm64` 固定摘要。

## 切换与恢复保护

- 切换前备份：`/opt/zizu-backups/pre-v0.4.95-schema052/omnithings.dump`
- 备份大小：`105,279,255` bytes
- 备份 SHA-256：`dbff8c95778a3af112504e109f6a0f73e62c3342ed1673625365b8335c5c6998`
- `pg_restore -l` 可读，共 806 个 TOC 项；Timescale 连续聚合循环外键提示不影响归档完成与目录校验。
- 只重建 backend；TimescaleDB、NanoMQ、Neuron/easyread 的容器 ID 保持不变。
- 保持原有 runtime env、`network_mode: host`、`tmpfs /dev/mqueue`、`restart: unless-stopped`、
  `no-new-privileges`、`cap_drop: ALL` 和业务卷。
- 未启用 Caddy/TLS，未发布实体、未启停告警或 JDM，未执行控制或设备写入。

## 运行证据

- 匿名 liveness 返回 `alive / 0.4.95`；backend 为 healthy、restart 0、aarch64，实际 image ID 与发布镜像一致。
- Schema 052 没有待执行迁移；Pipeline 已连接 MQTT，加载 100 条 tag 规则和 100 条 Neuron 来源映射。
- 最终只读数据库复核：frame head `69,894`；累计 `COMPLETE 53,075 / FAILED 16,818`；没有
  `PENDING/PROCESSING`；未发布 outbox 为 0。
- 最近 30 分钟内形成的帧为 `COMPLETE 44 / FAILED 132`，最后帧创建于 03:58:31 UTC；最后 telemetry
  接收于 03:58:30 UTC（北京时间 11:58:30）。页面 MQTT 消息计数继续增加，但没有新的已接收业务遥测，
  因此不能用消息总数替代帧吞吐证据。
- 一次瞬时资源快照约为：backend 19% CPU、TimescaleDB 62%、NanoMQ 4%、easyread 89%；只作当时观测，
  不作为性能承诺。

## Browser 主干验收

- Browser 登录后，前后端均显示 `0.4.95`，Pipeline 运行中、MQTT connected；控制台 warning/error 为空。
- 真实节点树可选择 PCS“变流器”；L0 实时展示 45 个点位以及值、质量、时间和 Neuron 来源。
  “交流总有功功率”为 `0 / 超时 / 2026-08-30 11:58:30`；L0 历史入口、点位选择和范围选择可见。
- 从该 L0 点位打开内联 L1 并只点击“检查结果”，结构校验提示可以发布；当前试算结果为
  `0 / 超时 / INPUT_STALE`，来源证据为 1 个来源、帧 69894、配置修订 28，数据时间与 L0 一致。
  这证明 v0.4.94 发现的假 GOOD 已在真实站点关闭。本轮没有点击“发布实体”。
- L2“标准实体”有 2 个实体；PCS 有功功率为 `0 kW / 超时`，展开可见“原始数据已 18 分钟未更新”、
  历史范围、原始来源和加工类型；IGBT 实体同样超时。
- 告警中心当前事件为 0；规则入口可用。JDM 页面可访问，现场暂无规则。EMS 固定工作台对 PCS、IGBT 和
  并网数据均以 `ENTITY_DATA_STALE` 失败关闭。
- 全程没有确认告警、发布配置、启停规则、运行 JDM、控制或写设备。

## 结论与未完成项

v0.4.95 固定摘要部署、自动门禁、容器健康和 Browser 主干无副作用验收已经完成；L1 试算质量已与 L0/L2
统一，过期数据不会伪装成 GOOD。持续活负载吞吐验收仍为 `INCOMPLETE`：现场业务遥测在 11:58:30 后
停更，缺少连续 5 分钟的真实 1 秒输入。点位恢复后只需补证 frame head 持续前进、未完成帧与最老帧龄不
增长、outbox 为零且无新增帧龄失败，再决定是否通过吞吐验收。

启动日志仍提示 insecure development mode 和示例凭据；本文不记录账号密码。1 号机仍是测试部署，不能
宣称达到生产安全要求。
