"use client";

import { STATUS_LABEL, fmtPrice, statusClasses } from "@/lib/format";
import type { StrategySetup } from "@/lib/types";
import { Panel } from "./ui";

const et = (t: number | null) =>
  t ? new Date(t * 1000).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " ET" : "—";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </div>
  );
}

/** Full audit view of one strategy setup. */
export default function StrategyDetail({ setup, onClose }: { setup: StrategySetup; onClose: () => void }) {
  const s = setup;
  const dir = s.direction === "LONG" ? "long" : "short";
  const pct = (p: number | null) => (p === null || s.entry === null ? "" : ` (${(((p - s.entry) / s.entry) * 100).toFixed(2)}%)`);
  return (
    <Panel
      title="Setup Detail"
      right={
        <button onClick={onClose} className="rounded px-2 py-0.5 text-[10px] text-muted ring-1 ring-line-strong hover:text-ink">
          ✕ Close
        </button>
      }
      bodyClassName="overflow-y-auto"
    >
      <div className="space-y-4 p-3">
        <div className={`rounded-md p-3 ring-1 ${statusClasses(s.status, dir)}`}>
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-bold uppercase tracking-[0.15em]">{STATUS_LABEL[s.status]}</span>
            <span className="text-[11px] font-semibold">{s.direction === "LONG" ? "▲ LONG" : "▼ SHORT"}</span>
          </div>
          <p className="mt-1 text-sm font-semibold text-ink">
            {s.ticker} · {s.strategy_name}
          </p>
          <p className="text-[11px] text-muted">
            {s.timeframe} · session {s.session_date} ({s.session_state}) · evaluated {new Date(s.evaluated_at).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" })} ET
          </p>
        </div>

        <div className="rounded-md border border-line bg-panel-2 px-3 py-1 text-xs">
          <p className="py-1">
            <span className="text-muted">Higher-timeframe bias: </span>
            <span className={s.htf_bias.label === "bullish" ? "text-bull" : s.htf_bias.label === "bearish" ? "text-bear" : "text-wait"}>
              {s.htf_bias.label}
            </span>
          </p>
          <p className="pb-1 text-[10px] text-faint">{s.htf_bias.detail}</p>
        </div>

        {s.entry !== null && s.stop !== null && (
          <div className="divide-y divide-line rounded-md border border-line bg-panel-2 px-3 text-xs">
            {[
              ["Entry", s.entry, "text-accent"],
              ["Stop-loss", s.stop, "text-bear"],
              ["Target 1", s.target1, "text-bull"],
              ["Target 2", s.target2, "text-bull"],
            ].map(([label, v, cls]) => (
              <div key={label as string} className="flex justify-between py-1.5">
                <span className="text-muted">{label as string}</span>
                <span className={`num font-medium ${cls}`}>
                  {fmtPrice(v as number | null)}
                  <span className="text-faint">{label === "Entry" ? "" : pct(v as number | null)}</span>
                </span>
              </div>
            ))}
            <div className="flex justify-between py-1.5">
              <span className="text-muted">Reward : risk (T1)</span>
              <span className="num font-medium">{s.risk_reward?.toFixed(2)} : 1</span>
            </div>
            <div className="flex justify-between py-1.5">
              <span className="text-muted">Triggered</span>
              <span className="num">{et(s.confirmed_at)}</span>
            </div>
            {s.outcome && (
              <div className="flex justify-between py-1.5">
                <span className="text-muted">Since trigger (info only)</span>
                <span className="num">{s.outcome.state.replaceAll("_", " ")}</span>
              </div>
            )}
          </div>
        )}
        {s.targets_basis.length > 0 && <p className="text-[10px] text-faint">{s.targets_basis.join(" · ")}</p>}

        <Section title="Why">
          <ul className="space-y-1">
            {s.reasons.map((r, i) => (
              <li key={i} className="text-xs leading-relaxed text-ink/85">{r}</li>
            ))}
          </ul>
        </Section>

        {s.confirmation.length > 0 && (
          <Section title="Confirmation conditions">
            <ul className="space-y-1">
              {s.confirmation.map((c, i) => (
                <li key={i} className="flex items-start gap-2">
                  <span
                    className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded text-[10px] font-bold ${
                      c.passed === null ? "bg-white/5 text-faint" : c.passed ? "bg-bull/20 text-bull" : "bg-bear/20 text-bear"
                    }`}
                  >
                    {c.passed === null ? "–" : c.passed ? "✓" : "✕"}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-xs text-ink/90">
                      {c.name}
                      {!c.required && <span className="ml-1 text-[9px] uppercase text-faint">optional</span>}
                      {c.passed === null && <span className="ml-1 text-[9px] uppercase text-faint">n/a</span>}
                    </span>
                    <span className="block text-[11px] text-faint">{c.detail}</span>
                  </span>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {s.invalidation.length > 0 && (
          <Section title="Invalidation">
            <ul className="space-y-1">
              {s.invalidation.map((r, i) => (
                <li key={i} className="text-xs text-bear/90">{r}</li>
              ))}
            </ul>
          </Section>
        )}

        {s.supporting_levels.length > 0 && (
          <Section title="Supporting key levels">
            <ul className="space-y-0.5">
              {s.supporting_levels.map((l, i) => (
                <li key={i} className="flex justify-between gap-2 text-[11px]">
                  <span className="truncate text-ink/85">{l.label}</span>
                  <span className="num shrink-0 text-faint">
                    {l.low === l.high ? fmtPrice(l.low) : `${fmtPrice(l.low)}–${fmtPrice(l.high)}`}
                    {l.strength !== null ? ` · ${l.strength}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {s.evidence.length > 0 && (
          <Section title="Technical evidence">
            <ul className="space-y-0.5">
              {s.evidence.map((e, i) => (
                <li key={i} className="num flex justify-between text-[11px]">
                  <span className="text-ink/85">{e.kind}</span>
                  <span className="text-faint">
                    {fmtPrice(e.price)} @ {et(e.time)}
                  </span>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {s.triggers_today.length > 1 && (
          <Section title="All triggers this session">
            <ul className="space-y-0.5">
              {s.triggers_today.map((t, i) => (
                <li key={i} className="num text-[10px] text-faint">
                  {et(t.time)} · E {fmtPrice(t.entry)} S {fmtPrice(t.stop)} · {t.risk_reward}R ·{" "}
                  {t.accepted ? "accepted" : `rejected: ${t.blocked}`}
                </li>
              ))}
            </ul>
          </Section>
        )}
        <p className="text-[10px] leading-relaxed text-faint">
          Rules-based, completed-candle signal. Educational analysis only; TradeScope AI does not place trades.
        </p>
      </div>
    </Panel>
  );
}
