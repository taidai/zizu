# ZiZu IoT Platform — 架构设计书 v1.1

> 替代 ThingsBoard 的工业 IoT 开发平台
> 核心能力：数据管道 + 自定义点位 + 节点树聚合 + 可视化规则引擎
> 技术栈：Neuron + nanoMQ + FastAPI + GoRules(ZEN) + TimescaleDB + React
>
> **⚠️ 此文档已被 g11-feature-domains.md (v2.1) 部分取代。**
> **功能域架构(F0→F1→F3→F2)、统一节点模型、方案B CE、三种管道模式 → 以 g11 为准。**
> **本文档保留作为 v1.0 原始设计参考，数据模型细节(Node/Rule/VP/RulesService)仍有效但需按 g11 Schema 对齐。**
> **控制命令的现行安全契约以 ADR-0007 和根目录 README 为准；下方旧 RPC 时序不代表当前公开 API。**

---

## 1. 需求与设计目标

### 1.1 用户核心诉求

**一句话**：用户通过界面配置，零代码实现工业控制系统。

### 1.2 三大功能域

| # | 功能域 | 描述 | 用户操作 |
|---|--------|------|----------|
| F1 | 多级节点架构 | 场站→电站→能源节点→设备→点位 五层树 | 拖拽/表单建树 |
| F2 | 自定义实时点位 | 物理点位(来自Neuron) + 逻辑点位(公式计算) | 导入模板/填公式 |
| F3 | 自定义控制规则 | GoRules JDM 决策表/决策图绑定到任意节点 | 可视化编辑器 |

### 1.3 设计原则

- **配置即平台**：所有业务逻辑通过界面配置产生，不写死代码
- **五层节点一棵树**：一个 JSON 描述完整层级关系，不拆成多个概念（TB 的 Asset/Device/Profile 三件套合并为一个 Node Tree）
- **物理/逻辑点位统一寻址**：前端不区分点位来源，统一用 `node_path.tag_name` 访问
- **规则跟随节点**：规则绑定在任意层级的节点上，自动继承给子节点
- **最小闭环优先**：Phase 1 只做"设备上线→数据显示→规则触发→控制下发"一条链路

---

## 2. 五层节点树模型

### 2.1 层级定义

```
Site (场站)           ← 最高级，如"某某工业园"
 └── Station (电站)    ← 如"1号光储充站"
      └── EnergyNode (能源节点)  ← 四种类型
           ├── ESS (储能系统)
           │    ├── Device: PCS (#1, #2, ...)
           │    ├── Device: BMS (#1, #2, ...)
           │    └── Device: 电表
           ├── PV (光伏系统)
           │    ├── Device: 逆变器 (#1, #2, ...)
           │    └── Device: 光伏电表
           ├── GRID (电网接入)
           │    └── Device: 并网电表
           └── EVSE (充电桩)
                └── Device: 充电桩 (#1, #2, ...)
                └── Device: 充电电表
                └── Tag (点位)     ← 叶子节点
                     ├── PhysicalTag (物理点位，Neuron采集)
                     └── LogicalTag (逻辑点位，公式计算)
```

### 2.2 数据模型（SQLModel / PostgreSQL）

```python
# backend/app/models/node.py
from sqlmodel import SQLModel, Field, Relationship
from typing import Optional
from enum import Enum
from datetime import datetime


class NodeType(str, Enum):
    SITE = "site"              # 场站
    STATION = "station"        # 电站
    ENERGY_NODE = "energy_node"# 能源节点
    DEVICE = "device"          # 设备
    TAG = "tag"                # 点位


class EnergyNodeType(str, Enum):
    ESS = "ess"               # 储能
    PV = "pv"                 # 光伏
    GRID = "grid"             # 电网
    EVSE = "evse"             # 充电桩


class TagType(str, Enum):
    PHYSICAL = "physical"     # 物理点位（Neuron 采集）
    LOGICAL = "logical"       # 逻辑点位（公式计算）


class Node(SQLModel, table=True):
    """五层节点树 — 统一模型"""
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # 树结构
    name: str = Field(index=True)
    node_type: NodeType
    parent_id: Optional[int] = Field(default=None, foreign_key="node.id")
    
    # 能源节点专用
    energy_node_type: Optional[EnergyNodeType] = None
    
    # 属性（通用）
    description: Optional[str] = None
    config: dict = Field(default_factory=dict)  # 灵活配置JSON
    
    # 元数据
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    # 关系
    parent: Optional["Node"] = Relationship(back_populates="children")
    children: list["Node"] = Relationship(back_populates="parent")
    
    # 设备级扩展
    device_profile_id: Optional[str] = None   # 关联品牌模板
    neuron_node_name: Optional[str] = None    # Neuron node 名
    
    # 点位级扩展
    tag_type: Optional[TagType] = None
    unit: Optional[str] = None                 # 单位
    data_type: str = "float"                  # float/int/bool/string/enum
    
    # 物理点位扩展
    neuron_group: Optional[str] = None         # Neuron group 名
    neuron_tag: Optional[str] = None          # Neuron tag 名
    register_address: Optional[str] = None     # 寄存器地址（参考）
    scale_factor: float = 1.0                  # 缩放因子
    offset: float = 0.0                        # 偏移量
    
    # 逻辑点位扩展
    formula: Optional[str] = None              # 公式表达式
    formula_type: Optional[str] = None         # expression / aggregate / condition
    source_tag_paths: list[str] = Field(default_factory=list)  # 依赖的点位路径列表
    aggregation: Optional[str] = None          # SUM / AVG / MAX / MIN / COUNT
    
    __table_args__ = (
        # 确保同父节点下名字唯一
        {"comment": "五层统一节点树"},
    )
```

```python
# backend/app/models/rule.py
from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class RuleType(str, Enum):
    ALARM = "alarm"            # 告警规则
    CONTROL = "control"        # 控制策略
    FAULT_MAP = "fault_map"    # 故障码映射
    LINKAGE = "linkage"        # 联动规则
    CUSTOM = "custom"          # 自定义决策


class Rule(SQLModel, table=True):
    """GoRules 规则定义"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str                                           # 规则名称（唯一键）
    rule_type: RuleType
    bind_node_id: int = Field(foreign_key="node.id")    # 绑定到的节点
    
    # JDM 内容（GoRules 格式）
    jdm_content: dict                                   # Decision Table 或 Graph 的 JSON
    jdm_version: int = 1                                # 版本号
    
    # 运行时状态
    enabled: bool = True
    last_eval_at: Optional[datetime] = None             # 最后评估时间
    eval_count: int = 0                                 # 累计评估次数
    
    # 元数据
    description: Optional[str] = None
    created_by: str = "system"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    # 关系
    bind_node: "Node" = Relationship()
```

### 2.3 节点树示例（JSON 表示）

```json
{
  "id": 1,
  "name": "某某工业园",
  "node_type": "site",
  "children": [
    {
      "id": 10,
      "name": "1号光储充站",
      "node_type": "station",
      "parent_id": 1,
      "children": [
        {
          "id": 100,
          "name": "储能系统",
          "node_type": "energy_node",
          "energy_node_type": "ess",
          "parent_id": 10,
          "children": [
            {
              "id": 1001,
              "name": "PCS #1",
              "node_type": "device",
              "device_profile_id": "en9_pcs",
              "neuron_node_name": "en9_pcs_01",
              "parent_id": 100,
              "children": [
                {
                  "id": 100101,
                  "name": "有功功率",
                  "node_type": "tag",
                  "tag_type": "physical",
                  "data_type": "float",
                  "unit": "kW",
                  "neuron_group": "data",
                  "neuron_tag": "activePower",
                  "scale_factor": 0.001,
                  "parent_id": 1001
                },
                {
                  "id": 100102,
                  "name": "储能总功率(虚拟)",
                  "node_type": "tag",
                  "tag_type": "logical",
                  "data_type": "float",
                  "unit": "kW",
                  "formula_type": "aggregate",
                  "aggregation": "SUM",
                  "source_tag_paths": [
                    "/site/1/station/10/ess/100/device/1001/tag/100101",
                    "/site/1/station/10/ess/100/device/1002/tag/100201"
                  ],
                  "formula": "SUM(activePower_kW)",
                  "parent_id": 100
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

---

## 3. 点位系统设计

### 3.1 两类点位对比

| 维度 | PhysicalTag（物理） | LogicalTag（逻辑/虚拟） |
|------|--------------------|-----------------------|
| 数据来源 | Neuron MQTT 上报 | 引擎计算派生 |
| 更新频率 | 跟随采集周期(1s/500ms/2s) | 依赖源点位更新后立即触发 |
| 用户配置 | 选择 Neuron node/group/tag | 写表达式或选聚合方式 |
| 是否可写 | 是（通过 Neuron REST API） | 否（只读） |
| 存储位置 | Hypertable `t_telemetry` | 同表，`is_virtual=true` 标记 |

### 3.2 物理点位映射流程

```
Neuron 上报 MQTT payload:
{
  "node_name": "en9_pcs_01",
  "values": {
    "activePower": 45000,       ← 原始寄存器值
    "soc": 852,                  ← 原始值 (实际 85.2%)
    "dcVoltage": 7200            ← mV → 7.200 V
  }
}
         ↓ paho-mqtt 收到
    Data Normalizer:
    1. 根据 neuron_node_name 找到 Device 记录
    2. 遍历 values 中每个 key
    3. 匹配 PhysicalTag 的 neuron_tag 字段
    4. 应用 scale_factor 和 offset
    5. 输出归一化后的标准字段名
         ↓
    归一化结果:
    {
      "device_path": "/site/1/station/10/ess/100/device/1001",
      "timestamp": "2026-07-13T22:00:00Z",
      "values": {
        "activePower_kW": 45.0,     ← 45000 * 0.001
        "soc_pct": 85.2,             ← 852 * 0.1
        "dcVoltage_v": 7.200         ← 7200 * 0.001
      }
    }
```

### 3.3 逻辑点位计算引擎

```python
# backend/app/core/virtual_point_engine.py
"""
虚拟点位计算引擎 — 在进程内运行，收到物理点位更新后级联触发
"""
import asyncio
import ast
import operator
from typing import Any
from collections import defaultdict


class VirtualPointEngine:
    """
    支持三种公式类型：
    - expression: 单节点内数学表达式 (a * b + c)
    - aggregate:  跨子节点聚合 (SUM / AVG / MAX / MIN)
    - condition:  条件判断输出布尔值
    """
    
    # 安全的运算符白名单
    _OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.Gt: operator.gt,
        ast.Ge: operator.ge,
        ast.Lt: operator.lt,
        ast.Le: operator.le,
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.And: lambda a,b: bool(a) and bool(b),
        ast.Or: lambda a,b: bool(a) or bool(b),
        ast.Not: operator.not_,
    }
    
    _AGG_OPS = {
        'SUM': sum,
        'AVG': lambda x: sum(x)/len(x) if x else 0,
        'MAX': max,
        'MIN': min,
        'COUNT': len,
        'ANY': any,
        'ALL': all,
    }
    
    def __init__(self, db):
        self.db = db
        self._latest_values: dict[str, dict] = {}  # path -> {field: value}
        self._dependents: dict[str, list[str]] = defaultdict(list)  # path -> [dependent_vp_paths]
        self._vp_cache: dict[str, tuple] = {}  # vp_path -> (formula, source_paths, formula_type)
        
    def register_logical_tags(self, tags: list[dict]):
        """启动时注册所有逻辑点位及其依赖关系"""
        for tag in tags:
            path = tag['path']
            sources = tag.get('source_tag_paths', [])
            self._vp_cache[path] = (
                tag.get('formula', ''),
                sources,
                tag.get('formula_type', 'expression'),
                tag.get('aggregation'),
                tag.get('source_field'),  # 聚合时的取值字段
            )
            for src in sources:
                self._dependents[src].append(path)
    
    async def on_physical_update(self, device_path: str, field: str, value: Any, timestamp):
        """
        物理点位更新回调
        1. 缓存最新值
        2. 找到依赖此点位的所有逻辑点位
        3. 级联计算（支持链式依赖 A→B→C）
        4. 返回所有被更新的点位及值
        """
        full_path = f"{device_path}.{field}"
        self._latest_values.setdefault(device_path, {})[field] = value
        
        updated = {}
        await self._cascade_compute(full_path, visited=set(), results=updated)
        return updated
    
    async def _cascade_compute(self, trigger_path: str, visited: set, results: dict):
        """级联计算依赖此路径的所有逻辑点位"""
        for vp_path in self._dependents.get(trigger_path, []):
            if vp_path in visited:
                continue  # 防止循环依赖
            visited.add(vp_path)
            
            formula, sources, formula_type, agg_op, source_field = self._vp_cache[vp_path]
            
            try:
                if formula_type == 'expression':
                    value = self._eval_expression(formula, sources)
                elif formula_type == 'aggregate':
                    value = self._eval_aggregate(sources, agg_op, source_field)
                elif formula_type == 'condition':
                    value = bool(self._eval_expression(formula, sources))
                else:
                    continue
                    
                results[vp_path] = value
                # 此逻辑点位的更新可能触发更上层的逻辑点位
                await self._cascade_compute(vp_path, visited, results)
                
            except Exception as e:
                print(f"[VP] 计算失败 {vp_path}: {e}")
    
    def _eval_expression(self, formula: str, source_paths: list[str]) -> Any:
        """
        安全的表达式求值
        支持: 数字运算、比较、逻辑运算、括号
        不支持: 函数调用、属性访问、import
        """
        # 从缓存中提取源点位变量
        variables = {}
        for sp in source_paths:
            parts = sp.rsplit('.', 1)
            if len(parts) == 2:
                dev_path, field = parts
                val = self._latest_values.get(dev_path, {}).get(field)
                # 用字段名的最后一部分作为变量名
                var_name = field.split('.')[-1]
                variables[var_name] = val
        
        # 安全 AST 求值
        tree = ast.parse(formula, mode='eval')
        return self._eval_node(tree.body, variables)
    
    def _eval_node(self, node, variables):
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Name):
            if node.id not in variables:
                raise NameError(f"未定义变量: {node.id}")
            return variables[node.id]
        elif isinstance(node, ast.BinOp):
            left = self._eval_node(node.left, variables)
            right = self._eval_node(node.right, variables)
            op = self._OPS.get(type(node.op))
            if not op: raise TypeError(f"不支持的操作符: {type(node.op)}")
            return op(left, right)
        elif isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand, variables)
            op = self._OPS.get(type(node.op))
            return op(operand)
        elif isinstance(node, ast.Compare):
            left = self._eval_node(node.left, variables)
            for op, comparator in zip(node.ops, node comparators):
                right = self._eval_node(comparator, variables)
                op_func = self._OPS.get(type(op))
                if not op_func or not op_func(left, right):
                    return False
                left = right
            return True
        elif isinstance(node, ast.BoolOp):
            values = [self._eval_node(v, variables) for v in node.values]
            op = self._OPS.get(type(node.op))
            return op(values)
        else:
            raise TypeError(f"不支持的表达式节点: {type(node)}")
    
    def _eval_aggregate(self, source_paths: list[str], 
                         agg_op: str, source_field: str) -> Any:
        """跨设备聚合计算"""
        values = []
        for sp in source_paths:
            parts = sp.rsplit('.', 1)
            if len(parts) == 2:
                dev_path, field = parts
                actual_field = source_field or field.split('.')[-1]
                val = self._latest_values.get(dev_path, {}).get(actual_field)
                if val is not None:
                    values.append(val)
        
        func = self._AGG_OPS.get(agg_op.upper())
        if not func or not values:
            return None
        return func(values)
```

---

## 4. GoRules 规则引擎集成

### 4.1 架构位置

```
FastAPI 进程内:

paho-mqtt 回调
    ↓ 收到遥测
Data Normalizer（归一化）
    ↓
Virtual Point Engine（计算逻辑点位）
    ↓
┌─────────────────────────────┐
│  RulesService (GoRules)      │
│                              │
│  evaluate_alarm(context)     │  → 告警决策表
│  evaluate_control(context)   │  → 控制策略图
│  evaluate_fault(code)        │  → 故障码映射表
│  evaluate_linkage(event)     │  → 联动规则图
│                              │
│  微秒级延迟 · Rust 核心       │
│  JDM JSON 格式               │
│  热更新（不改版无需重启）      │
└──────────┬──────────────────┘
           ↓ decision result
Alarm Dispatcher / RPC Controller
    ↓
TimescaleDB 入库 + WebSocket 推送
```

### 4.2 RulesService 实现

```python
# backend/app/core/rules_service.py
import json
import zen
from typing import Any, Optional
from datetime import datetime


class RulesService:
    """
    GoRules ZEN Engine 封装
    - 启动时从 DB 加载所有 enabled 规则
    - 提供 evaluate 方法供业务调用
    - 支持热更新单个规则
    """
    
    def __init__(self, db_session_factory):
        self.db = db_session_factory
        self._cache: dict[str, str] = {}  # rule_name -> jdm_json_string
        self.engine: Optional[zen.ZenEngine] = None
        self._init_engine()
    
    def _init_engine(self):
        """初始化引擎（应用启动时调用一次）"""
        self.engine = zen.ZenEngine({
            'loader': self._load_from_cache
        })
    
    def _load_from_cache(self, key: str) -> str:
        """引擎内部加载回调 — 从内存缓存读取"""
        content = self._cache.get(key)
        if content is None:
            raise KeyError(f"Rule not found: {key}")
        return content
    
    def load_rules_from_db(self):
        """启动时 / 定时从 DB 加载所有 enabled 规则到缓存"""
        with self.db() as session:
            rules = session.exec(
                select(Rule).where(Rule.enabled == True)
            ).all()
            
            new_cache = {}
            for r in rules:
                key = f"{r.name}_v{r.jdm_version}"
                new_cache[key] = json.dumps(r.jdm_content, ensure_ascii=False)
            
            self._cache = new_cache
            print(f"[GoRules] 已加载 {len(new_cache)} 条规则")
    
    async def evaluate(self, rule_name: str, context: dict) -> Any:
        """
        执行规则评估
        :param rule_name: 规则名称（不含版本后缀）
        :param context: 评估上下文（遥测数据+虚拟点位值）
        :return: 评估结果（dict / list[dict] / None）
        """
        key = self._resolve_key(rule_name)
        try:
            result = self.engine.evaluate(key, context)
            # 更新统计
            self._update_eval_stats(rule_name)
            return result
        except Exception as e:
            print(f"[GoRules] 评估失败 [{rule_name}]: {e}")
            return None
    
    async def evaluate_alarm(self, context: dict) -> list[dict]:
        """告警评估（hitPolicy=collect，返回所有匹配项）"""
        result = await self.evaluate("ems-alarm-rules", context)
        if result and isinstance(result, list):
            return result
        return []
    
    async def evaluate_control(self, context: dict) -> Optional[dict]:
        """控制策略评估（hitPolicy=first，返回第一个匹配的动作）"""
        return await self.evaluate("ess-control-policy", context)
    
    async def evaluate_fault_code(self, fault_code: int, device_type: str) -> Optional[dict]:
        """故障码映射"""
        rule_name = f"{device_type}-fault-map"
        return await self.evaluate(rule_name, {"fault_code_int": fault_code})
    
    def hot_reload(self, rule_name: str, new_jdm: dict, version: int):
        """热更新单条规则（JDM Editor 保存后调用）"""
        key = f"{rule_name}_v{version}"
        self._cache[key] = json.dumps(new_jdm, ensure_ascii=False)
        print(f"[GoRules] 热更新: {rule_name} → v{version}")
    
    def _resolve_key(self, rule_name: str) -> str:
        """解析最新的规则 key"""
        # 找最高版本的
        ver = 1
        while f"{rule_name}_v{ver}" in self._cache:
            ver += 1
        return f"{rule_name}_v{ver - 1}" if ver > 1 else f"{rule_name}_v1"
    
    def _update_eval_stats(self, rule_name: str):
        """异步更新评估统计（不阻塞主流程）"""
        pass  # 可用 background task 异步写 DB
```

### 4.3 内置规则模板

#### 4.3.1 EMS 告警规则表

```json
{
  "name": "ems-alarm-rules-template",
  "type": "decision-table",
  "hitPolicy": "collect",
  "inputs": [
    {"field": "soc_pct", "label": "SOC %", "type": "number"},
    {"field": "max_temp_c", "label": "BMS Max Temp C", "type": "number"},
    {"field": "power_kW", "label": "Active Power kW", "type": "number"},
    {"field": "grid_power_kW", "label": "Grid Power kW", "type": "number"}
  ],
  "outputs": [
    {"field": "level", "label": "Severity", "type": "string"},
    {"field": "code", "label": "Alarm Code", "type": "string"},
    {"field": "action", "label": "Auto Action", "type": "string"},
    {"field": "message", "label": "Message", "type": "string"}
  ],
  "rules": [
    {
      "input": [">= 95", "> 60", null, null],
      "output": ["CRITICAL", "ESS_OVER_SOC_TEMP", "emergency_stop", "储能过充+高温，紧急停机"]
    },
    {
      "input": [">= 95", null, null, null],
      "output": ["MAJOR", "ESS_OVER_SOC", "limit_charge", "SOC >= 95%，限制充电"]
    },
    {
      "input": ["<= 10", null, null, null],
      "output": ["MAJOR", "ESS_LOW_SOC", "limit_discharge", "SOC <= 10%，限制放电"]
    },
    {
      "input": [null, "> 55", null, null],
      "output": ["MAJOR", "BMS_OVER_TEMP", "reduce_power", "BMS 温度 > 55°C，降功率运行"]
    },
    {
      "input": [null, "> 65", null, null],
      "output": ["CRITICAL", "BMS_CRITICAL_TEMP", "emergency_stop", "BMS 临界温度，紧急停机"]
    },
    {
      "input": [null, null, null, "< -50"],
      "output": ["WARNING", "REVERSE_POWER", "reduce_pv", "检测到逆功率，降低光伏出力"]
    },
    {
      "input": [null, null, "< -100", null],
      "output": ["MAJOR", "OVER_DISCHARGE", "limit_discharge", "放电超限"]
    }
  ]
}
```

#### 4.3.2 储能控制策略图

```json
{
  "name": "ess-control-policy-template",
  "type": "graph",
  "nodes": [
    {
      "id": "input",
      "type": "input",
      "schema": {
        "soc_pct": "number",
        "max_temp_c": "number",
        "power_kW": "number",
        "grid_status": "string",
        "time_of_day": "string",
        "peak_price_flag": "bool"
      }
    },
    {
      "id": "check_safety",
      "type": "switch",
      "branches": [
        {"condition": "max_temp_c > 65 OR soc_pct >= 98", "target": "emergency_stop"},
        {"condition": "max_temp_c > 55 OR soc_pct >= 95 OR soc_pct <= 10", "target": "limit_mode"},
        {"condition": "true", "target": "normal_operation"}
      ]
    },
    {
      "id": "emergency_stop",
      "type": "expression",
      "output": {
        "pcs_command": 3,
        "charge_limit_kw": 0,
        "discharge_limit_kw": 0,
        "reason": "Emergency stop triggered"
      }
    },
    {
      "id": "limit_mode",
      "type": "expression",
      "output": {
        "pcs_command": 2,
        "charge_limit_kW": "soc_pct >= 95 ? 0 : 20",
        "discharge_limit_kW": "soc_pct <= 10 ? 0 : 20",
        "reason": "Limit mode - auto calculated"
      }
    },
    {
      "id": "normal_operation",
      "type": "decision-table",
      "ref": "ess-normal-operation-table"
    }
  ]
}
```

---

## 5. 技术组件与数据流

### 5.1 Docker Compose 编排

```yaml
# docker-compose.yml
version: "3.9"

services:
  # === 南向采集网关 ===
  neuron:
    image: emqx/neuron:latest
    hostname: neuron
    ports:
      - "7000:7000"    # Web UI + REST API
    volumes:
      - neuron_data:/opt/neuron/data
    restart: unless-stopped
    networks: [claw-net]

  # === MQTT 消息总线 ===
  nanomq:
    image: emqx/nanomq:latest
    ports:
      - "1883:1883"    # MQTT TCP
      - "8883:8883"    # MQTT WebSocket
    volumes:
      - nanomq_conf:/etc/nanomq
    command: nanomq start --conf /etc/nanomq/nanomq.conf
    restart: unless-stopped
    depends_on: [neuron]
    networks: [claw-net]

  # === 时序数据库 + 关系数据库 ===
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_USER: claw
      POSTGRES_PASSWORD: ${DB_PASSWORD:-claw_dev_2026}
      POSTGRES_DB: claw_iot
    ports:
      - "5432:5432"
    volumes:
      - tsdb_data:/var/lib/postgresql/data
      - ./init-db:/docker-entrypoint-initdb.d
    restart: unless-stopped
    networks: [claw-net]

  # === Redis (可选 Phase 5) ===
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    restart: unless-stopped
    profiles: ["optional"]
    networks: [claw-net]

  # === 平台核心 (FastAPI + React) ===
  platform-backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    environment:
      DATABASE_URL: postgresql://claw:${DB_PASSWORD:-claw_dev_2026}@timescaledb:5432/claw_iot
      MQTT_BROKER_HOST: nanomq
      MQTT_BROKER_PORT: 1883
      NEURON_URL: http://neuron:7000
      REDIS_URL: redis://redis:6379/0
    ports:
      - "8000:8000"    # FastAPI + Uvicorn
    volumes:
      - ./backend/app:/app/app
    restart: unless-stopped
    depends_on: [timescaledb, nanomq, neuron]
    networks: [claw-net]

  platform-frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    environment:
      VITE_API_BASE_URL: http://localhost:8000
      VITE_WS_URL: ws://localhost:8000/ws
    ports:
      - "3000:3000"    # Vite dev server / nginx
    restart: unless-stopped
    depends_on: [platform-backend]
    networks: [claw-net]

volumes:
  neuron_data:
  nanomq_conf:
  tsdb_data:
  redis_data:

networks:
  claw-net:
    driver: bridge
```

### 5.2 上行数据流（完整）

```
时间线（单条遥测消息处理）:

T+0ms    Neuron 发布 MQTT
           topic: telemetry/en9_pcs_01
           payload: {"ts": 1720876800000, "values": {"activePower": 45000, ...}}

T+1ms    nanoMQ 转发

T+2ms    paho-mqtt on_message 回调
         
T+3ms    Data Normalizer:
           - 查询 Device 表匹配 neuron_node_name="en9_pcs_01"
           - 应用 scale_factor (45000 * 0.001 → 45.0)
           - 输出: {device_path: "...", values: {activePower_kW: 45.0, ...}}

T+5ms    写入 TimescaleDB t_telemetry Hypertable:
           INSERT INTO t_telemetry (ts, node_path, tag_name, value, is_virtual)
           VALUES (now(), '/.../pcs_01', 'activePower_kW', 45.0, false)

T+8ms    Virtual Point Engine.on_physical_update():
           - 缓存 latest_values['...pcs_01'] = {activePower_kW: 45.0, ...}
           - 查找依赖此点位的逻辑点位（如 ess_total_power）
           - 计算: SUM(pcs_01.activePower_kW, pcs_02.activePower_kW) = 87.5
           - 递归查找依赖 ess_total_power 的上层逻辑点位

T+12ms   写入虚拟点位到 TSDB (is_virtual=true)

T+15ms   GoRules evaluate_alarm():
           - 传入上下文: {soc_pct: 85.2, max_temp_c: 42.3, power_kW: 45.0, ...}
           - Rust 引擎执行 JDM 决策表
           - 结果: [] (无告警触发)

T+17ms   WebSocket 广播给所有订阅此设备的浏览器客户端:
           WS → {type: "telemetry", path: "...", values: {...}, vpoints: {...}, alarms: []}

总计 ~20ms（含 DB 写入和规则评估）
```

### 5.3 旧版下行数据流（RPC 控制，已废止）

```
时间线:

T+0ms    用户在前端点击 RPC 按钮 "PCS 停机"
         
T+1ms    Frontend → WebSocket/FastAPI POST /api/v1/devices/{id}/rpc
           body: {method: "remote_control", params: "3"}  (3=停机)

T+3ms    RPC Controller:
           - 权限校验（当前用户是否有此设备控制权限）
           - 参数验证
           - 查询 Device 记录获取 neuron_node_name

T+5ms    GoRules evaluate_control() (可选的前置策略校验):
           - 当前上下文是否允许执行此操作？
           - 返回 approve/deny/modify

T+8ms    paho-mqtt publish:
           topic: command/en9_pcs_01
           payload: {tag: "remote_control", value: 3, source: "user:admin"}

T+10ms   nanoMQ → Neuron (Neuron 订阅了 command/{node_name} topic)

T+12ms   Neuron REST API: POST /api/v2/write
           body: {node: "en9_pcs_01", group: "cmd", tag: "remote_control", value: 3}

T+50~200ms  Modbus RTU/TCP 写入 PCS 寄存器
           
T+52ms   PCS 响应成功 → Neuron → nanoMQ (可选: response topic)
           
T+55ms   (可选) FastAPI 收到响应 → WebSocket 推送给前端按钮 "操作成功"

总计 ~55~205ms（主要耗时在 Modbus 通信）
```

### 5.4 TimescaleDB Schema

```sql
-- init-db/001-schema.sql

-- ====== 1. 关系型表（SQLModel 管理）======

-- Node 表由 ORM 自动创建（见 models/node.py）

-- Rule 表由 ORM 自动创建（见 models/rule.py）

-- ====== 2. 时序表（Hypertable）=======

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 遥测主表（物理点位 + 虚拟点位共用）
CREATE TABLE t_telemetry (
    time        TIMESTAMPTZ       NOT NULL,
    node_path   TEXT              NOT NULL,  -- 完整节点路径
    tag_name    TEXT              NOT NULL,  -- 点位名称（标准化字段名）
    value       DOUBLE PRECISION,            -- 数值（字符串/布尔另存）
    value_str   TEXT,                        -- 字符串值备用
    quality     SMALLINT          NOT NULL DEFAULT 192,  -- OPC UA Quality: 192=Good
    is_virtual  BOOLEAN           NOT NULL DEFAULT FALSE
);

-- 按 time 列创建 Hypertable（分区键）
SELECT create_hypertable('t_telemetry', 'time');

-- 复合索引（查询性能关键）
CREATE INDEX idx_tele_node_tag_time ON t_telemetry (node_path, tag_name, time DESC);
CREATE INDEX idx_tele_virtual ON t_telemetry (is_virtual, time DESC);

-- ====== 3. 告警记录表 ======

CREATE TABLE t_alarms (
    id          BIGSERIAL PRIMARY KEY,
    time        TIMESTAMPTZ       NOT NULL,
    node_path   TEXT              NOT NULL,
    rule_name   TEXT              NOT NULL,
    level       TEXT              NOT NULL,  -- CRITICAL / MAJOR / WARNING / INFO
    code        TEXT              NOT NULL,
    message     TEXT              NOT NULL,
    context     JSONB,                       -- 触发时的完整上下文快照
    acknowledged BOOLEAN DEFAULT FALSE,
    ack_time    TIMESTAMPTZ,
    ack_by      TEXT,
    resolved    BOOLEAN DEFAULT FALSE,
    resolve_time TIMESTAMPTZ
);

SELECT create_hypertable('t_alarms', 'time');
CREATE INDEX idx_alarms_level ON t_alarms (level, time DESC);
CREATE INDEX idx_alarms_active ON t_alarms (resolved, time DESC) WHERE resolved = FALSE;

-- ====== 4. 操作审计日志 ======

CREATE TABLE t_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    time        TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    actor       TEXT              NOT NULL,  -- user:admin / system:rules / system:scheduler
    action      TEXT              NOT NULL,  rpc / config_change / rule_update / alarm_ack
    target_type TEXT,                        -- device / node / rule / site
    target_id   TEXT,
    detail      JSONB,
    result      TEXT                         -- success / failed / timeout
);

SELECT create_hypertable('t_audit_log', 'time');

-- ====== 5. 连续聚合视图（历史统计）=======

-- 设备级小时聚合（用于趋势图）
CREATE MATERIALIZED VIEW v_device_hourly_agg AS
SELECT 
    time_bucket('1 hour', time) AS bucket,
    node_path,
    tag_name,
    avg(value) as avg_val,
    min(value) as min_val,
    max(value) as max_val,
    count(*) as sample_count
FROM t_telemetry
WHERE is_virtual = false
GROUP BY 1, 2, 3;

-- 转为 Continuous Aggregate（自动刷新）
-- SELECT add_continuous_aggregate_policy('v_device_hourly_agg',
--     start_offset => INTERVAL '3 months',
--     end_offset => INTERVAL '1 hour',
--     schedule_interval => INTERVAL '1 hour');
```

---

## 6. API 设计

### 6.1 节点树 API

```
GET    /api/v1/nodes                          # 获取完整节点树（扁平列表含 parent_id）
POST   /api/v1/nodes                          # 创建节点（任意层级）
GET    /api/v1/nodes/{id}                      # 获取节点详情（含子节点和点位）
PUT    /api/v1/nodes/{id}                      # 更新节点
DELETE /api/v1/nodes/{id}                      # 删除节点（级联删除子节点）

GET    /api/v1/nodes/{id}/tree                 # 获取以该节点为根的子树
POST   /api/v1/nodes/{id}/children             # 批量创建子节点
POST   /api/v1/nodes/import                    # 从 CSV/YAML 导入节点树模板
GET    /api/v1/nodes/export?root={id}&format=yaml  # 导出节点树
```

### 6.2 点位 API

```
GET    /api/v1/nodes/{id}/tags                 # 获取某节点的所有点位
POST   /api/v1/nodes/{id}/tags                 # 创建点位
PUT    /api/v1/tags/{tag_id}                   # 更新点位配置
DELETE /api/v1/tags/{tag_id}                   # 删除点位

POST   /api/v1/tags/import-neuron              # 从 Neuron 同步物理点位
  body: {neuron_node_name: "en9_pcs_01"}       # 自动扫描并导入

GET    /api/v1/telemetry?paths={path1,path2}&fields=f1,f2&from=T&to=T&agg=raw|1m|1h|1d
                                            # 查询历史遥测数据
GET    /api/v1/telemetry/latest?paths={path1,path2}  # 最新值批量查询
```

### 6.3 规则 API

```
GET    /api/v1/rules                           # 规则列表（可按 bind_node_id 过滤）
POST   /api/v1/rules                           # 创建规则
GET    /api/v1/rules/{id}                      # 规则详情（含 JDM 内容）
PUT    /api/v1/rules/{id}                      # 更新规则（自动版本+1）
DELETE /api/v1/rules/{id}                      # 删除规则

PUT    /api/v1/rules/{id}/jdm                   # 更新 JDM 内容（热更新）
  body: {jdm_content: {...}}                   # 前端 JDM Editor 保存时调用

POST   /api/v1/rules/{id}/simulate             # 模拟执行（传测试数据看结果）
  body: {test_context: {soc_pct: 96, max_temp_c: 58}}

GET    /api/v1/rules/templates                 # 内置规则模板列表
POST   /api/v1/rules/from-template/{name}      # 从模板创建规则（填参数即可）
```

### 6.4 旧版控制 API（已废止）

当前 `POST /api/v1/devices/{node_id}/rpc` 的新形态接受 `entity_instance_id` 和 `value`；
受限旧形态只允许 `command` 精确匹配已确认实体实例的定义 ID，并读取 `payload.value`。
两种形态都会创建可查询控制命令，绝不会发布任意 MQTT topic。`POST /api/v1/neuron/write`
同样先唯一映射到已确认的实体实例。二者的 `201` 不是现场成功，只有控制命令达到
`readback_confirmed` 才是成功。详见 ADR-0007。

```
POST   /api/v1/devices/{id}/rpc                # 发送 RPC 控制命令
  body: {method: "remote_control", params: "3"}
  
GET    /api/v1/devices/{id}/rpc/history        # 控制命令历史

POST   /api/v1/batch-rpc                       # 批量控制（同一能源节点下多台设备）
  body: {node_path: "...", commands: [{device_id, method, params}, ...]}
```

### 6.5 实时通信

```
WS     /ws/telemetry                          # WebSocket 遥测推送
       订阅: {"action": "subscribe", "paths": [...]}
       服务端推送: {"type": "telemetry", "path": "...", "values": {...}, "ts": "..."}
       
WS     /ws/alarms                              # WebSocket 告警推送
       服务端推送: {"type": "alarm", "level": "MAJOR", "message": "...", "path": "..."}

WS     /ws/rpc-response                       # RPC 控制结果回推
       服务端推送: {"type": "rpc_result", "request_id": "...", "success": true, "value": ...}
```

---

## 7. 前端配置交互设计

### 7.1 节点树配置页面

```
┌─────────────────────────────────────────────────────────┐
│  节点管理                              [+ 新建站点]      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  🏭 某某工业园 (Site)                                   │
│    └── 📦 1号光储充站 (Station)                          │
│         ├── 🔋 储能系统 (ESS)                            │
│         │    ├── 📟 PCS #1                             │
│         │    │    ├── ⚡ 有功功率 (physical, kW)         │
│         │    │    ├── 🔋 SOC (physical, %)              │
│         │    │    └── 📊 储能总功率 (virtual, Σ)         │
│         │    ├── 📟 BMS #1                             │
│         │    └── ⚡ 电表                               │
│         ├── ☀️ 光伏系统 (PV)                            │
│         │    └── 📟 逆变器 #1                          │
│         ├── 🔌 电网接入 (GRID)                         │
│         └── 🚗 充电桩 (EVSE)                            │
│                                                         │
│  ─────────────────────────────────────────────────────  │
│  选中: PCS #1                                          │
│  [编辑] [同步Neuron点位] [添加物理点位] [添加逻辑点位]   │
│  [绑定规则: ems-alarm-rules v3] [新建规则]              │
└─────────────────────────────────────────────────────────┘
```

### 7.2 物理点位配置弹窗

```
┌─ 配置物理点位 ───────────────────────────────┐
│                                               │
│  名称:     [有功功率_____________]            │
│  标识符:   activePower_kW                    │
│  数据类型: [float ▼]   单位: [kW___]         │
│                                               │
│  Neuron 映射:                                 │
│    Node:    [en9_pcs_01 ▼]                   │
│    Group:   [data ▼]                         │
│    Tag:     [activePower ▼]                  │
│                                               │
│  数据转换:                                    │
│    缩放因子: [0.001____]  原始值×系数=工程值  │
│    偏移量:   [0________]                      │
│                                               │
│  示例: 原始值 45000 → 45.0 kW                │
│                                               │
│              [取消]  [保存并生效]               │
└───────────────────────────────────────────────┘
```

### 7.3 逻辑点位配置弹窗

```
┌─ 配置逻辑点位 ───────────────────────────────┐
│                                               │
│  名称:     [储能总功率__________]             │
│  标识符:   ess_total_power_kW                │
│  数据类型: [float ▼]   单位: [kW___]         │
│                                               │
│  公式类型:                                     │
│  ○ 数学表达式                                 │
│  ○ 跨设备聚合                                 │
│  ○ 条件判断                                   │
│                                               │
│  ── 当选择"跨设备聚合" ─────────              │
│  聚合方式: [SUM ▼]                            │
│                                               │
│  源点位:                                       │
│  ☑ PCS #1 → activePower_kW                   │
│  ☑ PCS #2 → activePower_kW                   │
│  ☐ BMS #1 → dischargePower_kW               │
│                                               │
│  (+ 从节点树选择更多点位)                      │
│                                               │
│  预览公式: SUM(activePower_kW)                │
│                                               │
│              [取消]  [保存并生效]               │
└───────────────────────────────────────────────┘
```

### 7.4 规则配置页面（嵌入 JDM Editor）

```
┌─ 规则管理: ems-alarm-rules v3 ──────────────┐
│                                              │
│  类型: [告警规则 ▼]  绑定节点: 储能系统       │
│  状态: ✅ 已启用  最后评估: 2秒前             │
│                                              │
│  ═══ JDM 可视化编辑器 ═══                    │
│  ┌────────────────────────────────────────┐ │
│  │  ┌─ Input ─┐  ┌─ Output ──────────┐   │ │
│  │  │ SOC %   │  │ Severity │ Action  │   │ │
│  │  │ MaxTemp │  │ Message  │         │   │ │
│  │  │ Power   │  │          │         │   │ │
│  │  ├─────────┤  ├──────────┴─────────┤   │ │
│  │  │ ≥ 95  >60│  │ CRITICAL │ emergency│   │ │
│  │  │ ≥ 95  *  │  │ MAJOR    │ limit_   │   │ │
│  │  │ *     >55│  │ WARNING  │ reduce_  │   │ │
│  │  │ *     <-50│  │ ...      │ ...     │   │ │
│  │  └─────────┘  └─────────────────────┘   │ │
│  │                                         │ │
│  │  [+ 添加规则行]  [- 删除选中行]          │ │
│  └────────────────────────────────────────┘ │
│                                              │
│  [▶ 模拟测试]  [🔄 版本历史]                  │
│              [保存]  [保存并启用]              │
└──────────────────────────────────────────────┘
```

---

## 8. 目录结构（项目骨架）

```
claw-platform/
├── docker-compose.yml
├── .env
│
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml              # uv / pip 项目配置
│   ├── alembic.ini                 # DB 迁移
│   └── app/
│       ├── main.py                 # FastAPI app 入口
│       ├── config.py               # Settings (pydantic-settings)
│       │
│       ├── core/                   # 核心引擎
│       │   ├── mqtt_client.py      # paho-mqtt 连接管理
│       │   ├── normalizer.py       # 数据归一化
│       │   ├── virtual_engine.py   # 逻辑点位计算引擎
│       │   ├── rules_service.py    # GoRules 封装
│       │   ├── alarm_manager.py    # 告警分发
│       │   └── rpc_controller.py   # 下行控制
│       │
│       ├── models/                 # SQLModel 数据模型
│       │   ├── node.py            # 五层节点树
│       │   ├── rule.py            # 规则定义
│       │   └── user.py            # 用户/权限
│       │
│       ├── api/                    # API 路由
│       │   ├── nodes.py           # 节点 CRUD
│       │   ├── tags.py            # 点位 CRUD
│       │   ├── telemetry.py       # 遥测查询
│       │   ├── rules.py           # 规则 CRUD + simulate
│       │   ├── rpc.py             # 控制 API
│       │   └── websocket.py       # WS 端点
│       │
│       ├── services/               # 业务服务层
│       │   ├── node_tree_service.py
│       │   ├── tag_sync_service.py # Neuron 点位同步
│       │   └── rule_template_service.py
│       │
│       └── db/
│           ├── session.py         # DB 会话管理
│           └── init_db.py         # 建表脚本
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── bun.lock
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── lib/
│       │   ├── api-client.ts      # OpenAPI 自动生成
│       │   └── ws-client.ts       # WebSocket 封装
│       ├── pages/
│       │   ├── Dashboard.tsx
│       │   ├── NodeTree.tsx       # 节点树管理页
│       │   ├── TagConfig.tsx      # 点位配置页
│       │   ├── RuleDesigner.tsx   # 规则编辑页 (含 JDM Editor)
│       │   ├── TelemetryView.tsx  # 实时数据监视
│       │   └── AlarmPanel.tsx     # 告警面板
│       └── components/
│           ├── ui/                # shadcn/ui 组件
│           ├── charts/            # 图表组件
│           ├── tree/              # 树形控件
│           └── jdm-editor/        # GoRules JDM Editor 嵌入
│
├── init-db/
│   └── 001-schema.sql             # TimescaleDB 建表脚本
│
└── docs/
    └── architecture.md            # 本文档
```

---

## 9. 实施路线

### Phase 1: 最小闭环（Day 1-5）

**目标**: 一台模拟设备 → 数据入库 → 页面显示数字 → 触发告警

| 任务 | 产出 | 验收 |
|------|------|------|
| Fork fastapi-template → 改名 claw-platform | 项目骨架跑通 | `docker compose up` 全容器健康 |
| 加入 Neuron + nanoMQ + TimescaleDB 到 Compose | 五容器编排完成 | 各服务端口可达 |
| paho-mqtt subscribe → 收消息打印日志 | MQTT 链路打通 | 发测试消息能看到日志 |
| Node 模型建表 + API CRUD | 节点树增删改查可用 | curl 测试通过 |
| 前端显示设备在线状态 | Dashboard 有绿点 | 浏览器看到 |

### Phase 2: 上行闭环（Day 6-12）

**目标**: Neuron 采真实数据 → 归一化 → 入库 → 前端图表

| 任务 | 产出 | 验收 |
|------|------|------|
| Data Normalizer 完成 | 原始值→工程值转换 | 日志输出正确数值 |
| PhysicalTag 模型 + Neuron 点位同步 API | 一键从 Neuron 导入点位 | 导入后数据库有 tag 记录 |
| TimescaleDB Hypertable 写入 | 遥测持久化 | pgsql 查到数据 |
| Virtual Point Engine MVP | 至少一个 SUM 聚合正常工作 | 日志输出计算结果 |
| 前端实时数值卡片 + 趋势图(ECharts) | 页面跳动数字 | 刷新率≥1Hz |

### Phase 3: 下行闭环（Day 13-17）

**目标**: 前端按钮 → 后端 API → Neuron write → 设备响应

| 任务 | 产出 | 验收 |
|------|------|------|
| RPC Controller API | POST /rpc 可发送命令 | curl 返回 success |
| paho-mqtt publish 到 Neuron | 命令到达 Neuron | Neuron 日志确认收到 |
| Neuron write → Modbus | 寄存器写入成功 | 设备状态变化 |
| 前端 RPC 控件 | 按钮点击→结果展示 | UI 反馈 < 3s |
| JWT 权限校验 | 无权限用户不能控制 | 403 正确返回 |

### Phase 4: GoRules 规则引擎（Day 18-27）

**目标**: JDM 编辑器 → 规则保存 → 实时评估 → 告警/联动

| 任务 | 产出 | 验收 |
|------|------|------|
| GoRules Python SDK 集成 | `pip install zen-engine` 可 evaluate | 单元测试通过 |
| Ruleservice + DB 持久化 | 规则 CRUD API 可用 | 创建/修改/删除正常 |
| JDM Editor 嵌入前端 | 可拖拽编辑决策表 | 保存后后端收到 JDM JSON |
| 告警规则评估链路 | 遥测越限 → 自动生成告警 | 告警面板出现红色条目 |
| 控制策略评估链路 | 告警触发 → 自动下发 RPC | 设备自动响应 |
| 内置规则模板库 | 5+ 开箱即用的 EMS 规则 | 从模板一键创建 |
| 规则模拟器 | 传测试数据 → 显示命中哪条 | 调试效率提升 |

### Phase 5: 配置体验打磨（Day 28-37）

**目标**: 用户零代码完成全流程配置

| 任务 | 产出 | 验收 |
|------|------|------|
| 节点树可视化构建器 | 拖拽/右键建树 | 5 层树 3 分钟搭完 |
| 物理点位一键导入 | 选 Neuron node → 自动扫描 | 50 个点位 10 秒导完 |
| 逻辑点位公式编辑器 | 选源点位 → 写公式 → 预览 | 新增虚拟点位即时生效 |
| Dashboard Builder | 拖拽卡片布局 | 自定义面板 5 分钟搞定 |
| Redis 实时缓存 | 最新值 < 1ms 查询 | 万级并发不卡 |
| 多租户/多站点 | 站点隔离 + RBAC | 新增站点 2 分钟 |
| 国标报表 | 日/月/年发电量统计 | PDF 导出 |

### Phase 6: 生产化（持续迭代）

- Ansible 自动化部署脚本
- Grafana 全局监控大盘
- Prometheus 指标采集
- 备份恢复策略
- 性能压测 & 优化
- 国标合规性验证

---

## 10. 技术风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| GoRules Python SDK Windows 兼容性 | 高 | 低 | PyPI 明确支持 win32-msvc；备选方案：GoRules Agent 独立部署为 HTTP 服务 |
| nanoMQ 高连接数稳定性 | 中 | 低 | 单站 200 msg/s 远低于 nanoMQ 极限；备选：换 EMQX Lite |
| 虚拟点位循环依赖 | 高 | 中 | 引擎内置 visited Set 防环；UI 层禁止自引用校验 |
| Hypertable 性能随数据量下降 | 中 | 低 | TimescaleDB 压缩 + 自动分区分区；冷数据降采样 |
| Neuron JWT Token 过期 | 高 | 高 | 后端定时 login 刷新 token；存 Redis 共享 |
| JDM Editor 与自定义主题冲突 | 低 | 中 | CSS isolation + scoped styles |

---

*文档版本: v1.0*
*最后更新: 2026-07-13*
