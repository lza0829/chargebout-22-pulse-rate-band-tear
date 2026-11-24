from __future__ import annotations
from datetime import datetime
from app.services import rate_surface

from fastapi import HTTPException

from app.services.core import (
    PRICE_MODE,
    estimate_replay,
    get_setting,
    is_day_sealed,
    now_str,
    resolve_band_price,
    today,
)


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


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

    # 开充快照：仅记录开充时刻的档与价，作为开场记录；
    # 后续脉冲按写入时刻档价独立落价（全局口径=逐笔脉冲落价）。
    snap = resolve_band_price(conn, pile["rate_plan_id"], at)
    price = snap["price"]
    opening_band = snap["band"]
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,opening_band,price_mode,started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "开会话", ts, f"开充快照 {opening_band}档 {price}（{PRICE_MODE}）"),
    )
    return {
        "会话号": slip,
        "会话编号": sid,
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
    # 每笔脉冲按「写入时刻」峰平谷取价并永久落价，不受开充快照与后续改档影响。
    snap = resolve_band_price(conn, se["rate_plan_id"], dt)
    unit_price = snap["price"]
    pulse_amount = round(float(delta_kwh) * unit_price, 2)
    conn.execute(
        """INSERT INTO pulses(session_id,delta_kwh,power_kw,unit_price,band,amount,rate_plan_id,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (session_id, delta_kwh, power_kw, unit_price, snap["band"], pulse_amount, se["rate_plan_id"], ts),
    )
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    # 预估金额 = 该会话全部脉冲金额之和（可重放，不按单价乘总量）。
    replay = estimate_replay(conn, session_id)
    amount = replay["重放金额"]
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh} {snap['band']}档 {unit_price}={pulse_amount}"),
    )
    return {
        "会话编号": session_id,
        "累计电量": round(new_kwh, 3),
        "预估金额": amount,
        "本笔档": snap["band"],
        "本笔单价": unit_price,
        "本笔金额": pulse_amount,
        "档价口径": PRICE_MODE,
    }


def settle(conn, session_id: int, at: datetime | None = None) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    ts = now_str()
    # 关账金额 = 脉冲重放金额；电量 = 脉冲重放电量。
    replay = estimate_replay(conn, session_id)
    amount = replay["重放金额"]
    kwh = replay["重放电量"]
    conn.execute(
        "UPDATE sessions SET status='已关账', kwh=?, amount=?, closed_at=? WHERE id=?",
        (kwh, amount, ts, session_id),
    )
    conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"金额{amount}（脉冲重放 {PRICE_MODE}）"),
    )
    return {
        "会话编号": session_id,
        "电量": kwh,
        "金额": amount,
        "状态": "已关账",
        "档价口径": PRICE_MODE,
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
        # 底座默认：置故障/维护时若仍有在充，拒绝（后续题可改互斥）
        raise HTTPException(400, detail="仍有在充会话，不可直接改故障或维护")
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}

_add_pulse_impl = add_pulse


def add_pulse(*args, **kwargs):
    out = _add_pulse_impl(*args, **kwargs)
    return rate_surface.after_pulse(args[0] if args else None, out, *args[1:], **kwargs)

