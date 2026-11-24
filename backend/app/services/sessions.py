from __future__ import annotations
from datetime import datetime

from fastapi import HTTPException

from app.bands import (
    BAND_FLAT,
    BAND_PEAK,
    BAND_VALLEY,
    PRICE_MODE,
    PRICING_GAP,
    PRICING_OK,
    band_for,
)
from app.services.core import (
    estimate_replay,
    evidence_issues,
    get_setting,
    is_day_sealed,
    locked_price_for_band,
    now_str,
    plan_lock,
    today,
    verify_session,
)


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def _sync_material(conn, session_id: int) -> dict:
    """把会话缓存的电量/金额回退到脉冲重放值；不触碰脉冲行与锁档。"""
    replay = estimate_replay(conn, session_id)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (replay["重放电量"], replay["重放金额"], session_id),
    )
    return replay


def open_session(conn, pile_id: int, gun_id: int | None = None, at: datetime | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (pile["rate_plan_id"],)).fetchone()
    if not plan:
        raise HTTPException(400, detail="桩位费率表不存在，不可开充")

    # 锁档入口：整张峰平谷表冻结到会话；开充档价 = 锁档在该档的取值。
    lock = plan_lock(plan)
    opening_band = band_for(at or datetime.now())
    price = lock[opening_band]
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,
                                 kwh,amount,price_locked,opening_band,price_mode,
                                 locked_peak,locked_flat,locked_valley,lock_at,pricing_state,
                                 started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            opening_band,
            PRICE_MODE,
            lock[BAND_PEAK],
            lock[BAND_FLAT],
            lock[BAND_VALLEY],
            (at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
            PRICING_OK,
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (
            sid,
            "开会话",
            ts,
            f"锁档 峰{lock[BAND_PEAK]}/平{lock[BAND_FLAT]}/谷{lock[BAND_VALLEY]}；"
            f"开充 {opening_band}档 {price}（{PRICE_MODE}）",
        ),
    )
    return {
        "会话号": slip,
        "会话编号": sid,
        "会话锁档": {"峰": lock[BAND_PEAK], "平": lock[BAND_FLAT], "谷": lock[BAND_VALLEY]},
        "开充档价快照": {"档": opening_band, "单价": price},
        "档价口径": PRICE_MODE,
        "状态": "充电中",
    }


def add_pulse(
    conn,
    session_id: int,
    delta_kwh: float,
    power_kw: float = 0,
    at: datetime | None = None,
) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    dt = at or datetime.now()
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    band = band_for(dt)
    unit_price = locked_price_for_band(se, band)
    if unit_price is None or se["pricing_state"] != PRICING_OK:
        # 命中「半截/漂价」会话：拒绝继续写入，须先对账/回滚，避免双套价。
        raise HTTPException(409, detail="该会话档价锁不全或处待核态，禁止继续记脉冲")
    pulse_amount = round(float(delta_kwh) * unit_price, 2)
    conn.execute(
        """INSERT INTO pulses(session_id,delta_kwh,power_kw,unit_price,band,amount,rate_plan_id,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (session_id, delta_kwh, power_kw, unit_price, band, pulse_amount, se["rate_plan_id"], ts),
    )
    replay = _sync_material(conn, session_id)

    # 写入后同事务对账：任一笔档价证据对不上即整笔回滚（含脉冲行与累计金额），回执不得假绿。
    verification = verify_session(conn, session_id)
    issues = evidence_issues(verification)
    if not verification["材料一致"]:
        issues = issues + ["会话累计电量/金额≠脉冲合计"]
    if issues:
        reason = "；".join(issues)
        raise HTTPException(409, detail=f"脉冲写入对账不一致，已整笔回滚：{reason}")
    conn.execute(
        "UPDATE sessions SET pricing_state=? WHERE id=?", (PRICING_OK, session_id)
    )

    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh} {band}档 ×锁档 {unit_price} = {pulse_amount}"),
    )
    return {
        "会话编号": session_id,
        "累计电量": replay["重放电量"],
        "预估金额": replay["重放金额"],
        "本笔档": band,
        "本笔单价": unit_price,
        "本笔单价来源": "会话锁档",
        "本笔金额": pulse_amount,
        "档价口径": PRICE_MODE,
        "对账一致": True,
        "脉冲灯": "绿",
    }


def settle(conn, session_id: int, at: datetime | None = None) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")

    # 关账入口先对账：材料不一致按重放回退；证据缺口则整笔回滚并冻结金额宣称。
    verification = verify_session(conn, session_id, se)
    if not verification["材料一致"]:
        _sync_material(conn, session_id)
        verification = verify_session(conn, session_id)
    issues = evidence_issues(verification)
    if issues:
        raise HTTPException(
            409,
            detail="关账被拒：脉冲档价证据与会话锁档对不上，需先对账；" + "；".join(issues),
        )

    ts = now_str()
    amount = verification["重放金额"]
    kwh = verification["重放电量"]
    conn.execute(
        "UPDATE sessions SET status='已关账', kwh=?, amount=?, closed_at=?, pricing_state=? WHERE id=?",
        (kwh, amount, ts, PRICING_OK, session_id),
    )
    conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"电量{kwh} 金额{amount}（脉冲重放 {PRICE_MODE}）"),
    )
    return {
        "会话编号": session_id,
        "电量": kwh,
        "金额": amount,
        "状态": "已关账",
        "档价口径": PRICE_MODE,
        "对账一致": True,
    }


def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        (f"超过{minutes}分钟未关", session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟"),
    )
    return {"会话编号": session_id, "状态": "已超时"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    verification = verify_session(conn, session_id, se)
    if not verification["材料一致"]:
        _sync_material(conn, session_id)
        verification = verify_session(conn, session_id)
    issues = evidence_issues(verification)
    if issues:
        raise HTTPException(
            409,
            detail="中断被拒：脉冲档价证据与会话锁档对不上，需先对账；" + "；".join(issues),
        )
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
        (ts, reason, session_id),
    )
    conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, reason),
    )
    return {"会话编号": session_id, "状态": "已中断", "原因": reason}


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "占用", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    open_se = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中')",
        (pile_id,),
    ).fetchone()
    if open_se and status in ("故障", "维护"):
        raise HTTPException(400, detail="仍有在充会话，不可直接改故障或维护")
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}


def _repair_evidence(conn, session_id: int) -> list[str]:
    """按会话锁档回退落账金额：合法的档，单价与金额一律回正到锁档价。

    这是「档价不一致则整笔回滚并回退落账金额」的对账修复面；档非法等无法由锁档
    推导的行不动，保留为硬缺口，需人工核查。返回修复说明供审计。
    """
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    repairs = []
    for p in conn.execute(
        "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall():
        band = p["band"]
        lock_price = locked_price_for_band(se, band) if band in (BAND_PEAK, BAND_FLAT, BAND_VALLEY) else None
        if lock_price is None:
            continue
        expected_amount = round(float(p["delta_kwh"]) * lock_price, 2)
        if (
            p["unit_price"] is None
            or abs(float(p["unit_price"]) - lock_price) > 1e-9
            or p["amount"] is None
            or round(float(p["amount"]), 2) != expected_amount
        ):
            conn.execute(
                "UPDATE pulses SET unit_price=?, amount=? WHERE id=?",
                (lock_price, expected_amount, p["id"]),
            )
            repairs.append(
                f"脉冲#{p['id']} {band}档 回正为 {lock_price}×{p['delta_kwh']}={expected_amount}"
            )
    if repairs:
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (session_id, "档价回正", now_str(), "；".join(repairs)),
        )
    return repairs


def reconcile_session(conn, session_id: int) -> dict:
    """对账入口：档价回正到会话锁档 → 缓存按重放回退 → 余下硬缺口标待核冻结。"""
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    repairs = _repair_evidence(conn, session_id)
    reverted = False
    before = verify_session(conn, session_id)
    if not before["材料一致"]:
        replay = _sync_material(conn, session_id)
        reverted = True
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (
                session_id,
                "对账回退",
                now_str(),
                f"会话缓存回退到脉冲重放：电量{replay['重放电量']} 金额{replay['重放金额']}",
            ),
        )
    after = verify_session(conn, session_id)
    if after["一致"]:
        conn.execute(
            "UPDATE sessions SET pricing_state=?, note='' WHERE id=?",
            (PRICING_OK, session_id),
        )
        state = PRICING_OK
    else:
        reason = "；".join(after["问题"])
        conn.execute(
            "UPDATE sessions SET pricing_state=?, note=? WHERE id=?",
            (PRICING_GAP, f"待核：{reason}", session_id),
        )
        state = PRICING_GAP
    return {
        "会话编号": session_id,
        "档价口径": PRICE_MODE,
        "已回正脉冲": repairs,
        "已回退材料": reverted,
        "对账": after,
        "对账状态": state,
    }
