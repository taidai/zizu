# ZiZu

> 自足物联网平台 · 开源工业 IoT 低代码平台
>
> **简单配置即可交付工业控制系统** — 替代 ThingsBoard 的轻量级方案。
>
> 当前版本：**v0.4.30**

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

独立验收/同步脚本也不再携带数据库或 Neuron 默认口令：运行
`backend/acceptance_f0_f3.py` 与 `backend/test_f0_e2e.py` 前必须设置
`ZIZU_DSN`；运行 `backend/scripts/sync_neuron_tags.py` 前必须设置
`ZIZU_DSN` 与 `NEURON_PASSWORD`。

### 2. 一键启动（推荐）

```bash
docker compose up -d --build
```

访问：
- `http://localhost:9000` — 前端页面（点位管理 + 实时趋势 + 规则引擎/告警中心）
- 规则引擎支持 GoRules JDM Editor 编辑决策图/决策表
- `http://localhost:9000/api/docs` — Swagger API 文档

> 首次启动会自动执行 `init-db/*.sql` 初始化数据库。

### 3. 本地开发（可选）

**后端**：
```bash
cd backend
pip install fastapi "uvicorn[standard]" psycopg2-binary paho-mqtt loguru pydantic pydantic-settings pint websockets
uvicorn app.main:app --reload --port 9000
```

**前端**：
```bash
cd frontend
npm install
npm run dev    # Vite dev server @5173
```

### 4. e606 裁剪内核部署

```bash
docker compose -f docker-compose.yml -f docker-compose.e606.yml up -d
```

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
| POST | `/api/v1/query` | SELECT-only SQL 查询 |
| WS | `/api/v1/ws/telemetry` | 实时原始值/工程值推送 |
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
# 后端测试
cd backend && python -m pytest test_f0_pure.py -v

# 前端构建
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
