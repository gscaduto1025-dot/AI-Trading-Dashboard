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
CREATE TABLE IF NOT EXISTS backtest_runs (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL,              -- running | done | failed | interrupted
    request TEXT NOT NULL,
    summary TEXT,
    result TEXT,
    error TEXT
);
-- The user's own trade log. ACTUAL and PAPER trades only: the CHECK constraint makes it
-- impossible to store simulated backtest trades here (they live in backtest_runs).
CREATE TABLE IF NOT EXISTS journal_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL CHECK (account IN ('actual', 'paper')),
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    strategy TEXT,
    setup_id TEXT,
    planned_stop REAL,
    planned_target REAL,
    notes TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES journal_trades(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price > 0),
    fees REAL NOT NULL DEFAULT 0 CHECK (fees >= 0),
    executed_at INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (trade_id, side, quantity, price, executed_at)  -- the same fill twice is rejected
);
CREATE INDEX IF NOT EXISTS journal_fills_trade ON journal_fills (trade_id);
-- Saved scanner filter presets (user-defined; filters is the JSON object of /api/scanner
-- query parameters).
CREATE TABLE IF NOT EXISTS scanner_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    filters TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
-- One row per Claude API call (cost controls and reporting; never the API key).
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    model TEXT,
    outcome TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS ai_usage_time ON ai_usage (called_at);
-- Universe scanner. One row per universe build, one per scan run (progress, lease
-- heartbeat for no-duplicate jobs), and the latest result per (symbol, mode).
CREATE TABLE IF NOT EXISTS universe_builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    built_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universe_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    heartbeat INTEGER NOT NULL,
    status TEXT NOT NULL,          -- running | completed | interrupted | failed
    trigger TEXT NOT NULL,
    total INTEGER NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    setups INTEGER NOT NULL DEFAULT 0,
    build_id INTEGER,
    error TEXT,
    requests INTEGER
);
CREATE TABLE IF NOT EXISTS universe_results (
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    scanned_at INTEGER NOT NULL,
    status TEXT NOT NULL,          -- ok | DATA_UNAVAILABLE
    payload TEXT NOT NULL,         -- {"rows": [...], "info": {...}, "error": ...}
    PRIMARY KEY (symbol, mode)
);
CREATE TABLE IF NOT EXISTS universe_setup_seen (
    setup_id TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
-- Detected setups the user chose to keep (scanner or universe). A saved setup is NEVER a
-- trade: it has no fills and is excluded from every P&L statistic.
CREATE TABLE IF NOT EXISTS saved_setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK (source IN ('watchlist', 'universe')),
    ticker TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status_at_save TEXT NOT NULL,
    payload TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    saved_at INTEGER NOT NULL
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
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._migrate()

    # Columns added after the first release, applied to existing databases in place.
    JOURNAL_COLUMNS = {
        "mode": "TEXT CHECK (mode IN ('day', 'swing'))",
        "asset_type": "TEXT NOT NULL DEFAULT 'stock' CHECK (asset_type IN ('stock', 'option'))",
        "option_symbol": "TEXT",
        "multiplier": "REAL NOT NULL DEFAULT 1 CHECK (multiplier > 0)",
        "planned_target2": "REAL",
        "quality": "REAL",
        "evidence_status": "TEXT",
        "evidence_label": "TEXT",
    }

    def _migrate(self) -> None:
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(journal_trades)")}
        for col, decl in self.JOURNAL_COLUMNS.items():
            if col not in have:
                self._conn.execute(f"ALTER TABLE journal_trades ADD COLUMN {col} {decl}")
        self._conn.commit()

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

    # ------------------------------------------------------------------------ backtests

    def create_backtest(self, run_id: str, request: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO backtest_runs (id, created_at, status, request) VALUES (?,?,?,?)",
                (run_id, int(time.time()), "running", json.dumps(request)))

    def finish_backtest(self, run_id: str, status: str, summary: dict | None = None,
                        result: dict | None = None, error: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE backtest_runs SET status=?, finished_at=?, summary=?, result=?, error=?
                   WHERE id=?""",
                (status, int(time.time()), json.dumps(summary) if summary else None,
                 json.dumps(result) if result else None, error, run_id))

    def backtests(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, created_at, finished_at, status, request, summary, error
                   FROM backtest_runs ORDER BY created_at DESC, id DESC LIMIT ?""",
                (limit,)).fetchall()
        return [{**dict(r), "request": json.loads(r["request"]),
                 "summary": json.loads(r["summary"]) if r["summary"] else None} for r in rows]

    def backtest(self, run_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM backtest_runs WHERE id=?",
                                   (run_id,)).fetchone()
        if not r:
            return None
        return {**dict(r), "request": json.loads(r["request"]),
                "summary": json.loads(r["summary"]) if r["summary"] else None,
                "result": json.loads(r["result"]) if r["result"] else None}

    def delete_backtest(self, run_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM backtest_runs WHERE id=?", (run_id,))
        return cur.rowcount > 0

    def interrupt_running_backtests(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE backtest_runs SET status='interrupted', error=? WHERE status='running'",
                ("The server stopped while this backtest was running.",))
        return cur.rowcount

    # ---------------------------------------------------------------------- trade log

    def journal_insert_trade(self, account: str, row: dict, fills: list[dict]) -> int:
        """Insert a trade and its fills atomically (all or nothing)."""
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO journal_trades (account, ticker, direction, strategy, setup_id,
                   planned_stop, planned_target, notes, created_at, updated_at, mode,
                   asset_type, option_symbol, multiplier, planned_target2, quality,
                   evidence_status, evidence_label)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account, row["ticker"], row["direction"], row.get("strategy"),
                 row.get("setup_id"), row.get("planned_stop"), row.get("planned_target"),
                 row.get("notes") or "", now, now, row.get("mode"),
                 row.get("asset_type") or "stock", row.get("option_symbol"),
                 row.get("multiplier") or 1.0, row.get("planned_target2"), row.get("quality"),
                 row.get("evidence_status"), row.get("evidence_label")))
            tid = cur.lastrowid
            for f in fills:
                self._insert_fill(tid, f)
        return tid

    def _insert_fill(self, trade_id: int, f: dict) -> int:
        cur = self._conn.execute(
            """INSERT INTO journal_fills (trade_id, side, quantity, price, fees, executed_at, note)
               VALUES (?,?,?,?,?,?,?)""",
            (trade_id, f["side"], f["quantity"], f["price"], f.get("fees") or 0.0,
             f["executed_at"], f.get("note") or ""))
        return cur.lastrowid

    def journal_add_fill(self, trade_id: int, f: dict) -> int:
        with self._lock, self._conn:
            fid = self._insert_fill(trade_id, f)
            self._conn.execute("UPDATE journal_trades SET updated_at=? WHERE id=?",
                               (int(time.time()), trade_id))
        return fid

    def journal_trades(self, account: str) -> list[dict]:
        with self._lock:
            trades = self._conn.execute(
                "SELECT * FROM journal_trades WHERE account=? ORDER BY created_at DESC, id DESC",
                (account,)).fetchall()
            fills = self._conn.execute(
                """SELECT f.* FROM journal_fills f JOIN journal_trades t ON t.id = f.trade_id
                   WHERE t.account=? ORDER BY f.executed_at, f.id""", (account,)).fetchall()
        by: dict[int, list] = {}
        for f in fills:
            by.setdefault(f["trade_id"], []).append(dict(f))
        return [dict(t) | {"fills": by.get(t["id"], [])} for t in trades]

    def journal_trade(self, account: str, trade_id: int) -> dict | None:
        with self._lock:
            t = self._conn.execute("SELECT * FROM journal_trades WHERE id=? AND account=?",
                                   (trade_id, account)).fetchone()
            if not t:
                return None
            fills = self._conn.execute(
                "SELECT * FROM journal_fills WHERE trade_id=? ORDER BY executed_at, id",
                (trade_id,)).fetchall()
        return dict(t) | {"fills": [dict(f) for f in fills]}

    def journal_update_trade(self, account: str, trade_id: int, fields: dict) -> bool:
        allowed = {k: v for k, v in fields.items()
                   if k in ("strategy", "setup_id", "planned_stop", "planned_target", "notes",
                            "mode", "option_symbol", "planned_target2", "quality",
                            "evidence_status", "evidence_label")}
        if not allowed:
            return False
        sets = ", ".join(f"{k}=?" for k in allowed)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE journal_trades SET {sets}, updated_at=? WHERE id=? AND account=?",
                (*allowed.values(), int(time.time()), trade_id, account))
        return cur.rowcount > 0

    def journal_delete_trade(self, account: str, trade_id: int) -> bool:
        with self._lock, self._conn:
            self._conn.execute("""DELETE FROM journal_fills WHERE trade_id IN
                                  (SELECT id FROM journal_trades WHERE id=? AND account=?)""",
                               (trade_id, account))
            cur = self._conn.execute("DELETE FROM journal_trades WHERE id=? AND account=?",
                                     (trade_id, account))
        return cur.rowcount > 0

    def journal_delete_fill(self, account: str, trade_id: int, fill_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """DELETE FROM journal_fills WHERE id=? AND trade_id=? AND trade_id IN
                   (SELECT id FROM journal_trades WHERE account=?)""",
                (fill_id, trade_id, account))
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

    # ----------------------------------------------------------------- saved setups

    def save_setup(self, row: dict) -> dict:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO saved_setups (setup_id, source, ticker, mode, strategy, direction,
                   timeframe, status_at_save, payload, note, saved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(setup_id) DO UPDATE SET
                   note=excluded.note""",
                (row["setup_id"], row["source"], row["ticker"], row["mode"], row["strategy"],
                 row["direction"], row["timeframe"], row["status_at_save"],
                 json.dumps(row["payload"], default=str), row.get("note") or "",
                 int(time.time())))
        return next(x for x in self.saved_setups() if x["setup_id"] == row["setup_id"])

    def saved_setups(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saved_setups ORDER BY saved_at DESC, id DESC").fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def delete_saved_setup(self, sid: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM saved_setups WHERE id=?", (sid,)).rowcount > 0

    # ------------------------------------------------------------- universe scanner

    def save_universe(self, payload: dict) -> int:
        with self._lock, self._conn:
            return self._conn.execute(
                "INSERT INTO universe_builds (built_at, payload) VALUES (?,?)",
                (payload["built_at"], json.dumps(payload))).lastrowid

    def latest_universe(self) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT id, payload FROM universe_builds ORDER BY id DESC LIMIT 1").fetchone()
        return (json.loads(r["payload"]) | {"build_id": r["id"]}) if r else None

    def claim_universe_run(self, trigger: str, total: int, build_id: int | None,
                           stale_after: int = 180) -> int | None:
        """Start a run unless another live run holds the lease (heartbeat within
        `stale_after` s). Stale running rows are marked interrupted. Atomic."""
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE universe_runs SET status='interrupted', finished_at=? "
                "WHERE status='running' AND heartbeat < ?", (now, now - stale_after))
            if self._conn.execute("SELECT 1 FROM universe_runs WHERE status='running'"
                                  ).fetchone():
                return None
            return self._conn.execute(
                """INSERT INTO universe_runs (started_at, heartbeat, status, trigger, total,
                   build_id) VALUES (?,?,?,?,?,?)""",
                (now, now, "running", trigger, total, build_id)).lastrowid

    def update_universe_run(self, run_id: int, **fields) -> None:
        fields["heartbeat"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE universe_runs SET {cols} WHERE id=?",
                               (*fields.values(), run_id))

    def interrupt_universe_runs(self) -> int:
        """At startup: no run can still be alive in this process."""
        with self._lock, self._conn:
            return self._conn.execute(
                "UPDATE universe_runs SET status='interrupted', finished_at=? "
                "WHERE status='running'", (int(time.time()),)).rowcount

    def universe_runs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM universe_runs ORDER BY id DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [dict(r) for r in rows]

    def put_universe_result(self, symbol: str, mode: str, run_id: int, status: str,
                            payload: dict) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO universe_results (symbol, mode, run_id, scanned_at, status,
                   payload) VALUES (?,?,?,?,?,?) ON CONFLICT(symbol, mode) DO UPDATE SET
                   run_id=excluded.run_id, scanned_at=excluded.scanned_at,
                   status=excluded.status, payload=excluded.payload""",
                (symbol, mode, run_id, now, status, json.dumps(payload, default=str)))
            for r in payload.get("rows") or []:
                self._conn.execute(
                    """INSERT INTO universe_setup_seen (setup_id, first_seen, last_seen)
                       VALUES (?,?,?) ON CONFLICT(setup_id) DO UPDATE SET
                       last_seen=excluded.last_seen""", (r["id"], now, now))

    def universe_results(self, symbols: list[str] | None = None) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM universe_results").fetchall()
            seen = {r["setup_id"]: (r["first_seen"], r["last_seen"]) for r in
                    self._conn.execute("SELECT * FROM universe_setup_seen").fetchall()}
        out = []
        for r in rows:
            if symbols is not None and r["symbol"] not in symbols:
                continue
            p = json.loads(r["payload"])
            for row in p.get("rows") or []:
                f, last = seen.get(row["id"], (None, None))
                row["first_seen"], row["last_seen"] = f, last
            out.append({"symbol": r["symbol"], "mode": r["mode"], "run_id": r["run_id"],
                        "scanned_at": r["scanned_at"], "status": r["status"], **p})
        return out

    # ----------------------------------------------------------- scanner presets

    def presets(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scanner_presets ORDER BY name COLLATE NOCASE").fetchall()
        return [{"id": r["id"], "name": r["name"], "filters": json.loads(r["filters"]),
                 "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]

    def save_preset(self, name: str, filters: dict) -> dict:
        """Create a preset, or replace the filters of the preset with this name."""
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO scanner_presets (name, filters, created_at, updated_at)
                   VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE
                   SET filters=excluded.filters, updated_at=excluded.updated_at""",
                (name, json.dumps(filters, sort_keys=True), now, now))
        return next(p for p in self.presets() if p["name"] == name)

    def delete_preset(self, preset_id: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM scanner_presets WHERE id=?",
                                      (preset_id,)).rowcount > 0

    # ----------------------------------------------------------------- AI usage

    def record_ai_usage(self, symbol: str, mode: str, model: str | None, outcome: str,
                        usage: dict | None, cost_usd: float | None) -> None:
        u = usage or {}
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO ai_usage (called_at, symbol, mode, model, outcome, input_tokens,
                   output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (int(time.time()), symbol, mode, model, outcome, u.get("input_tokens", 0),
                 u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0),
                 u.get("cache_creation_input_tokens", 0), cost_usd))

    def ai_usage_since(self, since: int) -> dict:
        with self._lock:
            r = self._conn.execute(
                """SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost,
                   COALESCE(SUM(input_tokens), 0) AS inp, COALESCE(SUM(output_tokens), 0) AS outp,
                   COALESCE(SUM(cache_read_tokens), 0) AS cr
                   FROM ai_usage WHERE called_at >= ?""", (since,)).fetchone()
        return {"calls": r["calls"], "cost_usd": round(r["cost"], 6), "input_tokens": r["inp"],
                "output_tokens": r["outp"], "cache_read_tokens": r["cr"]}

    def ai_usage_recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM ai_usage ORDER BY id DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [dict(r) for r in rows]

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
