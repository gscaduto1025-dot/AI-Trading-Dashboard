"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { BacktestDefaults, BacktestRunDetail, BacktestRunRow, BacktestStats, BacktestTrade, Mode } from "@/lib/types";
import { DivergingBar, DrawdownChart, EquityChart, QualityChart, RHistogram } from "./BacktestCharts";
import BacktestTradeInspector from "./BacktestTradeInspector";
import { Empty, Panel } from "./ui";

const n = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));
const sR = (v: number | null | undefined, d = 2) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}R`);
const usd = (v: number | null | undefined) =>
  v == null ? "—" : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const day = (t: number) => new Date(t * 1000).toLocaleString("en-US", { timeZone: "America/New_York", dateStyle: "short", timeStyle: "short" });

const VERDICT_CLS: Record<string, string> = {
  "POSITIVE EVIDENCE (hypothetical)": "border-bull/40 bg-bull/5",
  "NOT EVIDENCE": "border-wait/40 bg-wait/5",
};

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-3 py-2">
      <p className="text-[10px] text-muted">{label}</p>
      <p className="text-lg font-semibold text-ink">{value}</p>
      {sub && <p className="text-[10px] text-faint">{sub}</p>}
    </div>
  );
}

function StatsTable({ rows, names }: { rows: [string, BacktestStats][]; names?: Record<string, string> }) {
  const max = Math.max(0.5, ...rows.map(([, s]) => Math.abs(s.expectancy_r ?? 0)));
  if (!rows.length) return <Empty>No trades.</Empty>;
  return (
    <table className="w-full text-[11px]">
      <thead className="text-left text-[10px] uppercase tracking-wider text-faint">
        <tr>
          {["", "Trades", "Win %", "Avg win", "Avg loss", "PF", "Expectancy", "", "95% CI", "Max DD"].map((h, i) => (
            <th key={i} className="px-2 py-1 font-medium">{h}</th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-line">
        {rows.map(([k, s]) => (
          <tr key={k} className="tabular-nums">
            <td className="px-2 py-1 font-sans font-medium">{names?.[k] ?? k}</td>
            <td className="px-2 py-1">{s.trades}</td>
            <td className="px-2 py-1">{n(s.win_rate, 1)}</td>
            <td className="px-2 py-1">{sR(s.avg_win_r)}</td>
            <td className="px-2 py-1">{sR(s.avg_loss_r)}</td>
            <td className="px-2 py-1">{n(s.profit_factor)}</td>
            <td className="px-2 py-1">{sR(s.expectancy_r)}</td>
            <td className="px-2 py-1"><DivergingBar value={s.expectancy_r} max={max} /></td>
            <td className="px-2 py-1 text-faint">
              {s.expectancy_ci95_r ? `${sR(s.expectancy_ci95_r[0])} … ${sR(s.expectancy_ci95_r[1])}` : "—"}
            </td>
            <td className="px-2 py-1">{s.max_drawdown_r != null ? `${n(s.max_drawdown_r, 1)}R` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NewRun({ defaults, onStarted }: { defaults: BacktestDefaults; onStarted: (id: string) => void }) {
  const [mode, setMode] = useState<Mode | "both">("swing");
  const [capital, setCapital] = useState("100000");
  const [riskPct, setRiskPct] = useState("1");
  const [posPct, setPosPct] = useState("25");
  const [source, setSource] = useState<"market" | "bundled">("bundled");
  const [symbols, setSymbols] = useState("AAPL, GOOG");
  const [start, setStart] = useState("2005-01-03");
  const [end, setEnd] = useState("2017-02-16");
  const [exit, setExit] = useState("t1");
  const [spread, setSpread] = useState("");
  const [slip, setSlip] = useState("");
  const [comm, setComm] = useState("");
  const [delay, setDelay] = useState("0");
  const [universe, setUniverse] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const s = defaults.settings;

  const pickSource = (v: "market" | "bundled") => {
    setSource(v);
    if (v === "bundled") {
      setMode("swing");
      setSymbols(defaults.sources.bundled.symbols.join(", "));
    } else setSymbols(defaults.sources.market.symbols.slice(0, 3).join(", "));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    const settings: Record<string, unknown> = {
      exit_policy: exit,
      entry_delay_bars: Number(delay) || 0,
      starting_capital: Number(capital) || 100000,
      risk_per_trade_pct: Number(riskPct) || 1,
      max_position_pct: Number(posPct) || 25,
    };
    const modes = mode === "both" ? ["day", "swing"] : [mode];
    if (spread) settings.half_spread_bps = Object.fromEntries(modes.map((m) => [m, Number(spread)]));
    if (slip) settings.slippage_bps = Object.fromEntries(modes.map((m) => [m, Number(slip)]));
    if (comm) settings.commission_per_share = Number(comm);
    let uni = null;
    if (universe.trim()) {
      // one per line: SYMBOL[,start][,end]
      uni = universe
        .trim()
        .split("\n")
        .map((l) => l.split(",").map((x) => x.trim()))
        .map(([symbol, s0, e0]) => ({ symbol, start: s0 || undefined, end: e0 || undefined }));
    }
    try {
      const r = await api.startBacktest({
        mode,
        data_source: source,
        symbols: symbols.split(/[\s,]+/).filter(Boolean),
        start,
        end,
        settings,
        universe: uni,
      });
      onStarted(r.id);
    } catch (x) {
      setErr((x as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const input = "rounded bg-white/5 px-2 py-1 ring-1 ring-line-strong";
  return (
    <form onSubmit={submit} className="grid gap-2 p-3 text-[11px] md:grid-cols-4">
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Data source</span>
        <select value={source} onChange={(e) => pickSource(e.target.value as "market" | "bundled")} className={input}>
          {(["bundled", "market"] as const).map((k) => (
            <option key={k} value={k}>{defaults.sources[k].label}</option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Mode</span>
        <select value={mode} onChange={(e) => setMode(e.target.value as Mode | "both")} className={input} disabled={source === "bundled"}>
          <option value="swing">Swing (daily/weekly)</option>
          <option value="day">Day (5/15-minute)</option>
          <option value="both">Day + swing (split by mode)</option>
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">From</span>
        <input type="date" value={start} onChange={(e) => setStart(e.target.value)} className={input} />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">To (max {defaults.max_days[mode === "both" ? "day" : mode]} days)</span>
        <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className={input} />
      </label>
      <label className="flex flex-col gap-0.5 md:col-span-2">
        <span className="text-faint">Symbols (ignored if a point-in-time universe is given)</span>
        <input value={symbols} onChange={(e) => setSymbols(e.target.value)} className={input} />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Exit policy</span>
        <select value={exit} onChange={(e) => setExit(e.target.value)} className={input}>
          <option value="t1">All at T1</option>
          <option value="t2">All at T2</option>
          <option value="split">Half at T1, half at T2</option>
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Entry delay (candles after signal)</span>
        <input value={delay} onChange={(e) => setDelay(e.target.value)} className={input} inputMode="numeric" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Half spread, bps (default {s.half_spread_bps[mode === "both" ? "day" : mode]})</span>
        <input value={spread} onChange={(e) => setSpread(e.target.value)} className={input} inputMode="decimal" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Slippage, bps (default {s.slippage_bps[mode === "both" ? "day" : mode]})</span>
        <input value={slip} onChange={(e) => setSlip(e.target.value)} className={input} inputMode="decimal" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Commission $/share/side (default {s.commission_per_share})</span>
        <input value={comm} onChange={(e) => setComm(e.target.value)} className={input} inputMode="decimal" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Starting capital ($)</span>
        <input value={capital} onChange={(e) => setCapital(e.target.value)} className={input} inputMode="numeric" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Risk per trade (% of equity)</span>
        <input value={riskPct} onChange={(e) => setRiskPct(e.target.value)} className={input} inputMode="decimal" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-faint">Max position (% of equity, no margin)</span>
        <input value={posPct} onChange={(e) => setPosPct(e.target.value)} className={input} inputMode="decimal" />
      </label>
      <label className="flex flex-col gap-0.5 md:row-span-2">
        <span className="text-faint">Point-in-time universe (optional): SYMBOL,from,to per line</span>
        <textarea value={universe} onChange={(e) => setUniverse(e.target.value)} rows={3}
          placeholder={"GOOG,2005-01-01\nAAPL,,2016-12-31"} className={`${input} font-mono`} />
      </label>
      <div className="flex items-end gap-2 md:col-span-3">
        <button disabled={busy} className="rounded bg-accent/15 px-3 py-1.5 font-semibold text-accent ring-1 ring-accent/40 disabled:opacity-50">
          {busy ? "Starting…" : "Run backtest"}
        </button>
        <span className="text-faint">
          Uses the live strategy and planning engines, replayed candle by candle. Hypothetical trades only — no orders are ever sent.
        </span>
        {err && <span className="text-bear">{err}</span>}
      </div>
    </form>
  );
}

const PAGE = 50;

export default function BacktestView() {
  const [defaults, setDefaults] = useState<BacktestDefaults | null>(null);
  const [runs, setRuns] = useState<BacktestRunRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<BacktestRunDetail | null>(null);
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [showNew, setShowNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [sort, setSort] = useState<{ k: keyof BacktestTrade; desc: boolean }>({ k: "entry_time", desc: false });
  const [group, setGroup] = useState<"by_strategy" | "by_ticker" | "by_timeframe" | "by_direction" | "by_mode" | "by_regime">("by_strategy");

  const loadRuns = useCallback(() => {
    api
      .backtests()
      .then((r) => {
        setRuns(r.runs);
        setSelected((cur) => cur ?? r.runs[0]?.id ?? null);
      })
      .catch((e: ApiError) => setError(e.message));
  }, []);

  useEffect(() => {
    api.backtestDefaults().then(setDefaults).catch((e: ApiError) => setError(e.message));
    loadRuns();
  }, [loadRuns]);

  const fkey = JSON.stringify(filters);
  useEffect(() => {
    if (!selected) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const f = JSON.parse(fkey) as Record<string, string>;
    const poll = () =>
      api
        .backtest(selected, Object.fromEntries(Object.entries(f).filter(([, v]) => v)))
        .then((d) => {
          if (!live) return;
          setDetail(d);
          if (d.status === "running") timer = setTimeout(poll, 1500);
          else loadRuns();
        })
        .catch((e: ApiError) => live && setError(e.message));
    poll();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [selected, fkey, loadRuns]);

  const rep = detail?.id === selected ? detail?.report : undefined;
  const run = detail?.id === selected ? detail?.run : undefined;
  const names = useMemo(
    () => Object.fromEntries((rep?.trades ?? []).map((t) => [t.strategy, t.strategy_name])),
    [rep],
  );
  const allTrades = useMemo(() => {
    const t = [...(rep?.trades ?? [])];
    t.sort((a, b) => {
      const x = a[sort.k] as number | string;
      const y = b[sort.k] as number | string;
      return (x < y ? -1 : x > y ? 1 : 0) * (sort.desc ? -1 : 1);
    });
    return t;
  }, [rep, sort]);
  const o = rep?.overall;
  const splitTs = rep?.out_of_sample.from ? Date.parse(`${rep.out_of_sample.from}T00:00:00-05:00`) / 1000 : null;
  const [known, setKnown] = useState<{ id: string | null; strategy: string[]; timeframe: string[]; mode: string[]; regime: string[]; names: Record<string, string> }>({
    id: null, strategy: [], timeframe: [], mode: [], regime: [], names: {},
  });
  const [inspect, setInspect] = useState<BacktestTrade | null>(null);
  useEffect(() => {
    // Options come from the unfiltered run so a filter never hides its siblings.
    if (!rep || !selected) return;
    const t = setTimeout(() =>
      setKnown((k) => {
        const base = k.id === selected ? k : { id: selected, strategy: [], timeframe: [], mode: [], regime: [], names: {} };
        const u = (a: string[], b: Record<string, unknown>) => Array.from(new Set([...a, ...Object.keys(b)])).sort();
        return {
          id: selected,
          strategy: u(base.strategy, rep.by_strategy),
          timeframe: u(base.timeframe, rep.by_timeframe),
          mode: u(base.mode, rep.by_mode),
          regime: u(base.regime, rep.by_regime),
          names: { ...base.names, ...names },
        };
      }), 0);
    return () => clearTimeout(t);
  }, [rep, selected, names]);
  const options = {
    strategy: known.id === selected ? known.strategy : [],
    ticker: run?.symbols ?? [],
    timeframe: known.id === selected ? known.timeframe : [],
    mode: known.id === selected ? known.mode : [],
    regime: known.id === selected ? known.regime : [],
  };
  const setF = (k: string, v: string) => {
    setPage(0);
    setFilters((f) => ({ ...f, [k]: v }));
  };
  const sel = "rounded bg-white/5 px-2 py-1 ring-1 ring-line-strong";

  return (
    <main className="space-y-3 p-3">
      <section className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-xs">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Backtesting</span>
        <select aria-label="Backtest run" value={selected ?? ""} onChange={(e) => { setSelected(e.target.value || null); setFilters({}); setPage(0); }} className={`${sel} max-w-[420px]`}>
          {!runs?.length && <option value="">No runs yet</option>}
          {runs?.map((r) => (
            <option key={r.id} value={r.id}>
              {r.request.mode} · {r.request.data_source === "bundled" ? "bundled real" : "market"} · {(r.request.symbols ?? []).join(",")} ·{" "}
              {r.request.start}→{r.request.end} · {r.status === "done" ? `${r.summary?.trades} trades · ${r.summary?.verdict}` : r.status}
            </option>
          ))}
        </select>
        <button onClick={() => setShowNew((v) => !v)} className="rounded px-2 py-1 text-accent ring-1 ring-accent/40 hover:bg-accent/10">
          {showNew ? "Close" : "New backtest"}
        </button>
        {selected && detail?.status === "done" && (
          <a href={`/api/backtests/${selected}/trades.csv`} className="rounded px-2 py-1 text-muted ring-1 ring-line-strong hover:text-ink">
            Export trades (CSV)
          </a>
        )}
        {selected && detail && detail.status !== "running" && (
          <button
            onClick={() => api.deleteBacktest(selected).then(() => { setSelected(null); setDetail(null); loadRuns(); })}
            className="rounded px-2 py-1 text-muted ring-1 ring-line-strong hover:text-bear"
          >
            Delete run
          </button>
        )}
        <span className="ml-auto text-[10px] text-faint">Hypothetical results · no real-money or automatic trading</span>
      </section>

      {error && <p className="rounded border border-bear/30 bg-bear/10 px-3 py-2 text-xs text-bear">{error}</p>}

      {showNew && defaults && (
        <Panel title="New backtest">
          <NewRun defaults={defaults} onStarted={(id) => { setShowNew(false); setFilters({}); setSelected(id); loadRuns(); }} />
        </Panel>
      )}

      {!selected ? (
        <Empty>No backtests yet. Start one with “New backtest”.</Empty>
      ) : !detail || detail.id !== selected ? (
        <Empty>Loading…</Empty>
      ) : detail.status === "running" ? (
        <section className="rounded-lg border border-line bg-panel p-4 text-xs">
          <p className="mb-2 text-ink">Running… {detail.live?.message}</p>
          <div className="h-2 w-full overflow-hidden rounded bg-white/5">
            <div className="h-2 rounded bg-accent transition-all" style={{ width: `${Math.round((detail.live?.progress ?? 0) * 100)}%` }} />
          </div>
        </section>
      ) : detail.status !== "done" ? (
        <section className="rounded-lg border border-bear/30 bg-bear/10 p-3 text-xs text-bear">
          Backtest {detail.status}: {detail.error}
        </section>
      ) : rep && run && o ? (
        <>
          {/* Filters: one row, scoping everything below */}
          <section className="flex flex-wrap items-center gap-2 text-xs">
            {(["strategy", "ticker", "timeframe", "mode", "regime"] as const).map((k) => (
              <select key={k} aria-label={k} value={filters[k] ?? ""} onChange={(e) => setF(k, e.target.value)} className={sel}>
                <option value="">All {k === "strategy" ? "strategies" : k === "mode" ? "modes (day/swing)" : k === "regime" ? "market regimes" : `${k}s`}</option>
                {options[k].map((v) => (
                  <option key={v} value={v}>{k === "strategy" ? known.names[v] ?? names[v] ?? v : v}</option>
                ))}
              </select>
            ))}
            <select aria-label="direction" value={filters.direction ?? ""} onChange={(e) => setF("direction", e.target.value)} className={sel}>
              <option value="">Long & short</option>
              <option value="LONG">Long</option>
              <option value="SHORT">Short</option>
            </select>
            <label className="flex items-center gap-1 text-faint">
              from <input type="date" aria-label="from" value={filters.start ?? ""} onChange={(e) => setF("start", e.target.value)} className={sel} />
            </label>
            <label className="flex items-center gap-1 text-faint">
              to <input type="date" aria-label="to" value={filters.end ?? ""} onChange={(e) => setF("end", e.target.value)} className={sel} />
            </label>
            {Object.values(filters).some(Boolean) && (
              <button onClick={() => setFilters({})} className="text-muted hover:text-ink">Clear filters</button>
            )}
            <span className="ml-auto text-[10px] text-faint">
              {run.data_description} · {run.start} → {run.end} · {run.engine_evaluations} engine evaluations · regimes from{" "}
              {run.regime_basis ?? "—"}
            </span>
          </section>

          <section className={`rounded-lg border px-3 py-2 text-xs ${VERDICT_CLS[rep.verdict.label] ?? "border-line-strong bg-panel"}`}>
            <p className="font-semibold">
              <span aria-hidden className="mr-1">{rep.verdict.positive_evidence ? "✓" : "⚠"}</span>
              Evidence: {rep.verdict.label}
            </p>
            <ul className="mt-1 list-disc pl-5 text-ink/80">
              {rep.verdict.reasons.map((r) => <li key={r}>{r}</li>)}
            </ul>
          </section>

          <section className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
            <Tile label="Total trades" value={`${o.trades}`} sub={`${o.wins ?? 0} wins · ${o.losses ?? 0} losses · ${o.sample_note ?? ""}`} />
            <Tile label="Win rate" value={o.win_rate == null ? "—" : `${o.win_rate.toFixed(1)}%`} />
            <Tile label="Average R (expectancy)" value={sR(o.expectancy_r)} sub={o.expectancy_ci95_r ? `95% CI ${sR(o.expectancy_ci95_r[0])} … ${sR(o.expectancy_ci95_r[1])}` : "CI needs ≥ 2 trades"} />
            <Tile label="Profit factor" value={n(o.profit_factor)} />
            <Tile label="Average winner" value={sR(o.avg_win_r)} sub={o.avg_win_pct == null ? undefined : `${o.avg_win_pct.toFixed(2)}% per share`} />
            <Tile label="Average loser" value={sR(o.avg_loss_r)} sub={o.avg_loss_pct == null ? undefined : `${o.avg_loss_pct.toFixed(2)}% per share`} />
            <Tile label="Avg planned R:R" value={o.avg_planned_rr == null ? "—" : `${o.avg_planned_rr.toFixed(2)}:1`} sub={o.realized_win_loss_ratio == null ? undefined : `realized win/loss ${o.realized_win_loss_ratio.toFixed(2)}`} />
            <Tile label="Max drawdown" value={`${rep.portfolio.max_drawdown_pct.toFixed(1)}%`} sub={`${usd(rep.portfolio.max_drawdown_usd)} · ${o.max_drawdown_r?.toFixed(1) ?? "—"}R`} />
          </section>
          <section className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Tile label="Total return (position-sized)" value={`${rep.portfolio.total_return_pct >= 0 ? "+" : ""}${rep.portfolio.total_return_pct.toFixed(2)}%`} sub={`${usd(rep.portfolio.total_pnl)} on ${usd(rep.portfolio.starting_capital)}`} />
            <Tile label="Trades taken by the portfolio" value={`${rep.portfolio.trades_taken} of ${o.trades}`} sub={Object.entries(rep.portfolio.skipped).map(([k, v]) => `${v} ${k}`).join(" · ") || "none skipped for capital"} />
            <Tile label="Max open positions" value={`${rep.portfolio.max_concurrent}`} sub={rep.portfolio.assumptions} />
            <Tile label="Total R (sum)" value={sR(o.total_r)} sub={`avg cost ${sR(o.avg_cost_r)} per trade`} />
          </section>

          <section className="grid gap-3 xl:grid-cols-2">
            <Panel title={`Portfolio equity (position-sized, net of all costs)`} bodyClassName="p-2">
              <EquityChart points={rep.portfolio.curve} splitTime={splitTs} base={rep.portfolio.starting_capital}
                fmt={(v) => `$${v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : `${(v / 1e3).toFixed(v % 1000 ? 1 : 0)}K`}`} />
              <p className="mt-2 px-1 text-[10px] text-muted">Drawdown from the running peak</p>
              <DrawdownChart points={rep.portfolio.curve} />
            </Panel>
            <div className="grid gap-3">
              <Panel title="Distribution of realized R multiples" bodyClassName="p-2">
                <RHistogram rs={rep.trades.map((t) => t.r)} />
              </Panel>
              <Panel title="Quality score vs realized outcome" bodyClassName="p-2">
                <QualityChart buckets={rep.quality.buckets} />
                <p className="mt-1 px-1 text-[11px] text-ink/80">
                  {rep.quality.verdict}{" "}
                  <span className="text-faint">Quality is a rules score, not a win probability.</span>
                </p>
              </Panel>
            </div>
          </section>

          <section className="grid gap-3 xl:grid-cols-2">
            <Panel title="In-sample vs out-of-sample vs walk-forward" bodyClassName="overflow-x-auto p-1">
              <StatsTable
                rows={[
                  [`In-sample (until ${rep.in_sample.until})`, rep.in_sample],
                  [`Out-of-sample (from ${rep.out_of_sample.from})`, rep.out_of_sample],
                  ["Walk-forward out-of-sample", rep.walk_forward.oos],
                ]}
              />
              <p className="px-2 py-1 text-[10px] text-faint">{rep.walk_forward.method}</p>
              <table className="mt-1 w-full text-[11px]">
                <thead className="text-left text-[10px] uppercase tracking-wider text-faint">
                  <tr>{["Fold", "Train", "Train trades", "Min quality chosen", "Test period", "Test trades", "Test expectancy"].map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
                </thead>
                <tbody className="divide-y divide-line tabular-nums">
                  {rep.walk_forward.folds.map((f) => (
                    <tr key={f.fold}>
                      <td className="px-2 py-1">{f.fold}</td>
                      <td className="px-2 py-1">{f.train[0]} → {f.train[1]}</td>
                      <td className="px-2 py-1">{f.train_trades}</td>
                      <td className="px-2 py-1">{f.min_quality}</td>
                      <td className="px-2 py-1">{f.test_period[0]} → {f.test_period[1]}</td>
                      <td className="px-2 py-1">{f.test.trades}</td>
                      <td className="px-2 py-1">{sR(f.test.expectancy_r)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
            <Panel
              title="Results by"
              right={
                <span className="flex gap-1">
                  {(["by_strategy", "by_ticker", "by_timeframe", "by_direction", "by_mode", "by_regime"] as const).map((g) => (
                    <button key={g} onClick={() => setGroup(g)} className={`rounded px-1.5 py-0.5 text-[10px] ${group === g ? "bg-white/10 text-ink" : "text-muted hover:text-ink"}`}>
                      {g.replace("by_", "")}
                    </button>
                  ))}
                </span>
              }
              bodyClassName="overflow-x-auto p-1"
            >
              <StatsTable rows={Object.entries(rep[group])} names={group === "by_strategy" ? names : undefined} />
              <p className="px-2 py-1 text-[10px] text-faint">
                Exits: {Object.entries(o.exits ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"} · average cost {sR(o.avg_cost_r)} per trade
              </p>
            </Panel>
          </section>

          {inspect && selected && (
            <BacktestTradeInspector runId={selected} trade={inspect} onClose={() => setInspect(null)} />
          )}

          <Panel title={`Trade-level report (${allTrades.length}) · simulated, separate from your trade log · click a row to inspect`} bodyClassName="overflow-x-auto">
            {!allTrades.length ? (
              <Empty>No trades match the filters.</Empty>
            ) : (
              <>
                <table className="w-full text-[11px]">
                  <thead className="text-left text-[10px] uppercase tracking-wider text-faint">
                    <tr>
                      {([["entry_time", "Entry"], ["ticker", "Ticker"], ["strategy", "Strategy"], ["timeframe", "TF"], ["direction", "Dir"], ["entry", "Fill"], ["stop", "Stop"], ["target1", "T1"], ["exit", "Exit"], ["exit_reason", "Exit reason"], ["bars_held", "Bars"], ["r", "R (net)"], ["gross_r", "R (gross)"], ["return_pct", "Return %"], ["quality", "Quality"], ["planned_rr", "Planned R:R"], ["shares", "Shares"], ["pnl_usd", "P&L $"], ["regime", "Regime"]] as [keyof BacktestTrade, string][]).map(([k, label]) => (
                        <th key={k} className="cursor-pointer px-2 py-1 font-medium hover:text-ink" onClick={() => setSort((s) => ({ k, desc: s.k === k ? !s.desc : true }))}>
                          {label}{sort.k === k ? (sort.desc ? " ↓" : " ↑") : ""}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line tabular-nums">
                    {allTrades.slice(page * PAGE, page * PAGE + PAGE).map((t) => (
                      <tr key={t.id} onClick={() => setInspect(t)}
                        className={`cursor-pointer hover:bg-white/[0.03] ${inspect?.id === t.id ? "bg-accent/10" : ""}`}
                        title={`Click to inspect · signal ${day(t.signal_time)} · MAE ${t.mae_r}R · MFE ${t.mfe_r}R`}>
                        <td className="px-2 py-1">
                          {t.mode === "swing"
                            ? new Date(t.entry_time * 1000).toLocaleDateString("en-US", { timeZone: "America/New_York", dateStyle: "short" })
                            : day(t.entry_time)}
                        </td>
                        <td className="px-2 py-1 font-sans font-semibold">{t.ticker}</td>
                        <td className="px-2 py-1 font-sans">{t.strategy_name}</td>
                        <td className="px-2 py-1">{t.timeframe}</td>
                        <td className="px-2 py-1">{t.direction === "LONG" ? "Long" : "Short"}</td>
                        <td className="px-2 py-1">{n(t.entry)}</td>
                        <td className="px-2 py-1">{n(t.stop)}</td>
                        <td className="px-2 py-1">{n(t.target1)}</td>
                        <td className="px-2 py-1">{n(t.exit)}</td>
                        <td className="px-2 py-1 font-sans">{t.exit_reason}</td>
                        <td className="px-2 py-1">{t.bars_held}</td>
                        <td className={`px-2 py-1 font-semibold ${t.r >= 0 ? "text-bull" : "text-bear"}`}>{sR(t.r)}</td>
                        <td className="px-2 py-1 text-faint">{sR(t.gross_r)}</td>
                        <td className="px-2 py-1">{n(t.return_pct)}</td>
                        <td className="px-2 py-1">{n(t.quality, 1)}</td>
                        <td className="px-2 py-1">{t.planned_rr == null ? "—" : `${t.planned_rr}:1`}</td>
                        <td className="px-2 py-1">{t.shares || "—"}</td>
                        <td className={`px-2 py-1 ${(t.pnl_usd ?? 0) >= 0 ? "" : "text-bear"}`}>{t.pnl_usd == null ? "—" : t.pnl_usd.toFixed(0)}</td>
                        <td className="px-2 py-1 font-sans text-faint">{t.regime}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {allTrades.length > PAGE && (
                  <div className="flex items-center gap-2 px-3 py-2 text-[11px] text-muted">
                    <button disabled={page === 0} onClick={() => setPage((p) => p - 1)} className="disabled:opacity-40">← Prev</button>
                    <span>Page {page + 1} of {Math.ceil(allTrades.length / PAGE)}</span>
                    <button disabled={(page + 1) * PAGE >= allTrades.length} onClick={() => setPage((p) => p + 1)} className="disabled:opacity-40">Next →</button>
                  </div>
                )}
              </>
            )}
          </Panel>

          <section className="grid gap-3 xl:grid-cols-3">
            <Panel title="Signals not traded" bodyClassName="p-3 text-[11px]">
              {Object.keys(rep.skipped_reasons).length === 0 && !run.not_live.length ? (
                <p className="text-faint">Every live signal was simulated.</p>
              ) : (
                <ul className="space-y-0.5 text-ink/80">
                  {Object.entries(rep.skipped_reasons).map(([k, v]) => <li key={k}><span className="tabular-nums font-semibold">{v}</span> · {k}</li>)}
                  {run.not_live.length > 0 && (
                    <li>
                      <span className="tabular-nums font-semibold">{run.not_live.length}</span> · accepted triggers the live engine did not show as TRIGGERED at
                      confirmation (e.g. a re-trigger after the first trigger of the session resolved) — excluded, as they never alerted live
                    </li>
                  )}
                </ul>
              )}
            </Panel>
            <Panel title="Data integrity" bodyClassName="max-h-56 overflow-y-auto p-3 text-[11px]">
              {run.data_errors.map((e) => <p key={e.symbol} className="text-bear">{e.symbol}: {e.error}</p>)}
              {run.survivorship_biased && (
                <p className="mb-1 text-wait">⚠ Survivorship bias: the universe is a hindsight list of current tickers. Supply a point-in-time universe to reduce it.</p>
              )}
              <ul className="space-y-0.5 text-ink/80">
                {run.data_integrity.filter((d) => d.bars || d.issues.length).map((d) => (
                  <li key={`${d.symbol}-${d.timeframe}`}>
                    {d.symbol} {d.timeframe}: {d.bars} candles{d.first ? ` (${d.first.slice(0, 10)} → ${d.last?.slice(0, 10)})` : ""}
                    {d.issues.length ? <span className="text-wait"> · {d.issues.join("; ")}</span> : <span className="text-faint"> · no issues</span>}
                  </li>
                ))}
              </ul>
            </Panel>
            <Panel title="Assumptions" bodyClassName="max-h-56 overflow-y-auto p-3 text-[11px]">
              <ul className="space-y-0.5 text-ink/80">
                <li>Entry: market order at the open of the candle after the signal (+{run.settings.entry_delay_bars as number} candles), no chasing outside the plan&apos;s entry zone.</li>
                <li>Costs per side: half spread {JSON.stringify(run.settings.half_spread_bps)} bps, slippage {JSON.stringify(run.settings.slippage_bps)} bps, commission ${run.settings.commission_per_share as number}/share.</li>
                <li>Stop and target in the same candle: stop assumed first. Targets fill only when traded through by one tick.</li>
                <li>Exit policy: {run.settings.exit_policy as string}; day trades flat at the close; swing time exit after {JSON.stringify((run.settings.max_hold_bars as Record<string, number | null>).swing)} days.</li>
                <li>In-sample fraction {run.settings.in_sample_fraction as number}; walk-forward folds {run.settings.walk_forward_folds as number}.</li>
              </ul>
            </Panel>
          </section>

          <Panel title="Limitations of these results" bodyClassName="p-3">
            <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-ink/80">
              {run.limitations.map((l) => <li key={l}>{l}</li>)}
            </ul>
          </Panel>
        </>
      ) : null}
    </main>
  );
}
