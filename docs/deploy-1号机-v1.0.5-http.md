# v1.0.5：1 号机 HTTP 发布记录

状态：**已部署且健康，无头只读验收 PASSED；可见 Browser 工具抽查未完成**。

## 发布来源

- 固定源码与标签：`v1.0.5` → `29a43077666b953ffc5363fe9a70e0df9b7f895b`。
- 构建：[Actions 34211320712](https://github.com/taidai/zizu/actions/runs/34211320712) 成功，耗时 4 分 12 秒。`release_preflight verify` 已确认版本 1.0.5、Schema 063 和两种架构。
- ARM64 固定制品：`ghcr.io/taidai/zizu@sha256:255f3d61ac611a01c4a407f03c6da0aa2e8f77e765497975b2b5c615dbf3cc84`。
- 平台 manifest：`sha256:d7e1410613d4e1b677a180ef998621e65eec3b8f86cd4bb87de2e5c5a6b2aa92`；image ID：`sha256:a126ca83f14d62243651bf239aeda132cb4bfe98e231e580477ac7fac708848e`。已校验全部 13 层、配置摘要、ARM64 与版本标签。
- 离线归档 333,496,320 字节，SHA256 `c303eeba1fbb1efa2833f7a6ad457b51c18878b16e85af18e510b40b2fee3d6e`。此摘要链用于证明离线 image ID 对应 CI 制品，不把临时导入标签当作发布身份。
- 本机验收：[121 项浏览器检查及 18 张双尺寸截图](reviews/2026-09-08-v1.0.5-local-acceptance.md)。报告明确保留组合式 harness 证据及间歇焦点问题，不将历史运行冒充当前运行。

## 现场边界

- 仅替换 ZiZu backend；保持 host 网络与 `/dev/mqueue` tmpfs。不重启 TimescaleDB、NanoMQ 或 Neuron。
- 保持 HTTP，不启动 Caddy、不申请 TLS。制品清单内的 edge image 不是现场启用授权。
- 切换前全库备份至本机并隔离恢复验证；只应用 Schema 063，不重跑全部迁移、不改现场角色或密码。
- 保留旧镜像与配置恢复锚点。跨 Schema 回退需配合已验证的旧数据库备份，不能仅切旧镜像。
- 部署后沿节点 → L0 → L1 → L2 → 告警只读验收；不发布加工、不启用策略、不发送通知、不下发设备控制。

## 现场执行证据

- 2026-09-08 18:00（北京时间）执行一次 backend 替换，状态 `DEPLOYED_HEALTHY_READONLY_ACCEPTANCE_PENDING`，应用版本 1.0.5、healthy、restart 0。
- Schema 062 → 063，配置修订始终 777；启用策略、输出所有权、在途意图和命令均为 0。
- TimescaleDB、NanoMQ 容器 ID、启动时间、restart 0 前后一致；Neuron PID/启动时间一致。host 网络、`/dev/mqueue`、挂载和运行配置保持不变。
- 现场配置恢复锚点：`/opt/zizu-release-test-0.5.0/backups/v1.0.5-pre-20260908T100014Z`。旧镜像保留。
- 本机完整 062 归档 596,609,003 字节，SHA256 `13be3cc5ab71a2ddc0949bc0cceae4aa90afcbe97d979662a44a6382b86a41ff`，未删改现场业务数据。
- 全库采用分阶段隔离恢复验证：源版本 TimescaleDB 2.28.3；原始恢复曾因本机缺角色和恢复模式下外键验证失败，失败证据保留。同一数据退出恢复模式后原始外键通过，剩余 46 个 TOC 条目恢复 exit 0；195 项归档 FK 均存在，保留源 16 个既有 NOT VALID，不擅自修复旧数据。063 新增 3 项 FK 验证通过，迁移重放通过。不将其描述为单次 pg_restore exit 0。
- 无头 Chromium 现场 11 项只读检查通过：登录/版本、双尺寸首页、节点/L0刷新链路、L1打开取消、L0历史、L2历史来源、告警、设备监控、手动控制目录、调度目录、系统工具及 HTTP/故障映射嵌套表单开关。业务写入 0、pageerror 0、API错误 0，采集计数增长；初始未登录会话探测 401 为预期。
- 已实际查看现场截图：1024×768 首页无整页横向溢出；功率槽位未绑定时明确显示未配置，SOC 显示最后值（非当前），所选 L2 显示超时/INPUT_STALE，而非虚假 GOOD。只读访问通过不等于现场实时数据、控制和完整 EMS 交付通过。
- 可见 Browser 插件两次选择现场标签页超时；遵从维护者不修测试工具的要求，没有排查工具或伪称完成可见抽查。现场证据使用真实无头浏览器截图，不使用 Demo 数据。完整里程碑的可见抽查仍为 INCOMPLETE。
- 当前未记录要求 HTTPS/edge 的标准 release-lock；本次为既有 HTTP backend-only 部署，以固定制品、镜像和本记录关联，未绕称满足 TLS 发布模式。
