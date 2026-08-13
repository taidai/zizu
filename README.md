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

> 首次启动会自动执行 `init-db/*.sql` 初始化数据库。

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
| 临时告警创建/人工恢复（待移除） | ✓ | ✓ | — |

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
| POST | `/api/v1/entities/{id}/write` | 向实体写入控制值 |
| GET | `/api/v1/health/live` | 最小匿名存活契约，仅返回 `status` 与版本 |
| POST | `/api/v1/solution-packages/import` | multipart 上传并完整校验解决方案 ZIP |
| GET | `/api/v1/solution-packages` | 查询已验证的不可变解决方案包 |
| POST | `/api/v1/solution-packages/{package_record_id}/install-plans` | 为已验证包生成可审查安装计划 |
| GET | `/api/v1/install-plans/{id}` | 查询已保存的不可变安装计划 |
| POST | `/api/v1/install-plans/{id}/apply` | 按计划摘要幂等安装解决方案包 |
| GET | `/api/v1/solution-installations` | 查询解决方案安装记录 |
| POST | `/api/v1/solution-installations/{id}/acceptance-runs` | 运行包携带的白名单验收项 |
| GET | `/api/v1/delivery-reports/{id}` | 查询不可变机器交付报告 |

控制与管理能力矩阵：`system.manage` 仅 admin（系统、SQL、NanoMQ）；
`gateway.manage` 允许 admin/engineer（Neuron 接入管理）；`control.write` 允许三角色，
但后续仍须经统一控制命令模块补齐限值、联锁、幂等和回读状态机。所有这些端点在
业务执行前写不可变 requested 审计，成功后再写 success；审计不可用时 fail closed。

解决方案导入使用 `multipart/form-data` 的 `archive` 文件字段。安装执行请求体为
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
  tests.test_business_rest_authorization -v
python test_f0_pure.py

# 身份离线供应工具的标准库测试（从仓库根目录运行）
cd ..
python -m unittest scripts.test_bootstrap_admin -v

# 其余历史测试使用 pytest；如本机已安装 pytest：python -m pytest tests -q
# 当前基线有 2 个已知聚合器失败（SUM 去重、LAST 时间排序）。

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
