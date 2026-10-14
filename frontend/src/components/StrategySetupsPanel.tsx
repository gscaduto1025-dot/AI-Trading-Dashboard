"use client";

import { useState } from "react";
import { STATUS_LABEL, fmtPrice, statusClasses } from "@/lib/format";
import type { StrategyEvaluation, StrategySetup } from "@/lib/types";
import { DataUnavailable, Empty, Panel } from "./ui";

const SHORT_NAME: Record<string, string> = {
  breakout_retest: "Breakout & retest",
  opening_range_breakout: "ORB",
  liquidity_sweep_reversal: "Liquidity sweep",
  trend_pullback: "Trend pullback",
  sr_rejection: "S/R rejection",
};
const TF: Record<string, string> = { "5Min": "5m", "15Min": "15m" };

const etTime = (t: number | null) =>
  t ? new Date(t * 1000).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }) : "";

/** Day-trading setups from the strategy engine. Rows are clickable. */
export default function StrategySetupsPanel({
  data,
  error,
  selectedId,
  onSelect,
}: {
  data: StrategyEvaluation | null;
  error: string | null;
  selectedId: string | null;
  onSelect: (s: StrategySetup) => void;
}) {
  const [showNoTrade, setShowNoTrade] = useState(false);
  if (error) {
    return (
      <Panel title="Day-Trading Setups">
        <DataUnavailable compact message={error} />
      </Panel>
    );
  }
  const rows = (data?.setups ?? []).filter((s) => showNoTrade || s.status !== "NO_TRADE");
  const unavailable = Object.entries(data?.symbols ?? {}).filter(([, v]) => v.status !== "ok");
  return (
    <Panel
      title="Day-Trading Setups"
      right={
        <label className="flex items-center gap-1 text-[10px] text-muted" title="Show every strategy/direction that is not a trade">
          <input type="checkbox" checked={showNoTrade} onChange={(e) => setShowNoTrade(e.target.checked)} className="accent-[#4f9cf9]" />
          no-trade
        </label>
      }
      bodyClassName="overflow-y-auto"
    >
      {unavailable.map(([sym, v]) => (
        <p key={sym} className="mx-3 mt-2 rounded bg-bear/10 px-2 py-1 text-[10px] text-bear">
          {sym}: MARKET DATA UNAVAILABLE — {v.error}
        </p>
      ))}
      {!data ? (
        <Empty>Evaluating strategies…</Empty>
      ) : rows.length === 0 ? (
        <Empty>No triggered or developing setups in {data.universe.join(", ")}. NO_TRADE is a valid outcome.</Empty>
      ) : (
        <ul className="divide-y divide-line">
          {rows.map((s) => {
            const dir = s.direction === "LONG" ? "long" : "short";
            const active = s.id === selectedId;
            return (
              <li key={`${s.id}-${s.status}`}>
                <button
                  onClick={() => onSelect(s)}
                  className={`w-full px-3 py-2 text-left transition ${active ? "bg-accent/10 ring-1 ring-inset ring-accent/40" : "hover:bg-white/[0.03]"}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5">
                      <span className="text-xs font-bold">{s.ticker}</span>
                      <span className={`rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${statusClasses(s.status, dir)}`}>
                        {STATUS_LABEL[s.status]}
                      </span>
                      <span className={`text-[10px] font-bold ${s.direction === "LONG" ? "text-bull" : "text-bear"}`}>
                        {s.direction === "LONG" ? "▲" : "▼"} {s.direction}
                      </span>
                      {s.is_demo && <span className="text-[9px] font-bold uppercase text-wait">demo</span>}
                    </span>
                    <span className="num text-[10px] text-faint">
                      {TF[s.timeframe] ?? s.timeframe} {s.status === "TRIGGERED" ? `· ${etTime(s.confirmed_at)}` : ""}
                    </span>
                  </div>
                  <p className="mt-0.5 text-[11px] text-ink/85">{SHORT_NAME[s.strategy] ?? s.strategy_name}</p>
                  {s.status === "TRIGGERED" ? (
                    <p className="num mt-0.5 text-[10px] text-faint">
                      E {fmtPrice(s.entry)} · S {fmtPrice(s.stop)} · T1 {fmtPrice(s.target1)} · {s.risk_reward?.toFixed(2)}R
                      {s.outcome && s.outcome.state !== "open" && <span className="ml-1 text-muted">({s.outcome.state.replaceAll("_", " ")})</span>}
                    </p>
                  ) : (
                    <p className="mt-0.5 line-clamp-2 text-[10px] leading-snug text-faint">{s.reasons[0]}</p>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
