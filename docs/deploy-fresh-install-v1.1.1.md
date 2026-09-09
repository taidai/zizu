# v1.1.1 全新安装修正

本版保留 Schema 065 和 v1.1.0 页面，修复严格生产模式下的首次安装阻断：

- owner 初始化脚本不再授权 Schema 044 已删除的 `t_release_locks`；旧告警表仍只读。
- 数据主干启动检查修正多余括号。
- 通过 PostgreSQL 系统目录检查内部保留策略表是否存在，不向应用账号开放内部 schema 或保留令牌。

## 安装顺序

1. 确认目标机器、可用磁盘、端口、架构和已有服务；为新实例使用独立目录与数据库。
2. 按 `release.json` 固定摘要加载应用镜像；数据库和 MQTT 镜像亦须核对来源与架构。
3. 空库仅加载 `001-schema.sql` 和 `005-node-categories.sql`，不加载示例或现场映射。
4. 注入私有 owner/app 凭据，运行 `scripts/provision_database_roles.py`，确认迁移至 065。
5. 使用 `python -m scripts.bootstrap_admin --username <管理员> --password-stdin` 创建首个管理员。
6. 生产模式必须使用 HTTPS。可在隔离访问场景使用原生 Uvicorn TLS 与 SSH 转发；证书和私钥私有保存。自签名证书不等于公网可信证书，不禁用生产守卫。
7. 启动应用，核对存活接口、版本、数据库、MQTT，以及真实登录和空配置页面。未配置协议网关不等于设备链路已验收。

每个容器保留现场需要的 `network_mode: host` 和 `/dev/mqueue` tmpfs；日志设置轮转。不得顺带重启已有服务、迁移其他站点数据或启用设备控制。

## 专项回归

在明确授权的隔离空库上设置 `ZIZU_TEST_ROLE_PROVISIONING=1` 后运行
`python scripts/test_provision_database_roles.py`，验证 owner/app 权限初始化。
该测试会执行迁移与授权，不可对未授权的业务库运行。

对已初始化数据库，注入应用账号连接变量并设置 `ZIZU_TEST_PROVISIONED_GATE=1`，
在 backend 目录运行
`python -m unittest tests.test_data_trunk_startup_gate.ProvisionedDataTrunkGateTest`。
此项使用真实 PostgreSQL 执行启动检查，结束回滚并关闭连接，不修改 schema。

普通单元测试会显式跳过这两项；跳过不能当作数据库集成测试通过。
