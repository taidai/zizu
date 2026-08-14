# 光储充 EMS 参考交付包

这是公开、可审计的光储充 EMS 参考解决方案源。它不包含客户点位地址、设备 IP、密码、驱动
文件或现场规模；这些都由实施工程师在受保护界面通过节点、点位和本包的参数/绑定计划配置。

## 交付前输入

1. 平台管理员先按发布文档部署固定摘要制品并创建 engineer/operator 账户。
2. 实施工程师在产品界面接入已支持协议的 PCS、BMS、PV、EVSE 与关口电表，为每个节点设置
   与参数 `device_key` 相同的稳定 `source_catalog_key`，再创建规范点位名。
3. 实施工程师导入发布的 `.zizu.zip`，填写设备实例参数和 `gateway.credentials` 的
   `secret://` 引用，审查全部候选绑定并显式解决歧义。

本包要求的规范点位名见各 slot 文件，例如 PCS 的 `ActivePower`、`ActivePowerSetpoint`、
`ActivePowerReadback` 与 `BmsReady`。名称是可迁移的公开配置契约，不是现场地址。

## 交付后的机器验收

验收清单覆盖平台存活、PCS/BMS/PV/EVSE/关口电表的实时新鲜度、关口超限告警生命周期、
基础限购电策略的仿真和回读命令、以及发布锁一致性。告警与策略检查需要实施工程师先按
公开 API/工作台完成真实现场触发、告警确认/恢复和策略回读，验收报告不会偷偷下发控制。

## 发布制品构建

维护者在受控发布流水线运行：

```bash
python scripts/build_reference_delivery.py \
  --output dist/pv-storage-charging-ems-1.0.0.zizu.zip
```

流水线应发布该 ZIP 的 SHA-256；实施工程师只下载发布的 ZIP，不修改本目录中的源码。
