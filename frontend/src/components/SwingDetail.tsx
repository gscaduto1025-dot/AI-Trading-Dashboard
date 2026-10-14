"use client";

import { STATUS_LABEL, fmtPrice, statusClasses } from "@/lib/format";
import type { StrategyLevelRef, SwingKeyLevelRef, SwingSetup } from "@/lib/types";
import { Panel } from "./ui";

const day = (t: number | null) =>
  t ? new Date(t * 1000).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", year: "numeric" }) : "—";

const tone = (label: string | undefined) =>
  label === "bullish" ? "text-bull" : label === "bearish" ? "text-bear" : "text-wait";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </div>
  );
}

function LevelList({ levels, empty }: { levels: (StrategyLevelRef | SwingKeyLevelRef)[]; empty: string }) {
  if (levels.length === 0) return <p className="text-[11px] text-faint">{empty}</p>;
  return (
    <ul className="space-y-0.5">
      {levels.map((l, i) => (
        <li key={i} className="flex justify-between gap-2 text-[11px]">
          <span className="truncate text-ink/85">
            {l.label}
            {"position" in l && <span className="ml-1 text-[9px] uppercase text-faint">{l.position}</span>}
          </span>
          <span className="num shrink-0 text-faint">
            {l.low === l.high ? fmtPrice(l.low) : `${fmtPrice(l.low)}–${fmtPrice(l.high)}`}
            {l.strength !== null && l.strength !== undefined ? ` · ${l.strength}` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Full audit view of one swing setup. */
export default function SwingDetail({ setup, onClose, bare = false }: { setup: SwingSetup; onClose: () => void; bare?: boolean }) {
  const s = setup;
  const dir = s.direction === "LONG" ? "long" : "short";
  const ms = s.market_structure;
  const body = (
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
          {s.timeframe === "1Week" ? "Weekly" : "Daily"} candles · last completed {s.last_candle ?? "—"}
          {s.status !== "NO_TRADE" && (
            <>
              {" "}
              · quality <span className="num font-semibold text-ink">{s.quality.score.toFixed(0)}</span>/100
            </>
          )}
        </p>
      </div>

      <Section title="Market structure">
        <div className="space-y-0.5 rounded-md border border-line bg-panel-2 px-3 py-2 text-[11px]">
          <p>
            <span className="text-muted">Structure: </span>
            {ms.structure ?? "—"}
          </p>
          <p>
            <span className="text-muted">EMA 20/50/200: </span>
            {ms.ema_stack ?? "—"}
            <span className="num ml-1 text-faint">
              ({fmtPrice(ms.ema20 ?? null)} / {fmtPrice(ms.ema50 ?? null)} / {fmtPrice(ms.ema200 ?? null)})
            </span>
          </p>
          <p>
            <span className="text-muted">Higher timeframe: </span>
            <span className={tone(ms.higher_timeframe)}>{ms.higher_timeframe ?? "—"}</span>
          </p>
          {ms.higher_timeframe_detail && <p className="text-[10px] text-faint">{ms.higher_timeframe_detail}</p>}
          {ms.supporting && (
            <p>
              <span className="text-muted">Supporting: </span>
              {Object.entries(ms.supporting).map(([tf, v]) => (
                <span key={tf} className="mr-2">
                  {tf === "4Hour" ? "4H" : tf === "1Hour" ? "1H" : tf} <span className={tone(v)}>{v}</span>
                </span>
              ))}
            </p>
          )}
        </div>
      </Section>

      <Section title="Why">
        <ul className="space-y-1">
          {s.reasons.map((r, i) => (
            <li key={i} className="text-xs leading-relaxed text-ink/85">
              {r}
            </li>
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
                    {!c.required && <span className="ml-1 text-[9px] uppercase text-faint">supporting</span>}
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
              <li key={i} className="text-xs text-bear/90">
                {r}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {s.supporting_levels.length > 0 && (
        <Section title="Setup levels">
          <LevelList levels={s.supporting_levels} empty="" />
        </Section>
      )}
      <Section title="Relevant daily levels">
        <LevelList levels={s.daily_levels} empty="No daily key levels near price." />
      </Section>
      <Section title="Relevant weekly levels">
        <LevelList levels={s.weekly_levels} empty="No weekly key levels near price." />
      </Section>

      {s.evidence.length > 0 && (
        <Section title="Technical evidence">
          <ul className="space-y-0.5">
            {s.evidence.map((e, i) => (
              <li key={i} className="num flex justify-between gap-2 text-[11px]">
                <span className="text-ink/85">{e.kind}</span>
                <span className="shrink-0 text-faint">
                  {fmtPrice(e.price)} @ {day(e.time)}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {s.triggers.length > 0 && (
        <Section title="Recent triggers (last 60 candles)">
          <ul className="space-y-0.5">
            {s.triggers.slice(-6).map((t, i) => (
              <li key={i} className="num text-[10px] text-faint">
                {day(t.time)} · trig {fmtPrice(t.entry_trigger)} · S {fmtPrice(t.stop)} · {t.risk_reward}R ·{" "}
                {t.accepted ? "accepted" : `rejected: ${t.blocked}`}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <p className="text-[10px] leading-relaxed text-faint">
        Rules-based signal on completed daily/weekly candles. Educational analysis only; TradeScope AI does not place trades.
      </p>
    </div>
  );
  if (bare) return body;
  return (
    <Panel
      title="Swing Setup Detail"
      right={
        <button onClick={onClose} className="rounded px-2 py-0.5 text-[10px] text-muted ring-1 ring-line-strong hover:text-ink">
          ✕ Close
        </button>
      }
      bodyClassName="overflow-y-auto"
    >
      {body}
    </Panel>
  );
}
