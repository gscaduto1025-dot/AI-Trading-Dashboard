"use client";

import { STATUS_LABEL, fmtPrice, statusClasses } from "@/lib/format";
import type { PlanPrice, TradePlan } from "@/lib/types";

const TF: Record<string, string> = { "5Min": "5m", "15Min": "15m", "1Day": "Daily", "1Week": "Weekly" };
const QUALITY_LABEL: Record<string, string> = {
  higher_timeframe: "Higher-timeframe alignment",
  level_strength: "Supporting level strength",
  price_action: "Price-action confirmation",
  relative_volume: "Relative volume",
  trend_structure: "Trend structure",
  risk_reward: "Available risk/reward",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </div>
  );
}

function Mark({ passed }: { passed: boolean | null }) {
  return (
    <span
      className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded text-[10px] font-bold ${
        passed === null ? "bg-wait/15 text-wait" : passed ? "bg-bull/20 text-bull" : "bg-bear/20 text-bear"
      }`}
    >
      {passed === null ? "…" : passed ? "✓" : "✕"}
    </span>
  );
}

/** One price row: label, value, % from entry and the basis that produced it. */
function PriceRow({ label, p, entry, cls }: { label: string; p: PlanPrice | null; entry: number | null; cls: string }) {
  if (!p) return null;
  const pct = entry && label !== "Entry" ? ` (${(((p.price - entry) / entry) * 100).toFixed(2)}%)` : "";
  return (
    <div className="py-1.5">
      <div className="flex justify-between gap-2">
        <span className="text-muted">{label}</span>
        <span className={`num font-medium ${cls}`}>
          {fmtPrice(p.price)}
          <span className="text-faint">{pct}</span>
        </span>
      </div>
      <p className="mt-0.5 text-[10px] leading-snug text-faint">{p.basis}</p>
    </div>
  );
}

/** Complete conditional trade plan with every calculation and the basis of each level. */
export default function TradePlanView({ plan }: { plan: TradePlan }) {
  const p = plan;
  const dir = p.direction === "LONG" ? "long" : "short";
  const entry = p.entry?.price ?? null;
  const rr = (x: number | null) => (x === null ? "—" : `${x.toFixed(2)} : 1`);
  const confirmed = p.entry?.kind === "confirmation close";
  return (
    <div className="space-y-4 p-3">
      <div className={`rounded-md p-3 ring-1 ${statusClasses(p.status, dir)}`}>
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-bold uppercase tracking-[0.15em]">{STATUS_LABEL[p.status]}</span>
          <span className="text-[11px] font-semibold">{p.direction === "LONG" ? "▲ LONG" : "▼ SHORT"}</span>
        </div>
        <p className="mt-1 text-sm font-semibold text-ink">
          {p.ticker} · {p.strategy_name}
        </p>
        <p className="text-[11px] text-muted">
          {p.mode === "day" ? "Day trading" : "Swing trading"} · {TF[p.timeframe] ?? p.timeframe} candles
          {p.quality && (
            <>
              {" "}
              · quality <span className="num font-semibold text-ink">{p.quality.score.toFixed(0)}</span>/100
            </>
          )}
        </p>
        {p.reasons[0] && <p className="mt-1.5 text-[11px] leading-snug text-ink/85">{p.reasons[0]}</p>}
      </div>

      {p.entry_trigger ? (
        <Section title={confirmed ? (p.status === "TRIGGERED" ? "Trade plan" : "Plan at the trigger (not open)") : "Conditional plan (if triggered)"}>
          <div className="divide-y divide-line rounded-md border border-line bg-panel-2 px-3 text-xs">
            <PriceRow label="Entry trigger" p={p.entry_trigger} entry={entry} cls="text-accent" />
            {p.entry && (
              <PriceRow label={confirmed ? "Entry (confirmation close)" : "Entry (planned)"} p={p.entry} entry={null} cls="text-accent" />
            )}
            {p.entry_zone && (
              <div className="py-1.5">
                <div className="flex justify-between gap-2">
                  <span className="text-muted">Entry zone</span>
                  <span className="num">
                    {fmtPrice(p.entry_zone.low)}–{fmtPrice(p.entry_zone.high)}
                  </span>
                </div>
                <p className="mt-0.5 text-[10px] leading-snug text-faint">{p.entry_zone.basis}</p>
              </div>
            )}
            <PriceRow label="Stop-loss" p={p.stop} entry={entry} cls="text-bear" />
            <PriceRow label="Target 1" p={p.target1} entry={entry} cls="text-bull" />
            <PriceRow label="Target 2" p={p.target2} entry={entry} cls="text-bull" />
          </div>
        </Section>
      ) : (
        <p className="rounded-md border border-line bg-panel-2 px-3 py-2 text-[11px] text-faint">
          No objective entry trigger, so no prices are proposed. Entries are never arbitrary.
        </p>
      )}

      {p.risk_per_share !== null && (
        <Section title="Risk and reward per share">
          <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-line bg-line text-xs">
            {[
              ["Risk / share", fmtPrice(p.risk_per_share)],
              ["Reward / share (T1)", fmtPrice(p.reward_per_share_t1)],
              ["R:R to T1", rr(p.risk_reward_t1)],
              ["Reward / share (T2)", fmtPrice(p.reward_per_share_t2)],
              ["R:R to T2", rr(p.risk_reward_t2)],
              ["R:R at worst fill", rr(p.risk_reward_worst_fill)],
            ].map(([k, v]) => (
              <div key={k} className="bg-panel-2 px-2.5 py-1.5">
                <p className="text-[10px] text-muted">{k}</p>
                <p className="num font-medium">{v}</p>
              </div>
            ))}
          </div>
          <p className="mt-1 text-[10px] text-faint">
            Minimum R:R to T1: {p.min_rr}:1 (configurable). Targets are the nearest confirmed levels and are never moved to meet it.
          </p>
        </Section>
      )}

      {p.calculations.length > 0 && (
        <Section title="Calculations">
          <ul className="num space-y-0.5 rounded-md border border-line bg-panel-2 px-3 py-2 text-[10.5px] leading-relaxed text-ink/85">
            {p.calculations.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
            {p.atr.value !== null && (
              <li className="text-faint">
                {p.atr.label} = {p.atr.value.toFixed(4)}
              </li>
            )}
          </ul>
        </Section>
      )}

      <Section title="Planning rules">
        <ul className="space-y-1">
          {p.checks.map((c, i) => (
            <li key={i} className="flex items-start gap-2">
              <Mark passed={c.passed} />
              <span className="min-w-0">
                <span className="block text-xs text-ink/90">{c.name}</span>
                <span className="block text-[10.5px] text-faint">{c.detail}</span>
              </span>
            </li>
          ))}
        </ul>
      </Section>

      {p.confirmation.length > 0 && (
        <Section title="Confirmation requirements">
          <ul className="space-y-1">
            {p.confirmation.map((c, i) => (
              <li key={i} className="flex items-start gap-2">
                <Mark passed={c.passed === null ? null : c.passed} />
                <span className="min-w-0">
                  <span className="block text-xs text-ink/90">
                    {c.name}
                    {!c.required && <span className="ml-1 text-[9px] uppercase text-faint">supporting</span>}
                  </span>
                  <span className="block text-[10.5px] text-faint">{c.detail}</span>
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {p.invalidation.length > 0 && (
        <Section title="Setup invalidation">
          <ul className="space-y-1">
            {p.invalidation.map((r, i) => (
              <li key={i} className="text-xs text-bear/90">
                {r}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {p.quality && (
        <Section title="Setup quality score">
          <table className="w-full text-[11px]">
            <thead className="text-[9px] uppercase tracking-wider text-faint">
              <tr>
                <th className="py-1 text-left font-medium">Factor</th>
                <th className="py-1 text-right font-medium">Weight</th>
                <th className="py-1 text-right font-medium">Points</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {p.quality.components.map((c) => (
                <tr key={c.name} title={c.detail}>
                  <td className="py-1 pr-2">
                    <span className="block text-ink/90">{QUALITY_LABEL[c.name] ?? c.name}</span>
                    <span className="block text-[10px] text-faint">{c.detail}</span>
                  </td>
                  <td className="num py-1 text-right align-top text-faint">{c.weight}</td>
                  <td className="num py-1 text-right align-top">{c.points.toFixed(1)}</td>
                </tr>
              ))}
              {p.quality.conflict_penalty > 0 && (
                <tr>
                  <td className="py-1 pr-2">
                    <span className="block text-bear/90">Conflicting evidence</span>
                    <span className="block text-[10px] text-faint">{p.quality.conflicts.join("; ")}</span>
                  </td>
                  <td className="num py-1 text-right align-top text-faint">−10 each</td>
                  <td className="num py-1 text-right align-top text-bear">−{p.quality.conflict_penalty.toFixed(1)}</td>
                </tr>
              )}
              <tr className="font-semibold">
                <td className="py-1">Total</td>
                <td className="num py-1 text-right text-faint">100</td>
                <td className="num py-1 text-right">{p.quality.score.toFixed(1)}</td>
              </tr>
            </tbody>
          </table>
          <p className="mt-1 text-[10px] text-faint">{p.quality.note}</p>
        </Section>
      )}

      {p.reasons.length > 1 && (
        <Section title="Notes">
          <ul className="space-y-1">
            {p.reasons.slice(1).map((r, i) => (
              <li key={i} className="text-[11px] leading-snug text-ink/80">
                {r}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <p className="text-[10px] leading-relaxed text-faint">
        Rules-based plan from completed candles. Educational analysis only; TradeScope AI does not place trades.
      </p>
    </div>
  );
}
