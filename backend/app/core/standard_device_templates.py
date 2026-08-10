"""
ZiZu Standard Device Templates — 光储充国标设备模板

预置常见设备（PCS、BMS、光伏逆变器、充电桩、电表、电网侧）模板，
应用时一键生成节点、点位，并自动绑定到标准全局实体。
"""
from __future__ import annotations

from loguru import logger
from psycopg2.extras import Json

STANDARD_DEVICE_TEMPLATES: list[dict] = [
    {
        "name": "国标-储能变流器 PCS",
        "category": "pcs",
        "description": "储能 PCS 标准点位模板，适用于 GB/T 34120 储能变流器",
        "content": {
            "nodes": [
                {
                    "name": "PCS",
                    "node_type": "DEVICE",
                    "tags": [
                        {"name": "activePower", "display_name": "PCS有功功率", "data_type": "FLOAT", "unit": "kW", "source_path": "{prefix}/pcs/activePower", "entity_name": "pcs.activePower"},
                        {"name": "voltageA", "display_name": "A相电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/pcs/voltageA", "entity_name": "pcs.voltageA"},
                        {"name": "voltageB", "display_name": "B相电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/pcs/voltageB", "entity_name": "pcs.voltageB"},
                        {"name": "voltageC", "display_name": "C相电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/pcs/voltageC", "entity_name": "pcs.voltageC"},
                        {"name": "currentA", "display_name": "A相电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/pcs/currentA", "entity_name": "pcs.currentA"},
                        {"name": "currentB", "display_name": "B相电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/pcs/currentB", "entity_name": "pcs.currentB"},
                        {"name": "currentC", "display_name": "C相电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/pcs/currentC", "entity_name": "pcs.currentC"},
                        {"name": "frequency", "display_name": "频率", "data_type": "FLOAT", "unit": "Hz", "source_path": "{prefix}/pcs/frequency", "entity_name": "pcs.frequency"},
                        {"name": "powerFactor", "display_name": "功率因数", "data_type": "FLOAT", "source_path": "{prefix}/pcs/powerFactor", "entity_name": "pcs.powerFactor"},
                        {"name": "dcPower", "display_name": "直流侧功率", "data_type": "FLOAT", "unit": "kW", "source_path": "{prefix}/pcs/dcPower", "entity_name": "pcs.dcPower"},
                        {"name": "dcVoltage", "display_name": "直流侧电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/pcs/dcVoltage", "entity_name": "pcs.dcVoltage"},
                        {"name": "dcCurrent", "display_name": "直流侧电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/pcs/dcCurrent", "entity_name": "pcs.dcCurrent"},
                        {"name": "status", "display_name": "PCS状态", "data_type": "INT", "source_path": "{prefix}/pcs/status", "entity_name": "pcs.status"},
                        {"name": "faultCode", "display_name": "故障码", "data_type": "STRING", "source_path": "{prefix}/pcs/faultCode", "entity_name": "pcs.faultCode"},
                        {"name": "temp", "display_name": "内部温度", "data_type": "FLOAT", "unit": "°C", "source_path": "{prefix}/pcs/temp", "entity_name": "pcs.temp"},
                    ],
                }
            ]
        },
    },
    {
        "name": "国标-电池管理系统 BMS",
        "category": "bms",
        "description": "储能 BMS 标准点位模板，适用于 GB/T 36276/GB/T 36558",
        "content": {
            "nodes": [
                {
                    "name": "BMS",
                    "node_type": "DEVICE",
                    "tags": [
                        {"name": "soc", "display_name": "电池SOC", "data_type": "FLOAT", "unit": "%", "source_path": "{prefix}/bms/soc", "entity_name": "ess.soc"},
                        {"name": "soh", "display_name": "电池SOH", "data_type": "FLOAT", "unit": "%", "source_path": "{prefix}/bms/soh", "entity_name": "ess.soh"},
                        {"name": "voltage", "display_name": "电池总电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/bms/voltage", "entity_name": "ess.voltage"},
                        {"name": "current", "display_name": "电池总电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/bms/current", "entity_name": "ess.current"},
                        {"name": "maxCellTemp", "display_name": "最高单体温度", "data_type": "FLOAT", "unit": "°C", "source_path": "{prefix}/bms/maxCellTemp", "entity_name": "ess.maxCellTemp"},
                        {"name": "minCellTemp", "display_name": "最低单体温度", "data_type": "FLOAT", "unit": "°C", "source_path": "{prefix}/bms/minCellTemp", "entity_name": "ess.minCellTemp"},
                        {"name": "maxCellVoltage", "display_name": "最高单体电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/bms/maxCellVoltage", "entity_name": "ess.maxCellVoltage"},
                        {"name": "minCellVoltage", "display_name": "最低单体电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/bms/minCellVoltage", "entity_name": "ess.minCellVoltage"},
                        {"name": "status", "display_name": "储能系统状态", "data_type": "INT", "source_path": "{prefix}/bms/status", "entity_name": "ess.status"},
                        {"name": "faultCode", "display_name": "BMS故障码", "data_type": "STRING", "source_path": "{prefix}/bms/faultCode", "entity_name": "ess.faultCode"},
                    ],
                }
            ]
        },
    },
    {
        "name": "国标-光伏逆变器 PV",
        "category": "pv",
        "description": "光伏逆变器标准点位模板，适用于 GB/T 19963/IEC 61724",
        "content": {
            "nodes": [
                {
                    "name": "PV",
                    "node_type": "DEVICE",
                    "tags": [
                        {"name": "activePower", "display_name": "光伏有功功率", "data_type": "FLOAT", "unit": "kW", "source_path": "{prefix}/pv/activePower", "entity_name": "pv.activePower"},
                        {"name": "voltage", "display_name": "并网电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/pv/voltage", "entity_name": "pv.voltage"},
                        {"name": "current", "display_name": "并网电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/pv/current", "entity_name": "pv.current"},
                        {"name": "frequency", "display_name": "并网频率", "data_type": "FLOAT", "unit": "Hz", "source_path": "{prefix}/pv/frequency", "entity_name": "pv.frequency"},
                        {"name": "powerFactor", "display_name": "功率因数", "data_type": "FLOAT", "source_path": "{prefix}/pv/powerFactor", "entity_name": "pv.powerFactor"},
                        {"name": "irradiance", "display_name": "辐照度", "data_type": "FLOAT", "unit": "W/m²", "source_path": "{prefix}/pv/irradiance", "entity_name": "pv.irradiance"},
                        {"name": "dailyEnergy", "display_name": "日发电量", "data_type": "FLOAT", "unit": "kWh", "source_path": "{prefix}/pv/dailyEnergy", "entity_name": "pv.dailyEnergy"},
                        {"name": "totalEnergy", "display_name": "累计发电量", "data_type": "FLOAT", "unit": "kWh", "source_path": "{prefix}/pv/totalEnergy", "entity_name": "pv.totalEnergy"},
                        {"name": "status", "display_name": "逆变器状态", "data_type": "INT", "source_path": "{prefix}/pv/status", "entity_name": "pv.status"},
                        {"name": "faultCode", "display_name": "故障码", "data_type": "STRING", "source_path": "{prefix}/pv/faultCode", "entity_name": "pv.faultCode"},
                    ],
                }
            ]
        },
    },
    {
        "name": "国标-充电桩 EVSE",
        "category": "charger",
        "description": "充电桩标准点位模板，适用于 GB/T 18487.1/OCPP",
        "content": {
            "nodes": [
                {
                    "name": "Charger",
                    "node_type": "DEVICE",
                    "tags": [
                        {"name": "activePower", "display_name": "充电功率", "data_type": "FLOAT", "unit": "kW", "source_path": "{prefix}/charger/activePower", "entity_name": "charger.activePower"},
                        {"name": "voltage", "display_name": "充电电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/charger/voltage", "entity_name": "charger.voltage"},
                        {"name": "current", "display_name": "充电电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/charger/current", "entity_name": "charger.current"},
                        {"name": "status", "display_name": "桩状态", "data_type": "INT", "source_path": "{prefix}/charger/status", "entity_name": "charger.status"},
                        {"name": "gunStatus", "display_name": "枪状态", "data_type": "INT", "source_path": "{prefix}/charger/gunStatus", "entity_name": "charger.gunStatus"},
                        {"name": "socStart", "display_name": "起始SOC", "data_type": "FLOAT", "unit": "%", "source_path": "{prefix}/charger/socStart", "entity_name": "charger.socStart"},
                        {"name": "chargingDuration", "display_name": "充电时长", "data_type": "FLOAT", "unit": "min", "source_path": "{prefix}/charger/chargingDuration", "entity_name": "charger.chargingDuration"},
                    ],
                }
            ]
        },
    },
    {
        "name": "国标-关口电表 Meter",
        "category": "meter",
        "description": "并网关口电表标准点位模板",
        "content": {
            "nodes": [
                {
                    "name": "Meter",
                    "node_type": "DEVICE",
                    "tags": [
                        {"name": "activePower", "display_name": "有功功率", "data_type": "FLOAT", "unit": "kW", "source_path": "{prefix}/meter/activePower", "entity_name": "grid.activePower"},
                        {"name": "voltageA", "display_name": "A相电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/meter/voltageA", "entity_name": "grid.voltageA"},
                        {"name": "voltageB", "display_name": "B相电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/meter/voltageB", "entity_name": "grid.voltageB"},
                        {"name": "voltageC", "display_name": "C相电压", "data_type": "FLOAT", "unit": "V", "source_path": "{prefix}/meter/voltageC", "entity_name": "grid.voltageC"},
                        {"name": "currentA", "display_name": "A相电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/meter/currentA", "entity_name": "grid.currentA"},
                        {"name": "currentB", "display_name": "B相电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/meter/currentB", "entity_name": "grid.currentB"},
                        {"name": "currentC", "display_name": "C相电流", "data_type": "FLOAT", "unit": "A", "source_path": "{prefix}/meter/currentC", "entity_name": "grid.currentC"},
                        {"name": "frequency", "display_name": "频率", "data_type": "FLOAT", "unit": "Hz", "source_path": "{prefix}/meter/frequency", "entity_name": "grid.frequency"},
                        {"name": "powerFactor", "display_name": "功率因数", "data_type": "FLOAT", "source_path": "{prefix}/meter/powerFactor", "entity_name": "grid.powerFactor"},
                        {"name": "importEnergy", "display_name": "购电量", "data_type": "FLOAT", "unit": "kWh", "source_path": "{prefix}/meter/importEnergy", "entity_name": "grid.importEnergy"},
                        {"name": "exportEnergy", "display_name": "售电量", "data_type": "FLOAT", "unit": "kWh", "source_path": "{prefix}/meter/exportEnergy", "entity_name": "grid.exportEnergy"},
                    ],
                }
            ]
        },
    },
]


def seed_standard_device_templates() -> dict:
    """幂等播种系统设备模板。"""
    from app.services.telemetry_store import get_connection

    inserted = 0
    skipped = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for tpl in STANDARD_DEVICE_TEMPLATES:
                cur.execute("SELECT id FROM t_device_templates WHERE name = %s", (tpl["name"],))
                if cur.fetchone():
                    skipped += 1
                    continue
                cur.execute("""
                    INSERT INTO t_device_templates (name, category, description, content, is_system, enabled)
                    VALUES (%s, %s, %s, %s, TRUE, TRUE)
                    RETURNING id
                """, (tpl["name"], tpl.get("category"), tpl.get("description"), Json(tpl["content"])))
                cur.fetchone()
                inserted += 1
            conn.commit()
    logger.info("[StandardDeviceTemplates] seeded {} system templates, skipped {}", inserted, skipped)
    return {"seeded": inserted, "skipped": skipped}
