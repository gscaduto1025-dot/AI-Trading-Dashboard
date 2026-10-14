"use client";

import { useState } from "react";
import { STATUS_LABEL, fmtPrice, statusClasses } from "@/lib/format";
import type { SwingEvaluation, SwingSetup, SwingSortKey } from "@/lib/types";
import { DataUnavailable, Empty, Panel } from "./ui";

export const SWING_SHORT_NAME: Record<string, string> = {
  swing_breakout: "Daily breakout",
  swing_trend_continuation: "Trend continuation",
  swing_consolidation_breakout: "Consolidation BO",
  swing_sr_reversal: "S/R reversal",
  swing_trend_reversal: "Trend reversal",
};
const TF: Record<string, string> = { "1Day": "D", "1Week": "W" };
const SORTS: { key: SwingSortKey; label: string }[] = [
  { key: "quality", label: "Setup quality" },
  { key: "ticker", label: "Ticker" },
  { key: "strategy", label: "Strategy" },
  { key: "direction", label: "Direction" },
  { key: "risk_reward", label: "Risk/reward" },
  { key: "distance", label: "Distance to trigger" },
];

const distText = (s: SwingSetup) => {
  const d = s.distance_to_trigger_pct;
  if (d === null) return "—";
  if (d <= 0) return `beyond ${Math.abs(d).toFixed(1)}%`;
  return `${d.toFixed(1)}%`;
};

/** Swing-trading scanner: every swing strategy on daily/weekly candles, sortable. */
export default function SwingScannerPanel({
  data,
  error,
  sort,
  onSort,
  selectedId,
  onSelect,
}: {
  data: SwingEvaluation | null;
  error: string | null;
  sort: SwingSortKey;
  onSort: (k: SwingSortKey) => void;
  selectedId: string | null;
  onSelect: (s: SwingSetup) => void;
}) {
  const [showNoTrade, setShowNoTrade] = useState(false);
  if (error) {
    return (
      <Panel title="Swing Trading Scanner">
        <DataUnavailable compact message={error} />
      </Panel>
    );
  }
  const rows = (data?.setups ?? []).filter((s) => showNoTrade || s.status !== "NO_TRADE");
  const unavailable = Object.entries(data?.symbols ?? {}).filter(([, v]) => v.status !== "ok");
  const stale = data && data.sort !== sort;
  return (
    <Panel
      title="Swing Trading Scanner"
      right={
        <span className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-[10px] text-muted">
            sort
            <select
              aria-label="Sort swing setups"
              value={sort}
              onChange={(e) => onSort(e.target.value as SwingSortKey)}
              className="rounded bg-panel-2 px-1 py-0.5 text-[10px] text-ink ring-1 ring-line-strong"
            >
              {SORTS.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-[10px] text-muted" title="Show every strategy/direction that is not a trade">
            <input type="checkbox" checked={showNoTrade} onChange={(e) => setShowNoTrade(e.target.checked)} className="accent-[#4f9cf9]" />
            no-trade
          </label>
        </span>
      }
      bodyClassName="overflow-y-auto"
    >
      {unavailable.map(([sym, v]) => (
        <p key={sym} className="mx-3 mt-2 rounded bg-bear/10 px-2 py-1 text-[10px] text-bear">
          {sym}: MARKET DATA UNAVAILABLE — {v.error}
        </p>
      ))}
      {!data ? (
        <Empty>Scanning daily and weekly candles…</Empty>
      ) : rows.length === 0 ? (
        <Empty>No triggered or developing swing setups in {data.universe.join(", ")}. NO_TRADE is a valid outcome.</Empty>
      ) : (
        <table className={`w-full text-left text-[11px] ${stale ? "opacity-60" : ""}`}>
          <thead className="sticky top-0 bg-panel text-[9px] uppercase tracking-wider text-faint">
            <tr>
              <th className="px-3 py-1.5 font-medium">Ticker</th>
              <th className="px-1 py-1.5 font-medium">Strategy</th>
              <th className="px-1 py-1.5 font-medium">Status</th>
              <th className="px-1 py-1.5 text-right font-medium" title="Rule-based ranking 0-100, not a probability">
                Quality
              </th>
              <th className="px-1 py-1.5 text-right font-medium">Trigger</th>
              <th className="px-1 py-1.5 text-right font-medium">R:R</th>
              <th className="px-3 py-1.5 text-right font-medium" title="Move still needed to reach the entry trigger">
                Distance
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((s) => {
              const dir = s.direction === "LONG" ? "long" : "short";
              const active = s.id === selectedId;
              return (
                <tr
                  key={`${s.id}-${s.status}`}
                  onClick={() => onSelect(s)}
                  className={`cursor-pointer transition ${active ? "bg-accent/10 outline outline-1 -outline-offset-1 outline-accent/40" : "hover:bg-white/[0.03]"}`}
                >
                  <td className="px-3 py-1.5">
                    <button
                      className="flex items-center gap-1 text-left font-bold"
                      onClick={(e) => {
                        e.stopPropagation();
                        onSelect(s);
                      }}
                    >
                      {s.ticker}
                      <span className={`text-[10px] ${s.direction === "LONG" ? "text-bull" : "text-bear"}`}>
                        {s.direction === "LONG" ? "▲" : "▼"}
                      </span>
                      {s.is_demo && <span className="text-[8px] font-bold uppercase text-wait">demo</span>}
                    </button>
                  </td>
                  <td className="px-1 py-1.5 text-ink/85">
                    {SWING_SHORT_NAME[s.strategy] ?? s.strategy_name}
                    <span className="ml-1 text-[9px] text-faint">{TF[s.timeframe] ?? s.timeframe}</span>
                  </td>
                  <td className="px-1 py-1.5">
                    <span className={`rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${statusClasses(s.status, dir)}`}>
                      {STATUS_LABEL[s.status]}
                    </span>
                  </td>
                  <td className="num px-1 py-1.5 text-right">{s.status === "NO_TRADE" ? "—" : s.quality.score.toFixed(0)}</td>
                  <td className="num px-1 py-1.5 text-right">{fmtPrice(s.entry_trigger)}</td>
                  <td className="num px-1 py-1.5 text-right">{s.risk_reward !== null ? s.risk_reward.toFixed(2) : "—"}</td>
                  <td className="num px-3 py-1.5 text-right text-faint">{distText(s)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
