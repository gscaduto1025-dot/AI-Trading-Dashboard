"""Server-side alert engine.

Runs after every opportunity-scanner snapshot (the scanner runs on the server's scheduler,
so alerts are produced while no dashboard is open). It diffs the snapshot against the
state it persisted after the previous scan and stores new alerts in SQLite.

Rules
- Alerts are derived only from scanner rows, i.e. from the deterministic strategy and
  planning engines. Nothing is recomputed or invented here.
- Stale / incomplete data: a ticker x mode whose data is stale, invalid or unavailable
  produces one `data_stale` warning and nothing else; its previous setup state is carried
  forward unchanged, so a data outage can neither trigger nor "invalidate" a setup. An
  actionable alert additionally needs a fresh, TRIGGERED, complete plan (price, entry,
  stop, T1, R:R); anything missing suppresses it.
- Duplicates: each alert has a dedupe key (type | subject | candle) that is UNIQUE in the
  database, so the same setup and candle can never alert twice - not on a rescan, not
  after a restart, not with two scanner processes.
- Cooldowns: per type and subject (configurable in preferences). A suppressed alert is
  reported in `last_run["suppressed"]` with the reason.
- Preferences: per-type on/off, cooldowns, modes, muted tickers, proximity, quality and
  R:R floors. Turning a type off still advances the state, so re-enabling it does not
  replay old events.
- No brokerage order is ever placed; alerts are notifications only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from ..market.calendar import NY
from .prefs import ALERT_TYPES, merge_prefs

log = logging.getLogger("tradescope.alerts")

FRESH_OK = ("fresh", "demo")
RESOLVED = ("stopped", "target1_then_stopped", "target2")
ACTIVE = ("WATCHING", "TRIGGERED")
STATE_KEY, PREFS_KEY = "engine", "prefs"


def lineage(r: dict) -> str:
    """Identity of a setup across scans (ids change with each new trigger)."""
    return "|".join((r["ticker"], r["mode"], r["strategy"], r["timeframe"], r["direction"]))


def _fmt(x) -> str:
    return "-" if x is None else f"{x:.2f}"


def trigger_type(r: dict) -> str:
    if r["strategy"] == "breakout_retest":
        return "retest_confirmed"
    if r["pattern"] == "breakout":
        return "breakout_confirmed"
    return "setup_triggered"


def incomplete(r: dict) -> list[str]:
    """Why a TRIGGERED row is not complete enough for an actionable alert."""
    missing = [k for k in ("price", "entry", "stop", "target1", "risk_reward")
               if r.get(k) is None]
    out = [f"missing {', '.join(missing)}"] if missing else []
    if r.get("data_freshness") not in FRESH_OK:
        out.append(f"data {r.get('data_freshness')}")
    if not r.get("last_candle"):
        out.append("no completed candle")
    if not r.get("actionable"):
        failed = [c["name"] for c in r.get("entry_checks") or [] if not c["passed"]]
        out.append("not currently actionable" + (f" ({', '.join(failed)})" if failed else ""))
    return out


class AlertEngine:
    def __init__(self, db, clock=time.time, notifier=None) -> None:
        self.db = db
        self.clock = clock
        self.notifier = notifier  # optional webhook delivery (see webhook.py)
        self.last_run: dict | None = None

    # -------------------------------------------------------------------- preferences

    def prefs(self) -> dict:
        return merge_prefs(self.db.get_state(PREFS_KEY))

    def update_prefs(self, update: dict) -> dict:
        merged = merge_prefs(self.db.get_state(PREFS_KEY), update)
        self.db.put_state(PREFS_KEY, merged)
        return merged

    def reset_prefs(self) -> dict:
        self.db.put_state(PREFS_KEY, {})
        return self.prefs()

    # ------------------------------------------------------------------------ process

    def process(self, snap: dict) -> list[dict]:
        """Diff one scanner snapshot against the persisted state; store and return the
        new alerts (oldest first)."""
        now = int(self.clock())
        prefs = self.prefs()
        prev = self.db.get_state(STATE_KEY) or {}
        state = {"setups": {}, "levels": {}, "data": {}}
        run = _Run(self, snap, prefs, now)
        tickers = {t["ticker"]: t for t in snap.get("tickers", [])}

        usable: dict[tuple[str, str], bool] = {}
        for sym, info in tickers.items():
            for mode in ("day", "swing"):
                d = (info.get("data") or {}).get(mode) or {}
                bad_flags = [f for f in info.get("flags", []) if f.startswith(f"{mode}:")]
                ok = d.get("freshness") in FRESH_OK and not bad_flags
                usable[(sym, mode)] = ok
                k = f"{sym}|{mode}"
                state["data"][k] = "ok" if ok else "bad"
                if not ok and prev.get("data", {}).get(k) != "bad":
                    run.data_stale(info, mode, d, bad_flags)

        # ---- setups
        prev_setups = prev.get("setups", {})
        seen = set()
        for r in snap.get("setup_states", []):
            key = lineage(r)
            seen.add(key)
            before = prev_setups.get(key)
            if not usable.get((r["ticker"], r["mode"]), False):
                if before is not None:
                    state["setups"][key] = before  # carry forward: never diff stale data
                run.skip(r, "data not fresh")
                continue
            state["setups"][key] = run.setup(r, before)
        for key, before in prev_setups.items():  # lineages not evaluated this scan
            sym = key.split("|", 1)[0]
            if key not in seen and sym in tickers:
                state["setups"][key] = before

        # ---- key-level proximity
        prev_levels = prev.get("levels", {})
        for sym, info in tickers.items():
            for mode in ("day", "swing"):
                for side in ("support", "resistance"):
                    k = f"{sym}|{mode}|{side}"
                    if not usable[(sym, mode)]:
                        if k in prev_levels:
                            state["levels"][k] = prev_levels[k]
                        continue
                    state["levels"][k] = run.level(info, mode, side, prev_levels.get(k))

        # ---- user-defined price levels (one-shot)
        for lv in self.db.price_levels(active_only=True):
            info = tickers.get(lv["ticker"])
            if info and usable.get((lv["ticker"], "day")):
                run.price_level(lv, info)

        self.db.put_state(STATE_KEY, state)
        created = run.commit()
        self.last_run = {"at": now, "scan_id": snap.get("scan_id"), "created": len(created),
                         "suppressed": run.suppressed}
        return created


class _Run:
    """One pass over a snapshot: collects candidate alerts, applies preferences,
    cooldowns and dedupe, then stores them."""

    def __init__(self, engine: AlertEngine, snap: dict, prefs: dict, now: int) -> None:
        self.engine, self.snap, self.prefs, self.now = engine, snap, prefs, now
        self.candidates: list[tuple[dict, str, str]] = []
        self.suppressed: list[dict] = []

    # ---------------------------------------------------------------- building alerts

    def _feed(self, data: dict) -> dict:
        f = self.snap.get("feed") or {}
        return {"source": f.get("source"), "feed": f.get("label"), "latency": f.get("latency"),
                "delay_seconds": f.get("delay_seconds"), "freshness": data.get("freshness"),
                "reason": data.get("reason"), "as_of": data.get("as_of"),
                "last_candle": data.get("last_candle"), "timeframe": data.get("timeframe")}

    def _base(self, kind: str, ticker: str, mode: str | None, data: dict) -> dict:
        meta = ALERT_TYPES[kind]
        return {
            "type": kind, "label": meta["label"], "severity": meta["severity"],
            "actionable": meta["actionable"], "ticker": ticker, "mode": mode,
            "strategy": None, "setup": None, "timeframe": None, "direction": None,
            "price": None, "key_level": None, "status": None, "previous_status": None,
            "entry_trigger": None, "entry": None, "entry_conditions": [], "invalidation": [],
            "stop": None, "targets": [], "risk_reward": None, "quality": None,
            "setup_id": None, "trigger_time": None, "candle_time": data.get("last_candle"),
            "feed_status": self._feed(data), "is_demo": bool(self.snap.get("is_demo")),
            "scan_id": self.snap.get("scan_id"),
            "timestamp": datetime.fromtimestamp(self.now, NY).isoformat(timespec="seconds"),
            "title": "", "message": "",
        }

    def _from_row(self, kind: str, r: dict, previous: str | None) -> dict:
        a = self._base(kind, r["ticker"], r["mode"],
                       {"freshness": r.get("data_freshness"), "as_of": r.get("data_as_of"),
                        "last_candle": r.get("last_candle"), "timeframe": r.get("timeframe")})
        a.update({
            "strategy": r["strategy"], "setup": r["setup"], "timeframe": r["timeframe"],
            "direction": r["direction"], "price": r.get("price"), "key_level": r.get("key_level"),
            "status": r["status"], "previous_status": previous,
            "entry_trigger": r.get("entry_trigger"), "entry": r.get("entry"),
            "entry_conditions": r.get("entry_conditions") or [],
            "invalidation": r.get("invalidation") or [], "stop": r.get("stop"),
            "targets": [t for t in (r.get("target1"), r.get("target2")) if t is not None],
            "risk_reward": r.get("risk_reward"), "quality": r.get("quality"),
            "setup_id": r["id"], "trigger_time": r.get("trigger_time"),
            "signal_state": r.get("signal_state"),
            "volume_confirmation": r.get("volume_confirmation"),
            "live_price": r.get("live_price"), "live_risk_reward": r.get("live_risk_reward"),
            "entry_checks": r.get("entry_checks") or [],
        })
        return a

    def _emit(self, alert: dict, dedupe: str, cooldown_key: str) -> None:
        if alert["is_demo"]:
            alert["title"] = "[DEMO] " + alert["title"]
        self.candidates.append((alert, dedupe, cooldown_key))

    def skip(self, r: dict, reason: str) -> None:
        if r["status"] == "TRIGGERED":
            self.suppressed.append({"type": trigger_type(r), "ticker": r["ticker"],
                                    "mode": r["mode"], "subject": lineage(r), "reason": reason})

    # -------------------------------------------------------------------- data status

    def data_stale(self, info: dict, mode: str, d: dict, flags: list[str]) -> None:
        a = self._base("data_stale", info["ticker"], mode, d)
        a["price"] = info.get("price")
        detail = "; ".join(flags) or f"data {d.get('freshness')}: {d.get('reason') or 'not usable'}"
        a["title"] = f"{info['ticker']} {mode} data {d.get('freshness') or 'unavailable'}"
        a["message"] = (f"{detail}. Alerts for {info['ticker']} ({mode}) are suppressed until "
                        f"fresh data returns; no setup from it is actionable.")
        self._emit(a, f"data_stale|{info['ticker']}|{mode}|{d.get('freshness')}|"
                      f"{d.get('as_of') or d.get('last_candle') or self.now // 3600}",
                   f"data_stale|{info['ticker']}|{mode}")

    # -------------------------------------------------------------------------- setups

    def setup(self, r: dict, before: dict | None) -> dict:
        key = lineage(r)
        status, trig, outcome = r["status"], r.get("trigger_time"), r.get("outcome")
        b = before or {}
        new_session = (r["mode"] == "day" and b.get("session") and r.get("session_date")
                       and b["session"] != r["session_date"])
        if new_session:
            b = {}  # a new trading day: yesterday's intraday setups simply expired
        alerted = b.get("alerted_trigger")
        cur = {"status": status, "setup_id": r["id"], "trigger_time": trig,
               "outcome": outcome, "alerted_trigger": alerted,
               "candidate": False, "session": r.get("session_date")}

        # 1) new confirmed trigger (WATCHING/NO_TRADE/none -> TRIGGERED, or a fresh trigger)
        if status == "TRIGGERED" and trig is not None and trig != alerted:
            kind = trigger_type(r)
            why = incomplete(r)
            q, rr = r.get("quality") or 0, r.get("risk_reward") or 0
            if why:
                self.skip(r, "incomplete: " + "; ".join(why))  # retried on the next scan
            else:
                cur["alerted_trigger"] = trig
                if q < self.prefs["min_quality"]:
                    self.skip(r, f"quality {q} below preference {self.prefs['min_quality']}")
                elif self.prefs["min_risk_reward"] and rr < self.prefs["min_risk_reward"]:
                    self.skip(r, f"R:R {rr} below preference {self.prefs['min_risk_reward']}")
                else:
                    prev_status = b.get("status")
                    a = self._from_row(kind, r, prev_status)
                    arrow = f"{prev_status} -> TRIGGERED" if prev_status else "TRIGGERED"
                    a["title"] = (f"{r['ticker']} {r['setup']} {r['direction']} TRIGGERED "
                                  f"({r['mode']}, {r['timeframe']})")
                    a["message"] = (f"{arrow}: entry {_fmt(r['entry'])} (trigger "
                                    f"{_fmt(r['entry_trigger'])}), stop {_fmt(r['stop'])}, "
                                    f"T1 {_fmt(r['target1'])}"
                                    + (f", T2 {_fmt(r['target2'])}" if r.get("target2") else "")
                                    + f", R:R {rr:.2f}:1, quality {q}/100. "
                                    f"Conditional plan only - no order is placed.")
                    self._emit(a, f"{kind}|{key}|{trig}", f"{kind}|{key}")

        # 2) target / stop reached for a trigger we already knew while it was open
        if trig is not None and b.get("trigger_time") == trig and outcome != b.get("outcome"):
            self._outcome(r, b.get("outcome") or "open", outcome)

        # 3) a previously valid setup is no longer valid
        resolved = trig is not None and b.get("trigger_time") == trig and outcome in RESOLVED
        if b.get("status") in ACTIVE and status == "NO_TRADE" and not resolved:
            a = self._from_row("setup_invalidated", r, b["status"])
            reason = (r.get("requirement_issues") or [r.get("reason") or "conditions no longer met"])[0]
            a["title"] = f"{r['ticker']} {r['setup']} {r['direction']} invalidated ({r['mode']})"
            a["message"] = f"{b['status']} -> NO_TRADE: {reason}"
            self._emit(a, f"setup_invalidated|{key}|{b.get('setup_id')}|{r.get('last_candle')}",
                       f"setup_invalidated|{key}")

        # 4) strong new swing candidate
        if r["mode"] == "swing" and status in ACTIVE and \
                (r.get("quality") or 0) >= self.prefs["swing_candidate_min_quality"]:
            cur["candidate"] = True
            if not b.get("candidate"):
                a = self._from_row("swing_candidate", r, b.get("status"))
                a["title"] = (f"{r['ticker']} swing candidate: {r['setup']} {r['direction']} "
                              f"(quality {r['quality']})")
                a["message"] = (f"{status} on {r['timeframe']}: trigger "
                                f"{_fmt(r['entry_trigger'])}, stop {_fmt(r['stop'])}, T1 "
                                f"{_fmt(r['target1'])}, R:R {_fmt(r['risk_reward'])}:1. "
                                + ("Entry is not confirmed yet." if status == "WATCHING"
                                   else "Trigger confirmed."))
                self._emit(a, f"swing_candidate|{key}|{r.get('last_candle')}",
                           f"swing_candidate|{key}")
        return cur

    def _outcome(self, r: dict, old: str, new: str | None) -> None:
        key, trig = lineage(r), r.get("trigger_time")
        hits = []
        if new in ("target1", "target2", "target1_then_stopped") and old == "open":
            hits.append(("target_reached", "T1", r.get("target1")))
        if new == "target2":
            hits.append(("target_reached", "T2", r.get("target2")))
        if new in ("stopped", "target1_then_stopped"):
            hits.append(("stop_reached", "stop", r.get("stop")))
        for kind, which, level in hits:
            a = self._from_row(kind, r, r["status"])
            a["key_level"] = {"label": which, "low": level, "high": level,
                              "timeframe": r["timeframe"]}
            verb = "stop-loss" if which == "stop" else which
            a["title"] = f"{r['ticker']} {r['setup']} {r['direction']}: {verb} reached"
            a["message"] = (f"The {r['timeframe']} trigger reached its {verb} {_fmt(level)} "
                            f"(price {_fmt(r.get('price'))}). Outcome: "
                            f"{(new or '').replace('_', ' ')}.")
            self._emit(a, f"{kind}|{key}|{trig}|{which}", f"{kind}|{key}|{which}")

    # -------------------------------------------------------------------------- levels

    def level(self, info: dict, mode: str, side: str, before: str | None) -> str | None:
        lv = ((info.get("levels") or {}).get(mode) or {}).get(side)
        if not lv or lv.get("distance_atr") is None:
            return None
        if lv["distance_atr"] > self.prefs["proximity_atr"]:
            return None
        ident = f"{lv['label']}|{lv['low']}|{lv['high']}"
        if ident == before:
            return ident
        kind = f"approaching_{side}"
        d = info["data"][mode]
        a = self._base(kind, info["ticker"], mode, d)
        a.update(price=info.get("price"), timeframe=lv.get("timeframe"),
                 key_level={k: lv.get(k) for k in ("label", "low", "high", "timeframe",
                                                   "strength", "distance_pct",
                                                   "distance_atr", "inside")},
                 status=f"{info['counts']['TRIGGERED']} triggered / "
                        f"{info['counts']['WATCHING']} watching")
        zone = _fmt(lv["low"]) if lv["low"] == lv["high"] else f"{_fmt(lv['low'])}-{_fmt(lv['high'])}"
        where = "inside" if lv.get("inside") else f"{lv['distance_atr']} ATR from"
        a["title"] = f"{info['ticker']} approaching {side}: {lv['label']} {zone} ({mode})"
        a["message"] = (f"Price {_fmt(info.get('price'))} is {where} {lv['label']} {zone} "
                        f"({lv.get('timeframe')}, strength {lv.get('strength')}). "
                        f"Informational - not an entry signal.")
        self._emit(a, f"{kind}|{info['ticker']}|{mode}|{ident}|{d.get('last_candle')}",
                   f"{kind}|{info['ticker']}|{ident}")  # shared by day and swing
        return ident

    def price_level(self, lv: dict, info: dict) -> None:
        price = info.get("price")
        if price is None:
            return
        hit = price >= lv["price"] if lv["condition"] == "above" else price <= lv["price"]
        if not hit or not self.engine.db.mark_price_level(lv["id"], self.now, price):
            return
        d = info["data"]["day"]
        a = self._base("price_level", lv["ticker"], None, d)
        a.update(price=price, key_level={"label": lv["note"] or "Your level", "low": lv["price"],
                                         "high": lv["price"], "timeframe": None},
                 status=f"{lv['condition']} {_fmt(lv['price'])}")
        a["title"] = f"{lv['ticker']} {lv['condition']} {_fmt(lv['price'])}"
        a["message"] = (f"Price {_fmt(price)} reached your level {_fmt(lv['price'])} "
                        f"({lv['condition']})" + (f": {lv['note']}" if lv["note"] else "") + ".")
        self._emit(a, f"price_level|{lv['id']}", f"price_level|{lv['id']}")

    # -------------------------------------------------------------------------- commit

    def commit(self) -> list[dict]:
        db, p = self.engine.db, self.prefs
        out = []
        for alert, dedupe, cool in self.candidates:
            kind = alert["type"]
            reason = None
            if not p["enabled"].get(kind, True):
                reason = "type disabled"
            elif alert["ticker"] in p["muted_tickers"]:
                reason = "ticker muted"
            elif alert["mode"] and alert["mode"] not in p["modes"]:
                reason = "mode disabled"
            elif db.alert_exists(dedupe):
                reason = "duplicate"
            else:
                cd = p["cooldown_minutes"].get(kind, 0) * 60
                last = db.last_alert_time(cool) if cd else None
                if last is not None and self.now - last < cd:
                    reason = f"cooldown ({int(cd // 60)} min)"
            if reason:
                self.suppressed.append({"type": kind, "ticker": alert["ticker"],
                                        "mode": alert["mode"], "subject": dedupe,
                                        "reason": reason})
                continue
            alert["browser"] = kind in p["browser_types"]
            aid = db.insert_alert(alert, dedupe, cool, self.now)
            if aid is None:  # another process stored it first
                self.suppressed.append({"type": kind, "ticker": alert["ticker"],
                                        "mode": alert["mode"], "subject": dedupe,
                                        "reason": "duplicate"})
                continue
            alert.update(id=aid, acknowledged=False, created_at=self.now, delivery=None)
            if self.engine.notifier and kind in p["webhook_types"]:
                alert["delivery"] = self.engine.notifier.deliver(alert)
                db.set_alert_delivery(aid, alert["delivery"])
            out.append(alert)
        return out
