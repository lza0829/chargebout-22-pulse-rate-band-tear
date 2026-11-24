from __future__ import annotations
from datetime import datetime

from app.bands import (
    BAND_COLUMN,
    BAND_FLAT,
    BAND_LOCK_COLUMN,
    BAND_ORDER,
    BAND_PEAK,
    BAND_VALLEY,
    PRICE_MODE,
    band_for,
    plan_price_for_band,
)
from app.db import row_dict, rows

# 档价口径：本产品全局采用「会话锁档计价」。
# 开充时把整张峰平谷表冻结到会话上（locked_peak/locked_flat/locked_valley）；
# 每笔脉冲按「脉冲写入时刻所属档」×「会话锁档价」落价并永久记下单价与金额。
# 费率页改档只影响此后新开会话的锁档，绝不回写既有会话锁档或已落价脉冲。
# 唯一权威 = 会话档价锁 ∪ 脉冲行档价 ∪ 可重放累计金额；三者任一对不上即整笔回滚。
MATERIAL_PREFIXES = ("会话电量≠", "会话金额≠")


def now_dt() -> datetime:
    return datetime.now()


def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return now_dt().strftime("%Y-%m-%d")


def resolve_band_price(conn, rate_plan_id: int, at: datetime | None = None) -> dict:
    """按给定时刻（默认现在）解析某费率表的峰平谷档与单价。

    只用于开充时冻结锁档、以及费率页展示现价；脉冲落价绝不调用本函数。
    """
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


def plan_lock(plan) -> dict:
    """把整张峰平谷表冻成会话锁档。"""
    return {band: float(plan[BAND_COLUMN[band]]) for band in BAND_ORDER}


def locked_price_for_band(se, band: str) -> float | None:
    if band not in BAND_LOCK_COLUMN:
        return None
    value = se[BAND_LOCK_COLUMN[band]]
    return None if value is None else float(value)


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


def verify_session(conn, session_id: int, se=None) -> dict:
    """对账唯一入口：会话锁档 ∪ 脉冲行档价 ∪ 可重放累计金额。

    返回的「问题」里：
      - 以 MATERIAL_PREFIXES 开头的为材料不一致（会话缓存电量/金额≠脉冲合计），
        可由重放回退修复；
      - 其余为档价/脉冲证据缺口，必须整笔回滚，不得用展示或旁路改数掩盖。
    """
    if se is None:
        se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        return {
            "一致": False,
            "脉冲笔数": 0,
            "重放电量": 0.0,
            "重放金额": 0.0,
            "缓存电量": 0.0,
            "缓存金额": 0.0,
            "材料一致": False,
            "锁档": {band: None for band in BAND_ORDER},
            "问题": ["会话不存在"],
            "脉冲": [],
        }

    lock = {band: locked_price_for_band(se, band) for band in BAND_ORDER}
    issues: list[str] = []
    missing = [band for band, value in lock.items() if value is None]
    if missing:
        issues.append(f"会话锁档缺失：{'、'.join(missing)}档")

    pulse_rows = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    kwh = 0.0
    amount = 0.0
    detailed = []
    for p in pulse_rows:
        delta = float(p["delta_kwh"] or 0.0)
        kwh += delta
        band = p["band"]
        unit = p["unit_price"]
        pulse_amount = p["amount"]
        expected = lock.get(band) if band in BAND_ORDER else None
        row_issues: list[str] = []
        if band not in BAND_ORDER:
            row_issues.append("档非法")
        if unit is None:
            row_issues.append("未落单价")
        elif expected is not None and abs(float(unit) - expected) > 1e-9:
            row_issues.append("单价≠会话锁档")
        if pulse_amount is None:
            row_issues.append("未落金额")
        elif unit is not None and round(delta * float(unit), 2) != round(float(pulse_amount), 2):
            row_issues.append("金额≠电量×单价")
        if row_issues:
            issues.append(f"脉冲#{p['id']}：" + "；".join(row_issues))
        amount += float(pulse_amount or 0.0)
        detailed.append(
            {
                **p,
                "预期锁档单价": expected,
                "档价一致": not any(x in row_issues for x in ("单价≠会话锁档", "未落单价", "档非法")),
                "金额一致": not any(x in row_issues for x in ("金额≠电量×单价", "未落金额")),
                "问题": row_issues,
            }
        )

    replay_kwh = round(kwh, 3)
    replay_amount = round(amount, 2)
    cached_kwh = round(float(se["kwh"] or 0.0), 3)
    cached_amount = round(float(se["amount"] or 0.0), 2)
    material_ok = cached_kwh == replay_kwh and cached_amount == replay_amount
    if cached_kwh != replay_kwh:
        issues.append("会话电量≠脉冲合计")
    if cached_amount != replay_amount:
        issues.append("会话金额≠脉冲重放")

    return {
        "一致": not issues,
        "脉冲笔数": len(pulse_rows),
        "重放电量": replay_kwh,
        "重放金额": replay_amount,
        "缓存电量": cached_kwh,
        "缓存金额": cached_amount,
        "材料一致": material_ok,
        "锁档": lock,
        "问题": issues,
        "脉冲": detailed,
    }


def evidence_issues(verification: dict) -> list[str]:
    return [i for i in verification["问题"] if not i.startswith(MATERIAL_PREFIXES)]


def material_issues(verification: dict) -> list[str]:
    return [i for i in verification["问题"] if i.startswith(MATERIAL_PREFIXES)]


def estimate_replay(conn, session_id: int) -> dict:
    """由已落价脉冲重放预估：金额=Σ(每笔 pulse.amount)，电量=Σ(delta_kwh)。

    这是「预估必须能由会话锁档口径重放」的唯一权威来源；
    会话 amount/kwh 字段只是它的物化缓存，两者必须恒等。
    """
    verification = verify_session(conn, session_id)
    by_band = rows(
        conn.execute(
            """SELECT band, COUNT(*) AS n, COALESCE(SUM(delta_kwh),0) AS kwh,
                      COALESCE(SUM(amount),0) AS amount
               FROM pulses WHERE session_id=?
               GROUP BY band ORDER BY band""",
            (session_id,),
        ).fetchall()
    )
    return {
        "口径": PRICE_MODE,
        "脉冲笔数": verification["脉冲笔数"],
        "重放电量": verification["重放电量"],
        "重放金额": verification["重放金额"],
        "分档": by_band,
        "一致": verification["一致"],
        "问题": verification["问题"],
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

    # 在途桶一律由脉冲重放计算，绝不读会话物化金额或任何缓存快照。
    open_sessions = rows(
        conn.execute(
            "SELECT * FROM sessions WHERE status IN ('充电中','结算中') ORDER BY id"
        ).fetchall()
    )
    open_kwh = 0.0
    open_amount = 0.0
    open_pending = 0.0
    open_defects = []
    for se in open_sessions:
        v = verify_session(conn, se["id"], se)
        if v["一致"]:
            open_kwh += v["重放电量"]
            open_amount += v["重放金额"]
        else:
            # 证据对不上的会话不计入「已对齐」桶，单列异常，避免总览撒谎。
            open_pending += v["重放金额"]
            open_defects.append(
                {
                    "会话编号": se["id"],
                    "会话号": se["slip_no"],
                    "桩位": se["pile_id"],
                    "状态": se["status"],
                    "问题": v["问题"],
                }
            )

    # 本日关账金额同样以脉冲重放为准；关账缓存对不上时单列异常，不混进已对齐金额。
    closed = rows(
        conn.execute(
            "SELECT * FROM sessions WHERE business_day=? AND status='已关账' ORDER BY id",
            (today(),),
        ).fetchall()
    )
    today_amount = 0.0
    closed_defects = []
    for se in closed:
        v = verify_session(conn, se["id"], se)
        if v["脉冲笔数"]:
            today_amount += v["重放金额"]
            if abs(round(float(se["amount"] or 0.0), 2) - v["重放金额"]) > 0.005:
                closed_defects.append(
                    {
                        "会话编号": se["id"],
                        "会话号": se["slip_no"],
                        "缓存金额": round(float(se["amount"] or 0.0), 2),
                        "重放金额": v["重放金额"],
                    }
                )
        else:
            today_amount += float(se["amount"] or 0.0)

    # 占用态对账：在途会话必须同时占住桩位与枪线；被占枪线必须确有在途会话。
    occ = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM sessions se
                JOIN piles p ON p.id=se.pile_id
                JOIN guns g ON g.id=se.gun_id
               WHERE se.status IN ('充电中','结算中')
                 AND (p.status!='占用' OR g.status!='占用')) AS bad_open,
             (SELECT COUNT(*) FROM guns g WHERE g.status='占用'
                AND g.id NOT IN (SELECT gun_id FROM sessions WHERE status IN ('充电中','结算中'))) AS bad_gun"""
    ).fetchone()
    occupancy_defects = int(occ["bad_open"] or 0) + int(occ["bad_gun"] or 0)

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
        "在充电量": round(open_kwh, 3),
        "在充预估金额": round(open_amount, 2),
        "待核对账金额": round(open_pending, 2),
        "本日关账金额": round(today_amount, 2),
        "档价口径": PRICE_MODE,
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
        "对账异常会话数": len(open_defects),
        "对账异常会话": open_defects,
        "关账金额对账异常": closed_defects,
        "占用态对账异常": occupancy_defects,
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    where = "WHERE p.site_id=?" if site_id else ""
    params = (site_id,) if site_id else ()
    rs = conn.execute(
        f"""SELECT p.*, s.name AS site_name, r.name AS rate_name,
                   se.id AS open_session_id, se.slip_no AS open_slip_no,
                   se.kwh AS open_kwh, se.amount AS open_amount,
                   se.opening_band, se.locked_peak, se.locked_flat, se.locked_valley,
                   se.price_locked, se.lock_at, se.pricing_state
            FROM piles p
            JOIN sites s ON s.id=p.site_id
            JOIN rate_plans r ON r.id=p.rate_plan_id
            LEFT JOIN sessions se ON se.id = (
                SELECT MAX(se2.id) FROM sessions se2
                 WHERE se2.pile_id=p.id AND se2.status IN ('充电中','结算中')
            )
            {where}
            ORDER BY p.id""",
        params,
    ).fetchall()
    out = []
    for raw in rs:
        item = dict(raw)
        if item.get("open_session_id"):
            v = verify_session(conn, item["open_session_id"])
            item["在充金额"] = v["重放金额"]
            item["在充电量"] = v["重放电量"]
            item["对账一致"] = v["一致"]
            item["对账问题"] = v["问题"]
        else:
            item["在充金额"] = 0.0
            item["在充电量"] = 0.0
            item["对账一致"] = True
            item["对账问题"] = []
        out.append(item)
    return out


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
    lock_counts = {
        r["rate_plan_id"]: r["n"]
        for r in conn.execute(
            "SELECT rate_plan_id, COUNT(*) AS n FROM sessions "
            "WHERE status IN ('充电中','结算中') GROUP BY rate_plan_id"
        ).fetchall()
    }
    out = []
    for r in rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall()):
        item = dict(r)
        item["当前档"] = band
        item["当前档价"] = plan_price_for_band(r, band)
        item["在途会话数"] = lock_counts.get(r["id"], 0)
        item["锁档说明"] = "新开会话按此表冻结锁档；在途会话沿用各自锁档，改档不回写。"
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
    where = "WHERE se.status=?" if status else ""
    params = (status,) if status else ()
    rs = conn.execute(
        f"""SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name,
                   (SELECT COALESCE(SUM(amount),0) FROM pulses WHERE session_id=se.id) AS replay_amount,
                   (SELECT COALESCE(SUM(delta_kwh),0) FROM pulses WHERE session_id=se.id) AS replay_kwh,
                   (SELECT COUNT(*) FROM pulses WHERE session_id=se.id) AS pulse_n
            FROM sessions se
            JOIN piles p ON p.id=se.pile_id
            JOIN guns g ON g.id=se.gun_id
            JOIN sites s ON s.id=se.site_id
            {where}
            ORDER BY se.id DESC
            {'' if status else 'LIMIT 200'}""",
        params,
    ).fetchall()
    out = []
    for raw in rs:
        item = dict(raw)
        item["金额一致"] = (
            round(float(item.get("amount") or 0.0), 2) == round(float(item["replay_amount"]), 2)
            and round(float(item.get("kwh") or 0.0), 3) == round(float(item["replay_kwh"]), 3)
        )
        out.append(item)
    return out


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
    verification = verify_session(conn, session_id, se)
    d = row_dict(se)
    d["脉冲"] = pulses
    d["事件"] = events
    d["档价口径"] = PRICE_MODE
    d["重放"] = estimate_replay(conn, session_id)
    d["预估金额"] = round(float(d.get("amount") or 0), 2)
    d["重放金额"] = verification["重放金额"]
    d["预估与重放一致"] = verification["材料一致"]
    d["对账"] = verification
    d["对账一致"] = verification["一致"]
    d["对账问题"] = verification["问题"]
    d["会话锁档"] = {
        "峰": verification["锁档"][BAND_PEAK],
        "平": verification["锁档"][BAND_FLAT],
        "谷": verification["锁档"][BAND_VALLEY],
        "锁档时间": d.get("lock_at"),
        "说明": "开充时刻冻结的整张峰平谷表；每笔脉冲按脉冲时刻档 × 此锁档价落价，改档不回写。",
    }
    d["开充档价快照"] = {
        "档": d.get("opening_band"),
        "单价": float(se["price_locked"]) if se["price_locked"] is not None else None,
        "锁档时间": d.get("lock_at"),
        "说明": "开充时刻快照，是会话锁档在当前档位的取值。",
    }
    d["费率表现价（仅参考，不改锁）"] = {
        "峰": float(se["peak_price"]),
        "平": float(se["flat_price"]),
        "谷": float(se["valley_price"]),
    }
    return d


def pulse_audit(conn, limit: int = 200) -> dict:
    """脉冲审计：逐笔脉冲与会话锁档逐笔对账，金额/电量/档价同命运。"""
    records = rows(
        conn.execute(
            """SELECT p.*, se.slip_no, se.status AS session_status,
                      se.locked_peak, se.locked_flat, se.locked_valley,
                      se.opening_band, se.price_locked, se.pricing_state
               FROM pulses p JOIN sessions se ON se.id=p.session_id
               ORDER BY p.id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    )
    out = []
    anomalies = 0
    for p in records:
        expected = locked_price_for_band(p, p["band"]) if p["band"] in BAND_LOCK_COLUMN else None
        unit_ok = (
            p["unit_price"] is not None
            and expected is not None
            and abs(float(p["unit_price"]) - expected) <= 1e-9
        )
        amount_ok = (
            p["amount"] is not None
            and p["unit_price"] is not None
            and round(float(p["delta_kwh"]) * float(p["unit_price"]), 2)
            == round(float(p["amount"]), 2)
        )
        if not (unit_ok and amount_ok):
            anomalies += 1
        out.append(
            {
                **p,
                "预期锁档单价": expected,
                "档价一致": unit_ok,
                "金额一致": amount_ok,
            }
        )
    return {"档价口径": PRICE_MODE, "脉冲": out, "异常数": anomalies}


def reconcile_all(conn) -> dict:
    sessions = rows(
        conn.execute(
            "SELECT * FROM sessions WHERE status IN ('充电中','结算中') ORDER BY id"
        ).fetchall()
    )
    items = []
    for se in sessions:
        v = verify_session(conn, se["id"], se)
        items.append(
            {
                "会话编号": se["id"],
                "会话号": se["slip_no"],
                "桩位": se["pile_id"],
                "状态": se["status"],
                **v,
            }
        )
    return {
        "档价口径": PRICE_MODE,
        "会话": items,
        "异常数": sum(1 for x in items if not x["一致"]),
    }
