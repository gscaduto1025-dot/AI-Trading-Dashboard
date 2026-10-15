"use client";

import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import type { JournalAccount, PerfGroup, Performance } from "@/lib/types";
import { DataUnavailable, Empty, Panel } from "./ui";

const money = (v: number | null | undefined) =>
  v == null ? "—" : `${v < 0 ? "−" : v > 0 ? "+" : ""}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const inp = "rounded bg-panel-2 px-2 py-0.5 ring-1 ring-line-strong outline-none focus:ring-accent";

function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-3 py-2">
      <p className="text-[10px] text-muted">{label}</p>
      <p className={`num text-lg font-semibold ${tone ?? "text-ink"}`}>{value}</p>
      {sub && <p className="text-[10px] text-faint">{sub}</p>}
    </div>
  );
}

/** Cumulative realized net P&L by trade close (one series; zero line for reference). */
function CumulativeChart({ pts }: { pts: Performance["cumulative"] }) {
  if (pts.length < 2) return <Empty>At least two closed trades are needed for a P&L curve.</Empty>;
  const W = 760, H = 220, L = 70, R = 16, T = 16, B = 28;
  const ys = [0, ...pts.map((p) => p.cumulative)];
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const x = (i: number) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v: number) => T + ((hi - v) / (hi - lo || 1)) * (H - T - B);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.cumulative)}`).join(" ");
  const last = pts[pts.length - 1];
  const ticks = [lo, (lo + hi) / 2, hi];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[900px]" role="img" aria-label={`Cumulative net P&L, ending at ${money(last.cumulative)}`}>
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="var(--color-line)" />
          <text x={L - 6} y={y(t) + 4} textAnchor="end" fontSize="10" fill="var(--color-faint)">{money(Math.round(t))}</text>
        </g>
      ))}
      <line x1={L} x2={W - R} y1={y(0)} y2={y(0)} stroke="var(--color-muted)" />
      <path d={path} fill="none" stroke="var(--color-accent)" strokeWidth="2" />
      {pts.map((p, i) => (
        <circle key={i} cx={x(i)} cy={y(p.cumulative)} r="3" fill="var(--color-accent)">
          <title>{`${p.date} ${p.ticker}: trade ${money(p.net)}, cumulative ${money(p.cumulative)}`}</title>
        </circle>
      ))}
      <text x={x(pts.length - 1) - 4} y={y(last.cumulative) - 8} textAnchor="end" fontSize="11" fill="var(--color-ink)">{money(last.cumulative)}</text>
      <text x={L} y={H - 8} fontSize="10" fill="var(--color-faint)">{pts[0].date}</text>
      <text x={W - R} y={H - 8} textAnchor="end" fontSize="10" fill="var(--color-faint)">{last.date}</text>
    </svg>
  );
}

/** Monthly realized net P&L: bars from a zero baseline, gains green, losses red. */
function MonthlyChart({ rows }: { rows: Performance["monthly"] }) {
  if (!rows.length) return <Empty>No closed trades in range.</Empty>;
  const W = 760, H = 200, L = 70, R = 16, T = 18, B = 28;
  const vals = rows.map((r) => r.net_pnl);
  // 20% headroom on each side so value labels never reach the axis labels.
  const lo0 = Math.min(0, ...vals), hi0 = Math.max(0, ...vals), pad = (hi0 - lo0 || 1) * 0.2;
  const lo = lo0 < 0 ? lo0 - pad : 0, hi = hi0 + pad;
  const y = (v: number) => T + ((hi - v) / (hi - lo || 1)) * (H - T - B);
  const band = (W - L - R) / rows.length;
  const bw = Math.min(40, band * 0.7);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[900px]" role="img" aria-label="Monthly realized net P&L">
      <line x1={L} x2={W - R} y1={y(0)} y2={y(0)} stroke="var(--color-muted)" />
      {rows.map((r, i) => {
        const cx = L + band * i + band / 2;
        const top = Math.min(y(r.net_pnl), y(0));
        const h = Math.max(1, Math.abs(y(r.net_pnl) - y(0)));
        return (
          <g key={r.month}>
            <rect x={cx - bw / 2} y={top} width={bw} height={h} rx="2" fill={r.net_pnl >= 0 ? "var(--color-bull)" : "var(--color-bear)"} fillOpacity="0.75">
              <title>{`${r.month}: ${money(r.net_pnl)}`}</title>
            </rect>
            <text x={cx} y={r.net_pnl >= 0 ? top - 4 : top + h + 11} textAnchor="middle" fontSize="10" fill="var(--color-ink)">{money(Math.round(r.net_pnl))}</text>
            <text x={cx} y={H - 8} textAnchor="middle" fontSize="10" fill="var(--color-faint)">{r.month}</text>
          </g>
        );
      })}
    </svg>
  );
}

function GroupTable({ title, rows }: { title: string; rows: PerfGroup[] }) {
  return (
    <Panel title={title} bodyClassName="overflow-x-auto">
      {!rows.length ? (
        <Empty>No closed trades.</Empty>
      ) : (
        <table className="w-full text-[11px]">
          <thead className="text-left text-[10px] uppercase tracking-wider text-faint">
            <tr>{["", "Trades", "Net P&L", "Win rate", "Avg R", "Profit factor"].map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
          </thead>
          <tbody className="divide-y divide-line tabular-nums">
            {rows.map((g) => (
              <tr key={g.key} className={g.anecdotal ? "text-muted" : ""} title={g.anecdotal ? "Fewer than 5 closed trades: anecdotal" : undefined}>
                <td className="px-2 py-1 font-sans font-semibold">{g.key}</td>
                <td className="px-2 py-1">{g.trades}{g.anecdotal ? "*" : ""}</td>
                <td className={`px-2 py-1 ${g.net_pnl >= 0 ? "text-bull" : "text-bear"}`}>{money(g.net_pnl)}</td>
                <td className="px-2 py-1">{g.win_rate == null ? "—" : `${g.win_rate.toFixed(1)}%`}</td>
                <td className="px-2 py-1">{g.avg_r == null ? "—" : `${g.avg_r.toFixed(2)}R`}</td>
                <td className="px-2 py-1">{g.profit_factor ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

/** Trade performance from the Trade Log: one account at a time, closed trades only. */
export default function PerformanceView() {
  const [account, setAccount] = useState<JournalAccount>("actual");
  const [f, setF] = useState({ ticker: "", strategy: "", mode: "", asset_type: "", start: "", end: "" });
  const [res, setRes] = useState<{ key: string; data: Performance | null; error: string | null } | null>(null);
  const key = JSON.stringify({ account, ...f });
  useEffect(() => {
    let alive = true;
    const params = JSON.parse(key) as Record<string, string>;
    api
      .performance(Object.fromEntries(Object.entries(params).map(([k, v]) => [k, v || undefined])))
      .then((data) => alive && setRes({ key, data, error: null }))
      .catch((e) => alive && setRes({ key, data: null, error: e instanceof ApiError ? e.message : String(e) }));
    return () => {
      alive = false;
    };
  }, [key]);
  const cur = res?.key === key ? res : null;
  const d = cur?.data;
  const s = d?.summary;
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <main className="space-y-3 p-3">
      <section className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-[11px]">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Performance</span>
        <div className="flex rounded-md bg-panel-2 p-0.5 ring-1 ring-line" role="tablist" aria-label="Performance account">
          {(["actual", "paper"] as const).map((a) => (
            <button key={a} role="tab" aria-selected={account === a} onClick={() => setAccount(a)}
              className={`rounded px-3 py-1 text-xs font-semibold ${account === a ? "bg-ink text-bg" : "text-muted hover:text-ink"}`}>
              {a === "actual" ? "Actual trades" : "Paper trades"}
            </button>
          ))}
        </div>
        <input aria-label="Filter ticker" placeholder="Ticker" value={f.ticker} onChange={set("ticker")} className={`${inp} w-20 uppercase`} />
        <input aria-label="Filter strategy" placeholder="Strategy" value={f.strategy} onChange={set("strategy")} className={`${inp} w-36`} />
        <select aria-label="Filter mode" value={f.mode} onChange={set("mode")} className={inp}><option value="">Day + swing</option><option value="day">Day trading</option><option value="swing">Swing trading</option></select>
        <select aria-label="Filter instrument" value={f.asset_type} onChange={set("asset_type")} className={inp}><option value="">Stocks + options</option><option value="stock">Stocks</option><option value="option">Options</option></select>
        <label className="flex items-center gap-1 text-muted">From <input aria-label="From date" type="date" value={f.start} onChange={set("start")} className={inp} /></label>
        <label className="flex items-center gap-1 text-muted">to <input aria-label="To date" type="date" value={f.end} onChange={set("end")} className={inp} /></label>
        <span className="ml-auto text-[10px] text-faint">{d?.note}</span>
      </section>
      {cur?.error && <DataUnavailable compact message={cur.error} />}
      {s && (
        <section className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
          <Tile label="Logged trades" value={`${s.total_trades}`} sub={`${s.closed_trades} closed · ${d!.open_trades} open`} />
          <Tile label="Realized net P&L" value={money(s.net_pnl)} sub={`gross ${money(s.gross_pnl)} · fees ${money(s.fees)}`} tone={s.net_pnl > 0 ? "text-bull" : s.net_pnl < 0 ? "text-bear" : undefined} />
          <Tile label="Win rate" value={s.win_rate == null ? "—" : `${s.win_rate.toFixed(1)}%`} sub={`${s.breakeven} breakeven · ${s.sample_note}`} />
          <Tile label="Average win" value={money(s.avg_win)} />
          <Tile label="Average loss" value={money(s.avg_loss)} />
          <Tile label="Average R" value={s.avg_r == null ? "—" : `${s.avg_r.toFixed(2)}R`} sub={`${s.r_trades} with a planned stop`} />
          <Tile label="Profit factor" value={s.profit_factor == null ? "—" : `${s.profit_factor}`} />
          <Tile label="Max drawdown" value={money(-s.max_drawdown)} sub="peak-to-trough of realized P&L" />
        </section>
      )}
      {d && (
        <>
          <div className="grid gap-3 xl:grid-cols-2">
            <Panel title="Cumulative realized net P&L" bodyClassName="p-2"><CumulativeChart pts={d.cumulative} /></Panel>
            <Panel title="Monthly realized net P&L" bodyClassName="p-2"><MonthlyChart rows={d.monthly} /></Panel>
          </div>
          <p className="text-[11px] text-muted">
            Best strategy: {d.best_strategy ? `${d.best_strategy.key} (${money(d.best_strategy.net_pnl)}, ${d.best_strategy.trades} trades)` : "—"} · Worst strategy:{" "}
            {d.worst_strategy ? `${d.worst_strategy.key} (${money(d.worst_strategy.net_pnl)}, ${d.worst_strategy.trades} trades)` : "—"}
            {d.strategy_note && <span className="text-faint"> · {d.strategy_note}</span>}
          </p>
          <div className="grid gap-3 xl:grid-cols-2">
            <GroupTable title="By strategy" rows={d.by_strategy} />
            <GroupTable title="Day vs swing trading" rows={d.by_mode} />
            <GroupTable title="By ticker" rows={d.by_ticker} />
            <GroupTable title="Stocks vs options" rows={d.by_asset_type} />
          </div>
          <p className="text-center text-[10px] text-faint">
            Computed from your recorded fills. Actual and paper trades are never combined; saved setups, detected setups and backtests are excluded.
            * fewer than 5 trades. Past results do not predict future results.
          </p>
        </>
      )}
    </main>
  );
}
