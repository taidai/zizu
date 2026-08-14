# ZiZu

> 自足物联网平台 · 开源工业 IoT 低代码平台
>
> **简单配置即可交付工业控制系统** — 替代 ThingsBoard 的轻量级方案。
>
> 当前版本：**v0.4.77**

**中文** | [English](README_EN.md) | [官网 www.holoems.com](https://www.holoems.com)

---

## 这是什么

ZiZu 是一套面向**光储充 EMS、工业能耗监测、设备远程控制**场景的物联网平台。核心理念是「**配置即平台**」——所有业务逻辑通过界面配置产生，不写死代码。

它把工业 IoT 的四件套（设备接入 / 数据管道 / 点位计算 / 控制规则）整合成一条可插拔的管道：

```
设备接入 ──MQTT──► 消息总线 ──► [F0 数据管道] ──► [时序数据库]
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
                  [F1 Hook] [F3 Hook] [F2 Hook]
                  点位计算   节点聚合   控制回写
```

> **管道是骨架，功能域是挂在管壁上的器官。**

---

## 功能域分层

ZiZu 的能力被切成五个功能域，**渐进交付**：

| 域 | 一句话 | 核心能力 | 用户价值 |
|----|--------|---------|---------|
| **F0** | 数据管道流计算 | MQTT→解析→归一化→Hook 链→TSDB | **设备上线就能看到原始数据** |
| **F1** | 自定义物理/虚拟点位 | PhysicalTag 采集映射 + LogicalTag(SymPy 公式求值) | **灵活定义任意衍生指标** |
| **F3** | 节点树挂载点位 + 聚合 | 5 层统一树 + 每层 SUM/AVG/MAX 汇总 | **每层节点都是一等公民，有独立实时值** |
| **F2** | 控制策略 (GoRules) | GoRules ZEN 引擎 + JDM 可视化编辑 + RPC 回写 + 审计日志 | **安全可控地反向控制设备** |
| **F4** | 全局实体 | 业务语义实体（R/W/RW）绑定物理/虚拟点位，全局用于实时/历史/规则/控制 | **多品牌设备工况，配置即可适配** |

### 五层节点树

```
Site (场站)
 └── Station (电站)
      └── EnergyNode (能源节点)
           ├── ESS  储能系统  (PCS / BMS / 电表)
           ├── PV   光伏系统  (逆变器 / 光伏电表)
           ├── GRID 电网接入  (并网电表)
           └── EVSE 充电桩    (充电桩 / 充电电表)
                └── Tag (点位)
                     ├── PhysicalTag  ← Neuron 采集
                     └── LogicalTag   ← 公式计算
```

一棵 JSON 描述完整层级关系，不拆成多个概念（TB 的 Asset / Device / Profile 三件套合并为一个 Node Tree）。

---

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| **设备接入** | [Neuron](https://github.com/emqx/neuron) | 工业协议网关（Modbus / OPC-UA / IEC104 …） |
| **消息总线** | [nanoMQ](https://github.com/nanomq/nanomq) | 轻量 MQTT 5.0 Broker |
| **后端** | [FastAPI](https://fastapi.tiangolo.com/) + Python 3.12 | 数据管道 + Hook 链 + REST API |
| **时序存储** | [TimescaleDB](https://www.timescale.com/) | PostgreSQL + Hypertable + 连续聚合 (CAGG) |
| **规则引擎** | [GoRules ZEN](https://github.com/gorules/zen) | JDM 决策表 / 决策图（F2 控制域） |
| **前端** | React + Vite + TypeScript + Tailwind | 拟物化 (Neumorphism) 风格 |
| **单位换算** | [pint](https://github.com/hgrecco/pint) | 归一化器（原始值 → 工程值） |
| **公式求值** | [SymPy](https://github.com/sympy/sympy) | 虚拟点位符号计算 |

---

## 当前进展

| 模块 | 状态 | 说明 |
|------|------|------|
| **F0 数据管道** | ✅ | MQTT→Parser→Normalizer→TSDB 全链路，~10 msg/s 持续入库 |
| **F0 可视化** | ✅ | 点位列表 + 行内编辑 offset/scale + 实时值双列 + WebSocket 推送 |
| **Neuron 点位同步** | ✅ | `sync_neuron_tags.py` 一键发现节点/分组/标签，自动入库 |
| **F0 快照黑板** | ✅ | 节点级全量 JSONB 快照，时间戳对齐 |
| **F1 历史数据** | ✅ | `t_telemetry` hypertable + `t_telemetry_latest` 最新值缓存 |
| **F1 虚拟点位** | ✅ | SymPy 公式引擎，逻辑点位实时求值入库 |
| **F3 节点聚合** | ✅ | 5 层树 + SUM/AVG/MAX/MIN/COUNT/LAST 汇总（10s 周期） |
| **F2 控制规则** | ✅ | GoRules ZEN + JDM Editor + 告警 + RPC 下行 |
| **F2 告警中心** | ✅ | 分级告警（error1/2/3），支持 0/1 与字符串故障信息 |
| **F4 全局实体** | ✅ | 业务语义实体绑定物理/虚拟点位，实时/历史/规则/控制全局可用 |

> 验收：`backend/acceptance_f0_f3.py` 14 passed / 0 failed；已部署 e606 线上环境。

### 实现效果图

![ZiZu 首页](docs/images/zizu-home.png)

---

## 快速开始

### 前置条件

- Docker + Docker Compose
- Python 3.12+（本地开发）
- Node.js 18+（前端构建）

### 1. 克隆 & 配置

```bash
git clone https://github.com/taidai/zizu.git
cd zizu
python scripts/bootstrap_runtime_secrets.py  # 隐式输入已轮换的 Neuron 密码；生成其余 Secret
```

已有部署若仍使用公开默认值，引导脚本会拒绝静默覆盖。先在数据库、Neuron
或会话系统侧协调轮换并更新 `.env`；NanoMQ 安排 broker 与 backend 同步重启
的维护窗口后执行 `python scripts/bootstrap_runtime_secrets.py --rotate`。
Neuron 已轮换但 `.env` 尚未更新时使用 `--update-neuron` 隐式输入新值。

`DB_OWNER_*` 只属于受控的数据库迁移作业；后端只使用 `DB_USER`/`DB_PASSWORD` 的
非 owner 应用账号。首次 Compose 初始化会自动创建该账号并给予旧 `t_alarms` 的只读权限。
升级已有数据库时，先停止 backend，填入原 schema owner 的 `DB_OWNER_*`、为 `DB_USER`
设置一个新的非 owner 名称和密码，再在受控终端运行：

```bash
# 使用项目 backend 虚拟环境（其中包含 PostgreSQL 驱动）运行；不要在 web 容器中运行。
# 在 Compose 宿主机上，timescaledb 只是容器内 DNS，因此显式给 owner job 本机地址。
DB_OWNER_HOST=127.0.0.1 backend/.venv/Scripts/python.exe scripts/provision_database_roles.py  # Windows PowerShell 请改用 $env:DB_OWNER_HOST='127.0.0.1'
# Linux/macOS: DB_OWNER_HOST=127.0.0.1 backend/.venv/bin/python scripts/provision_database_roles.py
```

该作业以 owner 身份执行未应用迁移、撤销应用账号对旧告警历史的写权限并验证它不是
`t_alarms` owner。生产 backend 若仍以 owner 身份连接、缺少旧表只读权限或发现未应用迁移，
会直接拒绝启动；不要把 `DB_OWNER_PASSWORD` 注入 web 容器。

生产模式是默认值，缺失、空白或公开示例 Secret 都会阻止后端启动。只有完全
隔离的本机开发环境才可显式同时设置 `DEPLOYMENT_MODE=development` 和
`ALLOW_INSECURE_DEV_SECRETS=true`；进程启动时会在标准错误输出显示
`INSECURE DEVELOPMENT MODE` 警示。此模式不得用于任何可被其他主机访问的部署。
开发环境如还要跳过登录，必须另行显式设置
`ALLOW_INSECURE_ANONYMOUS_ACCESS=true`；生产模式设置该值会拒绝启动。匿名开发
响应带 `X-ZiZu-Security-Mode: insecure-development`，前端持续显示红色警示横幅。

独立验收/同步脚本也不再携带数据库或 Neuron 默认口令。运行
`backend/acceptance_f0_f3.py` 与 `backend/test_f0_e2e.py` 前必须显式设置
`ZIZU_API`（生产地址必须是 HTTPS）和 `ZIZU_DSN`。交互终端未设置
`ZIZU_API_TOKEN` 时，脚本会提示输入用户名并用无回显方式读取密码，再通过登录接口取得
短期会话；密码和 token 不进入命令行参数或 shell 历史。非交互式运行必须由 Secret
管理器注入活动 engineer/admin 会话的 `ZIZU_API_TOKEN`。运行
`backend/scripts/sync_neuron_tags.py` 前必须设置 `ZIZU_DSN` 与 `NEURON_PASSWORD`。

### 2. 一键启动（推荐）

```bash
docker compose up -d --build
```

数据库迁移完成后，首次部署必须创建唯一的首个平台管理员。推荐使用容器内的
交互式无回显输入；密码至少 14 个字符，不接受会出现在 shell 历史或进程列表中的
`--password` 参数：

```bash
docker compose exec backend \
  python -m scripts.bootstrap_admin --username admin
```

非交互式部署只能从标准输入传入。例如下面的 shell 变量不要 `export`，使用后立即
清除；正式自动化应由 Secret 管理器向标准输入提供值：

```bash
read -r -s -p "New ZiZu administrator password: " ZIZU_ADMIN_PASSWORD; printf '\n'
printf '%s\n' "$ZIZU_ADMIN_PASSWORD" | docker compose exec -T backend \
  python -m scripts.bootstrap_admin --username admin --password-stdin
unset ZIZU_ADMIN_PASSWORD
```

引导在数据库事务中串行执行并写入审计。已有同名活动管理员时幂等返回；已有其他
活动管理员时会拒绝。Ticket #3 的在线用户管理界面交付前，可用同一离线工具显式创建
engineer/operator 或迁移旧 viewer（密码仍只走交互终端，不进入命令行参数）：

```bash
docker compose exec backend \
  python -m scripts.bootstrap_admin \
  --provision-user --username site-engineer --role engineer
```

离线供应只有在库中已存在活动管理员时才会放行；创建、密码重置或角色迁移都会增加
`auth_version`、使旧会话失效并留下追加式审计。它不允许把活动管理员降权。

如需离线重置活动管理员密码，必须显式使用供应模式和 `admin` 角色；普通重复引导仍
保持幂等，不会改密码：

```bash
docker compose exec backend \
  python -m scripts.bootstrap_admin \
  --provision-user --username admin --role admin
```

访问（生产环境请使用已配置 TLS 的 HTTPS 域名）：
- `https://localhost:9000` — 前端页面（点位管理 + 实时趋势 + 规则引擎/告警中心）
- 规则引擎支持 GoRules JDM Editor 编辑决策图/决策表
- `https://localhost:9000/api/docs` — Swagger API 文档（仅 development 模式提供）

> 首次启动由 PostgreSQL schema owner 执行 `init-db/*.sql` 初始化数据库；生产 web
> 进程只验证迁移版本，不拥有 DDL 或旧告警历史写权限。

### 3. 认证与 HTTPS

生产环境保持 `AUTH_REQUIRE_HTTPS=true`。登录接口为 `POST /api/v1/auth/login`；
通过明文 HTTP 登录或携带 Bearer 调用受保护接口都会返回 `HTTPS_REQUIRED`，不会校验或
保存提交的凭据。会话默认
有效 480 分钟，可用 `AUTH_SESSION_MINUTES` 在 5-1440 分钟内调整；客户端通过
`Authorization: Bearer <opaque-session-token>` 调用受保护接口，并可用
`GET /api/v1/auth/me` 查询当前身份、`POST /api/v1/auth/logout` 主动注销。
浏览器实时订阅先以 Bearer 调用 `POST /api/v1/auth/ws-ticket` 获取 30 秒一次性票据，
再通过 WSS 首帧提交票据；长期会话令牌不会进入 WebSocket URL、代理日志或浏览器历史。

TLS 由反向代理终结时，backend 必须只允许该代理访问，并同时满足以下条件才可读取
`X-Forwarded-Proto`：

```env
AUTH_REQUIRE_HTTPS=true
AUTH_TRUST_PROXY_HEADERS=true
AUTH_TRUSTED_PROXY_CIDRS=["127.0.0.1/32","::1/128"]
```

`AUTH_TRUSTED_PROXY_CIDRS` 必须按实际代理源地址最小化配置且不能为空。来自列表之外
的直连客户端即使伪造 `X-Forwarded-Proto: https` 也不会被信任。默认
`AUTH_TRUST_PROXY_HEADERS=false`，不读取这些头。只有完全隔离、不可由其他主机
访问的开发环境，才可同时设置 `DEPLOYMENT_MODE=development` 和
`AUTH_REQUIRE_HTTPS=false`；生产模式不得关闭 HTTPS。

只有隔离在本机回环地址上的开发环境才可使用 HTTP。服务端必须同时显式设置
`DEPLOYMENT_MODE=development`、`ALLOW_INSECURE_DEV_SECRETS=true` 和
`AUTH_REQUIRE_HTTPS=false`；若还要匿名访问，另加
`ALLOW_INSECURE_ANONYMOUS_ACCESS=true`。运行独立验收脚本时还需显式确认客户端降级：

```env
ZIZU_API=http://127.0.0.1:9000/api/v1
ZIZU_ALLOW_INSECURE_LOCAL_HTTP=true
```

脚本拒绝向非回环 HTTP 地址发送密码或 Bearer token；生产环境不得设置该降级开关。

解决方案交付接口权限如下：

| 能力 | admin | engineer | operator |
|------|:-----:|:--------:|:--------:|
| 导入解决方案包 | ✓ | — | — |
| 查询解决方案包 | ✓ | — | — |
| 生成/查询/执行安装计划 | ✓ | ✓ | — |
| 查询站点配置版本 | ✓ | ✓ | — |
| 查询安装记录 | ✓ | ✓ | ✓ |
| 运行机器验收 | ✓ | ✓ | — |
| 查询交付报告 | ✓ | ✓ | ✓ |

非控制业务 REST 使用以下能力矩阵；角色判断只在后端执行，前端隐藏按钮不是安全边界：

| 能力 | admin | engineer | operator |
|------|:-----:|:--------:|:--------:|
| 运行状态、遥测与告警读取 | ✓ | ✓ | ✓ |
| 配置读取与导出 | ✓ | ✓ | — |
| 配置创建、修改、导入与绑定 | ✓ | ✓ | — |
| 告警确认 | ✓ | ✓ | ✓ |

operator 读取节点和点位运行视图时不会收到连接参数、来源路径、公式、缩放、阈值等
配置字段。告警确认主体固定来自服务端会话，客户端不能提交或伪造 `ack_user`。
配置写在进入业务处理前记录最小 `requested` 审计，成功返回后再记录 `success`；两者只含
服务端身份、稳定路由和请求 ID，不保存请求体、查询参数或 Bearer。现有存量配置接口
各自提交事务，因此 `success` 审计暂不能与全部业务写原子提交；后置审计失败会返回
`AUDIT_UNAVAILABLE`，此时客户端不得盲目重试，应先核对配置状态。解决方案安装主缝
已经使用同事务审计，存量配置接口将在统一 Unit of Work 后收口这一边界。
详细 `/api/v1/health` 与 `/api/v1/health/ready` 也需要登录；只有最小
`/api/v1/health/live` 存活探针匿名可用。

控制写、系统管理、Neuron/NanoMQ 管理和 WebSocket 已使用同一身份与能力边界收口。
这只证明应用接口已默认拒绝匿名访问；统一控制命令的限值、联锁、幂等与回读状态机，
以及 TLS、不可变 ARM64 制品和现场凭据轮换仍是生产发布门禁。在这些门禁完成前，不能
宣称整个公网 API 或 1 号机已生产就绪。

### 4. 本地开发（可选）

**后端**：
```bash
cd backend
pip install fastapi "uvicorn[standard]" psycopg2-binary paho-mqtt loguru pydantic pydantic-settings pint websockets python-multipart
uvicorn app.main:app --reload --port 9000
```

**前端**：
```bash
cd frontend
npm install
npm run dev    # Vite dev server @5173
```

### 5. e606 裁剪内核部署

Ticket #18 会把 e606 迁移到固定 digest 的 ARM64 制品。在该制品、backend-only 编排
和 HTTPS 入口完成前，旧的 e606 Compose/部署脚本只用于既有 v0.4.77 维护，不能用于
上线本节的认证版本，也不得在 e606 上现场构建或直接覆盖源码。

> e606 使用 `network_mode: host` + `tmpfs: /dev/mqueue`，端口直接占宿主机。

---

## 目录结构

```
zizu/
├── backend/
│   ├── app/
│   │   ├── api/            # REST + WebSocket 路由
│   │   ├── core/           # 配置 (pydantic-settings)
│   │   ├── db/             # 数据库连接池
│   │   ├── models/         # Pydantic 数据模型
│   │   ├── services/       # 核心管道
│   │   │   ├── mqtt_client.py     # MQTT 接入层
│   │   │   ├── parser.py          # Neuron 报文解析
│   │   │   ├── normalizer.py      # pint 单位归一化
│   │   │   ├── pipeline.py        # Hook 链 + 批量 flush
│   │   │   ├── telemetry_store.py # TSDB 写入
│   │   │   ├── entity_resolver.py # F4 实体解析/实时/历史/写入
│   │   │   └── rule_engine.py     # F2 GoRules 规则求值
│   │   └── main.py
│   ├── scripts/
│   │   └── sync_neuron_tags.py    # Neuron API → t_tags 自动同步
│   ├── tests/
│   └── pyproject.toml
│
├── frontend/
│   ├── src/
│   │   ├── App.tsx        # 主页面（点位表 + 实时值 + 状态栏）
│   │   ├── api/           # HTTP/WS 客户端
│   │   ├── components/    # charts / tree / ui
│   │   └── ...
│   └── Dockerfile         # Nginx 静态服务
│
├── init-db/
│   ├── 001-schema.sql     # 建表 + Hypertable + CAGG
│   ├── 002-test-data.sql  # 测试数据
│   ├── 003-real-device-mapping.sql # 真实设备映射
│   └── 004-node-snapshot.sql # 节点快照表
│
├── config/
│   └── nanomq.conf        # nanoMQ 配置
│
├── docs/
│   ├── architecture-v1.md      # 架构设计书
│   ├── ui-style-guide.md      # UI 风格规范
│   └── decisions/             # ADR 决策记录
│       ├── g11-feature-domains.md   # 功能域架构
│       ├── g7-goal-breakdown.md      # 目标拆解
│       └── ...
│
├── docker-compose.yml        # 三服务编排 (TimescaleDB + FastAPI + NanoMQ)
├── docker-compose.e606.yml   # e606 裁剪内核 override
└── .env.example             # 环境变量模板
```

---

## 核心 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/health` | 管道运行状态 (msg/s、入库率、最后消息) |
| GET | `/api/v1/nodes` | 节点列表（含 tag 数量） |
| GET | `/api/v1/tags?node_id=X&page=1` | 点位分页查询（含 offset/scale） |
| PUT | `/api/v1/tags/{tag_id}` | 修改 scale / offset / unit |
| PUT | `/api/v1/tags/batch` | 批量修改 scale / offset |
| GET | `/api/v1/telemetry` | 原始遥测数据查询 |
| GET | `/api/v1/snapshots` | 节点快照查询（数据黑板） |
| POST | `/api/v1/query` | admin：SELECT-only SQL 查询 |
| POST | `/api/v1/auth/ws-ticket` | 为实时订阅签发 30 秒一次性票据 |
| WS | `/api/v1/ws/telemetry` | WSS 首帧认证后实时原始值/工程值推送 |
| POST | `/api/v1/rules/{id}/simulate` | 模拟规则（返回 triggered/actions/engine） |
| GET | `/api/v1/entities` | 全局实体列表 |
| POST | `/api/v1/entities` | 创建全局实体 |
| GET | `/api/v1/entities/{id}` | 实体详情及绑定 |
| PUT | `/api/v1/entities/{id}` | 更新实体元数据 |
| DELETE | `/api/v1/entities/{id}` | 删除实体 |
| POST | `/api/v1/entities/{id}/bindings` | 实体绑定点位 |
| DELETE | `/api/v1/entities/{id}/bindings/{bid}` | 删除绑定 |
| GET | `/api/v1/entities/{id}/realtime` | 实体实时值 |
| GET | `/api/v1/entities/{id}/history` | 实体历史数据 |
| POST | `/api/v1/entities/{id}/write` | 旧全局实体写入兼容入口：仅在唯一映射到确认实体实例时创建控制命令，必须携带 `Idempotency-Key` |
| GET | `/api/v1/health/live` | 最小匿名存活契约，仅返回 `status` 与版本 |
| POST | `/api/v1/solution-packages/import` | multipart 上传并完整校验解决方案 ZIP |
| GET | `/api/v1/solution-packages` | 查询已验证的不可变解决方案包 |
| POST | `/api/v1/solution-packages/{package_record_id}/install-plans` | 为已验证包生成可审查安装计划 |
| GET | `/api/v1/install-plans/{id}` | 查询已保存的不可变安装计划 |
| POST | `/api/v1/install-plans/{id}/apply` | 按计划摘要幂等安装解决方案包 |
| GET | `/api/v1/solution-installations` | 查询解决方案安装记录 |
| GET | `/api/v1/site-configuration-versions/{version}` | 查询不可变站点配置版本 |
| GET | `/api/v1/entity-instances/{id}/realtime` | 从实体实例唯一确认主来源读取实时工程值 |
| GET | `/api/v1/entity-instances` | 查询规则/工作台可引用的稳定实体实例目录 |
| GET | `/api/v1/ems-workbench` | 读取当前安装包声明的固定 EMS 工作台（导航、分组、KPI、趋势及入口） |
| GET | `/api/v1/ems-workbench/trends/{trend_id}` | 读取工作台已声明趋势的确认实体实例历史，不接受任意点位查询 |
| POST | `/api/v1/ems-policies/{policy_id}/simulate` | 以包中固定场景仿真已安装的 EMS 策略，不下发设备写入 |
| POST | `/api/v1/ems-policies/{policy_id}/enable` | 在确认输入实体实例可读、新鲜且质量合格后，由工程师显式启用策略 |
| POST | `/api/v1/ems-policies/{policy_id}/evaluate` | 用当前确认实体实例观测评估已安装策略；触发时仅创建统一控制命令 |
| GET | `/api/v1/entity-instances/legacy-migration-preview` | 只读预览旧全局实体到实例的唯一、缺失和歧义分类 |
| GET | `/api/v1/entity-instances/{id}/source-failover` | 读取显式主备策略、当前角色与切换审计 |
| POST | `/api/v1/entity-instances/{id}/source-failover` | 携带预期当前角色、目标角色和原因执行人工切换 |
| GET | `/api/v1/alarm-events` | 查询 `model_version=v1` 的统一告警事件 |
| GET | `/api/v1/alarm-events/{id}` | 查询定义版本、实体实例、触发/确认/恢复证据 |
| GET | `/api/v1/alarm-events/{id}/transitions` | 查询事件的追加式状态转换时间线 |
| POST | `/api/v1/alarm-events/{id}/acknowledgements` | 确认活动未确认事件；不提供人工恢复命令 |
| POST | `/api/v1/entity-instances/{id}/control-confirmations` | 为高风险控制申请绑定主体和内容的 60 秒二次确认 |
| POST | `/api/v1/entity-instances/{id}/control-commands` | 以实体实例提交手动控制命令，必须携带 `Idempotency-Key` |
| GET | `/api/v1/control-commands/{id}` | 查询命令的持久状态与稳定机器码 |
| POST | `/api/v1/control-commands/{id}/reconcile` | 触发一次安全回读检查，不重发设备写入 |
| POST | `/api/v1/neuron/write` | 兼容 Neuron 写入：按已确认的实体实例映射创建控制命令，必须携带 `Idempotency-Key` |
| POST | `/api/v1/devices/{node_id}/rpc` | 兼容 RPC：新形态使用 `entity_instance_id` + `value`；受限旧形态使用定义 ID `command` + `payload.value`，均创建控制命令 |
| POST | `/api/v1/solution-installations/{id}/acceptance-runs` | 运行包携带的白名单验收项 |
| GET | `/api/v1/delivery-reports/{id}` | 查询不可变机器交付报告 |

控制与管理能力矩阵：`system.manage` 仅 admin（系统、SQL、NanoMQ）；
`gateway.manage` 允许 admin/engineer（Neuron 接入管理）；`control.write` 允许三角色。
新的实体实例控制命令统一执行服务端数据类型、限值、联锁、主体幂等、持久冷却和
回读确认；写入 Adapter 返回成功只会进入 `dispatched`，只有新鲜 GOOD 回读达到期望值
与容差后才成为 `readback_confirmed`。命令固定记录 `control.write` 权限动作、来源类型、
策略快照与每个状态转换关联的不可变审计事件；状态机只会前进，后台恢复只回读、不会重发
设备写入。所有这些端点在
业务执行前写不可变 requested 审计，成功后再写 success；审计不可用时 fail closed。

`/neuron/write` 与 `/devices/{node_id}/rpc` 处于有限兼容窗口：它们的 `201` 响应是控制命令
资源，而不是设备成功回执，统一包含命令 `id`、`status`、稳定 `code` 和
`migration.replacement`。调用方应随后查询命令或触发安全回读检查，只有
`readback_confirmed` 才表示现场已达到期望值。Neuron 地址必须唯一映射到已确认的可控实体
实例；RPC 的新形态必须同时提供属于该节点的 `entity_instance_id` 和 `value`。旧形态仅允许
`command` 精确等于已确认实体实例的定义 ID，并从 `payload.value` 取得目标值；`topic` 和
`qos` 不参与路由或执行。无映射、节点不匹配或任意 topic/payload 路由形式均返回稳定的迁移/
拒绝机器码，绝不执行直接 Neuron 或 MQTT 写入。

解决方案导入使用 `multipart/form-data` 的 `archive` 文件字段。创建安装计划的请求体为
`{"parameters":{"site.code":"EMS-01","pcs.count":2},"secret_references":{"neuron.credentials":"secret://site/neuron/credentials"}}`。
参数契约支持 string、integer、number、boolean、enum、address、port、duration、secret 和
`device_instances`；
可声明单位、必填、默认值、数值范围、枚举和正则模式。Secret 参数禁止提交明文值，只
接受 `secret://` 引用；缺失或非法输入产生稳定 blocker，阻止安装且不改变站点版本。
包导入结果和安装计划都会返回 `parameter_contracts`，实施端可据此生成配置表单；计划
只返回规范化后的非敏感参数和 Secret 引用，不返回提交过的 Secret 明文或非法原值。
计划与安装以规范化参数、Secret 引用和包摘要共同计算配置摘要；相同配置重复安装为
`preserve`，参数变化为 `update` 并生成新的追加式站点配置版本。计划的参数级 items
展示 before/after、unit、来源和 add/update/preserve/delete_candidate/block 动作；工程师
输入和包默认值分别标记为 `engineer_input`、`package_default`，安装版本记录来源与 UTC
修改时间。回到历史参数不是 preserve，而是创建新的版本，避免界面与当前运行配置不一致。

参数契约声明在 `solution.yaml` 顶层。每项都必须有稳定 `id`、`type`、布尔
`required` 和非空 `description`；非 Secret 项可声明与类型一致的 `default`。类型专属字段
仅允许：string/address 的 `pattern`，integer/number 的 `unit`、`minimum`、`maximum`，
port 的 `minimum`、`maximum`，enum 的非空唯一字符串 `values`。Secret 不允许默认值。
`device_instances` 使用 `minimumItems`、`maximumItems`（1..64），值是由
`instance_key`、`device_key`、可选 `standby_device_key` 与可选 `display_name` 组成的列表；
实例键和主来源键必须分别唯一。
以下片段可直接作为解决方案包的参数入口：

```yaml
parameters:
  - id: site.code
    type: string
    required: true
    pattern: '^[A-Z0-9-]{2,16}$'
    description: Stable site code
  - id: nominal.power
    type: number
    unit: kW
    required: true
    minimum: 0
    maximum: 10000
    description: Site nominal power
  - id: dispatch.mode
    type: enum
    required: false
    default: self_consumption
    values: [self_consumption, peak_shaving]
    description: Dispatch strategy
  - id: poll.interval
    type: duration
    required: false
    default: 5s
    description: Poll interval; use ms, s, m, or h
  - id: neuron.credentials
    type: secret
    required: true
    description: Runtime Neuron credential reference
  - id: pcs.instances
    type: device_instances
    required: true
    minimumItems: 1
    maximumItems: 16
    description: Site PCS instances and source catalog keys
```

设备实例与实体实例也由解决方案包声明，不另建一套现场 CRUD。槽位既兼容单设备参数，
也可通过一个 `device_instances` 站点参数生成多台同类设备；
安装计划根据稳定设备键与规范点位名列出候选，唯一兼容候选可预选，多候选必须由
engineer 在 `binding_selections` 中明确确认。来源目录或站点配置在批准后变化时，执行
返回过期错误并保持零写入；运行期只读已确认的唯一主来源，不按优先级或创建顺序猜测。
设备节点可通过 `source_catalog_key` 配置该稳定键；升级迁移仅在节点名称站点内唯一时
以名称初始化，重复名称必须由实施工程师显式设置不同键。
实体实时读取要求观测未超过槽位声明的新鲜度且 OPC 质量码为 GOOD(192)；缺失、陈旧
或坏质量分别返回 `ENTITY_DATA_MISSING`、`ENTITY_DATA_STALE`、
`ENTITY_DATA_QUALITY_BAD`，不会以带失败布尔值的 200 响应掩盖不可用状态。

实体实例告警也是包资产：它只引用稳定的槽位和实体定义，运行期由已确认实体观测驱动，
而不是引用 Neuron 节点、点位地址或 MQTT topic。`triggerDuration`、`recoveryDuration`
与 `notificationThrottle` 只能是正整数秒；恢复条件只能由连续的新鲜 GOOD 观测满足，
坏质量或陈旧观测会打断恢复计时，不能跨越数据空洞关闭事件。新告警事件 API 固定返回
`model_version: v1`；操作员只能确认 `active_unacknowledged`，确认后仍保持活动，直到现场
观测满足恢复条件。旧 `/alarms` 仍是兼容历史面，不能将其 `alarm_count` 当作新事件数量。
数据管道已停止调用旧实体告警引擎、标签告警引擎和 MQTT 告警写库器；实体、标签与 MQTT
只会把观测提交给同一状态机。标签必须已唯一确认到实体实例；MQTT 的外部 ID 也必须在该
确认来源中唯一，重复名称不会猜测路由。MQTT 告警 payload 顶层必须携带整数 `quality`，只有
OPC GOOD(192) 观测可触发或持续恢复；缺失/坏质量按不可恢复样本处理。无法映射的旧配置不再
产生新旧表写入，须通过解决方案包完成实体绑定后才可进入统一事件模型。规则告警同样只提交
观测：规则动作必须给出稳定 `id`、已安装告警资产 `alarm_definition`、目标
`entity_instance_id` 与计算值 `value`，不能携带等级、消息、物理地址或恢复指令。运行期按资产
选择当前或仍有活动事件的历史定义版本。旧 `/alarms` 仅保留历史只读查询，删除节点、标签、实体
或规则也不会改写其中的历史证据；`/alarms/counts`、
`/alarms/entities` 和 `/alarms/group-counts` 统计统一事件，创建、旧确认和人工恢复接口均已删除。

```yaml
# solution.yaml 的 assets/acceptance 增量
assets:
  - id: alarm.pcs.overpower
    kind: alarm_definition
    path: alarms/pcs-overpower.yaml
    sha256: "<sha256>"
  - id: acceptance.pcs-overpower-lifecycle
    kind: acceptance
    path: acceptance/pcs-overpower-lifecycle.yaml
    sha256: "<sha256>"
acceptance:
  - acceptance.pcs-overpower-lifecycle
```

```yaml
# alarms/pcs-overpower.yaml
schemaVersion: zizu.alarm-definition/v1alpha1
id: alarm.pcs.overpower
kind: alarm_definition
version: 1.0.0
slot: slot.pcs-primary
entityDefinition: pcs.activePower
trigger: {op: gt, value: 100}
triggerDuration: 10s
recovery: {op: lte, value: 90}
recoveryDuration: 5s
severity: MAJOR
notificationThrottle: 60s
```

```yaml
# 规则中的告警动作：规则只投递观测，定义决定等级、触发和恢复语义
_config:
  sourceEntityInstanceIds: ["<PCS-01.activePower-instance-uuid>"]
  inputMappings:
    grid_power: "<PCS-01.activePower-instance-uuid>"
  actions:
    - id: export-limit
      type: alarm
      alarm_definition: alarm.pcs.overpower
      entity_instance_id: "<PCS-01.activePower-instance-uuid>"
      value: "{{grid_power}}"
```

```yaml
# acceptance/pcs-overpower-lifecycle.yaml
schemaVersion: zizu.acceptance/v1alpha1
id: acceptance.pcs-overpower-lifecycle
kind: alarm_lifecycle
required: true
alarmDefinition: alarm.pcs.overpower
expectedState: recovered
timeout: 5s
```

`alarm_lifecycle` 验收要求本次安装的定义已完成触发、操作员确认和现场恢复，并进入声明
状态；报告保留事件 ID、状态和机器转换码，不回显物理来源地址或原始协议负载。

```yaml
# solution.yaml 的 assets 片段
assets:
  - id: pcs.activePower
    kind: entity_definition
    path: entities/pcs-active-power.yaml
    sha256: "<sha256>"
  - id: slot.pcs-primary
    kind: entity_instance_slot
    path: entities/pcs-primary.yaml
    sha256: "<sha256>"
  - id: acceptance.pcs-active-power
    kind: acceptance
    path: acceptance/pcs-active-power.yaml
    sha256: "<sha256>"
acceptance: [acceptance.pcs-active-power]
```

### 固定 EMS 运行工作台

解决方案包只能声明**数据配置**，不能携带任意前端代码、URL、样式或脚本。平台维护固定
页面组件；工作台引用的 `slot + definition` 在安装后解析为当前站点的确认实体实例。引用
不存在的槽位或实体定义会在导入阶段拒绝，安装后缺失/失活的引用会让
`GET /api/v1/ems-workbench` 返回 `WORKBENCH_REFERENCE_UNRESOLVED`，不会猜测其他来源。
趋势数据只能通过 `GET /api/v1/ems-workbench/trends/{trend_id}?range=1h|24h|7d|30d`
读取清单中已声明的实体实例；平台不会接受任意标签或物理来源作为图表输入。

```yaml
# solution.yaml 的 assets 增量
assets:
  - id: workbench.ems
    kind: ems_workbench
    path: workbench/ems.yaml
    sha256: "<sha256>"
```

```yaml
# workbench/ems.yaml
schemaVersion: zizu.ems-workbench/v1alpha1
id: workbench.ems
kind: ems_workbench
navigation:
  - {id: overview, label: 场站概览}
  - {id: trends, label: 运行趋势}
  - {id: alarms, label: 告警}
  - {id: controls, label: 控制}
groups:
  - id: pcs
    label: PCS
    entities:
      - {slot: slot.pcs, definition: pcs.activePower}
kpis:
  - id: pcs-power
    label: PCS 功率
    entity: {slot: slot.pcs, definition: pcs.activePower}
trends:
  - id: pcs-power-trend
    label: PCS 功率趋势
    defaultRange: 24h # 仅允许 1h、24h、7d、30d
    entities:
      - {slot: slot.pcs, definition: pcs.activePower}
alarms: {visible: true}
controls: {visible: true}
```

`overview`、`trends`、`alarms` 与 `controls` 是仅有的内置导航 ID。`controls` 只展示已确认且
可写的实体实例；真正下发仍必须经过统一控制命令权限、限值、联锁和回读，工作台配置不能扩权。

### 基础 EMS 策略与固定仿真

策略也是包内数据资产，不接受表达式、脚本、设备地址、MQTT 或 Neuron 配置。首版策略只支持
一个数值输入、一个阈值判断和一个固定数值动作；输入和目标都必须引用同一包中声明的
`slot + definition`，单位必须精确匹配，目标还必须声明可写控制策略。每个策略都携带一个
固定仿真；导入时仿真期望不成立即拒绝整个包。

```yaml
# solution.yaml 的 assets 增量
assets:
  - id: policy.grid-import-cap
    kind: ems_policy
    path: policies/grid-import-cap.yaml
    sha256: "<sha256>"
```

```yaml
# policies/grid-import-cap.yaml
schemaVersion: zizu.ems-policy/v1alpha1
id: policy.grid-import-cap
kind: ems_policy
revision: 1
input: {slot: slot.pcs-primary, definition: grid.activePower, unit: kW}
condition: {operator: gt, threshold: 100}
action:
  id: cap-import
  target: {slot: slot.pcs-primary, definition: pcs.setpoint}
  value: 50
  unit: kW
simulation:
  input: {value: 120, unit: kW}
  expected: {triggered: true, actionValue: 50}
```

工程师先调用 `simulate` 获得可重放的策略证据；`evaluate` 或平台定时调度使用新鲜、GOOD 的
确认实例观测。命中条件时只会创建 `source_type=policy` 的统一控制命令，仍受确认、限值、
联锁、冷却和回读约束；仿真永不下发设备写入。
安装计划先验证其输入/目标都能唯一落到确认实体实例；工程师还必须调用 `enable`，让平台在
当前输入可读、新鲜且质量合格时持久记录启用状态。只有已启用策略会被定时调度；运行期出现
缺失、陈旧或坏质量观测则明确拒绝本次评估，绝不会猜测数据或下发动作。升级后的新站点配置版本
需要重新启用，避免包变化静默接管自动控制。

需要把策略闭环纳入交付报告时，包可声明一个严格的 `policy_execution` 验收项。它先记录固定
仿真，再评估当前确认实例输入，并只在命令变为 `readback_confirmed` 后通过；报告保存输入、
仿真、命令及回读状态。该验收项会执行已声明的安全测试动作，因此只能指向经现场批准的测试
策略，不能替代运行期审批或高风险命令确认。

验收运行请求以 `policy_commands` 显式引用此前由工程师通过公开 `evaluate`、协议侧回读和公开
`reconcile` 完成的命令；报告不会自行下发策略动作：

```json
{"policy_commands": {"acceptance.policy-grid-import-cap": "<readback-confirmed-command-uuid>"}}
```

```yaml
# acceptance/policy-grid-import-cap.yaml
schemaVersion: zizu.acceptance/v1alpha1
id: acceptance.policy-grid-import-cap
kind: policy_execution
required: true
policy: policy.grid-import-cap
expectedAction: cap-import
timeout: 5s
```

```yaml
# entities/pcs-active-power.yaml
schemaVersion: zizu.entity-definition/v1alpha1
id: pcs.activePower
kind: entity_definition
displayName: Active power
deviceCategory: pcs
dataType: FLOAT
unit: kW
direction: R
```

```yaml
# entities/pcs-primary.yaml
schemaVersion: zizu.entity-instance-slot/v1alpha1
id: slot.pcs-primary
kind: entity_instance_slot
deviceCategory: pcs
count: 1
instanceKeyParameter: pcs.instance_key
displayName: Primary PCS
freshness: 30s
requiredEntities:
  - definition: pcs.activePower
    matcher:
      id: matcher.pcs-active-power
      deviceKeyParameter: pcs.device_key
      tagName: ActivePower
```

同一实体定义需要多台 PCS 实例时，槽位改为 `instancesParameter`，matcher 的设备键直接
来自列表中的 `device_key`，无需复制定义或槽位：

```yaml
# solution.yaml parameters
parameters:
  - id: pcs.instances
    type: device_instances
    required: true
    minimumItems: 1
    maximumItems: 16
    description: Site PCS instances and source catalog keys
---
# entities/pcs-fleet.yaml
schemaVersion: zizu.entity-instance-slot/v1alpha1
id: slot.pcs
kind: entity_instance_slot
deviceCategory: pcs
instancesParameter: pcs.instances
displayName: PCS
freshness: 30s
requiredEntities:
  - definition: pcs.activePower
    matcher:
      id: matcher.pcs-active-power
      tagName: ActivePower
```

创建计划时提交例如
`{"parameters":{"pcs.instances":[{"instance_key":"PCS-01","device_key":"edge-pcs-a","display_name":"东侧 PCS"},{"instance_key":"PCS-02","device_key":"edge-pcs-b","display_name":"西侧 PCS"}]}}`。
列表顺序和 `display_name` 变化不改变设备/实体实例 ID；`instance_key` 是站点稳定身份，
不能用展示名称代替。

```yaml
# acceptance/pcs-active-power.yaml
schemaVersion: zizu.acceptance/v1alpha1
id: acceptance.pcs-active-power
kind: entity_readiness
required: true
slot: slot.pcs-primary
definition: pcs.activePower
freshness: 30s
timeout: 5s
```

创建计划时可提交
`{"parameters":{"pcs.instance_key":"PCS-01","pcs.device_key":"PCS-01"},"binding_selections":{"slot.pcs-primary/PCS-01/pcs.activePower":"<tag UUID>"}}`。
候选与计划使用稳定机器码：`ENTITY_BINDING_MISSING`、`ENTITY_BINDING_AMBIGUOUS`、
`ENTITY_BINDING_TYPE_MISMATCH`、`ENTITY_BINDING_UNIT_MISMATCH`、
`ENTITY_BINDING_DIRECTION_MISMATCH` 和 `ENTITY_BINDING_PLAN_STALE`。验收同时检查确认
绑定、声明的新鲜度与质量码；陈旧或非 GOOD 数据不能得到 passed 报告。

规则输入的公开配置只接受 `_config.sourceEntityInstanceIds` 和将决策字段映射到实例 UUID
的 `_config.inputMappings`。新建/更新规则出现旧 `_config.sourceEntityIds` 时返回
`ENTITY_REFERENCE_LEGACY_FORBIDDEN`；已保存旧规则仅只读兼容并返回迁移提示。
`GET /api/v1/entity-instances/legacy-migration-preview` 根据已确认的物理来源将旧实体分类为
`unique`、`missing` 或 `ambiguous`，始终返回 `writes_applied: 0`，不自动猜测或改写规则。
实际规则引用同时持久化到带实体实例外键的 `t_rule_entity_instance_refs`。告警、控制和
EMS 工作台在各自状态机/命令/工作台票据中复用同一实例目录；Neuron 与 MQTT RPC 旧控制
入口已在 Ticket 09 转换为统一控制命令，不能把它们的创建响应当作现场成功。Ticket 10
已将规则控制动作收口为 `entity_instance_id + value`：规则只能创建统一控制命令，命令保存
`rule:<UUID>` 主体、规则版本、稳定动作标识和触发观测/输出证据；规则配置不得包含
Neuron 节点/组/点位、MQTT topic/payload/QoS、全局实体或本地冷却。相同触发重放返回原命令，
新的触发继续受持久命令冷却、联锁和回读确认约束。遗留
`/entities/{id}/write` 已在 Ticket 11 迁为有限兼容入口：它只接收旧实体 UUID、`value`
和 `Idempotency-Key`，仅在旧实体的已启用绑定能唯一对应一个已确认、活动、Neuron 来源的
实体实例时创建 `source_type=compatibility` 控制命令。多个、缺失或非 Neuron 候选均产生
持久化拒绝命令；此入口不再解析优先级、不再直接调用设备 Adapter，也不会把 `201` 解释为
设备写入成功。调用方必须使用响应中的 `links.command` 查询状态，只有
`readback_confirmed` 才表示现场已达到期望值。新集成必须使用
`POST /api/v1/entity-instances/{id}/control-commands`；兼容入口将在 v1.0 移除。
`/api/v1/nanomq/publish` 与前端“发布测试”已关闭，避免任意 MQTT topic/payload 形成未审计的
设备控制旁路；消息总线的状态、订阅、ACL、配置和重启仍由 `system.manage` 管理。

规则的控制动作是公开配置，必须只指向已确认的设备实体实例；每个 `id` 在同一规则版本内
必须稳定且唯一，控制规则至少声明一个实体实例输入（`sourceEntityInstanceIds` 或
`inputMappings`）。`value` 可以是决策输出模板或 JSON 标量。保存时会把每个动作写入实例引用目录，
避免规则绕过设备范围、控制策略或命令审计。

```yaml
_config:
  sourceEntityInstanceIds:
    - "<confirmed input entity-instance UUID>"
  inputMappings:
    bms_ready: "<confirmed input entity-instance UUID>"
  actions:
    - id: output:pcs_setpoint
      type: control
      entity_instance_id: "<confirmed entity-instance UUID>"
      value: "{{pcs_setpoint}}"
```

确需主备来源时，包在 matcher 上显式声明 `failoverPolicy: manual`，对应实例参数必须提供
与主来源不同的 `standby_device_key`。安装计划分别展示主、备候选并要求两者都唯一兼容；
安装后默认只读 primary，不会因缺失、陈旧或坏质量自动跳到 standby。engineer/admin 通过
`POST /api/v1/entity-instances/{id}/source-failover` 提交
`{"expected_current_role":"primary","target_role":"standby","reason":"..."}`；服务端以
乐观状态检查执行原子切换并追加不可变审计，重复旧状态请求返回
`ENTITY_FAILOVER_STATE_CHANGED`。升级若要删除策略或更换主备来源，必须先显式切回
primary；standby 状态下变更会返回 `ENTITY_FAILOVER_POLICY_CHANGE_REQUIRES_PRIMARY`，
避免包升级在没有切换审计的情况下静默改源。

可控实体在 `entity_definition` 中显式声明受限控制策略；没有 `control` 的 `W`/`RW`
定义不能被新命令接口写入。首版只支持同设备实例内的精确联锁和一个回读实体，避免将
任意表达式或物理地址带进站点配置：

```yaml
# entities/pcs-setpoint.yaml
schemaVersion: zizu.entity-definition/v1alpha1
id: pcs.setpoint
kind: entity_definition
displayName: PCS setpoint
deviceCategory: pcs
dataType: FLOAT
unit: kW
direction: RW
control:
  minimum: -100
  maximum: 100
  cooldown: 5s
  readback:
    definition: pcs.readback
    tolerance: 0.1
    timeout: 15s
  interlocks:
    - definition: bms.ready
      equals: true
  highRisk: false
```

数值实体必须同时声明 `minimum`、`maximum` 和非负 `tolerance`；`readback` 与目标类型、
单位必须一致。每个 `interlocks` 条目必须引用同一槽位的读实体定义，且其观测新鲜、
质量 GOOD 并精确等于 `equals`。`cooldown`、`timeout` 使用正整数秒；高风险值设为
`highRisk: true` 时，先调用 confirmations 接口，再用返回的 `confirmation_id` 提交相同
主体、目标和值的命令，确认 60 秒后或首次使用后失效。

命令状态为 `accepted`、`validated`、`dispatched`、`readback_confirmed`；终态为
`rejected`、`timeout`、`failed`、`mismatch`。同一主体与 `Idempotency-Key` 只能绑定一个
规范化命令内容；相同内容返回原命令，不同内容返回 `IDEMPOTENCY_KEY_REUSED`。在途命令
重启后继续观察回读，已到超时才安全进入 `timeout`，`reconcile` 不会再次下发写入。

安装执行请求体为
`{"plan_digest":"<64位小写SHA-256>"}`；安装和验收命令都必须携带
`Idempotency-Key` 请求头。相同调用主体、命令和请求摘要重用同一键会返回原结果，
同一键用于不同请求返回 `IDEMPOTENCY_KEY_REUSED`。归档/清单错误返回 HTTP 422，
计划过期、摘要不匹配或幂等冲突返回 HTTP 409；错误体稳定表示为：

```json
{"detail":{"code":"INSTALL_PLAN_STALE","message":"..."}}
```

包表示中的 `package_id` 是清单声明的稳定字符串标识；计划与安装表示中的
`package_record_id` 是本实例保存该包后生成的 UUID，二者不会复用同一字段名。

票据 01 的最小包格式、归档限额、机器码和报告字段以
[`docs/adr/0006-minimal-solution-delivery-tracer.md`](docs/adr/0006-minimal-solution-delivery-tracer.md)
为准。生产验收探针默认请求本实例 `APP_PORT`；反向代理或端口映射部署需设置
`PUBLIC_API_BASE_URL` 为 backend 可访问的公开 API 基址。

---

## 数据模型（简化）

```sql
-- 节点（五层树）
t_nodes(id, parent_id, node_type, name, ...)

-- 点位（物理 + 逻辑统一表）
t_tags(
  id, node_id, name, data_type,
  scale_factor, value_offset,     -- 工程值换算: eng = (raw + offset) × scale
  unit_from, unit_to,
  source, is_virtual, ...
)

-- 遥测（TimescaleDB Hypertable）
t_telemetry(ts, node_id, tag_id, value_int, value_float, value_bool, value_str)

-- 新实体实例交付路径（旧全局实体表在兼容期保留）
t_device_instances(id, identity_installation_id, slot_id, instance_key, ...)
t_entity_instances(id, device_instance_id, definition_id, data_type, unit, direction, ...)
t_entity_instance_bindings(id, entity_instance_id, tag_id, confirmation_audit_id, active)
t_entity_binding_confirmations(id, entity_instance_id, binding_id, actor, plan_digest, ...)

-- 节点快照（数据黑板）
t_node_snapshot(ts, node_id, data JSONB, raw_data JSONB, raw_message JSONB)

-- 全局实体（业务语义层）
t_entities(id, name, entity_type, data_type, unit, category, enabled)

-- 实体 ↔ 点位绑定
t_entity_bindings(entity_id, tag_id, node_id, binding_type, brand, priority, enabled)

-- 实体最新值缓存
t_entity_telemetry_latest(entity_id, binding_id, tag_id, node_id, ts, value_*)

-- 连续聚合（多粒度查询）
cagg_telemetry_1min, cagg_telemetry_5min, cagg_telemetry_1h
```

**工程值换算公式**：

```
engineering_value = (raw_value + value_offset) × scale_factor
```

例：BMS 电流原始值 16500，`value_offset = -16000`，`scale_factor = 0.1`
→ `(16500 + (-16000)) × 0.1 = 50 A`

---

## Neuron 点位同步

新设备上线后，无需手工录入点位：

```bash
python backend/scripts/sync_neuron_tags.py \
  --neuron-url http://localhost:7000 \
  --dry-run    # 先预览，确认后去掉此参数正式同步
```

脚本会：登录 Neuron → 发现所有驱动节点 → 枚举分组 → 抓取标签 → upsert 进 `t_nodes` / `t_tags`。

---

## 部署须知（裁剪内核 / ARM64）

如果目标服务器内核被裁剪（常见于嵌入式 ARM64 设备），Docker 可能遇到：

- `CONFIG_POSIX_MQUEUE` 缺失 → 容器启动报 mqueue 错误
- `CONFIG_VETH` 缺失 → bridge 网络残废

**避坑铁律**（缺一不可，`docker-compose.yml` 已内置）：

```yaml
services:
  every-service:
    network_mode: host       # 避坑 #1: 绕开 bridge
    tmpfs:
      - /dev/mqueue          # 避坑 #2: 替代内核 mqueue
```

---

## 设计哲学

1. **配置即平台** — 业务逻辑由界面配置产生，不写死代码
2. **一棵节点树** — 五层统一模型，不拆 Asset/Device/Profile
3. **物理/逻辑统一寻址** — 前端不区分来源，统一 `node_path.tag_name`
4. **规则跟随节点** — 规则绑定在任意层级，自动继承给子节点
5. **最小闭环优先** — 先跑通「设备上线→数据显示→规则触发→控制下发」

详见 [`docs/decisions/`](docs/decisions/) 下的 ADR 决策记录。

---

## 链接

- **官网**: [www.holoems.com](https://www.holoems.com)
- **GitHub**: [github.com/taidai/zizu](https://github.com/taidai/zizu)
- **文档**: [docs/](docs/)

## 开发

```bash
# 安装项目依赖后，导出一组仅用于本机测试的非公开默认 Secret
cd backend
export DB_PASSWORD=database-secret-value
export NEURON_PASSWORD=neuron-secret-value
export NANOMQ_API_PASSWORD=nanomq-secret-value
export JWT_SECRET=jwt-secret-value-that-is-at-least-32-chars

# 本次安全与交付功能回归、F0 独立验收（不需要额外测试依赖）
python -m unittest \
  tests.test_secure_settings \
  tests.test_authenticated_delivery_public_api \
  tests.test_delivery_public_api \
  tests.test_entity_instance_registry \
  tests.test_entity_delivery_public_api \
  tests.test_business_rest_authorization -v
python test_f0_pure.py

# 身份离线供应工具的标准库测试（从仓库根目录运行）
cd ..
python -m unittest scripts.test_bootstrap_admin -v

# 其余历史测试使用 pytest；如本机已安装 pytest：python -m pytest tests -q
# 当前基线有 2 个已知聚合器失败（SUM 去重、LAST 时间排序）。
# 隔离 Postgres 主缝另需指向名称以 _test 结尾的专用数据库，并设置
# ZIZU_POSTGRES_TEST=1 后运行：python -m unittest tests.test_delivery_postgres_public_api -v

# 前端构建（此时位于仓库根目录）
cd frontend && npm run build
```

---

## 许可证

本项目采用 **[十善业协议 (Daśa-kuśala License) v1.0](LICENSE)** — 以佛教十善业（身三、口四、意三）为伦理基础的开源许可证。

允许自由使用、修改、分发，但**禁止将软件用于有害用途**：

| 十善业 | 对应禁止条款 |
|--------|------------|
| 不杀生 | 禁用于武器、致死性设备、自动化杀伤系统 |
| 不偷盗 | 禁用于窃取数据、侵犯知识产权、入侵系统 |
| 不邪淫 | 禁用于色情、性剥削、未经同意的偷拍监控 |
| 不妄言 | 禁用于虚假信息、深度伪造、诈骗 |
| 不绮语 | 禁用于垃圾信息、恶意刷量、虚假流量 |
| 不两舌 | 禁用于煽动对立、认知战、舆论操控 |
| 不恶口 | 禁用于网络暴力、人身攻击、仇恨言论 |
| 不贪 | 禁用于掠夺性定价、剥削劳动、欺骗性设计 |
| 不嗔 | 禁用于报复攻击、勒索软件、DDoS |
| 不痴 | 禁用于传播迷信、伪科学、信息操控 |

> 本许可证非 OSI 认证标准许可证，GitHub 会显示为 "Other"。法律效力等同于自定义合同条款。

---

## 致谢

本项目整合了以下优秀开源组件：

- [Neuron](https://github.com/emqx/neuron) · 工业协议网关
- [nanoMQ](https://github.com/nanomq/nanomq) · 轻量 MQTT Broker
- [FastAPI](https://github.com/fastapi/fastapi) · 现代 Python Web 框架
- [TimescaleDB](https://github.com/timescale/timescaledb) · 时序数据库
- [GoRules ZEN](https://github.com/gorules/zen) · 规则引擎
- [pint](https://github.com/hgrecco/pint) · 单位换算
- [SymPy](https://github.com/sympy/sympy) · 符号计算
