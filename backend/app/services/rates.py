from __future__ import annotations

from fastapi import HTTPException

from app.bands import PRICE_MODE
from app.services.core import now_str


def _get_plan(conn, rate_plan_id: int):
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        raise HTTPException(404, detail="费率表不存在")
    return plan


def update_rate(conn, rate_plan_id: int, peak: float, flat: float, valley: float, note: str = "") -> dict:
    """改档写口：整表峰平谷同事务改写 + 记一条审计事件。

    只改费率表本身；绝不回写任何会话锁档或已落价脉冲——已写脉冲的金额是既成事实。
    在途会话沿用各自锁档，新开会话才按新表冻结锁档。
    """
    for label, v in (("峰价", peak), ("平价", flat), ("谷价", valley)):
        if v < 0:
            raise HTTPException(400, detail=f"{label}不得为负")
    plan = _get_plan(conn, rate_plan_id)
    old = (float(plan["peak_price"]), float(plan["flat_price"]), float(plan["valley_price"]))
    new = (float(peak), float(flat), float(valley))
    conn.execute(
        "UPDATE rate_plans SET peak_price=?, flat_price=?, valley_price=? WHERE id=?",
        (new[0], new[1], new[2], rate_plan_id),
    )
    ts = now_str()
    conn.execute(
        """INSERT INTO rate_change_events(rate_plan_id,old_peak,old_flat,old_valley,
                                          new_peak,new_flat,new_valley,changed_at,note)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (rate_plan_id, old[0], old[1], old[2], new[0], new[1], new[2], ts, note),
    )
    in_flight = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE rate_plan_id=? AND status IN ('充电中','结算中')",
        (rate_plan_id,),
    ).fetchone()["n"]
    return {
        "费率编号": rate_plan_id,
        "名称": plan["name"],
        "旧": {"峰": old[0], "平": old[1], "谷": old[2]},
        "新": {"峰": new[0], "平": new[1], "谷": new[2]},
        "改档时间": ts,
        "在途会话数": in_flight,
        "说明": (
            f"档价口径 {PRICE_MODE}：在途会话沿用各自锁档，已落价脉冲不变；"
            "仅此后新开会话按新表冻结锁档。"
        ),
    }
