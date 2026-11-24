from __future__ import annotations

from fastapi import HTTPException

from app.services.core import now_str


def _get_plan(conn, rate_plan_id: int):
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        raise HTTPException(404, detail="费率表不存在")
    return plan


def update_rate(conn, rate_plan_id: int, peak: float, flat: float, valley: float, note: str = "") -> dict:
    """改档写口：整表峰平谷同事务改写 + 记一条审计事件。

    只改费率表本身；绝不回写任何会话的 price_locked / opening_band，
    也绝不重算任何已落价脉冲——已写脉冲的金额是既成事实。
    在途会话的后续脉冲会按新表当时档价落价，预估随脉冲自然累进。
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
    return {
        "费率编号": rate_plan_id,
        "名称": plan["name"],
        "旧": {"峰": old[0], "平": old[1], "谷": old[2]},
        "新": {"峰": new[0], "平": new[1], "谷": new[2]},
        "改档时间": ts,
        "说明": "仅影响此后写入的脉冲；已落价脉冲与会话开充快照均不变。",
    }
