"""
标准故障码映射表 — 预置国标常用故障码，开箱可用。

参考标准：
  - GB/T 36276-2023  电力储能用锂离子电池 (BMS)
  - GB/T 19963-2024  光伏发电站接入电力系统技术规定 (PV并网保护)
  - GB/T 51048-2024  电化学储能电站设计规范 (消防/安全)
  - GB/T 36558-2024  电力系统电化学储能系统通用技术条件
"""
from __future__ import annotations

from loguru import logger
from psycopg2.extras import Json

# ── GB/T 36276: BMS 锂电池故障码 ──
BMS_FAULT_MAP = {
    "name": "GB/T 36276 BMS故障码",
    "description": "电力储能用锂离子电池 BMS 故障码映射 (GB/T 36276-2023)",
    "entries": [
        {"code": "1", "message": "单体过压"},
        {"code": "2", "message": "单体欠压"},
        {"code": "3", "message": "总压过压"},
        {"code": "4", "message": "总压欠压"},
        {"code": "5", "message": "充电过流"},
        {"code": "6", "message": "放电过流"},
        {"code": "7", "message": "过温"},
        {"code": "8", "message": "低温"},
        {"code": "9", "message": "绝缘电阻偏低"},
        {"code": "10", "message": "SOC过高"},
        {"code": "11", "message": "SOC过低"},
        {"code": "12", "message": "电池均衡异常"},
        {"code": "13", "message": "短路保护"},
        {"code": "14", "message": "BMS通信中断"},
        {"code": "15", "message": "继电器故障"},
        {"code": "16", "message": "热失控预警"},
        {"code": "17", "message": "充放电温差过大"},
        {"code": "18", "message": "电池簇压差过大"},
        {"code": "19", "message": "漏电流异常"},
        {"code": "20", "message": "电压采样异常"},
        {"code": "21", "message": "温度采样异常"},
        {"code": "22", "message": "电流采样异常"},
        {"code": "0x10", "message": "直流侧过压"},
        {"code": "0x11", "message": "交流侧过流"},
        {"code": "0x12", "message": "IGBT过温"},
        {"code": "0x13", "message": "DSP故障"},
        {"code": "0x14", "message": "辅助电源故障"},
    ],
}

# ── GB/T 19963: PV 并网保护 ──
PV_PROTECTION_MAP = {
    "name": "GB/T 19963 光伏并网保护",
    "description": "光伏发电站接入电力系统保护动作映射 (GB/T 19963-2024)",
    "entries": [
        {"code": "1", "message": "过压脱扣"},
        {"code": "2", "message": "欠压脱扣"},
        {"code": "3", "message": "过频脱扣"},
        {"code": "4", "message": "低频脱扣"},
        {"code": "5", "message": "防孤岛保护动作"},
        {"code": "6", "message": "过流保护"},
        {"code": "7", "message": "过压保护"},
        {"code": "8", "message": "欠压保护"},
        {"code": "9", "message": "逆变器故障停机"},
        {"code": "10", "message": "电网电压不平衡"},
        {"code": "11", "message": "谐波超限"},
        {"code": "12", "message": "直流注入超限"},
        {"code": "13", "message": "功率因数超限"},
        {"code": "14", "message": "孤岛检测失败"},
        {"code": "15", "message": "低电压穿越失败"},
        {"code": "16", "message": "高电压穿越失败"},
    ],
}

# ── GB/T 51048: 储能电站消防/安全 ──
FIRE_SAFETY_MAP = {
    "name": "GB/T 51048 储能消防安全",
    "description": "电化学储能电站设计规范消防安全映射 (GB/T 51048-2024)",
    "entries": [
        {"code": "1", "message": "烟感报警"},
        {"code": "2", "message": "温感报警"},
        {"code": "3", "message": "可燃气体检测报警"},
        {"code": "4", "message": "消防系统故障"},
        {"code": "5", "message": "消防通信中断"},
        {"code": "6", "message": "灭火系统启动"},
        {"code": "7", "message": "灭火系统故障"},
        {"code": "8", "message": "排风系统故障"},
        {"code": "9", "message": "急停按钮触发"},
        {"code": "10", "message": "电弧故障"},
        {"code": "11", "message": "柜门异常打开"},
        {"code": "12", "message": "水浸报警"},
        {"code": "13", "message": "防雷器故障"},
        {"code": "14", "message": "环境温度超限"},
        {"code": "15", "message": "环境湿度超限"},
    ],
}

ALL_STANDARD_FAULT_MAPS = [BMS_FAULT_MAP, PV_PROTECTION_MAP, FIRE_SAFETY_MAP]


def seed_standard_fault_maps() -> dict:
    """幂等播种标准故障码映射表。"""
    from app.services.telemetry_store import get_connection

    seeded = 0
    skipped = 0
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for fm in ALL_STANDARD_FAULT_MAPS:
                    cur.execute(
                        "SELECT id FROM t_fault_maps WHERE name = %s",
                        (fm["name"],),
                    )
                    if cur.fetchone():
                        skipped += 1
                        continue
                    cur.execute(
                        "INSERT INTO t_fault_maps (name, description, entries) "
                        "VALUES (%s, %s, %s)",
                        (fm["name"], fm["description"], Json(fm["entries"])),
                    )
                    seeded += 1
            conn.commit()
    except Exception as e:
        logger.error("[Seed] Standard fault maps failed: {}", e)
        return {"seeded": 0, "skipped": 0, "error": str(e)}

    logger.info("[Seed] Standard fault maps: seeded={}, skipped={}", seeded, skipped)
    return {"seeded": seeded, "skipped": skipped}
