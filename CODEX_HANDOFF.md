# ZiZu Handoff — v0.4.44

## 当前版本
v0.4.44 (2026-08-10)

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
