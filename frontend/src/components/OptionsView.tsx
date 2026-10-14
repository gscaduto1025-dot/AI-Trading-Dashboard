"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { fmtIsoNy, fmtPrice, fmtTime } from "@/lib/format";
import type { Mode, OptionContractRow, OptionEstimate, OptionsAnalysis, TradePlan } from "@/lib/types";
import { DataUnavailable, Empty, Panel, Skeleton } from "./ui";

const n = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));
const usd = (v: number | null | undefined, d = 0) =>
  v == null ? "—" : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d })}`;
const pct = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v.toFixed(d)}%`);
const int = (v: number | null | undefined) => (v == null ? "—" : Math.round(v).toLocaleString());
const scen = (e: OptionEstimate | null | undefined) =>
  !e ? "—" : `${usd(e.pnl_from_ask_per_contract)} (move ${e.move > 0 ? "+" : ""}${e.move.toFixed(2)}${e.reliability === "low" ? ", low reliability" : ""})`;

const STATUS_CLS: Record<string, string> = {
  TRIGGERED: "text-bull",
  WATCHING: "text-wait",
  NO_TRADE: "text-muted",
};

function Flags({ r, max = 3 }: { r: OptionContractRow; max?: number }) {
  const sorted = [...r.checks].sort((a, b) => (a.severity === "block" ? -1 : 1) - (b.severity === "block" ? -1 : 1));
  const shown = sorted.slice(0, max);
  return (
    <span className="flex flex-wrap gap-1">
      {r.eligible && (
        <span className="rounded bg-bull/15 px-1 text-[9px] font-semibold text-bull ring-1 ring-bull/30">passes</span>
      )}
      {shown.map((c) => (
        <span
          key={c.id}
          title={c.detail}
          className={`rounded px-1 text-[9px] font-semibold ring-1 ${
            c.severity === "block" ? "bg-bear/10 text-bear ring-bear/30" : "bg-wait/10 text-wait ring-wait/30"
          }`}
        >
          {c.label}
        </span>
      ))}
      {sorted.length > max && <span className="text-[9px] text-faint">+{sorted.length - max}</span>}
    </span>
  );
}

function KV({ k, v, hint }: { k: string; v: React.ReactNode; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5" title={hint}>
      <span className="text-faint">{k}</span>
      <span className="num text-right text-ink/90">{v}</span>
    </div>
  );
}

function ContractDetail({ r }: { r: OptionContractRow }) {
  const s = r.sensitivity;
  return (
    <div className="grid gap-3 bg-white/[0.02] px-3 py-2 text-[11px] md:grid-cols-3">
      <div>
        <p className="mb-1 font-semibold text-muted">Quote & activity (Alpaca)</p>
        <KV k="Bid × size" v={`${n(r.bid)} × ${int(r.bid_size)}`} />
        <KV k="Ask × size" v={`${n(r.ask)} × ${int(r.ask_size)}`} />
        <KV k="Mid / spread" v={`${n(r.mid, 3)} / ${n(r.spread, 3)} (${pct(r.spread_pct)})`} />
        <KV k="Quote time" v={r.quote_time ? fmtTime(r.quote_time) : "—"} />
        <KV k="Last trade" v={r.last != null ? `${n(r.last)} at ${fmtTime(r.last_time)}` : "—"} />
        <KV k="Volume" v={`${int(r.volume)}${r.volume_session ? ` (${r.volume_session})` : ""}`} />
        <KV k="Open interest" v={`${int(r.open_interest)}${r.open_interest_date ? ` (${r.open_interest_date})` : ""}`} />
        <KV k="Style / multiplier" v={`${r.style ?? "—"} / ${r.multiplier}`} />
      </div>
      <div>
        <p className="mb-1 font-semibold text-muted">Greeks & sensitivity</p>
        <KV k="Implied volatility" v={r.iv == null ? "—" : pct(r.iv * 100)} />
        <KV k="Delta" v={n(r.delta, 3)} hint="Approx. option price change per 1 USD stock move" />
        <KV k="Gamma" v={n(r.gamma, 4)} hint="Change in delta per 1 USD stock move" />
        <KV k="Theta" v={n(r.theta, 3)} hint="Value lost per day, other things equal" />
        <KV k="Vega" v={n(r.vega, 3)} hint="Change for a 1-point IV move" />
        <KV k="Per 1 USD stock move" v={usd(s.per_1_usd_move, 2) + " /contract"} />
        <KV k="Time decay" v={`${usd(s.theta_per_day_per_contract, 2)}/day (${pct(s.theta_pct_of_mid_per_day)} of mid)`} />
        <KV k="Per IV point" v={usd(s.vega_per_vol_point_per_contract, 2) + " /contract"} />
      </div>
      <div>
        <p className="mb-1 font-semibold text-muted">Option-level risk (per contract)</p>
        <KV k="Premium at ask = max loss" v={usd(r.premium_per_contract)} />
        <KV k="Spread cost (round trip)" v={usd(r.spread_cost_per_contract)} />
        <KV k="Breakeven at expiration" v={fmtPrice(r.breakeven)} />
        <KV k="Intrinsic / extrinsic" v={`${n(r.intrinsic)} / ${n(r.extrinsic)}`} />
        <KV
          k="Est. for the plan's reward move"
          v={scen(s.at_stock_target1)}
          hint="Stock moves entry→T1 distance from now; IV and time unchanged; delta capped at ±1. Not a price."
        />
        <KV
          k="Est. for the plan's risk move"
          v={scen(s.at_stock_stop)}
          hint="Stock moves entry→stop distance from now. Not a loss limit: the option can lose more, up to the full premium."
        />
        <KV k="Est. option reward/risk" v={s.estimated_option_rr == null ? "—" : `${s.estimated_option_rr}:1`} />
        {r.score && <KV k="Comparison score" v={`${r.score.total}/100`} hint={r.score.note} />}
      </div>
      <div className="md:col-span-3">
        <p className="mb-1 font-semibold text-muted">Checks</p>
        {r.checks.length === 0 ? (
          <p className="text-bull">Every quote-quality, liquidity and Greek check passed.</p>
        ) : (
          <ul className="space-y-0.5">
            {r.checks.map((c) => (
              <li key={c.id} className="flex gap-2">
                <span className={c.severity === "block" ? "text-bear" : "text-wait"}>
                  {c.severity === "block" ? "✗" : "!"}
                </span>
                <span>
                  <span className="font-medium">{c.label}:</span> <span className="text-ink/75">{c.detail}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Comparison({ rows }: { rows: OptionContractRow[] }) {
  if (!rows.length) return <Empty>No contract passed every check, so there is nothing to compare.</Empty>;
  const line: [string, (r: OptionContractRow) => React.ReactNode][] = [
    ["Expiration", (r) => `${r.expiration} (${r.dte}d)`],
    ["Strike", (r) => `${fmtPrice(r.strike)} ${r.moneyness ?? ""}`],
    ["Bid / ask", (r) => `${n(r.bid)} / ${n(r.ask)}`],
    ["Spread", (r) => pct(r.spread_pct)],
    ["Volume / OI", (r) => `${int(r.volume)} / ${int(r.open_interest)}`],
    ["IV", (r) => (r.iv == null ? "—" : pct(r.iv * 100))],
    ["Delta / gamma", (r) => `${n(r.delta, 2)} / ${n(r.gamma, 3)}`],
    ["Theta / day", (r) => `${usd(r.sensitivity.theta_per_day_per_contract, 2)} (${pct(r.sensitivity.theta_pct_of_mid_per_day)})`],
    ["Vega / IV pt", (r) => usd(r.sensitivity.vega_per_vol_point_per_contract, 2)],
    ["Premium (max loss)", (r) => usd(r.premium_per_contract)],
    ["Breakeven", (r) => fmtPrice(r.breakeven)],
    ["Est. reward move", (r) => scen(r.sensitivity.at_stock_target1)],
    ["Est. risk move", (r) => scen(r.sensitivity.at_stock_stop)],
    ["Score", (r) => (r.score ? `${r.score.total}` : "—")],
  ];
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-faint">
            <th className="px-2 py-1 font-medium">Contract</th>
            {rows.map((r, i) => (
              <th key={r.symbol} className="num px-2 py-1 font-semibold text-ink">
                {i === 0 && <span className="mr-1 text-accent">#1</span>}
                {r.symbol}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {line.map(([k, f]) => (
            <tr key={k}>
              <td className="px-2 py-1 text-faint">{k}</td>
              {rows.map((r) => (
                <td key={r.symbol} className="num px-2 py-1 text-ink/90">
                  {f(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Notes({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <p className="mb-1 text-[11px] font-semibold text-muted">{title}</p>
      <ul className="list-disc space-y-0.5 pl-4 text-[11px] leading-snug text-ink/80">
        {items.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
    </div>
  );
}

export default function OptionsView({
  symbol,
  mode,
  setupId,
  symbols,
  onSymbol,
  onSetup,
  onOpenChart,
}: {
  symbol: string;
  mode: Mode;
  setupId: string | null;
  symbols: string[];
  onSymbol: (s: string) => void;
  onSetup: (id: string | null) => void;
  onOpenChart: () => void;
}) {
  const [plans, setPlans] = useState<TradePlan[] | null>(null);
  const [expiration, setExpiration] = useState<string | null>(null);
  const [state, setState] = useState<{ key: string; data: OptionsAnalysis | null; error: string | null } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [onlyPassing, setOnlyPassing] = useState(false);
  const [tick, setTick] = useState(0);
  const key = `${symbol}|${mode}|${setupId ?? ""}|${expiration ?? ""}|${tick}`;

  useEffect(() => {
    let live = true;
    api
      .plans(symbol, mode)
      .then((r) => live && setPlans(r.plans))
      .catch(() => live && setPlans([]));
    return () => {
      live = false;
    };
  }, [symbol, mode]);

  useEffect(() => {
    let live = true;
    api
      .options(symbol, mode, setupId, expiration)
      .then((d) => live && setState({ key, data: d, error: null }))
      .catch((e: ApiError) => live && setState({ key, data: null, error: e.message }));
    return () => {
      live = false;
    };
  }, [symbol, mode, setupId, expiration, key]);

  const loading = !state || state.key !== key;
  const d = state?.data;
  const rows = (d?.contracts ?? []).filter((r) => !onlyPassing || r.eligible);
  const rec = d?.recommendation;
  const sr = d?.stock_risk;
  const orisk = d?.option_risk;
  const activePlans = (plans ?? []).filter((p) => p.entry_trigger);

  return (
    <main className="space-y-3 p-3">
      <section className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-xs">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Options</span>
        <select
          aria-label="Ticker"
          value={symbol}
          onChange={(e) => {
            setExpiration(null);
            onSymbol(e.target.value);
          }}
          className="rounded bg-white/5 px-2 py-1 ring-1 ring-line-strong"
        >
          {symbols.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select
          aria-label="Stock setup"
          value={setupId ?? ""}
          onChange={(e) => {
            setExpiration(null);
            onSetup(e.target.value || null);
          }}
          className="max-w-[340px] rounded bg-white/5 px-2 py-1 ring-1 ring-line-strong"
        >
          <option value="">Best {mode} setup (auto)</option>
          {activePlans.map((p) => (
            <option key={p.setup_id} value={p.setup_id}>
              {p.status} · {p.strategy_name} {p.direction} ({p.timeframe})
            </option>
          ))}
        </select>
        <select
          aria-label="Expiration"
          value={expiration ?? ""}
          onChange={(e) => setExpiration(e.target.value || null)}
          className="rounded bg-white/5 px-2 py-1 ring-1 ring-line-strong"
        >
          <option value="">
            Expirations in {mode} window{d?.dte_window ? ` (${d.dte_window[0]}–${d.dte_window[1]} days)` : ""}
          </option>
          {(d?.expirations ?? []).map((e) => (
            <option key={e.date} value={e.date}>
              {e.date} {e.weekday} · {e.dte}d{e.monthly ? " · monthly" : ""}
              {e.in_window ? "" : " · outside window"}
            </option>
          ))}
        </select>
        <button onClick={() => setTick((t) => t + 1)} className="rounded px-2 py-1 text-muted ring-1 ring-line-strong hover:text-ink">
          Refresh
        </button>
        <button onClick={onOpenChart} className="rounded px-2 py-1 text-accent ring-1 ring-accent/40 hover:bg-accent/10">
          Open chart
        </button>
        <span className="ml-auto text-[10px] text-faint">Analysis only — TradeScope never places option orders.</span>
      </section>

      {loading && !d ? (
        <Skeleton className="h-64" />
      ) : state?.error ? (
        <DataUnavailable title="OPTIONS DATA UNAVAILABLE" message={state.error} />
      ) : d && !d.available ? (
        <section className="space-y-3 rounded-lg border border-line bg-panel p-4 text-xs">
          <DataUnavailable title="OPTIONS DATA UNAVAILABLE" message={d.reason} />
          {d.capabilities && (
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <p className="mb-1 font-semibold text-muted">Alpaca options feeds</p>
                {Object.values(d.capabilities.feeds).map((f) => (
                  <p key={f.id} className="mb-1.5 text-ink/80">
                    <span className="font-semibold">{f.label}:</span> {f.coverage}{" "}
                    <span className="text-faint">{f.requirement}</span>
                  </p>
                ))}
              </div>
              <Notes title="Restrictions" items={d.capabilities.restrictions} />
            </div>
          )}
        </section>
      ) : d ? (
        <>
          <section
            className={`rounded-lg border px-3 py-2 text-[11px] ${
              d.feed?.actual_quotes && d.market_open ? "border-line bg-panel" : "border-wait/40 bg-wait/5"
            }`}
          >
            <p className="font-semibold">
              Feed: {d.feed?.label}
              {!d.feed?.actual_quotes && <span className="ml-2 text-wait">INDICATIVE QUOTES — not actual OPRA prices</span>}
              {d.market_open === false && <span className="ml-2 text-wait">MARKET CLOSED — quotes from the last session</span>}
              <span className="ml-2 font-normal text-faint">as of {fmtIsoNy(d.as_of)}</span>
            </p>
            <details className="mt-1">
              <summary className="cursor-pointer text-muted">Data limitations ({d.data_limitations?.length ?? 0})</summary>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-ink/75">
                {d.data_limitations?.map((x) => <li key={x}>{x}</li>)}
              </ul>
            </details>
          </section>

          <section className="grid gap-3 lg:grid-cols-3">
            <Panel title="Stock-level risk" bodyClassName="p-3 text-[11px]">
              {d.setup && sr ? (
                <>
                  <p className="mb-1 text-xs">
                    <span className="font-bold">{d.symbol}</span> {d.setup.strategy_name}{" "}
                    <span className={d.setup.direction === "LONG" ? "text-bull" : "text-bear"}>{d.setup.direction}</span>{" "}
                    <span className={STATUS_CLS[d.setup.status]}>{d.setup.status}</span>{" "}
                    <span className="text-faint">({d.setup.timeframe})</span>
                  </p>
                  <KV k="Underlying price" v={`${fmtPrice(d.underlying?.price)}${d.underlying?.stale ? " (stale)" : ""}`} />
                  <KV k="Entry trigger / entry" v={`${fmtPrice(sr.entry_trigger)} / ${fmtPrice(sr.entry)}`} />
                  <KV k="Stop-loss" v={fmtPrice(sr.stop)} />
                  <KV k="Targets" v={`${fmtPrice(sr.target1)} / ${fmtPrice(sr.target2)}`} />
                  <KV k="Risk per share" v={n(sr.risk_per_share)} />
                  <KV k="Risk per 100 shares" v={usd(sr.risk_per_100_shares)} />
                  <KV k="Risk/reward (T1)" v={sr.risk_reward_t1 == null ? "—" : `${sr.risk_reward_t1}:1`} />
                  <p className="mt-1 text-faint">{sr.note}</p>
                </>
              ) : (
                <Empty>{rec?.reasons[0] ?? "No stock setup."}</Empty>
              )}
            </Panel>

            <Panel title="Option-level risk" bodyClassName="p-3 text-[11px]">
              {orisk ? (
                <>
                  <p className="num mb-1 text-xs font-semibold">{orisk.contract}</p>
                  <KV k="Premium = maximum loss" v={usd(orisk.max_loss_per_contract)} />
                  <KV k="Spread cost (round trip)" v={usd(orisk.spread_cost_per_contract)} />
                  <KV k="Breakeven at expiration" v={fmtPrice(orisk.breakeven_at_expiration)} />
                  <KV k="Est. for the plan's risk move" v={scen(orisk.estimated_at_stock_stop)} />
                  <KV k="Est. for the plan's reward move" v={scen(orisk.estimated_at_stock_target1)} />
                  <KV k="Time decay" v={`${usd(orisk.theta_per_day_per_contract, 2)} / day`} />
                  <KV k="Days to expiration" v={orisk.dte} />
                  <p className="mt-1 text-wait">{orisk.max_loss_note} The stock stop is not an option stop.</p>
                </>
              ) : (
                <Empty>No contract passed the checks, so no option-level estimate is shown.</Empty>
              )}
            </Panel>

            <Panel title="Candidate for review" bodyClassName="p-3 text-[11px]">
              {rec?.contract ? (
                <>
                  <p className="num text-sm font-bold text-accent">{rec.contract.symbol}</p>
                  <p className="mb-1 text-ink/80">
                    {rec.contract.type.toUpperCase()} {fmtPrice(rec.contract.strike)} · {rec.contract.expiration} (
                    {rec.contract.dte}d) · bid {n(rec.contract.bid)} / ask {n(rec.contract.ask)} · Δ {n(rec.contract.delta)}
                  </p>
                  <p className="text-faint">{rec.label}</p>
                </>
              ) : (
                <>
                  <p className="mb-1 text-xs font-semibold text-wait">No contract recommended</p>
                  <ul className="list-disc space-y-0.5 pl-4 text-ink/80">
                    {rec?.reasons.map((x) => <li key={x}>{x}</li>)}
                  </ul>
                </>
              )}
              {d.type_rationale && <p className="mt-2 text-faint">{d.type_rationale}</p>}
              {d.blocked_by && Object.keys(d.blocked_by).length > 0 && (
                <p className="mt-2 text-faint">
                  Most common blockers:{" "}
                  {Object.entries(d.blocked_by)
                    .slice(0, 4)
                    .map(([k, v]) => `${d.check_labels?.[k] ?? k} (${v})`)
                    .join(", ")}
                </p>
              )}
            </Panel>
          </section>

          <Panel
            title={`Compare candidates (${d.eligible_count ?? 0} of ${d.contracts_total ?? 0} pass every check)`}
            bodyClassName="p-1"
          >
            <Comparison rows={d.candidates ?? []} />
          </Panel>

          <Panel
            title={`${d.option_type === "put" ? "Puts" : "Calls"} near the setup${loading ? " · updating…" : ""}`}
            right={
              <label className="flex items-center gap-1 text-[10px] text-muted">
                <input type="checkbox" checked={onlyPassing} onChange={(e) => setOnlyPassing(e.target.checked)} />
                only passing
              </label>
            }
            bodyClassName="overflow-x-auto"
          >
            {!rows.length ? (
              <Empty>No contracts to show.</Empty>
            ) : (
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-panel text-left text-[10px] uppercase tracking-wider text-faint">
                  <tr>
                    {["Exp", "DTE", "Strike", "Bid", "Ask", "Spread", "Vol", "OI", "IV", "Δ", "Γ", "Θ", "Vega", "Breakeven", "Flags"].map(
                      (h) => (
                        <th key={h} className="px-2 py-1.5 font-medium">
                          {h}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {rows.map((r) => (
                    <FragmentRow key={r.symbol} r={r} open={open === r.symbol} onToggle={() => setOpen(open === r.symbol ? null : r.symbol)} />
                  ))}
                </tbody>
              </table>
            )}
          </Panel>

          <Panel title="Risks of trading options on this setup" bodyClassName="grid gap-3 p-3 md:grid-cols-2">
            <Notes title="The stock stop is not an option loss limit" items={d.risk_notes?.stop_vs_option} />
            <Notes title="Time decay" items={d.risk_notes?.time_decay} />
            <Notes title="Short-dated options" items={d.risk_notes?.short_dated} />
            <Notes title="Implied volatility" items={d.risk_notes?.iv} />
            <Notes title="About the estimates" items={d.risk_notes?.estimates} />
            {d.thresholds && (
              <div className="text-[11px] text-ink/80">
                <p className="mb-1 font-semibold text-muted">Filters for {mode} setups</p>
                <p>
                  Spread ≤ {(d.thresholds.max_spread_pct * 100).toFixed(0)}% of mid · volume ≥ {d.thresholds.min_volume} · open
                  interest ≥ {d.thresholds.min_open_interest} · quote ≤ {d.thresholds.max_quote_age_seconds}s old · |Δ|{" "}
                  {d.thresholds.delta_band[0]}–{d.thresholds.delta_band[1]} · {d.thresholds.dte_window[0]}–
                  {d.thresholds.dte_window[1]} days to expiration · Greeks and IV present.
                </p>
              </div>
            )}
          </Panel>
        </>
      ) : null}
    </main>
  );
}

function FragmentRow({ r, open, onToggle }: { r: OptionContractRow; open: boolean; onToggle: () => void }) {
  return (
    <>
      <tr onClick={onToggle} className={`num cursor-pointer hover:bg-white/[0.03] ${r.eligible ? "" : "text-ink/60"}`}>
        <td className="px-2 py-1">{r.expiration}</td>
        <td className="px-2 py-1">{r.dte}</td>
        <td className="px-2 py-1 font-semibold">
          {fmtPrice(r.strike)} <span className="text-[9px] text-faint">{r.moneyness}</span>
        </td>
        <td className="px-2 py-1">{n(r.bid)}</td>
        <td className="px-2 py-1">{n(r.ask)}</td>
        <td className={`px-2 py-1 ${r.checks.some((c) => c.id === "wide_spread") ? "text-bear" : ""}`}>{pct(r.spread_pct)}</td>
        <td className={`px-2 py-1 ${r.checks.some((c) => c.id === "low_volume") ? "text-bear" : ""}`}>{int(r.volume)}</td>
        <td className={`px-2 py-1 ${r.checks.some((c) => c.id === "low_open_interest") ? "text-bear" : ""}`}>{int(r.open_interest)}</td>
        <td className="px-2 py-1">{r.iv == null ? "—" : pct(r.iv * 100)}</td>
        <td className="px-2 py-1">{n(r.delta)}</td>
        <td className="px-2 py-1">{n(r.gamma, 3)}</td>
        <td className="px-2 py-1">{n(r.theta, 3)}</td>
        <td className="px-2 py-1">{n(r.vega, 3)}</td>
        <td className="px-2 py-1">{fmtPrice(r.breakeven)}</td>
        <td className="px-2 py-1 font-sans">
          <Flags r={r} />
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={15}>
            <ContractDetail r={r} />
          </td>
        </tr>
      )}
    </>
  );
}
