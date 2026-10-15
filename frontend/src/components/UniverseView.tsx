"use client";

import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { DEFAULT_FILTERS, compact, fmtUsdCompact, toFilters, type FilterState } from "@/lib/scannerFilters";
import type { RulesExplanationResponse, ScannerRow, UniverseSetups, UniverseStatus } from "@/lib/types";
import FilterBar from "./FilterBar";
import RulesExplanation, { SetupActions } from "./RulesExplanation";
import { Table } from "./ScannerView";
import { DataUnavailable, DemoTag, Empty, Panel } from "./ui";

const POLL_MS = 10_000;
const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));
const ts = (t: number | null | undefined) =>
  t ? new Date(t * 1000).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " ET" : "—";

function Stat({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="rounded-md bg-panel-2 px-3 py-2 ring-1 ring-line">
      <p className="text-[10px] uppercase tracking-wider text-muted">{label}</p>
      <p className={`num text-base font-semibold ${tone ?? "text-ink"}`}>{value}</p>
    </div>
  );
}

function Detail({ row, onClose, onWatchlist }: { row: ScannerRow; onClose: () => void; onWatchlist: () => void }) {
  const [res, setRes] = useState<{ id: string; data: RulesExplanationResponse | null; error: string | null } | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .universeExplain(row.ticker, row.mode, row.id)
      .then((data) => alive && setRes({ id: row.id, data, error: null }))
      .catch((e) => alive && setRes({ id: row.id, data: null, error: errText(e) }));
    return () => {
      alive = false;
    };
  }, [row.ticker, row.mode, row.id]);
  const cur = res?.id === row.id ? res : null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/40" onClick={onClose}>
      <aside className="h-full w-full max-w-[640px] overflow-y-auto bg-panel p-4 ring-1 ring-line-strong" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-bold">
            {row.ticker} · {row.setup} <span className="text-faint">({row.direction}, {row.timeframe})</span>
          </h2>
          <button onClick={onClose} className="text-muted hover:text-ink" aria-label="Close">✕</button>
        </div>
        <SetupActions row={row} source="universe" onWatchlist={onWatchlist} />
        <div className="mt-3">
          {!cur ? (
            <p className="text-xs text-muted">Computing the rules-based explanation…</p>
          ) : cur.error ? (
            <DataUnavailable compact message={cur.error} />
          ) : cur.data?.explanation ? (
            <RulesExplanation x={cur.data.explanation} />
          ) : (
            <p className="text-xs text-muted">{cur.data?.reason}</p>
          )}
        </div>
      </aside>
    </div>
  );
}

/**
 * Market scanner: the server-side universe scan of liquid US stocks, using the same
 * engines as the watchlist scanner. Reads persisted results; scanning continues without
 * this page open.
 */
export default function UniverseView({ onWatchlistChanged }: { onWatchlistChanged: () => void }) {
  const [f, setF] = useState<FilterState>(DEFAULT_FILTERS);
  const [status, setStatus] = useState<UniverseStatus | null>(null);
  const [data, setData] = useState<{ key: string; res: UniverseSetups } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<ScannerRow | null>(null);
  const [showUnavailable, setShowUnavailable] = useState(false);
  const [tick, setTick] = useState(0);
  const key = JSON.stringify(compact(toFilters(f)));

  useEffect(() => {
    let alive = true;
    const load = () => {
      api.universeStatus().then((s) => alive && setStatus(s)).catch((e) => alive && setError(errText(e)));
      api
        .universeSetups(JSON.parse(key))
        .then((res) => {
          if (!alive) return;
          setData({ key, res });
          setError(null);
        })
        .catch((e) => alive && setError(errText(e)));
    };
    load();
    const t = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [key, tick]);

  const res = data?.key === key ? data.res : null;
  const rows = res?.rows ?? [];
  const run = status?.running ?? null;
  const last = status?.last_completed ?? null;
  const counts = { TRIGGERED: 0, WATCHING: 0, NO_TRADE: 0 } as Record<string, number>;
  rows.forEach((r) => (counts[r.status] = (counts[r.status] ?? 0) + 1));
  const start = async (rebuild: boolean) => {
    try {
      await api.universeRun(rebuild);
    } catch (e) {
      setError(errText(e));
    }
    setTick((x) => x + 1);
  };
  const total = status?.universe?.size ?? 0;
  const p = run ?? last;
  const complete = last && last.scanned === last.total;
  return (
    <div className="space-y-3 p-3">
      <Panel
        title="Market scanner — liquid US stock universe"
        right={
          <span className="flex items-center gap-2">
            {status?.is_demo && <DemoTag />}
            <button onClick={() => start(false)} disabled={!!run} className="rounded-md px-2.5 py-1 text-[11px] font-semibold text-ink ring-1 ring-line-strong hover:bg-white/5 disabled:opacity-50">
              {run ? "Scanning…" : "Scan now"}
            </button>
            <button onClick={() => start(true)} disabled={!!run} className="rounded-md px-2.5 py-1 text-[11px] text-muted ring-1 ring-line-strong hover:text-ink disabled:opacity-50" title="Re-rank the universe by liquidity, then scan">
              Rebuild universe
            </button>
          </span>
        }
      >
        <div className="space-y-3 p-3">
          {error && <DataUnavailable compact message={error} />}
          <p className="text-[11px] text-muted">{status?.coverage_note ?? "Loading…"}</p>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">
            <Stat label="Eligible stocks" value={total} />
            <Stat label={run ? "Scanned (this run)" : "Scanned (last run)"} value={p ? p.scanned : "—"} />
            <Stat label="Remaining" value={p ? p.total - p.scanned : "—"} />
            <Stat label="Scan failures" value={p ? p.failed : "—"} tone={p && p.failed ? "text-wait" : undefined} />
            <Stat label="Triggered" value={counts.TRIGGERED} tone="text-bull" />
            <Stat label="Watching" value={counts.WATCHING} tone="text-wait" />
            <Stat label="Data unavailable" value={res?.unavailable.length ?? "—"} tone={res?.unavailable.length ? "text-bear" : undefined} />
          </div>
          <p className="text-[11px] text-faint">
            {run ? `Scan #${run.id} running since ${ts(run.started_at)}: ${run.scanned} of ${run.total} stocks. ` : ""}
            Last completed scan: {last ? `#${last.id}, ${ts(last.finished_at)} (${last.scanned}/${last.total} stocks${last.failed ? `, ${last.failed} with data problems` : ""})` : "none yet"}
            {last && !complete ? " — not every eligible stock was evaluated." : ""}
            {" · "}Schedule: {status?.schedule.enabled ? `every ${Math.round((status.schedule.interval_seconds ?? 0) / 60)} min, server-side` : "off (UNIVERSE_SCAN_INTERVAL_SECONDS=0)"}
            {status?.universe && ` · universe built ${new Date(status.universe.built_at).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} ET from ${status.universe.source}`}
            {status?.last_error && <span className="text-bear"> · last error: {status.last_error}</span>}
          </p>
          <FilterBar f={f} setF={setF} strategies={res?.strategies ?? []} />
        </div>
      </Panel>
      <div className="grid gap-3 2xl:grid-cols-2">
        {f.side !== "bearish" && (
          <div className="h-[460px]">
            <Table rows={rows.filter((r) => r.side === "bullish")} title="Bullish setups" tone="bull" onOpen={setOpen} />
          </div>
        )}
        {f.side !== "bullish" && (
          <div className="h-[460px]">
            <Table rows={rows.filter((r) => r.side === "bearish")} title="Bearish setups" tone="bear" onOpen={setOpen} />
          </div>
        )}
      </div>
      <Panel
        title={`DATA_UNAVAILABLE (${res?.unavailable.length ?? 0})`}
        right={
          <button onClick={() => setShowUnavailable(!showUnavailable)} className="text-[11px] text-muted hover:text-ink">
            {showUnavailable ? "Hide" : "Show"}
          </button>
        }
      >
        {showUnavailable &&
          (!res?.unavailable.length ? (
            <Empty>Every scanned stock had usable data.</Empty>
          ) : (
            <ul className="space-y-0.5 p-3 text-[11px]">
              {res.unavailable.map((u) => (
                <li key={`${u.ticker}-${u.mode}`}>
                  <span className="font-semibold">{u.ticker}</span> <span className="text-faint">{u.mode}</span> — {u.error} <span className="text-faint">({ts(u.scanned_at)})</span>
                </li>
              ))}
            </ul>
          ))}
      </Panel>
      <p className="text-center text-[10px] text-faint">
        Same strategy, planning and validation-label rules as the watchlist scanner. Rows are technical setups, not recommendations; quality is a
        rules score, not a win probability. Liquidity filter uses consolidated (SIP) average dollar volume
        {res?.tickers.length ? ` (lowest in universe ${fmtUsdCompact(Math.min(...res.tickers.map((t) => t.liquidity?.adv ?? Infinity)))}/day)` : ""}. Click a row for the
        rules-based explanation.
      </p>
      {open && <Detail row={open} onClose={() => setOpen(null)} onWatchlist={onWatchlistChanged} />}
    </div>
  );
}
