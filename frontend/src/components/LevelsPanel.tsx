"use client";

import { useState } from "react";
import { fmtIsoNy, fmtPrice } from "@/lib/format";
import type { KeyLevel, KeyLevelReport, LevelCategory } from "@/lib/types";
import { LEVEL_STYLE } from "./PriceChart";
import { DataUnavailable, Empty, Panel } from "./ui";

const TF_SHORT: Record<string, string> = {
  "1Min": "1m",
  "5Min": "5m",
  "15Min": "15m",
  "30Min": "30m",
  "1Hour": "1H",
  "4Hour": "4H",
  "1Day": "D",
  "1Week": "W",
};

const FILTERS: { key: LevelCategory | "all"; label: string }[] = [
  { key: "all", label: "All" },
  { key: "zone", label: "Zones" },
  { key: "reference", label: "Prev/Session" },
  { key: "indicator", label: "VWAP/EMA" },
  { key: "swing", label: "Swings" },
  { key: "liquidity", label: "Liquidity" },
  { key: "breakout", label: "Breakouts" },
];

function swatch(l: KeyLevel): string {
  if (l.category === "zone") return l.type === "SUPPORT_ZONE" ? LEVEL_STYLE.support.border : LEVEL_STYLE.resistance.border;
  if (l.category === "liquidity") return LEVEL_STYLE.liquidity.border;
  if (l.type === "PDH" || l.type === "PDL") return LEVEL_STYLE.pd;
  if (l.type === "PWH" || l.type === "PWL") return LEVEL_STYLE.pw;
  if (l.type === "VWAP") return LEVEL_STYLE.vwap;
  if (l.type.startsWith("EMA")) return LEVEL_STYLE[`ema${l.type.slice(3)}` as "ema9"] ?? LEVEL_STYLE.swing;
  if (l.type === "BREAKOUT") return LEVEL_STYLE.breakout;
  if (l.type === "BREAKDOWN") return LEVEL_STYLE.breakdown;
  if (l.category === "reference") return LEVEL_STYLE.session;
  return LEVEL_STYLE.swing;
}

function Row({ l, price, open, onToggle }: { l: KeyLevel; price: number; open: boolean; onToggle: () => void }) {
  const dist = ((l.price - price) / price) * 100;
  const s = l.strength;
  return (
    <li className={`${l.valid ? "" : "opacity-45"}`}>
      <button onClick={onToggle} className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-white/[0.03]">
        <span className="h-3 w-1 shrink-0 rounded-sm" style={{ background: swatch(l) }} />
        <span className="num w-[112px] shrink-0 whitespace-nowrap text-[11px]">
          {l.is_zone ? `${fmtPrice(l.low)}–${fmtPrice(l.high)}` : fmtPrice(l.price)}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-ink/85" title={l.evidence.summary}>
          {l.label}
          {!l.valid && <span className="ml-1 text-[10px] text-bear">invalid</span>}
        </span>
        <span className="num w-7 shrink-0 text-center text-[10px] text-faint" title={l.timeframes.join(", ")}>
          {TF_SHORT[l.timeframes[0]] ?? l.timeframes[0]}
          {l.timeframes.length > 1 ? "+" : ""}
        </span>
        <span className="h-1 w-8 shrink-0 overflow-hidden rounded-full bg-white/5" title={`${s.label} · ${s.score}/100`}>
          <span
            className={`block h-full ${s.label === "strong" ? "bg-accent" : s.label === "moderate" ? "bg-accent/60" : "bg-white/25"}`}
            style={{ width: `${s.score}%` }}
          />
        </span>
        <span className="num w-6 shrink-0 text-right text-[10px] text-muted">{s.score}</span>
        <span className="num w-12 shrink-0 text-right text-[10px] text-faint">
          {dist > 0 ? "+" : ""}
          {dist.toFixed(2)}%
        </span>
      </button>
      {open && (
        <div className="mx-3 mb-2 space-y-1.5 rounded border border-line bg-panel-2 p-2 text-[11px] leading-relaxed">
          <p className="text-ink/85">{l.evidence.summary}</p>
          <p className="text-muted">
            <span className="text-faint">Status:</span> {l.status} · <span className="text-faint">valid:</span>{" "}
            {l.valid ? "yes" : "no"}
          </p>
          <p className="num text-muted">
            Strength {s.score} = reactions {s.components.reactions} + recency {s.components.recency} + timeframe{" "}
            {s.components.timeframe} + confluence {s.components.confluence}
            <span className="text-faint">
              {" "}
              ({s.inputs.reactions} reactions, {s.inputs.days_since_last_reaction}d since last
              {s.inputs.overlapping_levels.length ? `, overlaps ${s.inputs.overlapping_levels.join(", ")}` : ""})
            </span>
          </p>
          {l.evidence.events.length > 0 && (
            <p className="text-faint">
              Evidence:{" "}
              {l.evidence.events
                .slice(-6)
                .map((e) => `${e.kind} ${fmtPrice(e.price)} @ ${new Date(e.time * 1000).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" })}`)
                .join(" · ")}
            </p>
          )}
          <p className="text-faint">
            Formed {fmtIsoNy(l.formed_at)} · calculated {fmtIsoNy(l.calculated_at)}
          </p>
        </div>
      )}
    </li>
  );
}

export default function LevelsPanel({ report, error }: { report: KeyLevelReport | null; error: string | null }) {
  const [filter, setFilter] = useState<LevelCategory | "all">("all");
  const [showInvalid, setShowInvalid] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  if (error) {
    return (
      <Panel title="Key Technical Levels">
        <DataUnavailable compact message={error} />
      </Panel>
    );
  }
  if (!report) return <Panel title="Key Technical Levels"><Empty>—</Empty></Panel>;

  const shown = report.levels.filter((l) => (showInvalid || l.valid) && (filter === "all" || l.category === filter));
  const above = shown.filter((l) => l.price > report.last_price);
  const below = shown.filter((l) => l.price <= report.last_price);
  const toggle = (id: string) => setOpenId((cur) => (cur === id ? null : id));

  return (
    <Panel
      title="Key Technical Levels"
      right={
        <label className="flex items-center gap-1 text-[10px] text-muted" title="Show levels that are no longer valid">
          <input type="checkbox" checked={showInvalid} onChange={(e) => setShowInvalid(e.target.checked)} className="accent-[#4f9cf9]" />
          invalid
        </label>
      }
      bodyClassName="flex flex-col"
    >
      <div className="flex flex-wrap gap-1 border-b border-line px-2 py-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${filter === f.key ? "bg-white/10 text-ink" : "text-faint hover:text-ink"}`}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {report.warnings.map((w) => (
          <p key={w} className="mx-3 mt-2 rounded bg-wait/10 px-2 py-1 text-[10px] text-wait">{w}</p>
        ))}
        <ul>
          {above.map((l) => (
            <Row key={l.id} l={l} price={report.last_price} open={openId === l.id} onToggle={() => toggle(l.id)} />
          ))}
        </ul>
        <div className="my-1 flex items-center gap-2 px-3 text-[11px]">
          <span className="h-px flex-1 bg-accent/40" />
          <span className="num font-semibold text-accent">Last {fmtPrice(report.last_price)}</span>
          <span className="h-px flex-1 bg-accent/40" />
        </div>
        <ul>
          {below.map((l) => (
            <Row key={l.id} l={l} price={report.last_price} open={openId === l.id} onToggle={() => toggle(l.id)} />
          ))}
        </ul>
        {shown.length === 0 && <Empty>No levels in this filter.</Empty>}
        <p className="px-3 pb-2 pt-1 text-[10px] leading-relaxed text-faint">
          {report.method_version} · calculated {fmtIsoNy(report.calculated_at)} from completed candles only. {report.strength_note}
        </p>
      </div>
    </Panel>
  );
}
