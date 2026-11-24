from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH
from app.bands import (
    BAND_COLUMN,
    BAND_FLAT,
    BAND_LOCK_COLUMN,
    BAND_ORDER,
    BAND_PEAK,
    BAND_VALLEY,
    PRICE_MODE,
    band_for,
    parse_dt,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  opening_band TEXT,
  price_mode TEXT NOT NULL DEFAULT '会话锁档计价',
  locked_peak REAL,
  locked_flat REAL,
  locked_valley REAL,
  lock_at TEXT,
  pricing_state TEXT NOT NULL DEFAULT '锁档一致',
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  unit_price REAL,
  band TEXT,
  amount REAL,
  rate_plan_id INTEGER,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS rate_change_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rate_plan_id INTEGER NOT NULL,
  old_peak REAL NOT NULL,
  old_flat REAL NOT NULL,
  old_valley REAL NOT NULL,
  new_peak REAL NOT NULL,
  new_flat REAL NOT NULL,
  new_valley REAL NOT NULL,
  changed_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None → 显式 BEGIN IMMEDIATE，读-改-写整段持写锁，
    # 使「改费率」与「记脉冲」串行，杜绝半截改价与预估/落账拧花。
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧库补列；新建库由 SCHEMA 直接建好。

    锁档迁移一律从「会话自身证据」回填，绝不按当前费率表现价伪造锁档——
    否则改档后旧会话会被漂价。历史脉冲若从未落价，也按其所属档的会话锁档落价，
    使脉冲行档价与累计金额恒可重放。
    """
    sess_cols = _columns(conn, "sessions")
    added_lock = False
    for col in ("locked_peak", "locked_flat", "locked_valley", "lock_at"):
        if col not in sess_cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} REAL" if col != "lock_at" else "ALTER TABLE sessions ADD COLUMN lock_at TEXT")
            added_lock = True
    if "pricing_state" not in sess_cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN pricing_state TEXT NOT NULL DEFAULT '锁档一致'"
        )
    if "opening_band" not in sess_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN opening_band TEXT")
    if "price_mode" not in sess_cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN price_mode TEXT NOT NULL DEFAULT '会话锁档计价'"
        )

    pulse_cols = _columns(conn, "pulses")
    for col, decl in (
        ("unit_price", "REAL"),
        ("band", "TEXT"),
        ("amount", "REAL"),
        ("rate_plan_id", "INTEGER"),
    ):
        if col not in pulse_cols:
            conn.execute(f"ALTER TABLE pulses ADD COLUMN {col} {decl}")

    # 为缺锁档的旧会话补锁：优先按既有脉冲证据反推每档单价；
    # 否则用开充快照价 + 该档位费率列回填（仍是会话开充时点的价，非当前表）。
    if added_lock:
        plans = {
            r["id"]: r for r in conn.execute("SELECT * FROM rate_plans").fetchall()
        }
        sessions = conn.execute("SELECT * FROM sessions").fetchall()
        for se in sessions:
            plan = plans.get(se["rate_plan_id"])
            lock: dict[str, float] = {}
            evidence = conn.execute(
                """SELECT band, unit_price FROM pulses
                   WHERE session_id=? AND band IS NOT NULL AND unit_price IS NOT NULL
                   GROUP BY band""",
                (se["id"],),
            ).fetchall()
            for e in evidence:
                if e["band"] in BAND_COLUMN:
                    lock[e["band"]] = float(e["unit_price"])
            for band in BAND_ORDER:
                if band in lock:
                    continue
                if plan is not None:
                    lock[band] = float(plan[BAND_COLUMN[band]])
                elif se["price_locked"] is not None:
                    lock[band] = float(se["price_locked"])
                else:
                    lock[band] = 0.0
            conn.execute(
                "UPDATE sessions SET locked_peak=?, locked_flat=?, locked_valley=?, "
                "lock_at=COALESCE(lock_at, started_at) WHERE id=?",
                (lock[BAND_PEAK], lock[BAND_FLAT], lock[BAND_VALLEY], se["id"]),
            )

    # 旧脉冲没有落价：按其所属档的会话锁档落价，保证仍可逐笔重放。
    legacy = conn.execute(
        """SELECT p.id AS pulse_id, p.delta_kwh AS delta_kwh, p.band AS band,
                  p.created_at AS created_at,
                  s.price_locked AS price_locked, s.opening_band AS opening_band,
                  s.rate_plan_id AS rate_plan_id,
                  s.locked_peak AS locked_peak, s.locked_flat AS locked_flat,
                  s.locked_valley AS locked_valley
           FROM pulses p JOIN sessions s ON s.id=p.session_id
           WHERE p.unit_price IS NULL"""
    ).fetchall()
    for r in legacy:
        band = r["band"]
        if band not in BAND_ORDER:
            band = _band_for_ts(r["created_at"]) or r["opening_band"] or BAND_FLAT
        price = r[BAND_LOCK_COLUMN[band]]
        price = float(price) if price is not None else float(r["price_locked"] or 0.0)
        conn.execute(
            "UPDATE pulses SET unit_price=?, band=?, amount=?, rate_plan_id=? WHERE id=?",
            (
                price,
                band,
                round(float(r["delta_kwh"]) * price, 2),
                r["rate_plan_id"],
                r["pulse_id"],
            ),
        )

    # 在途会话的累计电量/金额改为脉冲合计，保证「预估可由脉冲重放」。
    open_ids = conn.execute(
        "SELECT id FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchall()
    for r in open_ids:
        agg = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS a, COALESCE(SUM(delta_kwh),0) AS k "
            "FROM pulses WHERE session_id=?",
            (r["id"],),
        ).fetchone()
        conn.execute(
            "UPDATE sessions SET amount=?, kwh=? WHERE id=?",
            (round(float(agg["a"]), 2), round(float(agg["k"]), 3), r["id"]),
        )

    # 旧库 price_mode 字面值归一到当前口径。
    conn.execute(
        "UPDATE sessions SET price_mode=? WHERE price_mode!='会话锁档计价' OR price_mode IS NULL",
        (PRICE_MODE,),
    )
    # 清理历史「假亮」缓存：surface_last/表面计数一律不再作为读数来源。
    conn.execute("DELETE FROM settings WHERE key='surface_last' OR key='surface_ok'")


def _band_for_ts(value: str | None) -> str | None:
    dt = parse_dt(value)
    return band_for(dt) if dt else None


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
