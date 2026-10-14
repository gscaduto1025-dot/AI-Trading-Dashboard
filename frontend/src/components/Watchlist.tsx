"use client";

import { fmtPct, fmtPrice } from "@/lib/format";
import type { Quote } from "@/lib/types";
import { DemoTag, Empty, Panel, Skeleton } from "./ui";

export default function Watchlist({
  quotes,
  selected,
  onSelect,
  isDemo,
  error,
}: {
  quotes: Quote[] | null;
  selected: string;
  onSelect: (s: string) => void;
  isDemo: boolean;
  error: string | null;
}) {
  return (
    <Panel title="Watchlist" right={isDemo ? <DemoTag /> : null} bodyClassName="overflow-y-auto">
      {error && <Empty>{error}</Empty>}
      {!quotes && !error && (
        <div className="space-y-2 p-3">
          {Array.from({ length: 9 }).map((_, i) => (
            <Skeleton key={i} className="h-9" />
          ))}
        </div>
      )}
      <ul className="flex gap-1 overflow-x-auto p-1.5 lg:block lg:space-y-0.5 lg:overflow-visible">
        {quotes?.map((q) => {
          const up = q.change_pct >= 0;
          const active = q.symbol === selected;
          return (
            <li key={q.symbol} className="shrink-0">
              <button
                onClick={() => onSelect(q.symbol)}
                className={`group flex w-full min-w-[150px] items-center justify-between gap-3 rounded-md px-2.5 py-2 text-left transition-colors lg:min-w-0 ${
                  active ? "bg-accent/10 ring-1 ring-accent/40" : "hover:bg-white/[0.04]"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={`h-6 w-0.5 rounded-full ${up ? "bg-bull" : "bg-bear"} ${active ? "" : "opacity-60"}`}
                  />
                  <span className="text-sm font-semibold tracking-wide">{q.symbol}</span>
                </span>
                <span className="text-right">
                  <span className="num block text-sm">{fmtPrice(q.price)}</span>
                  <span className={`num block text-[11px] ${up ? "text-bull" : "text-bear"}`}>
                    {fmtPct(q.change_pct)}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
