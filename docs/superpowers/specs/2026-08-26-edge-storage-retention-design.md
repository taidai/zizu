# 边缘数据容量与保留策略设计

**状态：** 已审阅确认
**日期：** 2026-08-26
**目标版本：** v0.4.85-rc.4 / Schema 045
**适用范围：** 1 号机及同规格单站边缘部署

## 1. 背景与现场证据

1 号机有两个独立文件系统：

- 根盘 `/dev/root`：16GB，已用 91%，主要占用是 `/opt/zizu-backups` 约
  3.33GB 和 `/home/omnithings/bak` 约 2GB；
- 数据盘 `/userdata`：13GB，已用 64%，数据库和 Docker 数据均在此盘。

当前数据库最大的对象不是 L0 历史，而是
`t_l0_observation_dedup`：约 362 万行、1.32GB，约 21.5 小时形成，物理增长约
55～58MB/小时。该表没有清理策略，并被 L0 历史、L0 latest 和 L2 来源表通过外键当作
永久证据引用。按当前速率，数据盘约 3～4 天进入满盘风险。

`t_telemetry` 已有 7 天后压缩、90 天后删除策略，但 chunk 跨度为 7 天，无法及时压缩
活跃数据。`tel_agg_1h` 和 `tel_agg_1d` 虽然存在，却没有刷新任务，当前均为空。

## 2. 目标与非目标

### 2.1 目标

- 把防重复缓存和永久来源证据分开，消除无限增长的去重表。
- 防重复缓存固定保留 6 小时。
- L0 明细固定保留 7 天，6 小时后自动压缩。
- 小时、日连续聚合自动刷新并长期保留。
- 清理旧备份和无用镜像后，根盘使用率低于 75%，数据盘至少剩余 6GB。
- 保持 L0 → L1 → L2 实时主干、质量、时间戳和来源追溯不变。
- 维护失败时可以恢复 Schema 044 和 v0.4.85-rc.3。

### 2.2 非目标

- 不建设通用存储策略中心、租户级策略或可视化策略编辑器。
- 不改变 L1 点位加工、L2 实体、告警、JDM 或控制语义。
- 不处理 TLS、Caddy、设备写控制或自动策略。
- 不调整低增长的 L2 历史、审计日志和其他业务表保留期。
- 不承诺保存 7 天以前的 L0 明细；长期查询只使用小时和日聚合。

## 3. 方案比较

### 3.1 采用：短期防重 + 独立历史证据

`t_l0_observation_dedup` 只保留近期观测身份和摘要，用于拒绝 MQTT 重发或事务结果丢失后的
重试。L0/L2 历史继续保存自身的观测身份、摘要、质量和时间依据，不再通过外键要求防重复
缓存永久存在。

优点是数据职责清楚、容量有上限、历史证据不丢，并能继续使用 TimescaleDB 原生压缩、保留和
任务能力。代价是 Schema 045 必须移除三处指向防重复缓存的历史外键。

### 3.2 拒绝：定期清空全部遥测

实现最少，但会反复制造历史空洞，无法满足故障追溯和工业交付要求，也不能解决职责混淆。

### 3.3 拒绝：保持 90 天明细并只扩容

可以延后风险，却没有容量上限；去重表仍永久增长，换更大的盘只会推迟下一次故障。

## 4. 数据结构与职责

### 4.1 防重复缓存

`t_l0_observation_dedup` 保留现有最小字段：

- `observation_id`
- `tag_id`
- `observed_at`
- `source_digest`
- `source_message_id`
- `source_sequence`
- `created_at`

`source_digest` 继续唯一。新增 `created_at` 清理索引。缓存只保留最近 6 小时，不作为上层查询
接口，也不成为历史表的外键目标。

Schema 045 移除以下外键，但保留列值和现有 CHECK：

- `t_telemetry.observation_id → t_l0_observation_dedup`
- `t_telemetry_latest.observation_id → t_l0_observation_dedup`
- `t_l2_observation_sources.l0_observation_id → t_l0_observation_dedup`

### 4.2 来源证据

证据仍保存在真正的历史事实中：

- L0 历史保存 observation ID、source digest、message ID、sequence、原始值、质量、接收时间和
  event-time basis；
- L2 来源保存 L0 observation ID 与 source digest；
- L2 历史保存加工修订、配置修订、时间依据和来源摘要。

因此清除过期防重复缓存不会删除 L0/L2 历史，也不会破坏上层事实的追溯字段。

### 4.3 防止超期重放造成错误

防重复缓存过期后，同一旧观测可能再次到达。L0 历史已有
`(tag_id, ts, source_digest)` 唯一索引；批量写入必须对该约束使用 `ON CONFLICT DO NOTHING`，
并只把实际插入历史的 observation 计为 accepted。这样旧重放既不会重复写历史，也不会让整批
事务失败。

## 5. 固定保留策略

### 5.1 防重复缓存

- 保存期限：6 小时；
- 清理频率：每 15 分钟；
- 实现：TimescaleDB 原生后台 job 调用固定 SQL procedure；
- procedure 只删除 `created_at < now() - interval '6 hours'` 的缓存行。

不增加应用层调度器、策略表或新的公开接口。

### 5.2 L0 明细

- 新 chunk 时间跨度：1 小时；
- 6 小时后转为 columnstore/compression；
- 7 天后删除明细 chunk；
- 当前 7 天跨度的活跃 chunk 不拆分，待其在 2026-08-27 关闭后按新策略压缩；后续 chunk 使用
  1 小时跨度。

### 5.3 小时和日聚合

- 为 `tel_agg_1h` 增加每小时刷新策略；
- 为 `tel_agg_1d` 增加每日刷新策略；
- 首次部署刷新当前可用的 L0 时间范围；
- 两个聚合不设置删除期限，长期保留；
- `tel_agg_5min` 本轮保持空闲，不增加任务，也不删除，避免扩大范围。

## 6. Schema 045 迁移

迁移必须从完整 Schema 044 开始，fresh、upgrade 和 replay 都需验证。维护窗内 backend 停止写入后：

1. 校验必需表、外键、索引、hypertable、既有 compression/retention job 和连续聚合结构；
2. 移除三处把防重复缓存当作永久证据的外键；
3. 新建同结构紧凑表，只复制最近 6 小时缓存；
4. 原子替换旧缓存表并重建主键、digest 唯一约束和 `created_at` 索引；
5. 调整 L0 history 的 duplicate conflict 写入语义；
6. 把 telemetry 新 chunk 间隔改为 1 小时，把压缩和保留策略改为 6 小时/7 天；
7. 安装唯一的防重复缓存清理 job，以及小时、日聚合刷新 job；
8. 登记 Schema 045。

表替换用于立即归还约 1GB 空间，避免 `DELETE + VACUUM FULL` 的额外长锁和手工步骤。任一步失败，
迁移事务回滚，Schema 044 保持不变。

## 7. 维护与清理顺序

### 7.1 先释放安全工作空间

执行删除前必须生成精确路径清单并验证每个 resolved path；禁止使用未展开 glob、`rm -rf` 指向
上级目录、`docker system prune` 或 `docker volume prune`。

1. 保留已校验 rc7 数据库备份、其 SHA 和 runtime env；
2. 保留 NanoMQ 回滚目录；
3. 删除 `/home/omnithings/bak` 中已列明的 2026-08-02/03 旧应用快照及旧应用 tar，预计释放约
   2GB；
4. 只删除 Docker dangling images，预计释放约 1.05GB；当前 rc.3 和上一版 rc.2 固定摘要必须保留；
5. 创建新的 `pre-v0.4.85-rc.4-schema044` PostgreSQL custom-format dump、SHA，并通过容器内
   `pg_restore -l` 验证；
6. 新备份验证成功后，删除 rc7 以前及其他已列明的旧数据库 dump，只保留新备份、rc7 和 NanoMQ
   回滚资料，预计再释放约 2.16GB。

### 7.2 维护窗

1. 记录切换前磁盘、表大小、job、容器和实时延迟；
2. 停止 backend；Neuron、NanoMQ、TimescaleDB 和设备保持运行；
3. 部署固定 ARM64 rc.4 镜像并应用 Schema 045；
4. 启动 backend，验证 MQTT、数据库、Schema 和实时接口；
5. 连续观察 10 分钟。

维护窗预计 5～15 分钟。因现场 MQTT 遥测不保证持久排队，这段时间允许形成明确的数据空档；
不得伪造补数。

## 8. 错误与回滚

- 清理清单与允许保留清单不一致：删除前停止；
- 新备份 checksum 或 `pg_restore -l` 失败：停止，不删除旧数据库备份；
- Schema 045 preflight、迁移或 replay 失败：不启动 rc.4，保留 Schema 044；
- rc.4 启动后实时链路、job 或容量验收失败：停止 rc.4，恢复迁移前 dump，再用 rc.3 固定摘要启动；
- 回滚恢复前再次核对目标数据库名和备份 SHA；不执行部分表手工回填。

## 9. 测试与交付门禁

### 9.1 自动测试

- Schema 044 → 045、fresh 045、replay 045、partial/corruption 拒绝；
- 最近 6 小时缓存保留、超期缓存删除、历史证据字段不变；
- 超过 6 小时的同源重放不重复写历史且不拖垮整批；
- 防重复、compression、retention、1h 和 1d refresh job 各恰好一份；
- 1 小时 chunk、6 小时压缩、7 天明细保留；
- 小时、日聚合在刷新后有数据，原始明细过期后聚合仍存在；
- 完整 backend、真实 PostgreSQL/TimescaleDB、发布脚本和前端 build 回归通过。

### 9.2 1 号机验收

- 根盘使用率低于 75%，数据盘可用空间至少 6GB；
- 防重复缓存紧凑后低于 500MB，并在持续采集下保持约 6 小时窗口；
- Schema=045，所有 retention/refresh job 正常且无重复；
- 容器 healthy、restart=0、运行错误=0；
- 认证 L2 realtime 为 HTTP 200、fresh=true、quality_good=true；
- 连续 10 分钟、每 30 秒取样，L0 与 L2 时间一致且延迟始终小于 30 秒；
- 不执行设备控制和自动策略。

## 10. 发布身份

源码、GitHub Actions run、ARM64 镜像摘要、Schema 045、备份路径/SHA、精确清理清单、迁移结果和
10 分钟验收结果写入新的 1 号机部署记录。只使用固定摘要，不部署 `latest`。
