"""充电过程中档价口径一致性夹具。

全局口径：逐笔脉冲落价。每笔脉冲在写入时刻按峰平谷取价并落价；
开充快照只作开场记录。以下夹具覆盖：
  A. 开充后改峰谷 → 后续脉冲按新表取价，开充快照不动；
  B. 跨时段连记两笔脉冲 → 各自档价累进；
  C. 改档与记脉冲半截并发 → 预估与详情长期一致、无不可解释预估；
  D. 费率页/会话详情/总览预估桶/脉冲回执同口径同刷。
"""
from __future__ import annotations

import threading
from datetime import datetime

from app.db import db
from app.services import core
from app.services import rates as rate_svc
from app.services import sessions as sess_svc


def _dt(h, m=0):
    return datetime(2026, 11, 24, h, m, 0)


def _open(conn, at, pile=1):
    return sess_svc.open_session(conn, pile, None, at=at)


def _pulse(conn, sid, kwh, at):
    return sess_svc.add_pulse(conn, sid, kwh, 7, at=at)


def _replay_amount(conn, sid):
    return core.estimate_replay(conn, sid)["重放金额"]


# ---------- A. 开会话后改峰谷 ----------

def test_opening_snapshot_not_overwritten_by_rate_change(conn):
    # 12:00 平价开充，快照=平价 0.9
    sid = _open(conn, _dt(12))["会话编号"]
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert se["opening_band"] == "平"
    assert se["price_locked"] == 0.9

    # 改费率：峰 2.0 / 平 1.0 / 谷 0.6
    rate_svc.update_rate(conn, 1, 2.0, 1.0, 0.6, note="改价")

    # 开充快照不得被回写
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert se["price_locked"] == 0.9
    assert se["opening_band"] == "平"

    # 09:00 峰时段脉冲，应按新峰价 2.0，而非开充快照 0.9
    r = _pulse(conn, sid, 10, _dt(9))
    assert r["本笔档"] == "峰"
    assert r["本笔单价"] == 2.0
    assert r["本笔金额"] == 20.0
    assert r["预估金额"] == 20.0

    d = core.session_detail(conn, sid)
    assert d["开充档价快照"]["单价"] == 0.9
    assert d["预估金额"] == 20.0
    assert d["重放金额"] == 20.0
    assert d["预估与重放一致"] is True


# ---------- B. 跨时段连记两笔 ----------

def test_cross_band_two_pulses_accumulate_by_band(conn):
    sid = _open(conn, _dt(5))["会话编号"]  # 05:00 谷
    r1 = _pulse(conn, sid, 4, _dt(6, 30))  # 06:30 谷 0.55
    assert (r1["本笔档"], r1["本笔单价"], r1["本笔金额"]) == ("谷", 0.55, 2.2)
    r2 = _pulse(conn, sid, 3, _dt(9, 30))  # 09:30 峰 1.2
    assert (r2["本笔档"], r2["本笔单价"], r2["本笔金额"]) == ("峰", 1.2, 3.6)
    assert r2["预估金额"] == 5.8
    assert r2["累计电量"] == 7.0

    d = core.session_detail(conn, sid)
    assert d["预估金额"] == 5.8
    assert d["重放金额"] == 5.8
    bands = {b["band"]: b for b in d["重放"]["分档"]}
    assert bands["谷"]["amount"] == 2.2
    assert bands["峰"]["amount"] == 3.6


# ---------- C. 改档与记脉冲半截并发 ----------

def test_concurrent_rate_change_and_pulses_stay_consistent():
    with db() as c:
        sid = _open(c, _dt(12))["会话编号"]

    errors = []
    barrier = threading.Barrier(2)

    def pulse_worker():
        try:
            barrier.wait()
            for i, h in enumerate([9, 10, 19, 20, 2, 3]):
                with db() as c:
                    sess_svc.add_pulse(c, sid, 1.0, 7, at=_dt(h, i))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def rate_worker():
        try:
            barrier.wait()
            for i in range(6):
                with db() as c:
                    rate_svc.update_rate(c, 1, 1.2 + i, 0.9 + i, 0.55 + i, note=f"并发{i}")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t1 = threading.Thread(target=pulse_worker)
    t2 = threading.Thread(target=rate_worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, errors

    with db() as c:
        # 无半截改价：所有脉冲都有可解释单价与金额
        rows_ = c.execute("SELECT * FROM pulses WHERE session_id=?", (sid,)).fetchall()
        assert len(rows_) == 6
        for p in rows_:
            assert p["unit_price"] is not None
            assert p["band"] in ("峰", "平", "谷")
            assert p["amount"] == round(p["delta_kwh"] * p["unit_price"], 2)

        replay = core.estimate_replay(c, sid)["重放金额"]
        se = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        assert se["amount"] == replay
        # 详情与预估长期拧花：不得出现预估≠重放
        d = core.session_detail(c, sid)
        assert d["预估与重放一致"] is True

        # 总览预估桶同源
        ov = core.overview(c)
        assert ov["在充预估金额"] == replay
        assert ov["档价口径"] == "逐笔脉冲落价"


# ---------- D. 费率页 / 会话详情 / 总览同口径 ----------

def test_overview_bucket_and_session_detail_share_caliber(conn):
    sid1 = _open(conn, _dt(12), pile=1)["会话编号"]
    sid2 = _open(conn, _dt(12), pile=2)["会话编号"]
    _pulse(conn, sid1, 5, _dt(9))   # 峰 1.2 → 6.0
    _pulse(conn, sid2, 2, _dt(2))   # 谷 0.55 → 1.1
    total = _replay_amount(conn, sid1) + _replay_amount(conn, sid2)
    ov = core.overview(conn)
    assert ov["在充预估金额"] == round(total, 2)
    assert ov["在充电量"] == 7.0

    d1 = core.session_detail(conn, sid1)
    assert d1["预估金额"] == d1["重放金额"] == 6.0
    assert d1["档价口径"] == "逐笔脉冲落价"
    # 关联费率页：详情带出该费率当前档价，且与费率表一致
    rates = {r["id"]: r for r in core.list_rates(conn)}
    assert d1["费率当前档价"]["峰"] == rates[1]["peak_price"]


# ---------- E. 结算金额由脉冲重放 ----------

def test_settle_amount_is_pulse_replay(conn):
    sid = _open(conn, _dt(12))["会话编号"]
    _pulse(conn, sid, 4, _dt(6, 30))  # 谷 0.55 → 2.20
    _pulse(conn, sid, 3, _dt(9, 30))  # 峰 1.20 → 3.60
    replay = _replay_amount(conn, sid)
    r = sess_svc.settle(conn, sid)
    assert r["金额"] == replay == 5.8
    assert r["电量"] == 7.0
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert se["status"] == "已关账"
    assert se["amount"] == 5.8


# ---------- F. 费率改档留审计事件 ----------

def test_rate_change_writes_audit_event(conn):
    rate_svc.update_rate(conn, 1, 1.5, 1.0, 0.6, note="审计")
    evs = core.list_rate_changes(conn, 1)
    assert evs and evs[0]["new_peak"] == 1.5
    assert evs[0]["old_peak"] == 1.2
    assert evs[0]["note"] == "审计"
