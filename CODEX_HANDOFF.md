# ZiZu Handoff — v0.4.44

## 当前版本
v0.4.45 (2026-08-10)

## 最近完成
### v0.4.44 — 告警中心国标合规 (TDD)
- **alarm_logic.py** — 共享纯函数模块，21 个 TDD 测试全绿
  - `match_fault_entry`: 精确/十六进制互转/通配符匹配
  - `build_alarm_message`: 统一消息构建（级别+类型+阈值+实际值+故障描述）
  - `is_alarm_active`: 激活判定
  - 15 个标准告警类型（过压/欠压/过流/过温/绝缘/通信中断/SOC超限/防孤岛/保护动作/消防/电弧/急停...）
- **migration_016** — t_alarms 加 alarm_type/threshold/source/count/code，t_tags 加 alarm_type/threshold
- **tag_alarm_engine** — 用 alarm_logic 统一逻辑，携带 alarm_type/threshold/alarm_source，alarm_count 累计去重
- **alarm_processor (MQTT 路径)** — 通过 _alarm_name_map 查 fault_map，统一故障码转义
- **pipeline** — _fetch_alarm_meta 加载 alarm_type/threshold/node_type，新增 reload_rules_now()
- **tags API** — batch/create 支持 alarm_type/threshold，新增 /tags/alarm-config 端点
- **alarms API** — 响应含新字段，新增 /alarms/alarm-types 端点
- **batch 更新后触发 pipeline 立即 reload**（不等 30s）
- **3 个国标故障码映射表** — GB/T 36276 BMS(27条) / GB/T 19963 PV保护(16条) / GB/T 51048 消防(15条)
- **前端** — NodeTagPanel 批量告警类型+阈值 UI，AlarmCenterPage 展示类型/来源/计数/阈值


## v0.4.45 — 自定义告警等级 + 全局实体批量绑定

**已完成：**
- 新增 `t_alarm_levels` 自定义告警等级表（code/name/severity/color/trigger_rules）
- 新增 `t_entity_alarm_bindings` 实体-等级绑定表，支持批量绑定与覆盖规则
- migration_017 为 `t_alarms` 增加 `entity_id` 列
- 新增 `backend/app/services/entity_alarm_engine.py`：
  - 触发规则：active / eq / ne / gte / gt / lte / lt / fault
  - `process_entity_alarms` 按 tag_id 索引批量评估并生成/恢复告警
- 更新 `backend/app/services/pipeline.py`：
  - 加载 `_entity_alarm_index`（tag_id → 绑定列表）
  - 在 `process_tag_alarms` 之后调用 `process_entity_alarms`
- 新增 `backend/app/api/alarm_levels.py`：
  - `/alarm-levels` CRUD
  - `/alarm-levels/{id}/entities` 批量绑定/解绑
  - `/entities/{id}/alarm-levels` 查询实体已绑等级
- 前端：
  - `client.ts` 增加 AlarmLevel / EntityAlarmBinding 类型与 API
  - 新增 `AlarmLevelManagerPage.tsx`：等级管理 + 批量勾选实体 + 规则覆盖
  - 重写 `AlarmCenterPage.tsx`：按动态告警等级展示分组
  - `App.tsx` 增加「告警等级」导航
- 新增 `backend/tests/test_entity_alarm_engine.py`：23 个 TDD 测试全绿

**部署状态：**
- 1号机 (e606.hlszh.com:9000)：已部署 v0.4.45，health OK，migration 017 已应用
- GitHub taidai/zizu main：已推送 (bd64efb)

**已知问题（与本次改动无关）：**
- `test_aggregator.py` 仍有 2 个 SQL 结构断言失败（pre-existing）


### v0.4.45-fix — 删繁就简（实体层统一入口）
- **EntityManagerPage**：详情面板拆分为「点位绑定 / 实时数据 / 历史数据」三个 tab；历史数据支持 1h/24h/7d 趋势图。
- **NodeTagPanel**：移除点位行的「历史趋势」按钮，回归原始实时值展示；历史数据入口迁移到全局实体。
- **rule_engine.py**：后端增加 `sourceEntityIds` 配置读取，按 entity_id 过滤上下文；保留 `sourceNodeIds` 兼容旧规则。
- 已部署到 1 号机，GitHub 已推送 (27fe135)。


### v0.4.45-fix2 — 规则引擎数据源切到全局实体
- **RuleEnginePage**：「数据源节点」改为「数据源实体」，多选全局实体；字段映射从 tag 名改为全局实体名。
- **后端 rule_engine**：已支持 `sourceEntityIds` 按 entity_id 过滤上下文，旧 `sourceNodeIds` 规则仍兼容。
- 已部署到 1 号机，GitHub 已推送 (c1db5db)。

## 部署状态
- 1号机 (e606.hlszh.com:13122, holo/holo123)：已部署 v0.4.44
  - health v0.4.44, pipeline RUNNING, MQTT+Neuron connected
  - 3 个标准故障码表已播种，203 个标准实体已播种
  - migration 016 已应用
- GitHub taidai/zizu main：已推送 (f959c42..b425bca)

## 已知约束
- shell_command 沙箱只读，写文件/跑命令用 mcp__node_repl__js
- node_repl 变量不能重复声明——reset kernel 或用唯一名
- SSH 需 paramiko，sudo: echo 'holo123' | sudo -S <cmd>
- PowerShell 内联 Python 的管道符会被 PS 拦截——写 .py 文件运行
- 不要用 UploadFile/File（镜像无 python-multipart）
- 版本号每次更新必须界面可见（health.version + FE，VERSION 文件复制到 backend/app/VERSION）

## 告警架构现状（全链路已通 + 国标合规）
- t_tags: alarm_level(error1/2/3) + alarm_type(15类) + alarm_threshold + fault_map_id
- t_alarms: alarm_type + alarm_threshold + alarm_source + alarm_count + alarm_code
- 两条路径统一用 alarm_logic.py 的 match_fault_entry + build_alarm_message
- alarm_count 累计去重（同类告警不重复创建，只累加计数）
- 预置 3 个国标故障码表（GB/T 36276/19963/51048）

## 下一步（待用户指定）
