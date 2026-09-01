# 1 号机 v0.6.4 HTTP 部署与验收记录

日期：2026-09-01

## 发布身份

- 最终版本：`0.6.4`
- 功能提交：`d5099ca7`；版本提交：`1a8d283c`
- GitHub Actions：`33470747033`，成功
- ARM64 固定镜像：
  `ghcr.io/taidai/zizu@sha256:24a71b6603d698ab028d0662107789897d0ff082892ee11d29c0ad84cb141aee`
- Schema：`058`
- 公网入口：`http://e606.hlszh.com:9000/`

## 根因与修复

- “原始点位没有删除”不是权限或按钮渲染故障，而是旧规格主动取消了物理删除，只允许停用；后端遗留的
  单点删除又绕过配置修订、数据帧栅栏和运行重载，不能直接暴露给用户。
- v0.6.4 在原始点位维护区增加批量“删除”。操作必须先选择点位并二次确认，明确提示点位、实时值和
  历史值不可恢复。删除成功后刷新点位总数和节点树计数。
- 后端把批量和旧单点调用统一到一个删除语义：先排空配置栅栏，再在一个事务内删除
  `t_telemetry_latest`、`t_telemetry`、`t_l0_observation_dedup` 和 `t_tags`，发布
  `raw_point.delete` 配置修订，提交后才重载运行配置。
- 点位若被 L1 加工计划/安装、L2 控制绑定或旧实体绑定引用，删除返回 `RAW_POINT_IN_USE`，提示改用
  停用；带旧告警配置的点位也拒绝删除。这样不会因清理错误导入点位而拆断 L0→L1→L2 主干。

## 验证证据

- TDD 先得到两个预期失败：界面选择模型没有删除能力；`DELETE /tags/maintenance` 被旧动态路由误识别
  为 UUID。实现后界面模型 5/5、维护 API 2/2、版本脚本 6/6、TypeScript 编译全部通过。
- 隔离真实 PostgreSQL 验证 2/2：未引用点位的身份、实时值、历史值和采集去重记录同事务归零；L1
  引用点位拒绝删除且配置修订不倒退。完整原始点位维护专项此前为 4/4。
- GitHub Actions 从最终提交完成 amd64/arm64 生产镜像构建、发布清单校验和参考交付包构建。
- 公网 Playwright 无头主干 6/6、0 失败、0 跳过，耗时 162.2 秒：覆盖节点 CRUD、Neuron 点位导入、
  L0 实时/历史、L1 加工、L2 实时/历史/质量/来源、模板生命周期、无动作规则绑定、永久删除及清理。
  现场 `raw_point.delete` 修订为 1 条，测试点位身份为 0。

## 切换与边界

- 只重建 Compose `backend`；TimescaleDB、NanoMQ、Neuron 均未重启。保持 `network_mode: host`、
  `/dev/mqueue` tmpfs，容器 `healthy`、restart 0，启动及验收日志无 ERROR、Traceback 或迁移失败。
- Schema 未变化，因此未重复制作数据库全量备份；运行配置备份为
  `/opt/zizu-release-test-0.5.0/release.env.pre-v0.6.4-1a8d283-20260901-125623`。
- 未启动 Caddy/TLS，未执行控制、设备写或自动策略。公网仍是 HTTP 测试/维护环境，不能宣称满足正式
  公网生产安全基线。
