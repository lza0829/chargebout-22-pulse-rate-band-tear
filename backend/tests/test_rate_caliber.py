"""充电过程中档价口径一致性夹具。

全局唯一口径：会话锁档计价。
- 开充时把整张峰平谷表冻结到会话（locked_peak/locked_flat/locked_valley）；
- 每笔脉冲按「脉冲时刻所属档」×「会话锁档价」落价，永久记下单价与金额；
- 费率页改档只影响此后新开会话的新锁档，绝不回写既有锁档或已落价脉冲；
- 唯一权威 = 会话档价锁 ∪ 脉冲行档价 ∪ 可重放累计金额；任一对不上即整笔回滚。

以下夹具覆盖：
  A. 开充后改峰谷 → 旧会话不漂价，新会话用新锁档；
  B. 跨时段连记两笔 → 各自用会话锁档价累进；
  C. 改档与记脉冲半截并发 → 预估与详情长期一致、无不可解释预估；
  D. 费率页/会话详情/总览预估桶/脉冲回执同口径同刷；
  E. 结算金额由脉冲重放；
  F. 费率改档留审计事件；
  G. 假计价收口五连：回执档价与行档拧花、改档后漂价、金额加电量未加、
     脉冲改档并发半截、关账吃进漂价。
"""
from __future__ import annotations

import threading
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.bands import PRICE_MODE
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


# ---------- A. 开会话后改峰谷：旧会话锁档不漂 ----------

def test_rate_change_does_not_drift_open_session(conn):
    # 12:00 平价开充；锁档=通用峰谷 峰1.2/平0.9/谷0.55
    sid = _open(conn, _dt(12))["会话编号"]
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert se["opening_band"] == "平"
    assert se["price_locked"] == 0.9
    assert (se["locked_peak"], se["locked_flat"], se["locked_valley"]) == (1.2, 0.9, 0.55)

    # 改费率：峰 2.0 / 平 1.0 / 谷 0.6
    rate_svc.update_rate(conn, 1, 2.0, 1.0, 0.6, note="改价")

    # 会话锁档不得被回写
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert (se["locked_peak"], se["locked_flat"], se["locked_valley"]) == (1.2, 0.9, 0.55)
    assert se["price_locked"] == 0.9

    # 09:00 峰时段脉冲，按会话锁档峰价 1.2，而非改档后的 2.0
    r = _pulse(conn, sid, 10, _dt(9))
    assert r["本笔档"] == "峰"
    assert r["本笔单价"] == 1.2
    assert r["本笔单价来源"] == "会话锁档"
    assert r["本笔金额"] == 12.0
    assert r["预估金额"] == 12.0

    # 同一桩新开会话拿到新锁档；旧会话仍不漂
    other = _open(conn, _dt(9), pile=2)["会话编号"]
    se2 = conn.execute("SELECT * FROM sessions WHERE id=?", (other,)).fetchone()
    assert se2["locked_peak"] == 2.0

    d = core.session_detail(conn, sid)
    assert d["会话锁档"]["峰"] == 1.2
    assert d["预估金额"] == 12.0
    assert d["重放金额"] == 12.0
    assert d["预估与重放一致"] is True
    assert d["对账一致"] is True


# ---------- B. 跨时段连记两笔：按锁档累进 ----------

def test_cross_band_two_pulses_accumulate_by_locked_band(conn):
    sid = _open(conn, _dt(5))["会话编号"]  # 05:00 谷
    r1 = _pulse(conn, sid, 4, _dt(6, 30))  # 06:30 谷，锁谷 0.55
    assert (r1["本笔档"], r1["本笔单价"], r1["本笔金额"]) == ("谷", 0.55, 2.2)
    r2 = _pulse(conn, sid, 3, _dt(9, 30))  # 09:30 峰，锁峰 1.2
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
        # 无半截改价：所有脉冲都有可解释单价与金额，且单价恒等于会话锁档
        rows_ = c.execute("SELECT * FROM pulses WHERE session_id=?", (sid,)).fetchall()
        assert len(rows_) == 6
        se = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        lock = {"峰": se["locked_peak"], "平": se["locked_flat"], "谷": se["locked_valley"]}
        for p in rows_:
            assert p["unit_price"] is not None
            assert p["band"] in ("峰", "平", "谷")
            assert p["unit_price"] == lock[p["band"]]
            assert p["amount"] == round(p["delta_kwh"] * p["unit_price"], 2)

        replay = core.estimate_replay(c, sid)["重放金额"]
        assert se["amount"] == replay
        # 详情与预估长期拧花：不得出现预估≠重放
        d = core.session_detail(c, sid)
        assert d["预估与重放一致"] is True
        assert d["对账一致"] is True

        # 总览预估桶同源
        ov = core.overview(c)
        assert ov["在充预估金额"] == replay
        assert ov["档价口径"] == PRICE_MODE
        assert ov["对账异常会话数"] == 0


# ---------- D. 费率页 / 会话详情 / 总览同口径 ----------

def test_overview_bucket_and_session_detail_share_caliber(conn):
    sid1 = _open(conn, _dt(12), pile=1)["会话编号"]
    sid2 = _open(conn, _dt(12), pile=2)["会话编号"]
    _pulse(conn, sid1, 5, _dt(9))   # 峰，锁峰 1.2 → 6.0
    _pulse(conn, sid2, 2, _dt(2))   # 谷，锁谷 0.55 → 1.1
    total = _replay_amount(conn, sid1) + _replay_amount(conn, sid2)
    ov = core.overview(conn)
    assert ov["在充预估金额"] == round(total, 2)
    assert ov["在充电量"] == 7.0

    d1 = core.session_detail(conn, sid1)
    assert d1["预估金额"] == d1["重放金额"] == 6.0
    assert d1["档价口径"] == PRICE_MODE
    # 关联费率页：详情带出该会话锁档，且与开充时的费率表一致
    assert d1["会话锁档"]["峰"] == 1.2


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


# ---------- G. 假计价收口五连 ----------

def test_pulse_receipt_band_matches_pulse_row(conn):
    """夹具1：脉冲回执的绿档与脉冲行档价不得拧花。"""
    sid = _open(conn, _dt(12))["会话编号"]
    r = _pulse(conn, sid, 6, _dt(9))
    row = conn.execute("SELECT * FROM pulses WHERE session_id=?", (sid,)).fetchone()
    assert r["本笔档"] == row["band"] == "峰"
    assert r["本笔单价"] == row["unit_price"] == 1.2
    assert r["本笔金额"] == row["amount"]
    # 回执不再注入任何假 surface/计价档字段
    assert "surface_ok" not in r and "计价档" not in r
    assert r["脉冲灯"] == "绿" and r["对账一致"] is True


def test_legacy_open_session_does_not_drift_after_rate_change(conn):
    """夹具2：改档后旧会话脉冲仍按旧锁档，不漂价。"""
    sid = _open(conn, _dt(12))["会话编号"]
    before = _pulse(conn, sid, 2, _dt(9))
    rate_svc.update_rate(conn, 1, 9.0, 9.0, 9.0, note="暴涨")
    after = _pulse(conn, sid, 2, _dt(19))
    assert before["本笔单价"] == after["本笔单价"] == 1.2
    assert _replay_amount(conn, sid) == 4.8


def test_amount_without_kwh_is_reverted_from_replay(conn):
    """夹具3：金额已加、电量未加 → 对账回退到脉冲重放。"""
    sid = _open(conn, _dt(12))["会话编号"]
    _pulse(conn, sid, 5, _dt(9))  # 6.0
    # 人为制造「金额加、电量未加」的半截缓存
    conn.execute("UPDATE sessions SET kwh=0, amount=99.0 WHERE id=?", (sid,))
    v = core.verify_session(conn, sid)
    assert v["一致"] is False and v["材料一致"] is False

    # 关账入口不得吃进漂价金额，而是按重放回退
    r = sess_svc.settle(conn, sid)
    assert r["金额"] == 6.0 and r["电量"] == 5.0
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert se["amount"] == 6.0 and se["kwh"] == 5.0


def test_concurrent_half_write_never_double_prices():
    """夹具4：脉冲与改档并发后不得双套价同亮。"""
    with db() as c:
        sid = _open(c, _dt(12))["会话编号"]
    errs = []

    def pulses():
        for h in (8, 9, 10):
            try:
                with db() as c:
                    sess_svc.add_pulse(c, sid, 1.0, 7, at=_dt(h))
            except Exception as e:  # pragma: no cover
                errs.append(e)

    def rate():
        for i in range(3):
            with db() as c:
                rate_svc.update_rate(c, 1, 5.0 + i, 5.0 + i, 5.0 + i, note=f"并发{i}")

    t1 = threading.Thread(target=pulses)
    t2 = threading.Thread(target=rate)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errs, errs
    with db() as c:
        se = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        assert se["locked_peak"] == 1.2  # 锁档未被并发改价污染
        units = {r["unit_price"] for r in c.execute("SELECT unit_price FROM pulses WHERE session_id=?", (sid,))}
        assert units == {1.2}  # 全部单套价
        v = core.verify_session(c, sid)
        assert v["一致"] is True


def test_settle_rejects_evidence_gap(conn):
    """夹具5a：脉冲档价与锁档对不上 → 关账拒绝，金额宣称冻结。"""
    sid = _open(conn, _dt(12))["会话编号"]
    _pulse(conn, sid, 5, _dt(9))
    # 旁路改脉冲行单价（模拟历史漂价残留），使证据撕裂
    conn.execute("UPDATE pulses SET unit_price=9.9 WHERE session_id=?", (sid,))
    v = core.verify_session(conn, sid)
    assert v["一致"] is False
    assert any("单价≠会话锁档" in x for x in v["问题"])
    with pytest.raises(HTTPException) as ei:
        sess_svc.settle(conn, sid)
    assert ei.value.status_code == 409
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert se["status"] == "充电中"  # 未关账，未被漂价金额污染


def test_settle_rejects_priced_stream_that_disagrees(conn):
    """夹具5b：关账不得吃进与锁档不一致的脉冲金额。"""
    sid = _open(conn, _dt(12))["会话编号"]
    _pulse(conn, sid, 4, _dt(9))  # 4.8
    # 篡改脉冲金额但保留合法单价 → 金额≠电量×单价
    conn.execute("UPDATE pulses SET amount=100.0 WHERE session_id=?", (sid,))
    conn.execute("UPDATE sessions SET amount=100.0 WHERE id=?", (sid,))
    with pytest.raises(HTTPException) as ei:
        sess_svc.settle(conn, sid)
    assert ei.value.status_code == 409


def test_overview_excludes_unreconciled_from_aligned_bucket(conn):
    """总览不得把证据撕裂的会话算进已对齐金额。"""
    good = _open(conn, _dt(12), pile=1)["会话编号"]
    bad = _open(conn, _dt(12), pile=2)["会话编号"]
    _pulse(conn, good, 5, _dt(9))  # 6.0
    _pulse(conn, bad, 2, _dt(9))   # 遗留
    # 撕坏 bad 的证据
    conn.execute("UPDATE pulses SET band='错' WHERE session_id=? AND id=(SELECT MIN(id) FROM pulses WHERE session_id=?)", (bad, bad))
    ov = core.overview(conn)
    assert ov["在充预估金额"] == 6.0  # 只含对齐会话
    assert ov["待核对账金额"] == round(_replay_amount(conn, bad), 2)
    assert ov["对账异常会话数"] == 1


def test_audit_flags_band_price_tear(conn):
    """脉冲审计逐笔对锁档，档价撕裂可核。"""
    sid = _open(conn, _dt(12))["会话编号"]
    _pulse(conn, sid, 3, _dt(9))
    ok = core.pulse_audit(conn)
    assert ok["异常数"] == 0
    conn.execute("UPDATE pulses SET band='谷' WHERE session_id=?", (sid,))  # 档与单价不再匹配
    torn = core.pulse_audit(conn)
    assert torn["异常数"] >= 1


def test_reconcile_repairs_legal_band_evidence_to_lock(conn):
    """对账把合法档的漂价单价/金额回正到会话锁档，随后可关账。"""
    sid = _open(conn, _dt(12))["会话编号"]
    _pulse(conn, sid, 5, _dt(9))  # 峰 1.2 → 6.0
    conn.execute("UPDATE pulses SET unit_price=9.9, amount=49.5 WHERE session_id=?", (sid,))
    conn.execute("UPDATE sessions SET amount=49.5 WHERE id=?", (sid,))
    assert core.verify_session(conn, sid)["一致"] is False

    r = sess_svc.reconcile_session(conn, sid)
    assert r["对账状态"] == "锁档一致"
    assert r["对账"]["一致"] is True
    assert r["已回正脉冲"]
    row = conn.execute("SELECT * FROM pulses WHERE session_id=?", (sid,)).fetchone()
    assert (row["unit_price"], row["amount"]) == (1.2, 6.0)
    assert sess_svc.settle(conn, sid)["金额"] == 6.0


def test_reconcile_keeps_illegal_band_as_hard_gap(conn):
    """档非法无法由锁档推导，对账不得伪绿，仍标待核并冻结金额宣称。"""
    sid = _open(conn, _dt(12))["会话编号"]
    _pulse(conn, sid, 5, _dt(9))
    conn.execute("UPDATE pulses SET band='错' WHERE session_id=?", (sid,))
    r = sess_svc.reconcile_session(conn, sid)
    assert r["对账"]["一致"] is False
    assert r["对账状态"] == "档价待核"
    with pytest.raises(HTTPException) as ei:
        sess_svc.settle(conn, sid)
    assert ei.value.status_code == 409
