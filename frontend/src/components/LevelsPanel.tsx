"use client";

import { fmtPrice } from "@/lib/format";
import type { Analysis } from "@/lib/types";
import { Empty, Panel } from "./ui";

export default function LevelsPanel({ analysis }: { analysis: Analysis | null }) {
  if (!analysis) return <Panel title="Key Levels"><Empty>—</Empty></Panel>;
  const resistance = analysis.levels.filter((l) => l.kind === "resistance");
  const support = analysis.levels.filter((l) => l.kind === "support");
  const row = (l: (typeof analysis.levels)[number]) => {
    const dist = ((l.price - analysis.price) / analysis.price) * 100;
    const isR = l.kind === "resistance";
    return (
      <li key={`${l.kind}-${l.price}`} className="flex items-center gap-2 py-1 text-xs">
        <span className={`num w-16 font-medium ${isR ? "text-bear" : "text-bull"}`}>{fmtPrice(l.price)}</span>
        <span className="min-w-0 flex-1 truncate text-ink/80">
          {l.source}
          {l.touches > 1 && <span className="text-faint"> · {l.touches} touches</span>}
        </span>
        <span className="h-1 w-10 overflow-hidden rounded-full bg-white/5" title={`Strength ${l.strength}`}>
          <span
            className={`block h-full ${isR ? "bg-bear/70" : "bg-bull/70"}`}
            style={{ width: `${l.strength * 100}%` }}
          />
        </span>
        <span className="num w-14 text-right text-[11px] text-faint">
          {dist > 0 ? "+" : ""}
          {dist.toFixed(2)}%
        </span>
      </li>
    );
  };
  return (
    <Panel title="Key Technical Levels" bodyClassName="overflow-y-auto">
      <div className="p-3">
        <ul>{resistance.map(row)}</ul>
        <div className="my-1.5 flex items-center gap-2 text-[11px]">
          <span className="h-px flex-1 bg-accent/40" />
          <span className="num font-semibold text-accent">Last {fmtPrice(analysis.price)}</span>
          <span className="h-px flex-1 bg-accent/40" />
        </div>
        <ul>{support.map(row)}</ul>
        {analysis.levels.length === 0 && <Empty>No levels detected.</Empty>}
      </div>
    </Panel>
  );
}
