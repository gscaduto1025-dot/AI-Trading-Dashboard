"""SQLite persistence: detected setups, the strategy-signal log, alerts (events, engine
state, preferences, user price levels), the watchlist and cached AI analyses."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_time INTEGER NOT NULL,
    status TEXT NOT NULL,
    setup_type TEXT NOT NULL,
    direction TEXT,
    title TEXT NOT NULL,
    entry REAL, stop REAL, target1 REAL, target2 REAL, risk_reward REAL,
    confidence INTEGER,
    is_demo INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    UNIQUE(symbol, mode, timeframe, bar_time, setup_type, status)
);
CREATE TABLE IF NOT EXISTS last_status (
    symbol TEXT NOT NULL, mode TEXT NOT NULL, timeframe TEXT NOT NULL,
    status TEXT NOT NULL, setup_key TEXT NOT NULL,
    PRIMARY KEY (symbol, mode, timeframe)
);
CREATE TABLE IF NOT EXISTS strategy_signals (
    id TEXT PRIMARY KEY,  -- hash of ticker|strategy|timeframe|direction|session|trigger time
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL,
    session_date TEXT NOT NULL,
    trigger_time INTEGER NOT NULL,
    confirmed_at INTEGER NOT NULL,
    entry REAL NOT NULL, stop REAL NOT NULL, target1 REAL, target2 REAL, risk_reward REAL,
    payload TEXT NOT NULL,
    is_demo INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    added_at INTEGER NOT NULL
);
-- Alert engine. `dedupe_key` (type | subject | candle) is UNIQUE so the same alert can be
-- stored only once even across restarts or concurrent scanner processes.
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    cooldown_key TEXT NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    actionable INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    mode TEXT,
    payload TEXT NOT NULL,
    is_demo INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    delivery TEXT
);
CREATE INDEX IF NOT EXISTS alert_events_cooldown ON alert_events (cooldown_key, created_at);
CREATE INDEX IF NOT EXISTS alert_events_created ON alert_events (created_at);
CREATE TABLE IF NOT EXISTS alert_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    price REAL NOT NULL,
    condition TEXT NOT NULL,          -- above | below
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    triggered_at INTEGER,
    triggered_price REAL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- setups/alerts

    def record_setup(self, analysis: dict, is_demo: bool) -> bool:
        """Store a FORMING/ACTIONABLE setup in the recent-setups feed. Returns True when
        the status or setup changed for this symbol/mode/timeframe. (Alerts are created
        by the alert engine, not here.)"""
        s = analysis["setup"]
        key = (analysis["symbol"], analysis["mode"], analysis["timeframe"])
        setup_key = f"{s['setup_type']}:{s.get('direction')}:{s.get('entry')}"
        now = int(time.time())
        with self._lock, self._conn:
            if s["status"] != "NO_TRADE":
                targets = s.get("targets") or [None, None]
                self._conn.execute(
                    """INSERT OR IGNORE INTO setups (symbol, mode, timeframe, bar_time, status,
                       setup_type, direction, title, entry, stop, target1, target2, risk_reward,
                       confidence, is_demo, detected_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*key, analysis["as_of"], s["status"], s["setup_type"], s.get("direction"),
                     s["title"], s.get("entry"), s.get("stop"), targets[0],
                     targets[1] if len(targets) > 1 else None, s.get("risk_reward"),
                     s.get("confidence"), int(is_demo), now),
                )
            row = self._conn.execute(
                "SELECT status, setup_key FROM last_status WHERE symbol=? AND mode=? AND timeframe=?",
                key).fetchone()
            changed = not row or row["status"] != s["status"] or row["setup_key"] != setup_key
            self._conn.execute(
                """INSERT INTO last_status (symbol, mode, timeframe, status, setup_key)
                   VALUES (?,?,?,?,?) ON CONFLICT(symbol, mode, timeframe)
                   DO UPDATE SET status=excluded.status, setup_key=excluded.setup_key""",
                (*key, s["status"], setup_key))
        return changed

    def recent_setups(self, limit: int = 25, mode: str | None = None) -> list[dict]:
        sql = "SELECT * FROM setups"
        args: tuple = ()
        if mode:
            sql += " WHERE mode=?"
            args = (mode,)
        sql += " ORDER BY detected_at DESC, id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        return [dict(r) | {"is_demo": bool(r["is_demo"])} for r in rows]

    # ------------------------------------------------------------- strategy signals

    def record_signal(self, signal_id: str, setup: dict, trigger: dict, is_demo: bool,
                      mode: str = "day") -> bool:
        """Append a TRIGGERED signal once (immutable log). Returns True if it was new."""
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO strategy_signals (id, ticker, strategy, timeframe,
                   direction, session_date, trigger_time, confirmed_at, entry, stop, target1,
                   target2, risk_reward, payload, is_demo, recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, setup["ticker"], setup["strategy"], setup["timeframe"],
                 setup["direction"], setup["session_date"], trigger["time"],
                 trigger["confirmed_at"], trigger["entry"], trigger["stop"], trigger["target1"],
                 trigger["target2"], trigger["risk_reward"], json.dumps(setup), int(is_demo), now))
            return cur.rowcount > 0

    def signals(self, limit: int = 100, ticker: str | None = None) -> list[dict]:
        sql = "SELECT * FROM strategy_signals"
        args: tuple = ()
        if ticker:
            sql += " WHERE ticker=?"
            args = (ticker,)
        sql += " ORDER BY confirmed_at DESC, id LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            d["is_demo"] = bool(d["is_demo"])
            out.append(d)
        return out

    # ------------------------------------------------------------------------ alerts

    def insert_alert(self, alert: dict, dedupe_key: str, cooldown_key: str,
                     created_at: int) -> int | None:
        """Store an alert unless one with the same dedupe key exists. Returns its id."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO alert_events (dedupe_key, cooldown_key, type, severity,
                   actionable, ticker, mode, payload, is_demo, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (dedupe_key, cooldown_key, alert["type"], alert["severity"],
                 int(alert["actionable"]), alert["ticker"], alert.get("mode"),
                 json.dumps(alert), int(alert.get("is_demo", False)), created_at))
            return cur.lastrowid if cur.rowcount else None

    def alert_exists(self, dedupe_key: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM alert_events WHERE dedupe_key=?",
                                      (dedupe_key,)).fetchone() is not None

    def last_alert_time(self, cooldown_key: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(created_at) AS t FROM alert_events WHERE cooldown_key=?",
                (cooldown_key,)).fetchone()
        return row["t"] if row else None

    def set_alert_delivery(self, alert_id: int, delivery: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE alert_events SET delivery=? WHERE id=?",
                               (delivery, alert_id))

    def alerts(self, limit: int = 50, unacknowledged_only: bool = False,
               ticker: str | None = None, types: list[str] | None = None,
               after_id: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM alert_events WHERE 1=1", []
        if unacknowledged_only:
            sql += " AND acknowledged=0"
        if ticker:
            sql += " AND ticker=?"
            args.append(ticker)
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        if after_id is not None:
            sql += " AND id>?"
            args.append(after_id)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        return [_alert_row(r) for r in rows]

    def alert_counts(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS total, SUM(acknowledged=0) AS unread,
                   SUM(acknowledged=0 AND actionable=1) AS unread_actionable,
                   MAX(id) AS latest_id FROM alert_events""").fetchone()
        return {"total": row["total"] or 0, "unread": row["unread"] or 0,
                "unread_actionable": row["unread_actionable"] or 0,
                "latest_id": row["latest_id"]}

    def acknowledge_alert(self, alert_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE alert_events SET acknowledged=1 WHERE id=?",
                                     (alert_id,))
        return cur.rowcount > 0

    def acknowledge_all_alerts(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE alert_events SET acknowledged=1 WHERE acknowledged=0")
        return cur.rowcount

    def get_state(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM alert_state WHERE key=?",
                                     (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def put_state(self, key: str, value: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO alert_state (key, value, updated_at) VALUES (?,?,?)",
                (key, json.dumps(value), int(time.time())))

    def price_levels(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM alert_levels"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) | {"active": bool(r["active"])} for r in rows]

    def add_price_level(self, ticker: str, price: float, condition: str, note: str) -> dict:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO alert_levels (ticker, price, condition, note, created_at)
                   VALUES (?,?,?,?,?)""", (ticker, price, condition, note, int(time.time())))
            row = self._conn.execute("SELECT * FROM alert_levels WHERE id=?",
                                     (cur.lastrowid,)).fetchone()
        return dict(row) | {"active": bool(row["active"])}

    def delete_price_level(self, level_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM alert_levels WHERE id=?", (level_id,))
        return cur.rowcount > 0

    def mark_price_level(self, level_id: int, when: int, price: float) -> bool:
        """One-shot: deactivate a level when it is reached. False if already triggered."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE alert_levels SET active=0, triggered_at=?, triggered_price=?
                   WHERE id=? AND active=1""", (when, price, level_id))
        return cur.rowcount > 0

    # --------------------------------------------------------------------- watchlist / AI

    def get_watchlist(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT symbol FROM watchlist ORDER BY position").fetchall()
        return [r["symbol"] for r in rows]

    def set_watchlist(self, symbols: list[str]) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM watchlist")
            self._conn.executemany(
                "INSERT INTO watchlist (symbol, position, added_at) VALUES (?,?,?)",
                [(s, i, now) for i, s in enumerate(symbols)])

    def get_ai(self, cache_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM ai_cache WHERE cache_key=?",
                                     (cache_key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_ai(self, cache_key: str, payload: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ai_cache (cache_key, payload, created_at) VALUES (?,?,?)",
                (cache_key, json.dumps(payload), int(time.time())))


def _alert_row(row: sqlite3.Row) -> dict:
    d = json.loads(row["payload"])
    d.update(id=row["id"], acknowledged=bool(row["acknowledged"]), delivery=row["delivery"],
             created_at=row["created_at"])
    return d
