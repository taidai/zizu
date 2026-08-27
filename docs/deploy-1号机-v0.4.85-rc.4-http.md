# 1 号机 v0.4.85-rc.4 / Schema 045 存储治理记录

**状态：** 已完成

**日期：** 2026-08-27

**范围：** HTTP 测试站；不启用 Caddy/TLS，不执行控制或自动策略

## 1. 固定发布身份

- 源码：`511f53734df407dd1e3e2ee9ebb31cd374ca7be0`
- GitHub Actions：`32963689849`（success）
- 平台：`0.4.85-rc.4`
- Schema：`045`
- linux/arm64：`ghcr.io/taidai/zizu@sha256:5ab0368078e03b1d7d87aae32ea570242bd6791359d39f05a8cbcb3dce9b7e23`
- 本地 `release_preflight.py verify`：通过，双架构和 Schema 045 均匹配。

## 2. 现场根因与清理前证据

- `/dev/root`：16GB，已用 14GB，可用 1.4GB，91%。
- `/userdata`：13GB，已用 12GB，可用 131MB，99%；inode 仅 5%，不是 inode 耗尽。
- Docker：images 3.448GB（reclaimable 1.048GB）、active volumes 约 3.73GB。
- ZiZu PostgreSQL：约 3041MB；`t_l0_observation_dedup` 约 2831MB、7,779,560 行，最老约
  40 小时；`t_l2_stream_outbox` 约 82MB、89,566 行、未发布 3 行。
- 现场仍是 Schema 044，只有旧 telemetry compression/retention jobs，**没有**
  `prune_l0_observation_dedup`。因此 6 小时防重缓存治理尚未部署，这是 `/userdata` 告急的直接根因。
- 当前 backend：v0.4.85-rc.3，固定摘要
  `sha256:7eb0b5650061ced14123be491a12b4dd97592e176adc4560205f6242e19b6e84`，healthy。

## 3. 固定保留集合

- `/opt/zizu-backups/rc7-20260825T080812Z/omnithings.dump`、`.sha256`、`runtime.env`；
- `/opt/zizu-backups/nanomq-before-0.25.5-20260825T033426Z/`；
- `/opt/zizu-backups/pre-v0.4.85-rc.4-schema044/`；
- 切换前 rc.3 image `b6ecd595c1eb` 和上一版 rc.2 image `1f3b2048a9a4`。

## 4. 精确清理清单

以下路径均已通过 `readlink -f` 解析并验证位于 `/home/omnithings/bak/`。目录 SHA 是对目录内按路径
稳定排序的逐文件 SHA 清单再次计算 SHA-256；大小为 `du -sb`。

```text
31578842  6f86d181e7db6cffc4c5602319cd08c9d6cd9f2bead92160ca362135e4055d0e  /home/omnithings/bak/20260802-130357
31578842  b2b02ee7dc52c0b056b815302ecdf120d2313b632599155eddd018b417b0bf6b  /home/omnithings/bak/20260802-130517
51163864  6b52a34bab8db3bf45038a5b8800fea39d6f3b372a8710be239a5b8199db8579  /home/omnithings/bak/20260802-141038
69999503  b948c91f3ffea18627fed27311478a6b13a93398023e3e85a7d6ef25fa98919a  /home/omnithings/bak/20260802-172031
78331150  35d3205fb7266e713149bbe47e6740672efdfcd47b974c578fdc81d457078807  /home/omnithings/bak/20260802-173024
86666868  739b45695afc81e3d966b8d8a7038b962913e6222ebbccdd518c725b9c3641ba  /home/omnithings/bak/20260802-174236
94781625  add09c471d5ca327dc67363a8d2144aa9dafa52458c652a1c3132f3a285307bd  /home/omnithings/bak/20260802-175959
102896600 4b63605df8ad602aa8455db57308a6011a23347e6549e0fc32e117afadc7f306  /home/omnithings/bak/20260802-194445
102896616 364f03bb77fdc758b337306ca4b13936a8c0ae9cd4fab6d074048f260ac8be97  /home/omnithings/bak/20260802-202802
102958868 cac0973a741f4dda57d259f11ea07046c254b70c2a91e5e744c7bfd71d5c2013  /home/omnithings/bak/20260802-221035
111365030 f342835d1eeeeb38486683f2f566f3c1e0e964fe35d1c425bb5fac4f0067ddd0  /home/omnithings/bak/20260802-224337
119713375 20fb6f7aed1d6b0fc00a5389de3dd3418332fde7dd465231c8b89c0ef33456f4  /home/omnithings/bak/20260802-230704
127856337 ebe24b5c56a3ba7b6a2cae033c15008c6f1882e13cb33529bbe9c284c8a662cc  /home/omnithings/bak/20260802-231109
135986219 e8285fb078a9612b1385c895932590509b16a2b4e5344e87b3f912a4cb458b1d  /home/omnithings/bak/20260802-231623
144115868 7abb15b93b4c50e71376c890c93241145ac82dc30812b02ef155798f0c9f39ae  /home/omnithings/bak/20260802-233858
152490623 4c075aabc5878da435c0f0247accae344823ebfd975cede655e303cb505f87f11  /home/omnithings/bak/20260803-074413
160653367 4b9e90be982c8b9148d534f867d04d01a8731c3c9ef84f8c61dabb1bf2b37de0  /home/omnithings/bak/20260803-082529
168790253 f575fcb6ff85268d73613bf2fe2d940853db799a6538775af53cd9fcdf0ee9bd  /home/omnithings/bak/20260803-090732
177201222 41635d46887a6459b1823b66ab8b9558a45c36576bc6309ffac7a7419262ba63  /home/omnithings/bak/20260803-114617
53976594  ec4193648ba4a8b41aec48a5307c6db6e674db332292db9f8d7ae06bdc35cea6  /home/omnithings/bak/backup-v0.4.12-20260805_192246.tar.gz
302878    991b3af62acff0c8828bcf6a97836ffd31135aed322b2fdfdcc251b506f165d7  /home/omnithings/bak/backup-v0.4.31-20260805_193559.tar.gz
```

明确删除的 unused image IDs（逐个 `docker image rm`，不使用 broad prune）：

```text
e7ffca230c60  v0.4.85-rc.1
e10c563887a1  v0.4.85-rc.1
ea9f83e0b0e1  v0.4.82
503200817eac  legacy omnithings latest-arm
```

新的 Schema 044 dump 完成 SHA 与 `pg_restore -l` 校验后，逐个删除以下旧 dump 及存在的 `.sha256`
旁文件：

```text
/opt/zizu-backups/zizu_iot-before-0.4.81-rc.1-20260822T044629Z.dump
/opt/zizu-backups/zizu_iot-before-0.4.82-rc.2-20260824T084922Z.dump
/opt/zizu-backups/zizu_iot-before-0.4.82-rc.3-20260824T112136Z.dump
/opt/zizu-backups/zizu_iot-before-0.4.82-rc5-20260825T022258Z.dump
/opt/zizu-backups/omnithings-before-0.4.82-rc6-20260825T0450Z.dump
/opt/zizu-backups/zizu-pre-0.4.85-rc.1-20260825.dump
```

`rc7` dump、NanoMQ 备份和本次新备份始终保留。以上旧目录、tar、dump 和 image 已从 1 号机删除，
被删文件本身不能在现场直接恢复；数据库仍可从保留且验证过的 dump 恢复，应用可回滚至保留的 rc.3/rc.2
镜像。

## 5. 备份与迁移

- 新建完整 custom-format 备份：
  `/opt/zizu-backups/pre-v0.4.85-rc.4-schema044/omnithings.dump`，633,910,143 bytes，
  SHA-256 `5a7214a366ac52c271963649afc0613e8c144d357fc6c649c6f0ecf63379fce1`。
- `sha256sum --check` 和 `pg_restore -l` 均通过；校验容器保持现场必须的 `network_mode: host` 与
  `/dev/mqueue` tmpfs。
- 后台停机后以 rc.4 ARM64 镜像单次执行 migration 045：`applied=['045']`、`errors=0`；随后正式
  backend 启动复核为 `applied=[]`、045 已跳过、`errors=0`。
- 本机 image ID：`sha256:d31ae25d3f7d1ba874a362db5133217617ebbbce3bc0f7ea1b119388b0f41557`；
  OCI version 为 `0.4.85-rc.4`、架构为 linux/arm64。

## 6. 旧运行数据清空

维护者明确要求清空旧数据。清理仅覆盖测量和运行事实，不删除节点、点位加工、全局实体、用户或告警
规则。清理事务临时解除三条 append-only 触发器与 L2 来源的两条复合外键，清空后在同一事务中按原定义
恢复；逐行删除因复合外键检查过慢而取消的尝试已整体回滚，没有留下中间状态。

切换 backend 前，下列表均精确为 0：

```text
t_telemetry
t_telemetry_latest
t_l0_observation_dedup
t_l2_observations
t_l2_observation_sources
t_l2_latest
t_l2_stream_outbox
t_runtime_health_samples
t_ingestion_failures
tel_agg_5min
tel_agg_1h
tel_agg_1d
```

清空后复核三条 append-only 触发器和两条 L2 复合外键均存在。配置基线保持不变：节点 6、点位 97、
已安装点位加工 1、全局实体实例 2、告警定义 1、用户 2。

## 7. 运行验收

- 最终空间：`/dev/root` 16GB，已用 9.7GB，可用 5.4GB，65%；`/userdata` 13GB，已用
  6.1GB，可用 6.1GB，50%。Docker active volumes 约 850.7MB。
- backend 保持原容器约束：`network=host`、`tmpfs=/dev/mqueue`、`restart=unless-stopped`、
  `zizu-release-test_zizu-data:/app/data`；health 为 healthy，restart count 为 0。
- 本机与公网 `GET /api/v1/health/live` 均返回 `alive / 0.4.85-rc.4`，公网首页 HTTP 200。
- F0 加载 97 条 tag rules，NanoMQ 连接成功；清空后新数据重新写入，首轮只读样本已经同时看到新的
  L0 telemetry/latest 与 L2 observations/latest。
- Schema 精确为 045。五项存储治理 job 均存在且最后执行成功：dedup 每 15 分钟清理、telemetry
  6 小时后压缩、7 天保留、1 小时与 1 天连续汇总刷新。
- 未启动 Caddy/TLS，未执行自动策略、控制命令或任何设备写入。本部署仍是 development HTTP 测试站，
  不宣称满足正式生产安全基线。
