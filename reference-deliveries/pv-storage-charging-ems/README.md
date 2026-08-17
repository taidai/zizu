# 光储充 EMS 参考交付包

这是公开、可审计的光储充 EMS 参考解决方案源。它不包含客户点位地址、设备 IP、密码、驱动
文件或现场规模；这些都由实施工程师在受保护界面通过节点、点位和本包的参数/绑定计划配置。

## 交付前输入

1. 平台管理员先按发布文档部署固定摘要制品并创建 engineer/operator 账户。
2. 实施工程师在产品界面接入已支持协议的 PCS、BMS、PV、EVSE 与关口电表，为每个节点设置
   与参数 `device_key` 相同的稳定 `source_catalog_key`，再创建规范点位名。
3. 实施工程师导入发布的 `.zizu.zip`，填写设备实例参数和 `gateway.credentials` 的
   `secret://` 引用，审查全部候选绑定并显式解决歧义。

本包要求的规范点位名见各 slot 文件。控制相关的 `ActivePowerSetpoint`、
`ActivePowerReadback` 与 `BmsReady` 仍使用独立、显式的直接控制匹配，不从读取转换反推写地址。
名称是可迁移的公开配置契约，不是现场地址。

## PCS 点位转换资产

`point-conversions/` 发布两套不可变 PCS 模板修订。Brand A 将 W 制有功功率、数字运行状态和
分号分隔故障码转换为标准 L2；Brand B 将 kW 制有功功率、字母运行状态和逗号分隔故障码转换为
完全相同的 `pcs.active_power`、`pcs.operating_state`、`pcs.fault_codes` 三个实体定义。更换品牌时
只替换模板修订和 L0 输入绑定，不改变 L2 实体身份或上层告警、策略、画面引用。

公开格式为 `zizu.point-conversion/v1alpha1`，顶层固定声明 `id`、`deviceCategory`、`brand`、
`model`、正整数 `revision`、`active|retired` 状态、强类型 `inputs` 与 `outputs`。每个输入只允许
`l0|l2` 来源、稳定源键和别名；每个输出必须引用已声明且类型、单位一致的实体定义，并给出正数
`freshness`。转换规则只允许：

- `numeric`：有限的 `scale`、`offset`、`minimum`、`maximum`，且上下限顺序有效；
- `enum`：显式原值到标准状态映射；
- `fault_codes`：显式分隔符及原始故障码到标准码、中文名称、默认严重度映射。

任意表达式、脚本、动态求值或未声明字段都会在包导入阶段被拒绝。`retired` 修订继续保留完整
审计契约和既有安装，但不能用于生成新的点位转换计划。

## 交付后的机器验收

验收清单覆盖平台存活、PCS/BMS/PV/EVSE/关口电表的实时新鲜度、关口电表 24 小时历史样本、
关口超限告警生命周期、基础限购电策略的仿真和回读命令、安装/手动控制/策略/告警确认/权限拒绝的
不可变审计覆盖、以及发布锁一致性。参考策略固定为 **10 kW** 的隔离小功率动作；它不是现场通用安全值。告警与策略检查需要实施工程师先按
公开 API/工作台完成真实现场触发、告警确认/恢复和策略回读，验收报告不会偷偷下发控制。

策略目标是高风险设定值时，包必须声明 `highRiskAuthorization.maximumAbsoluteValue`，且固定动作的绝对值
不得超过该上限。工程师在确认输入新鲜、质量合格后显式启用；可通过 `disable` 停止后续调度。
已分派命令仍只以现场回读确认或超时结束。生产现场必须重新评审功率上限、设备铭牌、并网约束和联锁，
不得把本参考包的 10 kW 直接当作现场设定。

## 发布制品构建

维护者在受控发布流水线运行：

```bash
python scripts/build_reference_delivery.py \
  --output dist/pv-storage-charging-ems-1.0.0.zizu.zip
```

流水线应发布该 ZIP 的 SHA-256；实施工程师只下载发布的 ZIP，不修改本目录中的源码。
