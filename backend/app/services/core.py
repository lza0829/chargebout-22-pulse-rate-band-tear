from __future__ import annotations
from datetime import datetime
from app.services import rate_surface

from app.db import db, row_dict, rows

# 档价口径：本产品全局采用「逐笔脉冲落价」。
# 每笔脉冲在写入时刻按当日峰平谷取价并永久记下单价与金额；
# 开会话时记下的 price_locked 只作开场快照，不覆盖后续脉冲，也不被回写。
PRICE_MODE = "逐笔脉冲落价"

# 峰 8–11、18–21；谷 23–7；其余为平。
BAND_PEAK = "峰"
BAND_FLAT = "平"
BAND_VALLEY = "谷"
BAND_COLUMN = {BAND_PEAK: "peak_price", BAND_FLAT: "flat_price", BAND_VALLEY: "valley_price"}



def now_dt() -> datetime:
    return datetime.now()


def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return now_dt().strftime("%Y-%m-%d")


def band_for(dt: datetime) -> str:
    hour = dt.hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return BAND_PEAK
    if 23 <= hour or hour < 7:
        return BAND_VALLEY
    return BAND_FLAT


def plan_price_for_band(plan, band: str) -> float:
    return float(plan[BAND_COLUMN[band]])


def resolve_band_price(conn, rate_plan_id: int, at: datetime | None = None) -> dict:
    """按给定时刻（默认现在）解析某费率表的峰平谷档与单价。"""
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return {"band": None, "price": 0.0, "rate_plan_id": rate_plan_id, "plan_name": None}
    dt = at or now_dt()
    band = band_for(dt)
    return {
        "band": band,
        "price": plan_price_for_band(plan, band),
        "rate_plan_id": rate_plan_id,
        "plan_name": plan["name"],
        "at": dt.strftime("%Y-%m-%d %H:%M:%S"),
    }


def current_band_price(conn, rate_plan_id: int, at: datetime | None = None) -> float:
    return resolve_band_price(conn, rate_plan_id, at)["price"]


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def estimate_replay(conn, session_id: int) -> dict:
    """由已落价脉冲重放预估：金额=Σ(每笔 pulse.amount)，电量=Σ(delta_kwh)。

    这是「预估必须能由选定档价口径重放」的唯一权威来源；
    会话 amount 字段只是它的物化缓存，两者应恒等。
    """
    agg = conn.execute(
        """SELECT COALESCE(SUM(delta_kwh),0) AS kwh,
                  COALESCE(SUM(amount),0) AS amount,
                  COUNT(*) AS n
           FROM pulses WHERE session_id=? AND unit_price IS NOT NULL""",
        (session_id,),
    ).fetchone()
    by_band = rows(
        conn.execute(
            """SELECT band, COUNT(*) AS n, COALESCE(SUM(delta_kwh),0) AS kwh,
                      COALESCE(SUM(amount),0) AS amount
               FROM pulses WHERE session_id=? AND unit_price IS NOT NULL
               GROUP BY band ORDER BY band""",
            (session_id,),
        ).fetchall()
    )
    return {
        "口径": PRICE_MODE,
        "脉冲笔数": int(agg["n"]),
        "重放电量": round(float(agg["kwh"]), 3),
        "重放金额": round(float(agg["amount"]), 2),
        "分档": by_band,
    }


def _session_open_row(conn, session_id: int):
    return conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name,
                  r.name AS rate_name,
                  r.peak_price, r.flat_price, r.valley_price
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()


def overview(conn) -> dict:
    piles = rows(conn.execute("SELECT status, COUNT(*) AS n FROM piles GROUP BY status").fetchall())
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    pile_map = {p["status"]: p["n"] for p in piles}
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        "SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchone()["k"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    # 总览预估桶：在途会话预估 = 各自脉冲金额之和；与脉冲口径同源。
    est = conn.execute(
        """SELECT COALESCE(SUM(amount),0) AS a FROM pulses
           WHERE session_id IN (SELECT id FROM sessions WHERE status IN ('充电中','结算中'))"""
    ).fetchone()["a"]
    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": sess_map.get("已超时", 0),
        "已中断": sess_map.get("已中断", 0),
        "在充电量": round(float(open_kwh), 3),
        "在充预估金额": round(float(est), 2),
        "本日关账金额": round(float(today_amount), 2),
        "档价口径": PRICE_MODE,
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    if site_id:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               WHERE p.site_id=? ORDER BY p.id""",
            (site_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               ORDER BY p.id"""
        ).fetchall()
    return rows(rs)


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        return rows(
            conn.execute(
                "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
                (pile_id,),
            ).fetchall()
        )
    return rows(
        conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    )


def list_rates(conn):
    dt = now_dt()
    band = band_for(dt)
    out = []
    for r in rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall()):
        item = dict(r)
        item["当前档"] = band
        item["当前档价"] = plan_price_for_band(r, band)
        out.append(item)
    return out


def list_rate_changes(conn, rate_plan_id: int | None = None):
    if rate_plan_id:
        rs = conn.execute(
            """SELECT e.*, r.name AS plan_name FROM rate_change_events e
               JOIN rate_plans r ON r.id=e.rate_plan_id
               WHERE e.rate_plan_id=? ORDER BY e.id DESC LIMIT 100""",
            (rate_plan_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT e.*, r.name AS plan_name FROM rate_change_events e
               JOIN rate_plans r ON r.id=e.rate_plan_id
               ORDER BY e.id DESC LIMIT 100"""
        ).fetchall()
    return rows(rs)


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    return rows(rs)


def session_detail(conn, session_id: int):
    se = _session_open_row(conn, session_id)
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = row_dict(se)
    d["脉冲"] = pulses
    d["事件"] = events
    replay = estimate_replay(conn, session_id)
    d["档价口径"] = PRICE_MODE
    d["重放"] = replay
    d["预估金额"] = round(float(d.get("amount") or 0), 2)
    d["重放金额"] = replay["重放金额"]
    d["预估与重放一致"] = d["预估金额"] == d["重放金额"]
    d["开充档价快照"] = {
        "档": d.get("opening_band"),
        "单价": float(se["price_locked"]) if se["price_locked"] is not None else None,
        "说明": "开充时刻快照，仅作开场记录；后续脉冲按写入时刻档价独立落价，不据此索赔。",
    }
    d["费率当前档价"] = {
        "峰": float(se["peak_price"]),
        "平": float(se["flat_price"]),
        "谷": float(se["valley_price"]),
    }
    return d

_overview_impl = overview


def overview(conn):
    return rate_surface.overlay_overview(_overview_impl(conn), conn)

