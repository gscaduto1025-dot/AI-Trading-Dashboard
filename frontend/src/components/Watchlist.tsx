"use client";

import { fmtPct, fmtPrice, fmtVolume } from "@/lib/format";
import type { Quote } from "@/lib/types";
import { DataUnavailable, DemoTag, Panel, Skeleton } from "./ui";

export default function Watchlist({
  quotes,
  selected,
  onSelect,
  isDemo,
  error,
  symbols,
}: {
  quotes: Quote[] | null;
  selected: string;
  onSelect: (s: string) => void;
  isDemo: boolean;
  error: string | null;
  symbols: string[];
}) {
  // When quotes are unavailable, still list the tickers so the user can switch charts.
  const rows: Quote[] =
    quotes ??
    symbols.map((s) => ({
      symbol: s,
      available: false,
      price: null,
      prev_close: null,
      change: null,
      change_pct: null,
      volume: null,
      price_time: null,
      session_date: "",
      stale: false,
      reason: null,
      latency: "unavailable",
    }));
  return (
    <Panel title="Watchlist" right={isDemo ? <DemoTag /> : null} bodyClassName="overflow-y-auto">
      {error && <DataUnavailable compact message={error} />}
      {!quotes && !error && symbols.length === 0 && (
        <div className="space-y-2 p-3">
          {Array.from({ length: 9 }).map((_, i) => (
            <Skeleton key={i} className="h-9" />
          ))}
        </div>
      )}
      <ul className="flex gap-1 overflow-x-auto p-1.5 lg:block lg:space-y-0.5 lg:overflow-visible">
        {rows.map((q) => {
          const up = (q.change_pct ?? 0) >= 0;
          const active = q.symbol === selected;
          return (
            <li key={q.symbol} className="shrink-0">
              <button
                onClick={() => onSelect(q.symbol)}
                title={q.reason ?? undefined}
                className={`group flex w-full min-w-[150px] items-center justify-between gap-3 rounded-md px-2.5 py-2 text-left transition-colors lg:min-w-0 ${
                  active ? "bg-accent/10 ring-1 ring-accent/40" : "hover:bg-white/[0.04]"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={`h-6 w-0.5 rounded-full ${q.available ? (up ? "bg-bull" : "bg-bear") : "bg-faint"} ${active ? "" : "opacity-60"}`}
                  />
                  <span>
                    <span className="block text-sm font-semibold tracking-wide">{q.symbol}</span>
                    {q.available && (
                      <span className="num block text-[10px] text-faint">Vol {fmtVolume(q.volume)}</span>
                    )}
                  </span>
                </span>
                {q.available ? (
                  <span className="text-right">
                    <span className="num block text-sm">{fmtPrice(q.price)}</span>
                    <span className={`num block text-[11px] ${up ? "text-bull" : "text-bear"}`}>
                      {fmtPct(q.change_pct)}
                      {q.stale && <span className="ml-1 text-bear">stale</span>}
                    </span>
                  </span>
                ) : (
                  <span className="text-right text-[9px] font-bold uppercase leading-tight text-bear">
                    Data
                    <br />
                    unavailable
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
