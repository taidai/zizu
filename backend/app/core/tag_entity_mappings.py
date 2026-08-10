"""
常用中文点位名 → 国标全局实体映射。

用于自动绑定：根据 tag 所在 node_type 与 tag 名称，推荐或自动建立
实体-点位绑定。映射可按项目扩展，不影响用户手动绑定。
"""
from __future__ import annotations

# 映射表结构：{(node_type, tag_name): entity_name}
# node_type 为 None 表示通配任意节点类型（优先级低）
TAG_ENTITY_MAP: dict[tuple[str | None, str], str] = {
    # ── Meter / Grid ──
    ("Meter", "总有功功率"): "grid.activePower",
    ("Meter", "总无功功率"): "grid.reactivePower",
    ("Meter", "总视在功率"): "grid.apparentPower",
    ("Meter", "有功功率A"): "grid.activePower",
    ("Meter", "有功功率B"): "grid.activePower",
    ("Meter", "有功功率C"): "grid.activePower",
    ("Meter", "无功功率A"): "grid.reactivePower",
    ("Meter", "无功功率B"): "grid.reactivePower",
    ("Meter", "无功功率C"): "grid.reactivePower",
    ("Meter", "视功功率A"): "grid.apparentPower",
    ("Meter", "视在功率B"): "grid.apparentPower",
    ("Meter", "视在功率C"): "grid.apparentPower",
    ("Meter", "总功率因数"): "grid.powerFactor",
    ("Meter", "功率因数A"): "grid.powerFactor",
    ("Meter", "功率因数B"): "grid.powerFactor",
    ("Meter", "功率因数C"): "grid.powerFactor",
    ("Meter", "频率"): "grid.frequency",
    ("Meter", "相电压A"): "grid.voltage",
    ("Meter", "相电压B"): "grid.voltage",
    ("Meter", "相电压C"): "grid.voltage",
    ("Meter", "相电流A"): "grid.current",
    ("Meter", "相电流B"): "grid.current",
    ("Meter", "相电流C"): "grid.current",
    ("Meter", "线电压AB"): "grid.voltage",
    ("Meter", "线电压BC"): "grid.voltage",
    ("Meter", "线电压CA"): "grid.voltage",
    ("Meter", "有功电度"): "grid.energyImport",
    ("Meter", "正有功电度"): "grid.energyImport",
    ("Meter", "负有功电度"): "grid.energyExport",
    ("Meter", "当前有功功率需量"): "grid.demand",
    ("Meter", "最大有功功率需量"): "grid.demand",
    ("Meter", "电压不平衡度"): "grid.voltageUnbalance",
    ("Meter", "电流不平衡度"): "grid.currentUnbalance",

    # ── PCS / 变流器 ──
    ("PCS", "交流总有功功率"): "pcs.activePower",
    ("PCS", "交流总无功功率"): "pcs.reactivePower",
    ("PCS", "交流总视在功率"): "pcs.apparentPower",
    ("PCS", "交流A相有功功率"): "pcs.activePower",
    ("PCS", "交流B相有功功率"): "pcs.activePower",
    ("PCS", "交流C相有功功率"): "pcs.activePower",
    ("PCS", "直流功率"): "pcs.dcPower",
    ("PCS", "输出A相电压"): "pcs.acVoltage",
    ("PCS", "输出B相电压"): "pcs.acVoltage",
    ("PCS", "输出C相电压"): "pcs.acVoltage",
    ("PCS", "输出A相电流"): "pcs.acCurrent",
    ("PCS", "输出B相电流"): "pcs.acCurrent",
    ("PCS", "输出C相电流"): "pcs.acCurrent",
    ("PCS", "输出AB线电压"): "pcs.acVoltage",
    ("PCS", "输出BC线电压"): "pcs.acVoltage",
    ("PCS", "输出CA线电压"): "pcs.acVoltage",
    ("PCS", "总母线电压"): "pcs.dcVoltage",
    ("PCS", "正母线电压"): "pcs.dcVoltage",
    ("PCS", "负母线电压"): "pcs.dcVoltage",
    ("PCS", "电池电压"): "pcs.dcVoltage",
    ("PCS", "电池电流"): "pcs.dcCurrent",
    ("PCS", "直流总电流"): "pcs.dcCurrent",
    ("PCS", "IGBT温度"): "pcs.temp",
    ("PCS", "电感温度"): "pcs.temp",
    ("PCS", "环境温度"): "pcs.ambientTemp",
    ("PCS", "电网频率"): "grid.frequency",
    ("PCS", "交流历史充电量"): "pcs.chargeEnergy",
    ("PCS", "交流历史放电量"): "pcs.dischargeEnergy",
    ("PCS", "交流日充电量"): "pcs.dailyChargeEnergy",
    ("PCS", "交流日放电量"): "pcs.dailyDischargeEnergy",

    # ── BMS / 电池簇 ──
    ("BMS", "电池电压"): "ess.voltage",
    ("BMS", "电池电流"): "ess.current",
    ("BMS", "单体最高电压"): "ess.maxCellVoltage",
    ("BMS", "单体最低电压"): "ess.minCellVoltage",
    ("BMS", "单体平均电压"): "ess.avgCellVoltage",
    ("BMS", "单体最高温度"): "ess.maxCellTemp",
    ("BMS", "单体最低温度"): "ess.minCellTemp",
    ("BMS", "单体平均温度"): "ess.avgCellTemp",
    ("BMS", "SOC"): "ess.soc",
    ("BMS", "SOH"): "ess.soh",
    ("BMS", "绝缘电阻"): "ess.insulationResistance",
    ("BMS", "故障码"): "ess.faultCode",
    ("BMS", "告警状态"): "ess.bmsAlarm",
    ("BMS", "簇压差"): "ess.cellVoltageDiff",

    # ── 通用匹配（当 node_type 未命中时兜底） ──
    (None, "频率"): "grid.frequency",
    (None, "环境温度"): "ambient.temperature",
}


def lookup_entity_name(node_type: str | None, tag_name: str) -> str | None:
    """根据节点类型和点位名查找推荐实体名。

    匹配优先级：
      1. (node_type, tag_name) 精确匹配
      2. (None, tag_name) 通配匹配
    """
    if node_type:
        key = (node_type, tag_name)
        if key in TAG_ENTITY_MAP:
            return TAG_ENTITY_MAP[key]
    return TAG_ENTITY_MAP.get((None, tag_name))
