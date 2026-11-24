from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

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
  price_mode TEXT NOT NULL DEFAULT '逐笔脉冲落价',
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
    """为旧库补列；新建库由 SCHEMA 直接建好。"""
    sess_cols = _columns(conn, "sessions")
    if "opening_band" not in sess_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN opening_band TEXT")
    if "price_mode" not in sess_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN price_mode TEXT NOT NULL DEFAULT '逐笔脉冲落价'")

    pulse_cols = _columns(conn, "pulses")
    if "unit_price" not in pulse_cols:
        conn.execute("ALTER TABLE pulses ADD COLUMN unit_price REAL")
    if "band" not in pulse_cols:
        conn.execute("ALTER TABLE pulses ADD COLUMN band TEXT")
    if "amount" not in pulse_cols:
        conn.execute("ALTER TABLE pulses ADD COLUMN amount REAL")
    if "rate_plan_id" not in pulse_cols:
        conn.execute("ALTER TABLE pulses ADD COLUMN rate_plan_id INTEGER")

    # 旧脉冲没有落价：按会话开充快照价回填，使历史脉冲仍可逐笔重放。
    legacy = conn.execute(
        """SELECT p.id, p.delta_kwh, s.price_locked, s.rate_plan_id
           FROM pulses p JOIN sessions s ON s.id=p.session_id
           WHERE p.unit_price IS NULL"""
    ).fetchall()
    for r in legacy:
        price = float(r["price_locked"] or 0.0)
        conn.execute(
            "UPDATE pulses SET unit_price=?, amount=?, rate_plan_id=? WHERE id=?",
            (price, round(float(r["delta_kwh"]) * price, 2), r["rate_plan_id"], r["id"]),
        )
    # 在途会话的累计金额改为脉冲金额之和，保证「预估可由脉冲重放」。
    open_ids = conn.execute(
        "SELECT id FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchall()
    for r in open_ids:
        total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS a FROM pulses WHERE session_id=?", (r["id"],)
        ).fetchone()["a"]
        conn.execute("UPDATE sessions SET amount=? WHERE id=?", (round(float(total), 2), r["id"]))


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
