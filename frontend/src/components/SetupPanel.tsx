"use client";

import { STATUS_LABEL, directionColor, fmtPrice, statusClasses } from "@/lib/format";
import type { Analysis } from "@/lib/types";
import { Empty, Panel, Skeleton } from "./ui";

function Row({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div className="flex items-baseline justify-between py-1.5">
      <span className="text-xs text-muted">{label}</span>
      <span className={`num text-sm font-medium ${className}`}>{value}</span>
    </div>
  );
}

export default function SetupPanel({ analysis, loading }: { analysis: Analysis | null; loading: boolean }) {
  if (!analysis) {
    return (
      <Panel title="Trade Setup">
        {loading ? (
          <div className="space-y-3 p-3">
            <Skeleton className="h-16" />
            <Skeleton className="h-32" />
            <Skeleton className="h-40" />
          </div>
        ) : (
          <Empty>No analysis loaded.</Empty>
        )}
      </Panel>
    );
  }
  const s = analysis.setup;
  const noTrade = s.status === "NO_TRADE";
  const pctFrom = (p: number | null) =>
    p === null ? "" : ` (${(((p - analysis.price) / analysis.price) * 100).toFixed(2)}%)`;
  const passed = s.checklist.filter((c) => c.passed).length;

  return (
    <Panel
      title="Trade Setup"
      right={<span className="text-[11px] text-faint">{analysis.mode === "day" ? "Day" : "Swing"} · {analysis.timeframe}</span>}
      bodyClassName="overflow-y-auto"
    >
      <div className="space-y-4 p-3">
        <div className={`rounded-md p-3 ring-1 ${statusClasses(s.status, s.direction)}`}>
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-bold uppercase tracking-[0.15em]">
              {STATUS_LABEL[s.status]}
            </span>
            {!noTrade && (
              <span className="text-[11px] font-semibold uppercase">
                {s.direction === "long" ? "▲ Long" : "▼ Short"}
              </span>
            )}
          </div>
          <p className="mt-1 text-sm font-semibold text-ink">{s.title}</p>
          {!noTrade && (
            <div className="mt-2">
              <div className="flex justify-between text-[10px] text-muted">
                <span>Confidence</span>
                <span className="num">{s.confidence}%</span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-black/40">
                <div
                  className={`h-full rounded-full ${s.status === "ACTIONABLE" ? (s.direction === "short" ? "bg-bear" : "bg-bull") : "bg-wait"}`}
                  style={{ width: `${s.confidence}%` }}
                />
              </div>
            </div>
          )}
        </div>

        {noTrade ? (
          <div>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
              Why no trade
            </h3>
            <ul className="space-y-2">
              {s.reasons.map((r, i) => (
                <li key={i} className="flex gap-2 text-xs leading-relaxed text-ink/85">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-faint" />
                  {r}
                </li>
              ))}
            </ul>
            <p className="mt-3 rounded-md border border-line bg-panel-2 p-2.5 text-xs text-muted">
              Standing aside is a position. The engine only flags setups with a defined trigger,
              stop and adequate reward/risk.
            </p>
          </div>
        ) : (
          <>
            <div className="divide-y divide-line rounded-md border border-line bg-panel-2 px-3">
              <Row label="Last price" value={fmtPrice(analysis.price)} />
              <Row label={s.triggered ? "Entry (triggered)" : "Entry trigger"} value={fmtPrice(s.entry) + pctFrom(s.entry)} className="text-accent" />
              <Row label="Stop-loss" value={fmtPrice(s.stop) + pctFrom(s.stop)} className="text-bear" />
              {s.targets.map((t, i) => (
                <Row key={i} label={`Target ${i + 1}`} value={fmtPrice(t) + pctFrom(t)} className="text-bull" />
              ))}
              <Row label="Risk / share" value={fmtPrice(s.risk_per_share)} />
              <Row
                label="Reward : Risk"
                value={s.risk_reward !== null ? `${s.risk_reward.toFixed(2)} : 1` : "—"}
                className={directionColor(s.direction)}
              />
            </div>
            <ul className="space-y-1.5">
              {s.reasons.map((r, i) => (
                <li key={i} className="text-xs leading-relaxed text-ink/85">{r}</li>
              ))}
              {s.invalidation && (
                <li className="text-xs leading-relaxed text-muted">
                  <span className="font-semibold text-bear/90">Invalidation: </span>
                  {s.invalidation}
                </li>
              )}
            </ul>
          </>
        )}

        {s.checklist.length > 0 && (
          <div>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                Confirmation checklist
              </h3>
              <span className="num text-[11px] text-faint">
                {passed}/{s.checklist.length}
              </span>
            </div>
            <ul className="space-y-1">
              {s.checklist.map((c, i) => (
                <li key={i} className="flex items-start gap-2 rounded px-1.5 py-1 hover:bg-white/[0.03]" title={c.detail}>
                  <span
                    className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded text-[10px] font-bold ${
                      c.passed ? "bg-bull/20 text-bull" : c.required ? "bg-bear/20 text-bear" : "bg-white/5 text-faint"
                    }`}
                  >
                    {c.passed ? "✓" : "✕"}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-xs text-ink/90">
                      {c.label}
                      {c.required && <span className="ml-1 text-[9px] uppercase text-faint">req</span>}
                    </span>
                    <span className="block text-[11px] text-faint">{c.detail}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {analysis.alternatives.length > 0 && (
          <div>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
              Other candidates considered
            </h3>
            <ul className="space-y-1.5">
              {analysis.alternatives.map((a, i) => (
                <li key={i} className="rounded border border-line px-2 py-1.5 text-[11px]">
                  <div className="flex justify-between gap-2">
                    <span className={directionColor(a.direction)}>{a.title}</span>
                    <span className="shrink-0 text-faint">{STATUS_LABEL[a.status]}</span>
                  </div>
                  {a.reasons[0] && <p className="mt-0.5 text-faint">{a.reasons[0]}</p>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Panel>
  );
}
