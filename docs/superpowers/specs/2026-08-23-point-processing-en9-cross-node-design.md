# v0.4.82 点位加工、恩玖 EN9 PCS 与跨节点公式设计

> **现行专项，部分已被取代。** 全局结论汇总见已确认的[核心架构总纲](./2026-08-27-zizu-platform-core-architecture-design.md)。本文件的强类型 DAG、公式 DSL 与 EN9 映射仍有效；其中解决方案资产、设备实例、迟到数据、逐实体周期/心跳、新鲜度下限和旧 outbox 语义已被后续 ADR-0013/0014 取代。

**状态：** 已确认

**日期：** 2026-08-23
**取代：** `2026-08-17-pcs-l0-l1-l2-data-trunk-design.md` 中面向后续版本的术语、EN9 输入模型、兼容策略与发布边界；其已经实现的 v0.4.81 数据主干事实继续保留

## 1. 目标

ZiZu 要成为“通过简单配置即可交付工业控制系统”的配置型 IoT 平台，光储充 EMS 是首个参考交付。v0.4.82 以一号机真实恩玖 EN9 PCS 为首个现场切片，完成以下闭环：

1. 从 Neuron 只读扫描并同步该 PCS 所需的 L0 原始点位；
2. 安装不可变 L1 点位加工版本；
3. 产生三项稳定 L2 全局实体及可追溯实时、历史和来源证据；
4. 在同一架构内继续交付跨节点复杂公式、时间窗口和历史重算；
5. 通过统一配置向导、机器验收、固定镜像摘要和可演练回退证明现场可交付。

本设计已经完成产品、数据、界面、运行安全和部署决策。实施不得重新引入“点位转换”双术语、L1 中间历史、跨节点品牌 L0 引用或旧接口兼容层。

## 2. 已选架构

平台采用“一棵物理节点树、两个时序数据层、一个版本化加工配置层”。

```text
物理节点树
├─ 设备节点：本节点 L0 → L1 点位加工 → 设备级 L2
└─ 系统/站点节点：下级节点 L2 → L1 强类型公式 → 系统/站点级 L2
```

- `t_nodes` 只保存站点、能源子系统和设备等真实对象；L0、L1、L2 是所选节点的三个数据视图，不是物理子节点。
- L0 保存协议采集事实和原始历史，供诊断与本节点加工使用。
- L1 保存模板、不可变修订、输入输出契约、公式、绑定、计划和安装记录，不保存第三份逐样本历史。
- L2 保存稳定业务实体、最新值、历史和来源证据，供告警、策略、控制、画面和报表统一消费。
- 同一设备内可读取本节点 L0；跨节点只能读取 L2，计算安装在输出实体所属节点。

## 3. 深模块与外部 seam

`PointProcessingDelivery` 是配置交付深模块，对调用者只暴露三个高杠杆 interface：

```python
class PointProcessingDelivery:
    def preview(self, node_id, template_revision_id) -> ProcessingPlan: ...
    def apply(self, plan_id, plan_digest, idempotency_key) -> ProcessingApplication: ...
    def inspect(self, node_id) -> ProcessingInstallationState: ...
```

`preview` 内部读取 Neuron 目录、形成 L0 同步差异、校验输入契约、编译加工表达式、检查全站依赖 DAG，并规划 L2 实体和绑定；`apply` 重新验证目录摘要及站点版本后，在一个 PostgreSQL 事务中完成 L0 元数据、L1 安装、L2 实体、绑定、站点版本和审计；`inspect` 返回当前修订、输入来源、质量、阻断项和运行证据。

运行期继续由既有 `DataTrunk.ingest` seam 接收协议观测。它读取当前已安装的点位加工修订，原子写入 L0、L2、latest、来源和 outbox。配置模块不复制采集事务逻辑，运行模块不负责猜测或修改配置。

## 4. 数据结构

### 4.1 节点、L0 和 L2

| 层级 | 定义/配置 | 实时 | 历史 |
|---|---|---|---|
| 节点 | `t_nodes` | — | — |
| L0 | `t_tags` | `t_telemetry_latest` | `t_telemetry` hypertable |
| L1 | 点位加工关系表 | 不单独保存 | 不单独保存 |
| L2 | `t_entity_instances` | `t_l2_latest` | `t_l2_observations` hypertable |

L0 点位元数据必须区分线协议类型和 Neuron 输出后的工程值类型。例如 EN9 总有功功率保存 `wire_data_type=INT16`、`value_data_type=FLOAT`、`decimal=0.1`、`unit=kW`。L0 保存 Neuron 已经输出的工程值，不把 decimal 再执行一次。

每台设备的实体实例以稳定 ID 标识；数据库保证同一设备实例与同一实体定义只有一个当前实体实例。更换设备品牌或加工修订只改变输入绑定和来源版本，不改变 L2 实体 ID。

### 4.2 L1 关系表

`migration_040` 后的规范表名为：

- `t_point_processing_templates`：模板身份、设备类别、品牌型号或系统计算场景；
- `t_point_processing_revisions`：不可变内容、版本、DSL 版本、摘要和发布时间；
- `t_solution_point_processing_assets`：解决方案包与加工修订关系；
- `t_point_processing_inputs`：输入键、来源层级、类型、单位、必需性、新鲜度和选择器；
- `t_point_processing_outputs`：输出键、实体定义、类型、单位、计算周期、历史策略和控制资格声明；
- `t_point_processing_expressions`：可读公式、规范化 AST、结果类型、单位及摘要；
- `t_point_processing_plans` / `t_point_processing_plan_items`：统一 L0/L1/L2 变更计划；
- `t_point_processing_installations`：节点当前及历史安装修订；
- `t_point_processing_input_bindings` / `t_point_processing_output_bindings`：冻结输入来源和稳定输出实体；
- `t_point_processing_applications` / `t_point_processing_idempotency`：应用结果与幂等证据；
- `t_point_processing_checkpoints`：时间窗口可恢复检查点，不作为业务事实；
- `t_point_processing_recalculations`：历史重算批次、操作者、范围、输入和输出版本。

核心身份、类型、单位、修订和绑定使用关系列、外键、唯一约束和 CHECK；复杂公式保存为经过 Schema 验证的规范化 AST，并同时保留可读 DSL 文本。运行时只执行已验证 AST，不解释任意代码。

计划项是计划详情的唯一事实来源，包含 `layer=L0|L1|L2`、`action=add|update|preserve|delete_candidate|block`、前后值和稳定 blocker code。计划头只保存基准站点版本、Neuron 目录摘要、状态和计划摘要，不再同时维护另一套可变的 items/blockers JSON 副本。

### 4.3 运行数据与来源

`t_l2_observations` 继续使用互斥强类型值列；`t_l2_latest` 只由业务时间和确定性顺序更晚的观测推进。`t_l2_observation_sources` 保存输入 L0/L2 观测、源时间、接收时间、质量、加工修订和来源摘要。

数值实体按配置周期写历史；布尔、枚举和 `CODE_SET` 在值或质量变化时写历史，并按配置周期写心跳。L1 不保存中间样本。迟到 L0 可以进入原始历史，但实时链路不自动改写已经发布的 L2 历史；修正必须创建显式历史重算批次，新结果带新加工版本和批次 ID，旧结果不可覆盖。

## 5. 恩玖 EN9 PCS revision 1

### 5.1 输入范围

首个现场模板只要求 90 项只读输入：

- 1 项交流总有功功率；
- 1 项并网/离网状态；
- 88 项布尔故障点。

协议依据为维护者提供、未纳入仓库的 `EN9_PCS tags.xlsx`；其中总有功功率位于 `1!424634`、线类型为 `INT16`、decimal 为 `0.1`，88 项故障从 `1!405889.0` 到 `1!405896.15`，均为只读 BIT。实际绑定以一号机 Neuron 目录为运行事实，模板同时使用节点、组、点位名、别名和寄存器/Bit 地址做确定校验；名称或分组差异可以形成可解释 update，缺失、重复、地址冲突或类型不兼容必须形成 blocker。进入公开解决方案包的 EN9 资产只能包含协议模板，不得包含现场节点名、连接参数或凭据。

模板不导入命令组、参数组和保留组，不修改 Neuron，不发送设备控制。`工作状态`、充放电模式和其他遥测继续保留为 L0 诊断点，但不进入首批三个输出。

每个必需输入的现场新鲜度阈值在安装计划中固化为 `max(3 × Neuron 采集组周期, 5 秒)`；采集组周期无法读取或不在平台允许范围时形成 blocker，不使用猜测默认值。

### 5.2 三项 L2 输出

#### `pcs.active_power`

- 输入：交流总有功功率；
- L0 工程类型：`FLOAT`；
- 输出类型和单位：`FLOAT / kW`；
- Neuron 已执行 decimal，L1 使用恒等缩放；
- 方向：正值表示放电或向交流侧输出，负值表示充电或从交流侧吸收；
- 安装验收必须比较 L0 与 L2，显式阻止重复缩放和符号颠倒。

#### `pcs.operating_state`

主输入使用独立的并网/离网状态枚举：

| 原值 | 标准值 |
|---:|---|
| 0 | `STOPPED` |
| 1 | `STARTING` |
| 2 | `STANDBY` |
| 3 | `RUNNING_OFF_GRID` |
| 4 | `RUNNING_GRID_CONNECTED` |
| 5 | `FAULT` |
| 6 | `COMMISSIONING` |

未映射值产生 `BAD / UNMAPPED_ENUM`，不得伪装为有效 `UNKNOWN`。

#### `pcs.fault_codes`

输出类型为稳定排序、去重的 `CODE_SET`。88 个布尔输入为 true 时输出对应跨品牌语义码，为 false 时不输出。示例：

```text
EPO故障        → pcs.hardware.epo
电网A相过压故障 → pcs.grid.phase_a_overvoltage
电池过压故障    → pcs.dc.battery_overvoltage
BMS通信故障     → pcs.communication.bms_failure
```

每项映射保留语义码、默认中文名称、故障类别、EN9 点位名、寄存器/Bit 地址和原始值。告警严重度不属于点位加工模板，由告警配置对实体和故障码独立定义。任一必需故障输入为 BAD 或 STALE 时，整个 `pcs.fault_codes` 无效并携带全部可得来源证据；不得输出不完整的空集合冒充“无故障”。

`pcs.fault_codes` 在集合或质量变化时写入历史，并每 60 秒写一条心跳观测；重复心跳不产生新的告警事件。

## 6. 跨节点强类型公式

### 6.1 引用与归属

- 本节点加工可以绑定本节点 L0；
- 跨节点加工只能绑定其他节点 L2；
- 加工安装在输出实体所属节点，例如多个 PCS 汇总功率安装在“储能系统”；
- 一个 L2 输出可以继续作为上级 L1 输入，形成设备、子系统、站点的逐级 DAG；
- 安装前编译全站依赖图，拒绝循环、超过 8 层、类型错误、单位不兼容和空的必需集合。

批量选择器采用“子树范围 + 节点类型 + 实体定义”。`preview` 时把选择器展开为明确实体清单并写入计划摘要，`apply` 时复核并冻结绑定；新增设备不会未经计划审查自动加入现行公式。

### 6.2 DSL 能力

安全 DSL 首版支持：

- 加减乘除、比较、布尔逻辑和条件表达式；
- 绝对值、最小/最大、限幅和显式单位换算；
- 多实体求和、平均、加权、最小/最大和计数；
- 滑动平均、窗口最小/最大、积分、差值和变化率；
- 显式可选输入和默认值。

公式文本在保存时解析为规范 AST，执行器只接受白名单节点和函数。Python、JavaScript、动态导入、网络、文件、数据库查询和系统命令均不属于 DSL。

### 6.3 调度、质量和控制资格

设备内无状态加工随输入观测到达触发；跨节点和时间窗口公式按 1～3600 秒的整数配置周期使用同一计算时点求值，站级实时功率公式默认 1 秒。窗口执行器用检查点加历史重放恢复，检查点损坏不能改变业务历史。

- 必需输入 BAD/STALE：输出无值且质量无效；
- 可选输入只有公式显式声明默认值才能继续，结果为 `UNCERTAIN`；
- 除零、溢出和运行类型错误产生明确 BAD 原因；
- `UNCERTAIN` 不得进入自动策略或控制；
- 公式输出只有显式 `control_eligible`、修订已批准、当前质量为 GOOD、未使用默认值且所有输入满足时效时，才可以被控制运行时接受。

点位加工本身不下发控制；所有人工和自动控制仍经过统一控制命令 seam、限值、联锁、幂等、冷却、权限、回读和审计。

## 7. 配置计划与界面

### 7.1 统一计划

一次计划覆盖三层：

```text
L0：点位 add / update / preserve / block
L1：修订、表达式和输入绑定 add / update / preserve / block
L2：实体实例和输出绑定 add / update / preserve / block
```

任何 blocker、站点版本变化、Neuron 目录摘要变化或计划摘要不符都使 `apply` 零运行写入。相同 actor 与幂等键重放返回同一应用结果；不同请求复用键稳定拒绝。

正式产品由 engineer 预览并确认；一号机 RC 部署作业可以在隔离演练和生产复核均为零 blocker 时自动应用同一个确定性计划。

### 7.2 节点数据工作台

左侧只显示真实节点树。选择节点后，主区固定显示：

1. 顶部 `Neuron → L0 → L1 点位加工 → L2 → 上层应用` 数据流；
2. `L0 原始点位` 页签：目录、实时值、质量、时间和同步动作；
3. `L1 点位加工` 页签：模板修订、输入、可视化公式、DSL 文本、输出、DAG 和发布约束；
4. `L2 全局实体` 页签：实时值、历史、质量、来源证据和消费者。

业主操作员默认只看 L2；实施工程师可以从 L2 下钻到 L1 和 L0。

### 7.3 五步统一配置向导

1. 选择目标节点和点位加工模板；
2. 只读扫描 Neuron 点位目录；
3. 在一页预览 L0、L1、L2 统一计划、摘要和 blocker；
4. engineer 确认后原子应用；
5. 自动进入机器验收并生成不可变报告。

界面不要求实施工程师编辑 SQL、UUID 或自由 JSON/YAML。可视化公式编辑与文本公式编辑必须编译为同一个规范 AST，不形成两套执行逻辑。

## 8. 公开接口、错误和权限

规范路由为：

| 方法 | 路径 | 能力 |
|---|---|---|
| GET | `/api/v1/point-processing-templates` | 查询适用模板 |
| POST | `/api/v1/nodes/{node_id}/point-processing-plans` | 生成统一计划 |
| GET | `/api/v1/point-processing-plans/{plan_id}` | 读取计划、摘要和 blocker |
| POST | `/api/v1/point-processing-plans/{plan_id}/apply` | 幂等原子应用 |
| GET | `/api/v1/nodes/{node_id}/data-trunk` | 查看节点 L0/L1/L2 状态 |
| GET | `/api/v1/entity-instances/{id}/realtime` | 读取 L2 当前值和来源 |
| GET | `/api/v1/entity-instances/{id}/history` | 读取 L2 历史及版本 |
| WS | `/api/v1/ws/entity-observations` | 订阅已提交 L2 增量 |

错误码统一使用 `POINT_PROCESSING_*`，至少包括 `INPUT_MISSING`、`INPUT_AMBIGUOUS`、`TYPE_MISMATCH`、`UNIT_MISMATCH`、`PLAN_STALE`、`PLAN_DIGEST_MISMATCH`、`DAG_CYCLE`、`FORMULA_INVALID` 和 `NOT_INSTALLED`。旧 `POINT_CONVERSION_*` 错误和 `/point-conversion-*` 路由不保留。

admin 管理模板生命周期；engineer 生成和应用计划并运行验收；operator 只读 L2。所有配置允许和拒绝进入统一审计，只有既定最小存活探针匿名。

## 9. migration_040 硬切换

`migration_040` 在单个数据库事务内执行预检、改名、约束重建和数据变换：

1. 要求输入 Schema 完整处于 039，混合结构或非法数据直接失败；
2. 把所有 `t_point_conversion_*`、安装表、输入输出绑定、revision 外键、trigger、function、index 和 CHECK 改为 `point_processing` 规范名称；
3. 把 `source_kind='point_conversion'`、pipeline stage 和来源字段改为 `point_processing`；
4. 把单输入故障字符串规则升级为 `TOKEN_SET` 或 `BOOLEAN_SET`，支持一个输出绑定多个布尔输入；
5. 从故障映射删除默认严重度；告警配置继续持有严重度；
6. 建立规范表和 contract gate，登记 Schema 040。

应用代码、Pydantic 类型、模块文件、前端类型、组件、资产目录、解决方案清单和测试同步使用 `PointProcessing`、`point_processing`、`point-processing` 和“点位加工”。不提供旧路由、别名类型、兼容视图、双写或运行期 fallback。新容器发现旧或混合 Schema 时拒绝启动；旧容器不得连接 040 数据库。

## 10. 交付分段

所有 RC 属于一个正式 `v0.4.82` 目标，但各自可部署、可验收、可回退：

1. `v0.4.82-rc.1`：migration_040、真实 EN9 L0 同步、三项 PCS 实体和 88 路 BOOLEAN_SET；
2. `v0.4.82-rc.2`：跨节点 DSL、选择器冻结、全站 DAG 和双模式公式编辑；
3. `v0.4.82-rc.3`：时间窗口、检查点、显式历史重算和综合机器验收；
4. 正式 `v0.4.82`：全部 RC 证据通过，并完成四小时独立配置交付试验。

不得为了等待 rc.2/rc.3 把 rc.1 做成不可运行的大分支；也不得把 rc.1 的三项实体验收表述为完整跨节点公式已经交付。
本规格是三个 RC 的共同架构基线；rc.1、rc.2、rc.3 必须分别编写和执行独立实施计划。整体设计批准后首先只为 rc.1 编写实施计划。

## 11. rc.1 测试与现场验收

### 11.1 提交前门禁

- 90 个 EN9 输入契约及 88 项故障逐项模拟；
- decimal 不重复缩放、功率符号、状态枚举和未知枚举测试；
- 任一故障输入 BAD/STALE 时整体失效测试；
- 计划摘要、过期拒绝、幂等和事务回滚测试；
- migration_040 全新安装、039 升级和重复执行测试；
- 静态扫描证明运行代码、公开接口和当前文档无遗留兼容入口；
- 完整后端测试和前端生产构建；
- 固定 linux/arm64 镜像摘要。

### 11.2 生产切换前隔离演练

冻结配置写入并记录当前 v0.4.81 容器摘要；完整备份生产数据库，恢复到隔离数据库，验证关键表和记录数量，证明旧容器可以读取原始备份；再对隔离数据库依次应用 040、041，启动 rc.1、读取一号机 Neuron 目录并生成计划。任何迁移、恢复、目录或 blocker 失败都保持生产 v0.4.81 不变。

### 11.3 一号机部署

部署沿用旧容器必要运行设置，包括 `network_mode: host` 和 `/dev/mqueue` tmpfs；不启动 Caddy、不申请 TLS、不改变既有公网入口。生产流程为：停止配置写入、停止旧容器、使用 owner migration job 依次应用 040、041、确认 `schema_migrations` 最新为 041、启动固定 rc.1 摘要、重新扫描现场目录、零 blocker 时应用计划、运行机器验收。生产复核出现 blocker 或关键验收失败时立即回退。

### 11.4 机器验收

不可变报告至少证明：

1. 健康、MQTT、数据库和采集链正常；
2. 90 项必需输入存在且质量合格；
3. `pcs.active_power` 与 L0 工程值一致，无十倍误差和符号颠倒；
4. `pcs.operating_state` 与现场枚举一致；
5. `pcs.fault_codes` 可读且 88 项来源证据完整；
6. 三项实体 latest、history、时间戳和来源持续更新；
7. 认证 WebSocket 收到已提交 L2 增量；
8. 加工修订、站点配置版本和来源摘要可追溯；
9. 容器重启后安装版本、实时链和历史继续工作；
10. 连续观察 30 分钟无新增数据库写入错误或异常质量漂移。

88 项故障逻辑使用离线模拟器逐项验收；一号机只读验证当前真实状态，不制造设备故障。rc.1 不启用或验证自动策略，不发送设备控制命令。

## 12. 回退

出现迁移失败、混合命名、任何 blocker、缩放或符号错误、L2 缺失、历史不落库、来源不完整、WebSocket 失败、重启失败、数据库写入错误增加或容器不稳定时，停止 rc.1，恢复部署前完整数据库备份，启动原 v0.4.81 固定摘要容器，并验证原站点健康与数据恢复。

因为采用硬切换，回退不尝试让 v0.4.81 读取 Schema 040/041。恢复需要维护窗口，备份后的生产写入可能丢失；该代价已经由维护者明确接受。

## 13. 非目标

- rc.1 不建设跨节点 DSL、时间窗口或历史重算；它们分别属于 rc.2 和 rc.3；
- rc.1 不启用自动策略、不执行低功率控制、不修改 Neuron；
- 不为 L1 建第三张时序超级表；
- 不允许任意脚本或运行期动态设备集合；
- 不把 L0/L1/L2 建成物理节点；
- 不迁移 BMS、光伏逆变器、充电桩和电表模板；
- 不建设通用拖拽大屏或通用工作流引擎；
- 不因首页可访问、HTTP 200 或单元测试通过而宣称正式版交付完成。
