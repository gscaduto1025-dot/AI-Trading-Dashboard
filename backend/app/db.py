"""SQLite persistence for detected setups, alerts and cached AI analyses."""

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
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    direction TEXT,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    is_demo INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS last_status (
    symbol TEXT NOT NULL, mode TEXT NOT NULL, timeframe TEXT NOT NULL,
    status TEXT NOT NULL, setup_key TEXT NOT NULL,
    PRIMARY KEY (symbol, mode, timeframe)
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

    def record_setup(self, analysis: dict, is_demo: bool) -> dict | None:
        """Store a FORMING/ACTIONABLE setup and raise an alert on status transitions.

        Returns the alert created, if any. An alert fires when a symbol/mode/timeframe
        becomes ACTIONABLE, or when a new setup starts FORMING.
        """
        s = analysis["setup"]
        key = (analysis["symbol"], analysis["mode"], analysis["timeframe"])
        setup_key = f"{s['setup_type']}:{s.get('direction')}:{s.get('entry')}"
        now = int(time.time())
        alert = None
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
            prev_status, prev_key = (row["status"], row["setup_key"]) if row else (None, None)
            changed = prev_status != s["status"] or prev_key != setup_key
            if changed and s["status"] in ("ACTIONABLE", "FORMING"):
                if s["status"] == "ACTIONABLE":
                    message = (f"{key[0]} {s['title']} is ACTIONABLE. Entry {s['entry']}, "
                               f"stop {s['stop']}, target {s['targets'][0] if s['targets'] else '-'}"
                               f" (R:R {s['risk_reward']}:1).")
                else:
                    message = f"{key[0]} {s['title']} is forming. {(s.get('reasons') or [''])[0]}"
                cur = self._conn.execute(
                    """INSERT INTO alerts (symbol, mode, timeframe, status, direction, title,
                       message, is_demo, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (*key, s["status"], s.get("direction"), s["title"], message, int(is_demo), now))
                alert = self._alert_by_id(cur.lastrowid)
            self._conn.execute(
                """INSERT INTO last_status (symbol, mode, timeframe, status, setup_key)
                   VALUES (?,?,?,?,?) ON CONFLICT(symbol, mode, timeframe)
                   DO UPDATE SET status=excluded.status, setup_key=excluded.setup_key""",
                (*key, s["status"], setup_key))
        return alert

    def _alert_by_id(self, alert_id: int | None) -> dict | None:
        row = self._conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return _alert_row(row) if row else None

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

    def alerts(self, limit: int = 50, unacknowledged_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM alerts"
        if unacknowledged_only:
            sql += " WHERE acknowledged=0"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (limit,)).fetchall()
        return [_alert_row(r) for r in rows]

    def acknowledge_alert(self, alert_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
        return cur.rowcount > 0

    # --------------------------------------------------------------------- AI cache

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
    d = dict(row)
    d["is_demo"] = bool(d["is_demo"])
    d["acknowledged"] = bool(d["acknowledged"])
    return d
