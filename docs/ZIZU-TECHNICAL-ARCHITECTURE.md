# ZiZu 技术架构说明 / Technical Architecture

> 当前基线 / Current baseline: `v0.8.4`
>
> 架构状态 / Architecture status: 目标架构已冻结，核心数据主干已落地，上层功能仍在逐项完成现场闭环。
> The target architecture is frozen. The core data trunk is implemented, while upper-layer functions are still being closed out through field acceptance.

## 中文

### 1. 产品目标

ZiZu 是用于开发和交付 EMS 的配置型工业 IoT 平台。实施工程师不修改平台源码、不直接编写 SQL，
只需在界面上建立真实节点、接入设备点位、配置点位加工和全局实体，再配置告警、JDM、控制与固定
EMS 工作台，即可交付单站工业控制系统。光储充 EMS 是首个参考交付场景，而不是平台唯一能支持的行业。

ZiZu 当前面向单站边缘部署，目标设备为 4 核 4 GB ARM 工控机。平台追求的是一条简单、可解释、可追溯、
故障时安全收敛的交付路径，而不是通用云平台、多租户系统或自由页面设计器。

### 2. 唯一数据主干

```text
真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体
                                           ↓
                         告警 / JDM / 控制 / 固定 EMS 工作台
```

- **真实节点树**只表示场站、系统和真实设备，不把点位、公式或实体伪装成物理节点。
- **L0 原始点位**保存设备实际上传的协议事实，包括原始值、工程值类型、单位、质量、数据时间、接收时间和来源。它提供实时与历史诊断，只允许本节点 L1 直接读取。
- **L1 点位加工**通过映射、单位换算、枚举和状态解析、多点组合及强类型公式消除品牌差异。本节点计算可读 L0，跨节点计算只能读已标准化的 L2；依赖必须组成无环图，不允许任意 Python 或 JavaScript。
- **L2 全局实体**是上层应用唯一业务数据接口。实体以 `node_id + definition_key` 保持稳定身份，并携带值、类型、单位、质量、时间戳和来源证据。更换设备品牌时只调整 L1 输入绑定，不改变 L2 身份及其上层引用。
- **统计加工**是 L1 的一种规则类型，结果仍然是普通 L2；平台不建立“统计实体层”或新的 L3。

L0、L1、L2 是所选真实节点的三个数据视角，不是节点树中的三类子节点。为降低使用门槛，普通实施界面
主要呈现“原始数据”和“标准实体”：工程师从 L0 选择数据、定义加工并发布 L2；点位加工模板只在需要
复用同类设备时使用，不是首台设备接入的前置条件。

### 3. 实时运行架构

```text
设备 / Modbus 等协议
        ↓
Neuron 协议网关
        ↓ MQTT
NanoMQ 消息总线
        ↓
FastAPI 数据管道 → 单站实时黑板 → 统一节拍冻结不可变数据帧
                                      ↓
                事务 A：数据帧 + 本拍变化的 L0
                                      ↓
                         固定配置修订运行 L1 DAG
                                      ↓
        事务 B：L0 latest + L2 历史/latest + 来源证据 + outbox
                                      ↓ 提交后可见
             告警 / JDM / 控制 / EMS 工作台 / WebSocket 实时页面
```

每个单站只有一个活动采集写者和一块进程内实时黑板。默认每秒一个节拍；只有数据或质量发生变化时才
冻结数据帧。同一拍内每个点只保留最后一个有效候选，重复、倒退和迟到样本直接放弃。系统先保存帧与
变化 L0，再按固定配置修订运行完整 L1，最后原子提交 L0 latest、L2、来源证据和统一 outbox。

数据库未提交前，页面不推送、告警不触发、JDM 不执行、控制不下发。上层模块按帧顺序消费 committed L2，
使用幂等收据承受至少一次投递；浏览器先读取完整终态快照和游标，再通过 WebSocket 接收提交后的帧增量。

质量使用 `GOOD / UNCERTAIN / BAD / STALE`。连续三个节拍没有新样本时保留最后值但转为 `STALE`；机器
消费者必须按质量 fail closed，非 `GOOD` 数据不得进入自动控制。帧处理失败在固定预算内重试，超限后
终结为 `FAILED`，标记受影响 L2 及其依赖下游为 `STALE`，并继续处理后续帧，禁止产生半帧或假 `GOOD`。

### 4. 上层功能边界

| 模块 | 职责 | 不允许 |
|---|---|---|
| 告警 | 针对 L2 配置等级、触发/恢复条件、多码故障映射和可选 HTTP 通知 | 直接读取 L0；把确认当作恢复 |
| JDM | 使用 GoRules JDM 对 L2 作版本化决策，产生判断或控制意图 | 充当 L1 公式引擎；绕过统一控制入口 |
| 控制 | 将人工或 JDM 意图变成可审计命令，经唯一 L0 写点下发并等待 L2 回读 | 把接口返回成功当作设备成功；使用非 GOOD 或旧修订数据 |
| EMS 工作台 | 按节点类型与稳定 L2 语义展示能流、功率、SOC、趋势和告警 | 建立第二套业务数据；提供自由页面设计器 |

控制是唯一反向数据链：

```text
操作员 / JDM → 控制意图 → 可控 L2 → 唯一确认的 L0 写点 → Neuron → 设备
                       ↑                                     ↓
                       └──────── 新 L2 回读确认 ← L1 ← 新 L0 ┘
```

只有新的现场观测经正常采集链形成 L2，并在容差和超时要求内达到期望值，控制命令才算成功。

### 5. 数据与配置结构

全站使用共享关系表和共享时序表，不按节点、设备、品牌或 L0/L1/L2 分别建表：

- 关系数据保存真实节点、L0 目录、L1 模板与不可变修订、输入输出绑定、L2 身份、告警/JDM/控制配置、配置修订和审计；
- TimescaleDB 共享时序表保存 L0 与 L2 的历史观测，latest 表保存最新终态可见值；
- 数据帧表保存帧序号、配置修订、状态、失败与完成信息；统一 outbox 负责提交后分发；
- 来源关系把每个 L2 追溯到实际 L0 观测、L1 修订、配置修订、质量与时间依据；
- L1 不保存普通时序数据，也不把整站全量数据复制成巨型 JSON 快照。

配置发布采用强类型校验、确定性绑定、不可变修订、计划预览和原子应用。缺少输入、存在多个候选、类型或
单位不兼容、公式循环、配置并发变化都会阻止发布；运行时不猜测来源，也不回退到旧 API、旧表或双写路径。

### 6. 技术栈与部署边界

| 层 | 技术 | 作用 |
|---|---|---|
| 协议接入 | Neuron | Modbus、OPC UA、IEC 104 等工业协议接入 |
| 消息总线 | NanoMQ | 单站轻量 MQTT 总线 |
| 后端 | Python 3.12 + FastAPI | 配置 API、采集管道、数据帧、上层运行服务 |
| 数据库 | PostgreSQL + TimescaleDB | 关系配置、审计、共享时序数据与连续聚合 |
| 决策 | GoRules ZEN / JDM | 唯一上层决策模型与试运行语义 |
| 前端 | React 18 + TypeScript + Vite | 节点数据、告警、JDM、控制和固定 EMS 工作台 |
| 部署 | Docker Compose | 单站可重现部署；现场使用固定镜像摘要和 Schema 版本 |

平台明确不引入 Redis、Kafka、新微服务、多租户、第二套规则引擎、任意脚本、自由页面设计器或运行期兼容
回退。正式发布必须锁定平台版本、镜像摘要、数据库 Schema、模板摘要和配置修订；部署前备份并验证可恢复。

### 7. 当前实现状态

`v0.8.4` 已具备真实节点、L0 导入与实时/历史、L1 点位加工与模板生命周期、L2 实时/历史与来源、实时
黑板、统一数据帧、节点实时界面，以及告警配置、状态机、HTTP 通知和通知记录管理等主链能力。告警功能
正在通过现场使用持续打磨。

JDM、统一控制和固定 EMS 工作台已有实现基础，但仍需在当前架构下完成真实光储充站点的端到端配置、
安全控制和长期运行验收。因此，当前准确状态是“核心数据主干已落地，平台处于功能打磨和参考交付闭环
阶段”，不能表述为完整光储充 EMS 已经交付就绪。

---

## English

### 1. Product goal

ZiZu is a configuration-driven industrial IoT platform for building and delivering energy management systems. An
implementation engineer should be able to model real assets, connect device points, define point processing and global
entities, and configure alarms, JDM decisions, control, and the fixed EMS workbench without changing platform source code
or writing SQL. A solar-storage-charging EMS is the first reference delivery, not the platform's only target domain.

ZiZu currently targets a single-site edge deployment on a 4-core, 4 GB ARM industrial computer. Its goal is one simple,
explainable, traceable, and fail-safe delivery path—not a general-purpose cloud platform, multi-tenant system, or free-form
dashboard designer.

### 2. The single data trunk

```text
Physical node tree → L0 raw points → L1 point processing → L2 global entities
                                                        ↓
                                  Alarms / JDM / Control / Fixed EMS workbench
```

- **The physical node tree** represents only sites, subsystems, and real equipment. Points, formulas, and entities are not physical child nodes.
- **L0 raw points** preserve protocol facts received from equipment: raw value, engineering type, unit, quality, data time, receive time, and source. L0 provides live and historical diagnostics and may be read directly only by L1 processing on the same node.
- **L1 point processing** removes vendor differences through mapping, unit conversion, enum and state decoding, multi-point composition, and strongly typed formulas. Same-node processing may read L0; cross-node processing may read only normalized L2. Dependencies must form a DAG, and arbitrary Python or JavaScript is forbidden.
- **L2 global entities** are the only business-data interface for upper applications. An entity keeps a stable identity through `node_id + definition_key` and carries its value, type, unit, quality, timestamp, and provenance. Replacing an equipment brand changes L1 input bindings, not the L2 identity or its consumers.
- **Statistical processing** is an L1 rule type whose result is an ordinary L2 entity. There is no separate statistical-entity layer or L3.

L0, L1, and L2 are three data views attached to the selected physical node, not three kinds of tree node. To reduce user
complexity, the normal engineering UI mainly exposes **Raw Data** and **Standard Entities**. The engineer selects L0 inputs,
defines processing, previews the result, and publishes L2. A reusable processing template is optional for repeated equipment;
it is not required for the first device.

### 3. Real-time runtime architecture

```text
Device / industrial protocol
        ↓
Neuron protocol gateway
        ↓ MQTT
NanoMQ message bus
        ↓
FastAPI pipeline → site-local real-time blackboard → immutable frame on a common tick
                                                     ↓
                           Transaction A: frame + changed L0 observations
                                                     ↓
                                  run the complete L1 DAG at one config revision
                                                     ↓
       Transaction B: L0 latest + L2 history/latest + provenance + outbox
                                                     ↓ visible after commit
                    Alarms / JDM / Control / EMS workbench / WebSocket UI
```

Each site has one active ingestion writer and one in-process real-time blackboard. The default tick is one second, and a
frame is frozen only when data or quality changes. For each point, only the last valid candidate in a tick is retained;
duplicate, regressive, and late samples are discarded. ZiZu first stores the frame and changed L0 observations, evaluates
the complete L1 graph at a fixed configuration revision, and then atomically commits L0 latest, L2, provenance, and the
shared outbox.

Nothing is pushed or acted on before the database commit: alarms do not transition, JDM does not execute, and control is
not issued. Upper modules consume committed L2 in frame order and use idempotent receipts for at-least-once delivery. A
browser first loads a complete committed snapshot and cursor, then receives committed frame deltas over WebSocket.

Quality is `GOOD`, `UNCERTAIN`, `BAD`, or `STALE`. After three ticks without a new sample, the last value is retained for
diagnosis but its quality becomes `STALE`. Machine consumers fail closed, and non-`GOOD` data cannot drive automatic
control. Frame processing retries within a fixed budget; an exhausted frame ends as `FAILED`, affected L2 outputs and
their dependency closure become `STALE`, and later frames continue without exposing partial frames or false `GOOD` data.

### 4. Upper-layer boundaries

| Module | Responsibility | Forbidden behavior |
|---|---|---|
| Alarms | Configure severity, trigger/recovery conditions, multi-code fault mappings, and optional HTTP notification against L2 | Reading L0 directly; treating acknowledgement as recovery |
| JDM | Apply a versioned GoRules JDM model to L2 and produce a decision or control intent | Acting as the L1 formula engine; bypassing the control boundary |
| Control | Convert human or JDM intent into an audited command, write through one confirmed L0 control point, and wait for L2 readback | Treating an API response as device success; using non-`GOOD` or obsolete-revision data |
| EMS workbench | Present energy flow, power, SOC, trends, and alarms by node type and stable L2 semantics | Creating a second business-data model; becoming a free-form page designer |

Control is the only reverse path:

```text
Operator / JDM → control intent → controllable L2 → one confirmed L0 write point → Neuron → device
                              ↑                                               ↓
                              └──────────── new L2 readback ← L1 ← new L0 ────┘
```

A command succeeds only when a new field observation returns through the normal ingestion path, produces L2, and reaches
the expected value within the declared tolerance and timeout.

### 5. Data and configuration model

One site uses shared relational and time-series tables; ZiZu does not create a table per node, device, vendor, or layer:

- relational data stores physical nodes, the L0 catalog, L1 templates and immutable revisions, bindings, stable L2 identities, alarm/JDM/control configuration, configuration revisions, and audit records;
- shared TimescaleDB tables store L0 and L2 observations, while latest tables expose the most recent terminal state;
- frame metadata records sequence, configuration revision, lifecycle, failure, and completion; one shared outbox performs post-commit fan-out;
- provenance links each L2 fact to actual L0 observations, its L1 revision, configuration revision, quality, and time basis;
- L1 has no ordinary time-series table, and ZiZu does not persist a giant whole-site JSON snapshot for every tick.

Configuration publishing uses strong typing, deterministic bindings, immutable revisions, plan preview, and atomic apply.
Missing or ambiguous inputs, incompatible types or units, formula cycles, and concurrent configuration changes block
publication. Runtime code never guesses a source or falls back to legacy APIs, compatibility tables, or dual-write paths.

### 6. Technology and deployment boundaries

| Layer | Technology | Role |
|---|---|---|
| Protocol integration | Neuron | Modbus, OPC UA, IEC 104, and other industrial protocols |
| Message bus | NanoMQ | Lightweight single-site MQTT bus |
| Backend | Python 3.12 + FastAPI | Configuration APIs, ingestion, frames, and upper-layer runtime services |
| Database | PostgreSQL + TimescaleDB | Relational configuration, audit, shared time series, and continuous aggregates |
| Decisions | GoRules ZEN / JDM | The only upper-layer decision model and simulation semantics |
| Frontend | React 18 + TypeScript + Vite | Node data, alarms, JDM, control, and the fixed EMS workbench |
| Deployment | Docker Compose | Reproducible single-site deployment with immutable image digest and Schema version |

ZiZu deliberately excludes Redis, Kafka, additional microservices, multi-tenancy, a second rules engine, arbitrary scripts,
a free-form page designer, and runtime compatibility fallbacks. A production release locks the platform version, image
digest, database Schema, template digests, and configuration revision; deployment requires a verified backup and recovery path.

### 7. Current implementation status

`v0.8.4` implements the main path for physical nodes, L0 import and live/history views, L1 processing and template lifecycle,
L2 live/history and provenance, the real-time blackboard, committed frames, the node real-time UI, and core alarm configuration,
state transitions, HTTP notifications, and notification-record management. Alarm usability is still being refined through
field operation.

JDM, unified control, and the fixed EMS workbench have implementation foundations, but they still require end-to-end
configuration, safe-control, and sustained field acceptance on a real solar-storage-charging site under the current
architecture. The accurate status is therefore: **the core data trunk is implemented, and the platform is in functional
hardening and reference-delivery closure**. It is not yet accurate to claim that a complete solar-storage-charging EMS is
delivery-ready.

---

## Architecture authority / 架构解释顺序

When documents disagree, use this order / 文档冲突时按以下顺序解释：

1. [核心架构总纲 / Core architecture specification](superpowers/specs/2026-08-27-zizu-platform-core-architecture-design.md)
2. Latest accepted [ADR](adr/)
3. Current subsystem specification
4. Historical specifications and deployment records

Historical documents explain evolution and must not restore removed concepts such as solution packages, device-instance
layers, compatibility APIs, or a separate statistical-entity system.
