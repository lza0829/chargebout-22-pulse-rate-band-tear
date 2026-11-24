"""峰平谷时段与档价列名：无依赖，供 db 迁移与 services 共用。"""
from __future__ import annotations

from datetime import datetime

# 峰 8–11、18–21；谷 23–7；其余为平。
BAND_PEAK = "峰"
BAND_FLAT = "平"
BAND_VALLEY = "谷"
BAND_ORDER = (BAND_PEAK, BAND_FLAT, BAND_VALLEY)
BAND_COLUMN = {
    BAND_PEAK: "peak_price",
    BAND_FLAT: "flat_price",
    BAND_VALLEY: "valley_price",
}
# 会话锁档字段：开充时把整张峰平谷表冻在会话上。
BAND_LOCK_COLUMN = {
    BAND_PEAK: "locked_peak",
    BAND_FLAT: "locked_flat",
    BAND_VALLEY: "locked_valley",
}

# 全局唯一档价口径与对账状态常量。
PRICE_MODE = "会话锁档计价"
PRICING_OK = "锁档一致"
PRICING_GAP = "档价待核"


def band_for(dt: datetime) -> str:
    hour = dt.hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return BAND_PEAK
    if 23 <= hour or hour < 7:
        return BAND_VALLEY
    return BAND_FLAT


def plan_price_for_band(plan, band: str) -> float:
    return float(plan[BAND_COLUMN[band]])


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
