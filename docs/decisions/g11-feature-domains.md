# g11: 功能域架构 (Feature Domain Architecture)

> 日期：2026-07-17
> 状态：**已确认（Phase 0 最终文档）**
> 决策者：郝交付（交付总监）+ 用户
>
> 控制写入的现行契约以 ADR-0007 为准：`/devices/{node_id}/rpc` 是创建控制命令的兼容入口，
> 不再执行任意 MQTT 发布或把下游受理当作设备成功。下文 F2 的历史 RPC 描述仅保留作阶段记录。

---

## 1. 核心理念

### 1.1 数据管道哲学

> "数采流，从采集到入库，所有模块都挂载在这条数据管道上。"

ZiZu 平台的本质是 **一条数据管道 + 可插拔的 Hook 链**。

```
Neuron ──MQTT──► nanoMQ ──► [F0 数据管道] ──► [TimescaleDB]
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
                  [F1 Hook] [F3 Hook] [F2 Hook]
                  点位计算   节点聚合   控制回写
```

**管道是骨架，功能域是挂在管壁上的器官。**

---

## 2. 功能域分层（已锁）

```
┌──────────────────────────────────────────────────┐
│  F2: 控制域                                      │
│  GoRules 策略 + RPC 回写 + 审计日志 + 设备联动     │  ← 写入通道
├──────────────────────────────────────────────────┤
│  F3: 节点树域                                     │
│  5 层统一节点模型 + 每层 LogicalTag 汇总值          │  ← 层级聚合
├──────────────────────────────────────────────────┤
│  F1: 点位域                                      │
│  PhysicalTag(采集) + LogicalTag(SymPy 公式计算)    │  ← 虚拟值生产
├──────────────────────────────────────────────────┤
│  F0: 数据管道 + 流计算骨架                         │  ← 基础设施
│  MQTT 入站 → 解析 → 归一化 → Hook 链 → 入库        │
│  CE 内置(方案B): CAGG + 事件驱动 + SQL 聚合        │
├══════════════════════════════════════════════════╤
│  TimescaleDB (持久层)                             │  ← 全域共享存储
│  t_nodes + t_tags + t_telemetry(Hypertable)       │
└───────────────────────────────────────────────────┘
```

### 2.1 各域精确定义

| 域 | 一句话 | 核心能力 | 用户价值 |
|----|--------|---------|---------|
| **F0** | 数据管道流计算 | MQTT→解析→归一化→Hook链→TSDB，CE 以透传模式内置 | **设备上线就能看到原始数据** |
| **F1** | 自定义物理/虚拟点位 | PhysicalTag 采集映射 + LogicalTag(SymPy 公式自动求值) + 级联传播 | **灵活定义任意衍生指标** |
| **F3** | 自定义节点树挂载点位+策略 | 5 层统一树 + 每层挂载点位 + 汇总聚合规则(SUM/AVG/MAX) | **每层都是一等公民，有独立实时值** |
| **F2** | 控制策略(GoRules) | RPC 回写通道 + JDM 策略校验 + 审计日志 + 设备联动 | **安全可控地反向控制设备** |
| **TSDB** | 持久存储 | Hypertable 时序写入 + CAGG 连续聚合 + 多粒度查询 | **所有域的共享数据底座** |

---

## 3. 依赖关系

### 3.1 依赖方向图

```
           运行时数据流方向              配置/开发时依赖
           (必须遵守)                   (可并行)

  F0 ─────► F1 ─────► F3 ─────► F2      F0 先完成
  (管道)    (点位计算)   (节点聚合)  (控制)   │
                                         ├── F1 可开始 (需 F0)
                                         ├── F3 可开始 (需 F0, 与 F1 ⊥)
                                         └── F2 可开始 (需 F0+F3)
```

### 3.2 双维度正交表

| 维度 | F0 vs F1 | F1 vs F3 | F3 vs F2 |
|------|----------|----------|----------|
| **运行时数据流** | F0 → F1 (管道输出是点位输入) | **F1 → F3** (虚拟值先算出，才能被上层聚合) | F3 → F2 (节点状态作为策略上下文) |
| **配置/开发时** | F0 必须先完成 | **⊥ 正交** (可并行开发，公式用名字字符串引用) | F3 必须先完成 (控制目标 = 节点) |

**关键认知**: F1 和 F3 在开发时完全独立——可以先建节点树再定义公式，也可以先写公式再挂到树上。两者通过 tag_name 字符串解耦，不需要 DB 外键绑定。

---

## 4. 三种管道模式

F0 作为数据骨架，根据激活的功能域组合，呈现三种渐进模式：

### Mode 1: 读存 (F0 only)

```
Neuron → nanoMQ → [M2 解析] → [M3 归一化] → [M4 TSDB入库] → [API 查询展示]

钩子链: M2 → M3 → M4
激活域: F0 + TSDB
用户看到: 原始物理点位的实时值和历史曲线
代码量: ~300 行
```
**交付标准**: 设备上线后，Dashboard 上能看到跳动的数字。

### Mode 2: 读写读存 (F0 + F2)

```
上行: Neuron → nanoMQ → [M2→M3→M4] → [M6 规则读取上下文] → TSDB
下行: 用户/API → [M7 RPC] → [M6 策略校验] → [审计日志] → Neuron REST API → Modbus → 设备

钩子链: M2 → M3 → M6(read) → M4   +   M7 → M6(write) → 审计
激活域: F0 + F2 + TSDB
用户能做: 看到数据 + 点击按钮控制设备 + 每次操作有记录
新增代码: ~400 行 (M6+M7)
```
**交付标准**: 按钮 Click → 设备实际响应 → 日志可查。

### Mode 3: 读算存 (F0 + F1 + F3)

```
Neuron → nanoMQ
    ↓
[M2 JSON解析]
    ↓
[M3 归一化: scale*value+offset, pint 单位转换]
    ↓
[M5 虚拟点位 ◀── F1 核心]
│   ├── SymPy 事件驱动公式求值 (仅当源值变化时触发)
│   ├── 级联传播 A→B→C (深度≤5, 循环检测)
│   └── 输出: logical_tag values 追加到消息中
    ↓
[M6 规则上下文注入 (GoRules read-only)]
    ↓
[M4 TSDB入库: physical + virtual 同表存储]
    ↓
[节点树聚合 ◀── F3 核心]
│   ├── 各层 LogicalTags = 下层子节点 SUM/AVG/MAX...
│   └── 输出: 完整的层级汇总值
    ↓
[WebSocket 推送] → 前端 Dashboard

[后台异步] CAGG 连续聚合 (SQL 级别, 零 Python 代码, 自动刷新)

钩子链: M2 → M3 → M5 → M6 → M4 → [F3聚合] → M9
激活域: F0 + F1 + F3 + TSDB
用户得到: 物理值 + 虚拟衍生值 + 每层节点的实时汇总 + 历史趋势
新增代码: ~450 行 (M5 + F3 聚合器 + WS)
```
**交付标准**: Dashboard 上同时显示 PCS 实时功率(F0)、系统效率%(F1)、场站总功率(F3)。

### 模式对比

| 维度 | Mode 1 | Mode 2 | Mode 3 |
|------|--------|--------|--------|
| **激活域** | F0 | F0+F2 | F0+F1+F3 |
| **数据方向** | 只读 | 双向 | 只读(增强) |
| **钩子数** | 3 个 | 6 个(+下行) | 8 个(+聚合) |
| **代码增量** | 基线 ~300 行 | +~400 行 | +~450 行 |
| **交付时间** | Day 1-3 | Day 4-7 | Day 5-10 |
| **用户价值** | 能看数据 | 能控设备 | 能看完整业务视图 |

---

## 5. CE 实现：方案B（已锁决策）

> 来自 g10 选型结论：**否决 StreamEngine(方案A ~450行)，选择 方案B (~120行)**

### 为什么不是独立的"引擎"

```
误区: CE = 一个独立的 StreamEngine 类，消费队列、窗口聚合、级联调度...

事实: ZiZu 场景的数据流特征:
  - 数据源固定 (几十台设备, 几千个 Tag)
  - 拓扑稳定 (设备不频繁上下线)
  - 公式数量有限 (几十~几百条)
  - 延迟要求宽松 (秒级够用, 不需要微秒级)

这些特征意味着:
  - 不需要分布式状态管理 (单机够用)
  - 不需要 Kafka changelog (内存 state 够用)
  - 不需要 SQL 编译器 (Python 直接写更灵活)
  - 窗口聚合属于数据库能力 (CAGG), 不属于应用层
```

### 方案B: 三条独立路径

```python
# CE 不是独立类! 它是 on_message() 协作函数集合
# 总计 ~120 行, 散布在 M2/M3/M4/M5 中

async def on_message(msg):
    """
    F0 管道的核心处理函数。
    三条 CE 路径在这里被调用, 但各自独立、互不耦合。
    """

    # ═══ Hook 1: 解析 (~30 行) ═══
    parsed = parse_neuron_json(msg)
    # 提取 node_name + timestamp + raw_values dict

    # ═══ Hook 2: 归一化 (~40 行) ═══
    normalized = normalize(parsed)
    # scale * value + offset
    # pint 单位转换 (W→kW, mV→V, Wh→kWh)
    # 字段名映射 (activePower → activePower_kW)
    # 纯函数! 无副作用, 最容易测试

    # ═══ Hook 3: 持久化 (~30 行) ═══
    await batch_insert_telemetry(normalized)
    # psycopg2.execute_values 批量写入 Hypertable
    # physical_tags 和 logical_tags 同表 (is_virtual 区分)

    # ════════════════════════════════════
    # ║   下面是 CE 的三条路径 (按需激活)   ║
    # ════════════════════════════════════

    # ── Path A: SymPy 公式计算 (F1 核心) ──
    # 事件驱动: 仅当变化的 tag 是某个公式的 source 时才触发
    await dispatch_logical_triggers(normalized)
    # → 调用 M5 VirtualPointEngine.symbols_to_values()
    # → sympify(formula_string).evalf(subs=variables)
    # → 结果追加到 normalized, 下一次 batch_insert 时一并入库
    # 代码量: ~25 行 (在 M5 内)

    # ── Path B: CAGG 窗口聚合 (TSDB 内置) ──
    # 零 Python 代码!
    # 在 TimescaleDB 里创建 Continuous Aggregate:
    #   CREATE MATERIALIZED VIEW agg_5min
    #   WITH (timescaledb.continuous) AS
    #   SELECT time_bucket('5 minutes', ts), node_path, tag_name,
    #          avg(value), min(value), max(value), count(*)
    #   FROM t_telemetry GROUP BY 1,2,3;
    # 自动刷新, 自动维护, 查询时直接读物化视图
    # 代码量: 0 行 Python (仅 DDL SQL, 在 init-db 脚本中)

    # ── Path C: 跨节点 SQL 聚合 (F3 汇总) ──
    # APScheduler 定时任务, 每 10s 执行一次:
    #   SELECT parent_id, tag_name, SUM(value), AVG(value)
    #   FROM t_telemetry WHERE ts > now() - interval '15 seconds'
    #   GROUP BY parent_id, tag_name;
    # 结果写入父节点的 LogicalTag, 或直接通过 WS 推送
    # 代码量: ~20 行 SQL + 10 行 scheduler 配置
```

### 方案A vs 方案B 对比

| 维度 | 方案A (StreamEngine, 已否决) | 方案B (CAGG+事件驱动, **已选**) |
|------|-------------------------------|----------------------------------|
| 架构形态 | 独立类 ~450 行 | 散布协作 ~120 行 |
| 新增依赖 | 无 | 无 (同样零依赖) |
| 窗口聚合 | pandas RollingWindow (内存) | **TimescaleDB CAGG (SQL)** |
| 公式计算 | SymPy (集成在 processor 链中) | **SymPy (独立事件驱动)** |
| 跨节点汇总 | WindowAggregator processor | **SQL SUM/GROUP BY** |
| 调试难度 | 中 (异步队列 + 批量消费) | **低 (同步调用链, 直观)** |
| 性能瓶颈 | asyncio.Queue 背压管理 | **无 (各路径独立, 无队列)** |
| 与 F0 关系 | 替代 F0 的 M2/M3 | **嵌入 F0 为 Hook** |
| 升级路径 | → eKuiper (整体替换) | **Path B 已是 CAGG, 无需升级** |

### CE 启动模式

```
Phase 0-1 (F0 开发期):
  CE = 透传模式
  Path A: dispatch_logical_triggers() → no-op (无公式注册, 直接 return)
  Path B: CAGG view 已创建但无数据
  Path C: scheduler job 已注册但不执行聚合 (无节点树)
  → 开销 ≈ 0

Phase 2 (F1 激活):
  Path A: 有公式注册 → SymPy 开始工作
  Path B/C: 仍透传

Phase 3 (F3 激活):
  Path A+B+C: 全部激活
  → 完整 CE 能力上线
```

---

## 6. 统一节点模型（F3 数据基础）

> 来自 g9 结论：**不需要独立的 ThingModel 表**

### 6.1 两张表搞定一切

```sql
-- 表 1: nodes — 所有的"东西"都是节点
CREATE TABLE t_nodes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,                -- "杭州XX项目" / "PCS_01" / "site_total_power"
    parent_id   UUID REFERENCES t_nodes(id),  -- 自引用 FK 构建树
    layer       SMALLINT NOT NULL,            -- 1=Site 2=Station 3=EnergyNode 4=Device 5=Tag
    node_type   TEXT NOT NULL,                -- "ESS" / "PV" / "GRID" / "PCS2000" / ...
    config      JSONB DEFAULT '{}',           -- 扩展配置(品牌/型号/IP/端口...)
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- 表 2: tags — 挂在任何层节点上的点位
CREATE TABLE t_tags (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id       UUID NOT NULL REFERENCES t_nodes(id),
                                            -- FK 指向 ANY 层的节点!
    tag_type      TEXT NOT NULL,             -- 'PHYSICAL' or 'LOGICAL'
    data_type     TEXT NOT NULL,             -- 'FLOAT' / 'INT' / 'BOOL' / 'STRING' / 'ENUM'
    unit          TEXT,                      -- 'kW' / '°C' / '%' / ...
    read_write    TEXT DEFAULT 'R',          -- 'R' / 'RW' / 'W'

    -- PhysicalTag 专用字段
    neuron_group  TEXT,                      -- Neuron group 名
    neuron_tag    TEXT,                      -- Neuron tag 名
    scale_factor  FLOAT DEFAULT 1.0,
    offset        FLOAT DEFAULT 0.0,
    unit_from     TEXT,
    unit_to       TEXT,

    -- LogicalTag 专用字段
    formula       TEXT,                      -- SymPy 表达式: "(a+b)*c"
    formula_type  TEXT,                      -- 'expression' / 'aggregate' / 'condition'
    sources       UUID[] DEFAULT '{}',       -- 依赖的其他 tag ID 数组
    aggregate_fn  TEXT,                      -- 'SUM' / 'AVG' / 'MAX' / 'MIN' (用于 F3 汇总)

    range_min     FLOAT,
    range_max     FLOAT,
    description   TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- 关键索引
CREATE INDEX idx_nodes_parent ON t_nodes(parent_id);
CREATE INDEX idx_nodes_layer ON t_nodes(layer);
CREATE INDEX idx_tags_node ON t_tags(node_id);
CREATE INDEX idx_tags_type ON t_tags(tag_type);
CREATE INDEX idx_tags_sources USING GIN(t_sources);  -- 数组索引, 加速依赖查询
```

### 6.2 "每层都是一等公民"的含义

```
Layer 1 (Site):     杭州XX项目
  ├─ LogicalTag: site_total_power = SUM(子站功率)     ← F3 汇总值
  ├─ LogicalTag: site_alarm_count                     ← F3 聚合值
  └─ Layer 2 (Station):  1号变电站
       ├─ LogicalTag: station_solar_power = SUM(PV功率)  ← F3
       ├─ LogicalTag: station_ess_soc = AVG(PCS SOC)      ← F3
       └─ Layer 3 (EnergyNode):  光伏系统
            ├─ LogicalTag: pv_total_irradiance = ...       ← F3 or F1
            └─ Layer 4 (Device):  Huawei逆变器#1
                 ├─ PhysicalTag: activePower_kW (45.2)    ← 原始采集
                 ├─ PhysicalTag: dc_voltage_v (720)         ← 原始采集
                 └─ LogicalTag: efficiency_pct = (pac/pdc)*100  ← F1 公式
                      (F5: SymPy 计算)
```

**核心洞察**: `t_tags.node_id` 可以指向 **任何一层** 的 `t_nodes.id`。
- PhysicalTag 通常挂在 Layer 4 (Device) 或 Layer 5 (Tag)
- LogicalTag (公式) 可以挂在 Layer 4 (设备级衍生指标)
- LogicalTag (汇总) 可以挂在 Layer 1-3 (站点/能源类型级聚合)

**一张 tags 表，三种用途，靠 `tag_type` + `formula` + `sources` 区分。**

---

## 7. 技术模块映射（M0-M12 → 功能域）

| 模块 | 所属域 | 角色 | 代码量 |
|------|--------|------|--------|
| **M0** 项目骨架 | 基础设施 | Docker 编排 + DB Schema + 目录结构 | ~50 行配置 |
| **M1** 节点树引擎 | **F3** | 统一节点模型 CRUD + 树形 API | ~250 行 |
| **M2** MQTT 接入层 | **F0** | 订阅 nanoMQ + JSON 解析 + 消息路由 | ~120 行 |
| **M3** 数据归一化器 | **F0** | scale/offset + pint 单位转换 (纯函数) | ~100 行 |
| **M4** 时序存储引擎 | **F0+TSDB** | 批量写入 Hypertable + 多粒度查询 API | ~180 行 |
| **M5** 虚拟点位引擎 | **F1** | SymPy 事件驱动公式求值 + 级联传播 | ~200 行 |
| **M6** GoRules 规则引擎 | **F2** | JDM 加载/评估/热更新 | ~180 行 |
| **M7** RPC 控制通道 | **F2** | JWT 鉴权 + 策略校验 + Neuron REST 写入 | ~180 行 |
| **M8** 定时任务调度器 | 基础设施 | APScheduler 统一周期任务 | ~120 行 |
| **M9** WebSocket 实时通信 | **F0→前端** | 服务端推送遥测/告警/RPC 结果 | ~150 行 |
| **M10** 前端 Dashboard | 展示层 | React + ECharts 7 页面 UI | ~1500 行 |
| **M11** 报表服务 | 展示层 | pandas 聚合 + openpyxl Excel 导出 | ~200 行 |
| **M12** 配置体验优化 | 展示层 | 向导 / 一键导入 / 可视化编辑器 | ~800 行 |

**总计后端核心: ~1680 行 + 前端 ~2300 行 = ~4000 行**

---

## 8. 开发顺序（按功能域推进）

### Phase 1: F0 基线 (Day 1-5)

```
目标: Mode 1 通线 — "设备上线就能看到原始数字"

S0: 项目骨架 (M0)
    docker-compose up 5 容器全部 healthy
    ↓
S1: Health API + DB Schema
    GET /health → {"status": "ok", "tsdb": "connected"}
    init-db/001-schema.sql (t_nodes + t_tags + t_telemetry)
    ↓
S2: MQTT Stub
    订阅 telemetry/# , 收到消息打印日志
    ↓
S3: NodeTree CRUD (M1 基础)
    POST /nodes 创建 Site→Station→EnergyNode→Device→Tag
    GET /nodes/{id}/tree 返回嵌套 JSON
    ↓
S4: Parse + Normalize (M2 + M3)
    JSON 解析 → scale*value+offset → pint 转换
    ↓
S5: Telemetry Write + Query (M4)
    execute_values 批量写入 + GET /telemetry?agg=raw|1m|1h|1d
    ↓
验收: Dashboard 显示一个跳动数字 ✅
```

**产出**: F0 管道跑通，Mode 1 交付。

### Phase 2: F1 + F3 (Day 6-14)

```
两条并行线 (F1 ⊥ F3):

  线路 A — F1 点位域:
  S6: VirtualPoint Engine 骨架 (M5)
      注册 LogicalTag + SymPy 求值框架
  S7: 公式编辑 UI
      前端选择源点位 → 输入公式 → 实时预览
  S8: 级联传播 + 循环检测
      DAG 拓扑排序, depth≤5 截断
  S9: 虚拟点位入库 + WS 推送
      is_virtual=True 写入 t_telemetry + M9 推送

  线路 B — F3 节点树域: (可与线路 A 并行!)
  S10: 节点树完善 (M1 补全)
       YAML 导入导出 + Neuron 同步导入
  S11: LogicalTag 汇总规则配置
       每层节点声明需要哪些聚合 (SUM/AVG/MAX)
  S12: F3 聚合器实现
       APScheduler Job 每 10s SQL GROUP BY → 更新父节点 LogicalTag
  S13: 节点树实时值展示 (M10 增强)
       前端树形控件 + 每层节点卡片显示汇总值

验收: 同时显示 PCS 功率(F0) + 效率%(F1) + 场站总功率(F3) ✅
```

**产出**: Mode 3 完整通线。

### Phase 3: F2 控制 (Day 15-20)

```
S14: GoRules 集成 (M6)
     加载 EMS 告警模板 + evaluate() 返回正确结果
S15: RPC 控制通道 (M7)
     POST /rpc → JWT 校验 → 策略评估 → Neuron write → 审计日志
S16: 控制面板 UI (M10)
     按钮 Click → 确认弹窗 → 设备响应 → 日志记录
S17: JDM Editor 嵌入 (M12 部分)
     可视化决策表编辑 + 热更新

验收: 按钮 Click → 设备实际响应 → 日志可查 ✅
```

**产出**: Mode 2 交付，读写闭环完整。

### Phase 4: 体验优化 (Day 21-30)

```
S18: 报表服务 (M11)
     日/月/年 Excel 导出
S19: 配置向导 (M12)
     3 步建站 ≤2min + 一键导入 ≤10s
S20: Dashboard Builder
     拖拽布局 (可选, 视优先级)
S21: 性能优化 + 监控
     CAGG 调优 + metrics endpoint + 告警通知
```

---

## 9. 数据库 Schema 锁定

### 9.1 核心三表

```sql
-- ══════════════════════════════════════
--  1. t_nodes: 统一节点表 (所有 5 层)
-- ══════════════════════════════════════
CREATE TABLE t_nodes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    parent_id   UUID REFERENCES t_nodes(id),
    layer       SMALLINT NOT NULL CHECK (layer BETWEEN 1 AND 5),
    node_type   TEXT NOT NULL,              -- 分类器: "SITE"/"STATION"/"ESS"/"PV"/"PCS2000"/...
    config      JSONB DEFAULT '{}',
    sort_order  INT DEFAULT 0,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_nodes_parent ON t_nodes(parent_id);
CREATE INDEX idx_nodes_layer ON t_nodes(layer);

-- ══════════════════════════════════════
--  2. t_tags: 点位表 (Physical + Logical 统一)
-- ══════════════════════════════════════
CREATE TABLE t_tags (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id       UUID NOT NULL REFERENCES t_nodes(id) ON DELETE CASCADE,
    tag_type      TEXT NOT NULL CHECK (tag_type IN ('PHYSICAL', 'LOGICAL')),
    data_type     TEXT NOT NULL CHECK (data_type IN ('FLOAT', 'INT', 'BOOL', 'STRING', 'ENUM')),
    name          TEXT NOT NULL,
    display_name  TEXT,
    unit          TEXT,
    read_write    TEXT DEFAULT 'R' CHECK (read_write IN ('R', 'RW', 'W')),

    -- PhysicalTag 字段
    source_type   TEXT DEFAULT 'NEURON',    -- 'NEURON' / 'MODBUS' / 'OPCUA' / 'CALCULATED'
    source_path   TEXT,                     -- "neuron://group/tag" 或 "modbus://slave/register"
    scale_factor  FLOAT DEFAULT 1.0,
    offset        FLOAT DEFAULT 0.0,
    unit_from     TEXT,
    unit_to       TEXT,

    -- LogicalTag 字段
    formula       TEXT,                     -- SymPy 表达式字符串
    formula_type  TEXT CHECK (formula_type IN ('expression', 'aggregate', 'condition')),
    sources       UUID[] DEFAULT '{}',      -- 依赖的 tag ID 列表
    aggregate_fn  TEXT CHECK (aggregate_fn IN ('SUM', 'AVG', 'MAX', 'MIN', 'COUNT', 'LAST')),

    -- 约束
    range_min     FLOAT,
    range_max     FLOAT,
    enum_options  TEXT[],                   -- 用于 ENUM 类型

    description   TEXT,
    sort_order    INT DEFAULT 0,
    enabled       BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_tags_node ON t_tags(node_id);
CREATE INDEX idx_tags_type ON t_tags(tag_type);
CREATE INDEX idx_tags_sources USING GIN(t_sources);

-- ══════════════════════════════════════
--  3. t_telemetry: 时序数据 (Hypertable)
-- ══════════════════════════════════════
CREATE TABLE t_telemetry (
    ts           TIMESTAMPTZ NOT NULL,
    node_id      UUID NOT NULL REFERENCES t_nodes(id),
    tag_id       UUID NOT NULL REFERENCES t_tags(id) ON DELETE CASCADE,
    value_float  FLOAT,
    value_int    BIGINT,
    value_bool   BOOLEAN,
    value_str    TEXT,
    is_virtual   BOOLEAN DEFAULT FALSE,     -- TRUE = LogicalTag 计算值
    quality      SMALLINT DEFAULT 192       -- 192=GOOD 64=UNCERTAIN 0=BAD
);
-- 转换为 TimescaleDB Hypertable
SELECT create_hypertable('t_telemetry', 'ts');
-- CAGG: 5 分钟连续聚合 (Path B, 零 Python 代码)
CREATE MATERIALIZED VIEW tel_agg_5min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', ts) AS bucket,
    node_id, tag_id,
    avg(value_float) AS avg_val,
    min(value_float) AS min_val,
    max(value_float) AS max_val,
    count(*) AS count
FROM t_telemetry
WHERE value_float IS NOT NULL
GROUP BY 1, 2, 3
WITH NO DATA;

-- CAGG: 1 小时连续聚合
CREATE MATERIALIZED VIEW tel_agg_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts) AS bucket,
    node_id, tag_id,
    avg(value_float) AS avg_val,
    sum(CASE WHEN is_virtual THEN 0 ELSE value_float END) AS sum_physical
FROM t_telemetry
WHERE value_float IS NOT NULL
GROUP BY 1, 2, 3
WITH NO DATA;

-- CAGG: 1 天连续聚合
CREATE MATERIALIZED VIEW tel_agg_1d
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', ts) AS bucket,
    node_id, tag_id,
    avg(value_float) AS avg_val,
    max(value_float) AS max_daily,
    min(value_float) AS min_daily
FROM t_telemetry
WHERE value_float IS NOT NULL
GROUP BY 1, 2, 3
WITH NO DATA;

CREATE INDEX idx_tel_node_tag ON t_telemetry(node_id, tag_id, ts DESC);
CREATE INDEX idxtel_virtual ON t_telemetry(is_virtual) WHERE is_virtual = TRUE;
```

### 9.2 辅助表

```sql
-- ══════════════════════════════════════
--  t_rules: 规则表 (F2/GoRules)
-- ══════════════════════════════════════
CREATE TABLE t_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    rule_type   TEXT NOT NULL CHECK (rule_type IN ('alarm', 'control', 'fault_map', 'linkage')),
    jdm_content JSONB NOT NULL,             -- JDM 决策表/图 JSON
    version     INT DEFAULT 1,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════
--  t_audit_log: 审计日志 (F2/RPC 操作)
-- ══════════════════════════════════════
CREATE TABLE t_audit_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT,
    action      TEXT NOT NULL,               -- 'RPC_WRITE' / 'RULE_UPDATE' / 'LOGIN' ...
    target_type TEXT,                        -- 'node' / 'tag' / 'rule'
    target_id   UUID,
    details     JSONB,
    ip_address  INET,
    created_at  TIMESTAMPTZ DEFAULT now()
);
SELECT create_hypertable('t_audit_log', 'ts');  -- 如果加了 ts 字段

-- ══════════════════════════════════════
--  t_alarms: 告警表 (F2/GoRules 输出)
-- ══════════════════════════════════════
CREATE TABLE t_alarms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id     UUID REFERENCES t_rules(id),
    node_id     UUID REFERENCES t_nodes(id),
    level       TEXT NOT NULL CHECK (level IN ('INFO', 'WARNING', 'MAJOR', 'CRITICAL')),
    message     TEXT NOT NULL,
    acknowledged BOOLEAN DEFAULT FALSE,
    ack_user    TEXT,
    ack_at      TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX idx_alarms_active ON t_alarms(level, acknowledged) WHERE resolved_at IS NULL;

-- ══════════════════════════════════════
--  t_users / t_roles: 用户权限 (M0 基础)
-- ══════════════════════════════════════
CREATE TABLE t_users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username    TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role        TEXT DEFAULT 'viewer' CHECK (role IN ('admin', 'operator', 'viewer')),
    created_at  TIMESTAMPTZ DEFAULT now()
);
```

---

## 10. API 端点清单（按功能域分组）

### F0: 数据管道 API

| Method | Path | 功能 | 域 |
|--------|------|------|-----|
| GET | `/api/v1/health` | 健康检查 (DB/MQTT 连接状态) | F0 |
| POST | `/api/v1/nodes` | 创建节点 | F3(M1) |
| GET | `/api/v1/nodes/{id}` | 获取节点详情 | F3(M1) |
| GET | `/api/v1/nodes/{id}/tree` | 获取子树 (递归展开) | F3(M1) |
| PUT | `/api/v1/nodes/{id}` | 更新节点 | F3(M1) |
| DELETE | `/api/v1/nodes/{id}` | 删除节点 (级联删除子节点?) | F3(M1) |
| POST | `/api/v1/nodes/import` | 从 YAML 导入节点树模板 | F3(M1) |
| GET | `/api/v1/nodes/export?root={id}` | 导出为 YAML | F3(M1) |
| POST | `/api/v1/tags` | 创建点位 (Physical 或 Logical) | F1/F3 |
| GET | `/api/v1/tags?node_id={id}` | 获取节点下所有点位 | F1/F3 |
| PUT | `/api/v1/tags/{id}` | 更新点位配置 | F1/F3 |
| POST | `/api/v1/tags/import-neuron` | 从 Neuron 同步物理点位 | F1 |
| GET | `/api/v1/telemetry/latest` | 最新值查询 | F0(M4) |
| GET | `/api/v1/telemetry/history` | 历史数据查询 (支持聚合粒度) | F0(M4) |

### F1: 点位域 API

| Method | Path | 功能 |
|--------|------|------|
| POST | `/api/v1/virtual-points` | 注册LogicalTag (含公式+依赖) |
| GET | `/api/v1/virtual-points` | 列出所有LogicalTag |
| PUT | `/api/v1/virtual-points/{id}` | 更新公式或依赖 |
| DELETE | `/api/v1/virtual-points/{id}}` | 删除LogicalTag |
| POST | `/api/v1/virtual-points/simulate` | 模拟公式求值 (给定输入, 看输出) |

### F2: 控制域 API

| Method | Path | 功能 |
|--------|------|------|
| POST | `/api/v1/devices/{node_id}/rpc` | 兼容入口：创建控制命令并返回命令 ID |
| GET | `/api/v1/rpc/history` | RPC操作历史 |
| POST | `/api/v1/rules` | 创建规则 (上传JDM) |
| GET | `/api/v1/rules` | 规则列表 |
| GET | `/api/v1/rules/{id}/jdm` | 获取JDM内容 |
| PUT | `/api/v1/rules/{id}/jdm` | 更新JDM (热更新) |
| POST | `/api/v1/rules/{id}/simulate` | 规则模拟 (传入context, 看命中) |
| GET | `/api/v1/alarms` | 告警列表 (支持过滤) |
| PUT | `/api/v1/alarms/{id}/acknowledge` | 确认告警 |

### WebSocket 端点

| Path | 方向 | 用途 |
|------|------|------|
| `/ws/telemetry` | S→C | 实时遥测推送 |
| `/ws/alarms` | S→C | 告警事件推送 |
| `/ws/rpc-response` | S→C | RPC操作结果推送 |

---

## 11. 页面清单 (M10 前端)

| # | 页面 | 路由 | 所属域 | 核心组件 | MVP优先级 |
|---|------|------|--------|----------|-----------|
| P1 | Dashboard 总览 | `/` | F0+F3 | 站点状态卡片 + KPI 数字 | P0 |
| P2 | 节点树管理 | `/nodes` | F3 | 树形控件 + CRUD 表单 | P0 |
| P3 | 实时数据监视 | `/monitor` | F0+F1 | ECharts 趋势图 + 跳动数字 | P0 |
| P4 | 点位配置 | `/tags` | F1 | PhysicalTag表格 + LogicalTag公式编辑器 | P1 |
| P5 | 控制面板 | `/control` | F2 | 设备按钮组 + RPC历史 | P1 |
| P6 | 告警中心 | `/alarms` | F2 | 告警列表 + 确认操作 | P1 |
| P7 | 规则编辑 | `/rules` | F2 | JDM Editor 嵌入 | P2 |
| P8 | 报表 | `/reports` | 展示 | 日期选择 + Excel下载按钮 | P2 |
| P9 | 系统设置 | `/settings` | 基础 | 用户管理 + Neuron连接配置 | P2 |

---

## 12. 设计 Token (UI 方向)

| Token | 值 | 来源 |
|-------|-----|------|
| 主色 | 科技绿 `#52c41a` | 用户指定 |
| 辅色 | 深蓝 `#102040` | 用户指定 |
| 背景 | 白色/浅灰 `#fafafa` | 用户偏好明亮浅色 |
| 字体 | Inter + Noto Sans SC | 标准 |
| 图标库 | Lucide Icons | 轻量一致 |
| 卡片风格 | 圆角 8px, 微阴影 | 现代 SaaS |
| 对标品牌 | Linear (信息密度) + Notion (简洁) | g8 结论 |
| 主题 | 浅色优先, 支持深色切换 | 工业场景偏好明亮 |

---

## 13. 决策记录

```
[2026-07-13] g4-v1.0 - 初版 12 模块拆分 - 基于 v1.0 架构草案
[2026-07-16] g10-stream-engine - 选择自研 asyncio StreamEngine (方案A) - 后续被推翻
[2026-07-16] g9-ha-borrow - HA/TB 数据模型分析 - 确认需要自建统一节点模型
[2026-07-16] 统一节点模型 - 否决独立 ThingModel 表 - node_type 作为分类器, 每层一等公民
[2026-07-17] 方案B - 否决 StreamEngine(方案A) - 选择 CAGG+事件驱动(~120行 vs ~450行)
[2026-07-17] 管道骨架模型 - "所有模块挂载在数采流上" - 用户核心设计哲学
[2026-07-17] 三种管道模式 - Mode1读存 / Mode2读写读存 / Mode3读算存 - 渐进复杂度
[2026-07-17] F1⊥F3 正交 - 配置时可并行, 运行时 F1→F3 有先后
[2026-07-17] CE内置于F0 - 不是独立层而是F0管道的Hook位 - 方案B三路径散布协作
[2026-07-17] g11-final - 功能域架构锁定 - F0→F1→F3→F2 + TSDB 共享 - Phase 0 完成
[2026-07-17] 平台重命名 - Claw → ZiZu - 品牌统一
```

---

## 14. 变更记录

| 版本 | 日期 | 变更内容 | 原因 |
|------|------|----------|------|
| v1.0 | 2026-07-13 | g4 初版 12 模块 | 架构 v1 草案 |
| v1.1 | 2026-07-16 | 新增 StreamEngine 模块 | g10 选型结果 |
| **v2.0** | **2026-07-17** | **全面重写: 功能域架构取代纯模块列表** | **方案B + 统一节点模型 + 管道哲学** |
| **v2.1** | **2026-07-17** | **平台名称: Claw → ZiZu** | **品牌统一** |

---

## 15. 下一步行动

g11 是 **Phase 0 最终文档**。用户确认后：

1. **立即进入 Phase 1**: S0(骨架) → S1(Health API) → S2(MQTT Stub)
2. **同步更新以下文档** (对齐 g11 结论):
   - `docs/architecture-v1.md` → 加入功能域层次图 + 三模式管道
   - `docs/decisions/g4-module-decomposition.md` → v2.0 (此文档已替代)
   - `docs/decisions/g7-goal-breakdown.md` → 加入 F0-F3 映射
   - `docs/decisions/g10-stream-engine.md` → 标记为"已 superseded by 方案B(g11)"

---

*文档版本: G11-v2.1 (FINAL)*
*状态: Phase 0 最后一份文档，待用户确认后启动 Phase 1*
