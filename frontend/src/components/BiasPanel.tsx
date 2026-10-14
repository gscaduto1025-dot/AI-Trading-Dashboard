"use client";

import { fmtPrice } from "@/lib/format";
import type { Analysis } from "@/lib/types";
import { Empty, Panel } from "./ui";

export default function BiasPanel({ analysis }: { analysis: Analysis | null }) {
  if (!analysis) return <Panel title="Market Bias"><Empty>—</Empty></Panel>;
  const b = analysis.bias;
  const color = b.label === "Bullish" ? "text-bull" : b.label === "Bearish" ? "text-bear" : "text-wait";
  // Position of the score on a -max..+max gauge.
  const pos = ((b.score + b.max_score) / (2 * b.max_score)) * 100;
  const ind = analysis.indicators;
  return (
    <Panel title="Market Bias" bodyClassName="overflow-y-auto">
      <div className="space-y-3 p-3">
        <div className="flex items-end justify-between">
          <span className={`text-2xl font-bold ${color}`}>{b.label}</span>
          <span className="num text-xs text-muted">
            score {b.score > 0 ? "+" : ""}{b.score} / {b.max_score}
          </span>
        </div>
        <div className="relative h-1.5 rounded-full bg-gradient-to-r from-bear/70 via-white/10 to-bull/70">
          <span
            className="absolute -top-1 h-3.5 w-1 -translate-x-1/2 rounded bg-ink shadow"
            style={{ left: `${pos}%` }}
          />
        </div>
        <ul className="space-y-1">
          {b.factors.map((f) => (
            <li key={f.label} className="flex items-center justify-between gap-2 text-xs">
              <span className="shrink-0 text-ink/85">{f.label}</span>
              <span
                className={`truncate text-right text-[11px] ${
                  f.signal === "bullish" ? "text-bull" : f.signal === "bearish" ? "text-bear" : "text-muted"
                }`}
                title={f.detail}
              >
                {f.detail}
              </span>
            </li>
          ))}
        </ul>
        <div className="grid grid-cols-3 gap-2 border-t border-line pt-2 text-center">
          <div>
            <div className="text-[10px] uppercase text-faint">RSI 14</div>
            <div className="num text-sm">{ind.rsi.toFixed(1)}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-faint">ATR 14</div>
            <div className="num text-sm">{fmtPrice(ind.atr)}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-faint">Rel. vol</div>
            <div className="num text-sm">{ind.volume_ratio.toFixed(2)}x</div>
          </div>
        </div>
      </div>
    </Panel>
  );
}
