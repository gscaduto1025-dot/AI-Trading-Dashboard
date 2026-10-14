"use client";

import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { STATUS_LABEL, fmtPct, fmtPrice, statusClasses } from "@/lib/format";
import type { Mode, ScannerFilters, ScannerRow, ScannerSnapshot, ScannerTicker } from "@/lib/types";
import { DataUnavailable, DemoTag, Empty, Panel } from "./ui";

const TF: Record<string, string> = { "5Min": "5m", "15Min": "15m", "1Day": "D", "1Week": "W" };
const PATTERNS = ["breakout", "retest", "pullback", "reversal"];
const POLL_MS = 15_000;

const et = (iso: string | null | undefined, withDate = false) =>
  iso
    ? new Date(iso).toLocaleString("en-US", {
        timeZone: "America/New_York",
        ...(withDate ? { month: "short", day: "numeric" } : {}),
        hour: "numeric",
        minute: "2-digit",
      }) + " ET"
    : "—";

const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

function Fresh({ value, title }: { value: string | null; title?: string }) {
  const cls =
    value === "fresh"
      ? "text-bull ring-bull/40"
      : value === "demo"
        ? "text-wait ring-wait/40"
        : "text-bear ring-bear/50 bg-bear/10";
  return (
    <span title={title} className={`rounded px-1 py-px text-[9px] font-bold uppercase ring-1 ${cls}`}>
      {value ?? "n/a"}
    </span>
  );
}

function Seg<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="flex rounded-md bg-panel-2 p-0.5 ring-1 ring-line">
      {options.map(([v, label]) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          className={`rounded px-2 py-0.5 text-[11px] font-medium ${value === v ? "bg-accent text-bg" : "text-muted hover:text-ink"}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function TickerCard({ t, onOpen }: { t: ScannerTicker; onOpen: (sym: string) => void }) {
  const up = (t.change_pct ?? 0) >= 0;
  const lv = t.levels.swing ?? t.levels.day;
  const near = (["day", "swing"] as Mode[]).flatMap((m) =>
    (["support", "resistance"] as const)
      .map((side) => ({ side, m, l: t.levels[m]?.[side] }))
      .filter((x) => x.l?.near),
  );
  return (
    <button
      onClick={() => onOpen(t.ticker)}
      className={`min-w-[170px] rounded-md p-2 text-left ring-1 transition hover:bg-white/[0.04] ${t.flagged ? "bg-bear/5 ring-bear/50" : "bg-panel-2 ring-line"}`}
      title={t.flags.join("\n") || undefined}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-bold">{t.ticker}</span>
        <span className="num text-xs">{fmtPrice(t.price)}</span>
      </div>
      <div className="flex items-center justify-between text-[10px]">
        <span className={`num ${up ? "text-bull" : "text-bear"}`}>{fmtPct(t.change_pct)}</span>
        <span className="text-faint">
          D <span className={t.bias.day === "bullish" ? "text-bull" : t.bias.day === "bearish" ? "text-bear" : "text-wait"}>{t.bias.day ?? "—"}</span> · S{" "}
          <span className={t.bias.swing === "bullish" ? "text-bull" : t.bias.swing === "bearish" ? "text-bear" : "text-wait"}>{t.bias.swing ?? "—"}</span>
        </span>
      </div>
      <div className="mt-1 flex gap-1 text-[9px] font-bold">
        <span className="rounded bg-bull/10 px-1 text-bull">{t.counts.TRIGGERED} trig</span>
        <span className="rounded bg-wait/10 px-1 text-wait">{t.counts.WATCHING} watch</span>
        <span className="rounded bg-white/5 px-1 text-muted">{t.counts.NO_TRADE} fail</span>
      </div>
      <p className="mt-1 truncate text-[10px] text-faint">
        {near.length
          ? `Near ${near[0].side}: ${near[0].l!.label}${near[0].l!.inside ? " (inside)" : ` ${near[0].l!.distance_pct}%`}`
          : lv?.resistance
            ? `Resistance ${lv.resistance.distance_pct}% away`
            : "No nearby S/R"}
      </p>
      {t.flagged && <p className="mt-0.5 truncate text-[10px] font-semibold text-bear">⚠ {t.flags[0]}</p>}
    </button>
  );
}

function Table({ rows, title, tone, onOpen }: { rows: ScannerRow[]; title: string; tone: "bull" | "bear"; onOpen: (r: ScannerRow) => void }) {
  return (
    <Panel
      title={`${title} (${rows.length})`}
      right={<span className={`h-2 w-2 rounded-full ${tone === "bull" ? "bg-bull" : "bg-bear"}`} />}
      bodyClassName="overflow-auto"
    >
      {rows.length === 0 ? (
        <Empty>No {title.toLowerCase()} match the filters.</Empty>
      ) : (
        <table className="w-full min-w-[980px] text-left text-[11px]">
          <thead className="sticky top-0 z-10 bg-panel text-[9px] uppercase tracking-wider text-faint">
            <tr>
              {["Ticker", "Price", "Day/Swing", "Bias", "Detected setup", "Dir", "Entry trigger", "Stop", "Target 1", "R:R", "Quality", "Status", "Last updated"].map(
                (h, i) => (
                  <th key={h} className={`px-2 py-1.5 font-medium ${i >= 6 && i <= 10 ? "text-right" : ""} ${i === 0 ? "pl-3" : ""}`}>
                    {h}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((r) => {
              const dir = r.direction === "LONG" ? "long" : "short";
              const dim = r.status === "NO_TRADE";
              const stale = r.data_freshness !== "fresh" && r.data_freshness !== "demo";
              return (
                <tr
                  key={r.id}
                  onClick={() => onOpen(r)}
                  title={dim || stale ? r.requirement_issues.join("\n") : r.reason}
                  className={`cursor-pointer transition hover:bg-white/[0.04] ${dim ? "opacity-60" : ""} ${stale ? "bg-bear/5" : ""}`}
                >
                  <td className="py-1.5 pl-3 pr-2 font-bold">
                    {r.ticker}
                    {stale && <span className="ml-1 text-[9px] text-bear">⚠</span>}
                  </td>
                  <td className="num px-2">{fmtPrice(r.price)}</td>
                  <td className="px-2">
                    {r.mode === "day" ? "Day" : "Swing"} <span className="text-faint">{TF[r.timeframe] ?? r.timeframe}</span>
                  </td>
                  <td className={`px-2 ${r.market_bias === "bullish" ? "text-bull" : r.market_bias === "bearish" ? "text-bear" : "text-wait"}`}>
                    {r.market_bias ?? "—"}
                  </td>
                  <td className="px-2">
                    {r.setup}
                    <span className="ml-1 text-[9px] uppercase text-faint">{r.pattern}</span>
                  </td>
                  <td className={`px-2 font-semibold ${r.direction === "LONG" ? "text-bull" : "text-bear"}`}>{r.direction === "LONG" ? "▲ L" : "▼ S"}</td>
                  <td className="num px-2 text-right">{fmtPrice(r.entry_trigger)}</td>
                  <td className="num px-2 text-right text-bear/90">{fmtPrice(r.stop)}</td>
                  <td className="num px-2 text-right text-bull/90">{fmtPrice(r.target1)}</td>
                  <td className="num px-2 text-right">{r.risk_reward !== null ? r.risk_reward.toFixed(2) : "—"}</td>
                  <td className="num px-2 text-right">{r.quality !== null ? r.quality.toFixed(0) : "—"}</td>
                  <td className="px-2">
                    <span className={`rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${statusClasses(r.status, dir)}`}>
                      {STATUS_LABEL[r.status]}
                    </span>
                    {r.actionable && <span className="ml-1 text-[9px] font-bold text-bull">●</span>}
                  </td>
                  <td className="whitespace-nowrap px-2 text-faint">
                    {et(r.last_updated)} <Fresh value={r.data_freshness} title={r.last_candle ? `Last candle ${et(r.last_candle, true)}` : undefined} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

/**
 * Multi-stock opportunity scanner. The backend scans the watchlist on its own schedule
 * whenever new candles close; this view only reads the latest snapshot.
 */
export default function ScannerView({
  onOpenRow,
  onOpenTicker,
  onWatchlistChanged,
}: {
  onOpenRow: (r: ScannerRow) => void;
  onOpenTicker: (sym: string) => void;
  onWatchlistChanged: () => void;
}) {
  const [mode, setMode] = useState<"all" | Mode>("all");
  const [side, setSide] = useState<"all" | "bullish" | "bearish">("all");
  const [statuses, setStatuses] = useState({ TRIGGERED: true, WATCHING: true, NO_TRADE: false });
  const [minRr, setMinRr] = useState("");
  const [minQ, setMinQ] = useState("");
  const [strategy, setStrategy] = useState("");
  const [snap, setSnap] = useState<{ key: string; data: ScannerSnapshot | null; error: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [newSym, setNewSym] = useState("");
  const [wlError, setWlError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const filters: ScannerFilters = {
    mode: mode === "all" ? undefined : mode,
    side: side === "all" ? undefined : side,
    status: Object.entries(statuses).filter(([, v]) => v).map(([k]) => k).join(",") || "NONE",
    min_rr: minRr ? Number(minRr) : undefined,
    min_quality: minQ ? Number(minQ) : undefined,
    strategy: strategy || undefined,
  };
  const key = JSON.stringify(filters);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .scanner(JSON.parse(key))
        .then((data) => alive && setSnap({ key, data, error: null }))
        .catch((e) => alive && setSnap({ key, data: null, error: errText(e) }));
    load();
    const t = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [key, tick]);

  const data = snap?.data ?? null;
  const rows = data?.rows ?? [];
  const bull = rows.filter((r) => r.side === "bullish");
  const bear = rows.filter((r) => r.side === "bearish");

  const scanNow = async () => {
    setBusy(true);
    try {
      await api.scannerRun();
    } catch (e) {
      setSnap({ key, data, error: errText(e) });
    } finally {
      setBusy(false);
      setTick((x) => x + 1);
    }
  };

  const changeWatchlist = async (fn: () => Promise<unknown>) => {
    setWlError(null);
    try {
      await fn();
      onWatchlistChanged();
      setBusy(true);
      await api.scannerRun().catch(() => {});
      setBusy(false);
      setTick((x) => x + 1);
    } catch (e) {
      setWlError(errText(e));
    }
  };

  const flagged = data?.tickers.filter((t) => t.flagged) ?? [];

  return (
    <div className="space-y-3 p-3">
      <Panel
        title="Opportunity Scanner"
        right={
          <span className="flex items-center gap-2">
            {data?.is_demo && <DemoTag />}
            <button
              onClick={scanNow}
              disabled={busy}
              className="rounded-md px-2.5 py-1 text-[11px] font-semibold text-ink ring-1 ring-line-strong hover:bg-white/5 disabled:opacity-50"
            >
              {busy ? "Scanning…" : "Scan now"}
            </button>
          </span>
        }
      >
        <div className="space-y-3 p-3">
          {snap?.error && <DataUnavailable compact message={snap.error} />}
          {data && (
            <p className="text-[11px] text-muted">
              Scan #{data.scan_id} ({data.trigger}) finished {et(data.finished_at, true)} in {(data.duration_ms / 1000).toFixed(1)} s ·{" "}
              {data.watchlist.length} tickers · {data.total_rows} detected setups · feed {data.feed.label} ({data.feed.latency}) · server
              rescans automatically when new candles close (checked every {data.poll_seconds || "—"} s)
              {data.due && " · new candles available, rescan pending"}
              {data.last_error && <span className="text-bear"> · last scan error: {data.last_error}</span>}
            </p>
          )}
          {flagged.length > 0 && (
            <div className="rounded-md bg-bear/10 px-3 py-2 text-[11px] text-bear ring-1 ring-bear/40">
              <span className="font-bold">STALE OR MISSING DATA</span> — setups for these tickers are not actionable:{" "}
              {flagged.map((t) => `${t.ticker} (${t.flags.join("; ")})`).join(" · ")}
            </div>
          )}
          {/* Watchlist editor */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Watchlist</span>
            {(data?.watchlist ?? []).map((s) => (
              <span key={s} className="flex items-center gap-1 rounded bg-panel-2 px-2 py-0.5 text-[11px] ring-1 ring-line">
                {s}
                <button
                  aria-label={`Remove ${s}`}
                  onClick={() => changeWatchlist(() => api.removeSymbol(s))}
                  className="text-faint hover:text-bear"
                >
                  ×
                </button>
              </span>
            ))}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const s = newSym.trim().toUpperCase();
                if (s) changeWatchlist(() => api.addSymbol(s)).then(() => setNewSym(""));
              }}
              className="flex items-center gap-1"
            >
              <input
                value={newSym}
                onChange={(e) => setNewSym(e.target.value)}
                placeholder="Add ticker"
                aria-label="Add ticker"
                className="w-24 rounded bg-panel-2 px-2 py-0.5 text-[11px] uppercase ring-1 ring-line-strong outline-none focus:ring-accent"
              />
              <button type="submit" className="rounded px-2 py-0.5 text-[11px] font-semibold text-accent ring-1 ring-accent/40 hover:bg-accent/10">
                Add
              </button>
            </form>
            {wlError && <span className="text-[11px] text-bear">{wlError}</span>}
          </div>
          {/* Ticker overview: price, bias, S/R proximity, freshness */}
          <div className="flex gap-2 overflow-x-auto pb-1">
            {(data?.tickers ?? []).map((t) => (
              <TickerCard key={t.ticker} t={t} onOpen={onOpenTicker} />
            ))}
          </div>
          {/* Filters */}
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <Seg value={mode} onChange={setMode} options={[["all", "All"], ["day", "Day trading"], ["swing", "Swing trading"]]} />
            <Seg value={side} onChange={setSide} options={[["all", "Both"], ["bullish", "Bullish"], ["bearish", "Bearish"]]} />
            {(["TRIGGERED", "WATCHING", "NO_TRADE"] as const).map((s) => (
              <label key={s} className="flex items-center gap-1 text-muted">
                <input
                  type="checkbox"
                  checked={statuses[s]}
                  onChange={(e) => setStatuses({ ...statuses, [s]: e.target.checked })}
                  className="accent-[#4f9cf9]"
                />
                {s === "NO_TRADE" ? "Fails requirements" : STATUS_LABEL[s]}
              </label>
            ))}
            <label className="flex items-center gap-1 text-muted">
              Min R:R
              <input value={minRr} onChange={(e) => setMinRr(e.target.value)} inputMode="decimal" placeholder="any"
                className="w-14 rounded bg-panel-2 px-1.5 py-0.5 ring-1 ring-line-strong outline-none focus:ring-accent" />
            </label>
            <label className="flex items-center gap-1 text-muted">
              Min quality
              <input value={minQ} onChange={(e) => setMinQ(e.target.value)} inputMode="numeric" placeholder="any"
                className="w-14 rounded bg-panel-2 px-1.5 py-0.5 ring-1 ring-line-strong outline-none focus:ring-accent" />
            </label>
            <label className="flex items-center gap-1 text-muted">
              Strategy
              <select value={strategy} onChange={(e) => setStrategy(e.target.value)}
                className="rounded bg-panel-2 px-1 py-0.5 text-ink ring-1 ring-line-strong">
                <option value="">All</option>
                {PATTERNS.map((p) => (
                  <option key={p} value={p}>
                    {p[0].toUpperCase() + p.slice(1)} setups
                  </option>
                ))}
                {(data?.strategies ?? []).map((s) => (
                  <option key={s} value={s}>
                    {s.replaceAll("_", " ")}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
      </Panel>
      <div className="grid gap-3 2xl:grid-cols-2">
        {side !== "bearish" && (
          <div className="h-[460px]">
            <Table rows={bull} title="Bullish setups" tone="bull" onOpen={onOpenRow} />
          </div>
        )}
        {side !== "bullish" && (
          <div className="h-[460px]">
            <Table rows={bear} title="Bearish setups" tone="bear" onOpen={onOpenRow} />
          </div>
        )}
      </div>
      <p className="text-center text-[10px] text-faint">
        Every row is a setup computed by the day/swing strategy engines and the trade-planning engine; ranked by status, then the
        transparent quality score (not a win probability). ● = triggered on fresh data. Click a row to open its chart and full analysis.
      </p>
    </div>
  );
}
