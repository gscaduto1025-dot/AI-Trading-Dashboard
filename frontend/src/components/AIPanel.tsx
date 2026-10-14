"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { STATUS_LABEL, fmtPrice, statusClasses } from "@/lib/format";
import type { AIResponse, Mode } from "@/lib/types";
import { DemoTag, Panel, Skeleton } from "./ui";

const AUTO_KEY = "tradescope.ai.auto";
const BIAS_CLS: Record<string, string> = { bullish: "text-bull", bearish: "text-bear", neutral: "text-wait" };

type Entry = { key: string; phase: "loading" | "generating" | "done"; data: AIResponse | null; error: string | null };

function readAuto(): boolean {
  try {
    return window.localStorage.getItem(AUTO_KEY) !== "off";
  } catch {
    return true;
  }
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h4>
      {children}
    </div>
  );
}

function Para({ text }: { text: string }) {
  return text ? <p className="text-xs leading-relaxed text-ink/85">{text}</p> : <p className="text-[11px] text-faint">—</p>;
}

function Bullets({ items, dot }: { items: string[]; dot: string }) {
  if (!items.length) return <p className="text-[11px] text-faint">—</p>;
  return (
    <ul className="space-y-1">
      {items.map((t, i) => (
        <li key={i} className="flex gap-2 text-xs leading-relaxed text-ink/85">
          <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
          {t}
        </li>
      ))}
    </ul>
  );
}

/**
 * Claude's explanation of the selected setup (or the symbol's best plan). Follows the
 * selected ticker/setup: checks the server cache first (never calls Claude), then
 * generates after a short pause when auto-explain is on. The status and every number come
 * from the deterministic engines; Claude only explains them.
 */
export default function AIPanel({
  symbol,
  mode,
  setupId,
  dataKey,
  aiEnabled,
  isDemo,
}: {
  symbol: string;
  mode: Mode;
  setupId: string | null;
  dataKey: string;
  aiEnabled: boolean;
  isDemo: boolean;
}) {
  const key = `${symbol}|${mode}|${setupId ?? "best"}|${dataKey}`;
  const [entry, setEntry] = useState<Entry | null>(null);
  const [auto, setAuto] = useState<boolean>(true);
  const current = useRef(key);

  useEffect(() => {
    // Sync the persisted preference once on mount (localStorage is client-only).
    const stored = readAuto();
    const t = setTimeout(() => setAuto(stored), 0);
    return () => clearTimeout(t);
  }, []);

  const run = (k: string, generate: boolean, refresh = false) => {
    const call = generate ? api.aiGenerate(symbol, mode, setupId, refresh) : api.aiCached(symbol, mode, setupId);
    return call
      .then((data) => {
        if (current.current === k) setEntry({ key: k, phase: "done", data, error: null });
        return data;
      })
      .catch((e) => {
        if (current.current === k)
          setEntry({ key: k, phase: "done", data: null, error: e instanceof ApiError || e instanceof Error ? e.message : String(e) });
        return null;
      });
  };

  useEffect(() => {
    const k = key;
    current.current = k;
    let timer: ReturnType<typeof setTimeout> | undefined;
    run(k, false).then((data) => {
      if (!data || current.current !== k) return;
      if (!data.available && data.not_generated && aiEnabled && auto) {
        // Debounce generation so flicking through setups does not spend API calls.
        timer = setTimeout(() => {
          if (current.current !== k) return;
          setEntry({ key: k, phase: "generating", data, error: null });
          run(k, true);
        }, 900);
      }
    });
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, aiEnabled, auto]);

  const view = entry?.key === key ? entry : null;
  const data = view?.data ?? null;
  const facts = data?.facts ?? null;
  const ex = data?.available ? data.explanation : undefined;
  const status = ex?.setup_status ?? data?.status ?? null;
  const dir = facts?.setup?.direction === "SHORT" ? "short" : "long";
  const generating = view?.phase === "generating";

  const toggleAuto = () => {
    const next = !auto;
    setAuto(next);
    try {
      window.localStorage.setItem(AUTO_KEY, next ? "on" : "off");
    } catch {
      /* storage unavailable: preference lasts for this page only */
    }
  };

  const generate = (refresh: boolean) => {
    setEntry({ key, phase: "generating", data, error: null });
    run(key, true, refresh);
  };

  const right = (
    <span className="flex items-center gap-2">
      {isDemo && <DemoTag />}
      <label className="flex items-center gap-1 text-[10px] text-muted" title="Explain automatically when the ticker or setup changes (cached per data)">
        <input type="checkbox" checked={auto} onChange={toggleAuto} className="accent-[#4f9cf9]" />
        auto
      </label>
      <button
        onClick={() => generate(Boolean(ex))}
        disabled={!aiEnabled || generating || !facts}
        className="rounded-md bg-accent/15 px-2.5 py-1 text-[11px] font-semibold text-accent ring-1 ring-accent/40 transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {generating ? "Analyzing…" : ex ? "Regenerate" : "Explain with Claude"}
      </button>
    </span>
  );

  return (
    <Panel title="AI Analysis" right={right} bodyClassName="overflow-y-auto">
      <div className="space-y-3 p-3">
        {!view || view.phase === "loading" ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
          </div>
        ) : view.error ? (
          <p className="rounded-md bg-bear/10 px-3 py-2 text-xs text-bear">AI explanation unavailable: {view.error}</p>
        ) : (
          facts && (
            <>
              {/* Deterministic header: always shown, even when the AI is unavailable. */}
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
                {status && (
                  <span className={`rounded px-1.5 py-px text-[10px] font-bold uppercase ring-1 ${statusClasses(status, dir)}`}>
                    {STATUS_LABEL[status]}
                  </span>
                )}
                <span className="font-semibold text-ink">{ex?.setup_name ?? (facts.setup ? `${facts.setup.name} (${facts.setup.direction}, ${facts.setup.timeframe})` : "No setup")}</span>
                <span className="text-muted">
                  bias <span className={BIAS_CLS[facts.market_bias.label]}>{facts.market_bias.label}</span>
                </span>
                <span className="num text-muted">last {fmtPrice(facts.last_price)}</span>
              </div>
              <p className="text-[10px] text-faint">
                {facts.symbol} · {facts.trading_mode} trading · data as of{" "}
                {new Date(facts.data_timestamp).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} ET · {facts.data_source}
              </p>

              {ex ? (
                <>
                  <Section title="Technical summary">
                    <Para text={ex.technical_summary} />
                  </Section>
                  {ex.key_level_explanations.length > 0 && (
                    <Section title="Key levels">
                      <ul className="space-y-1.5">
                        {ex.key_level_explanations.map((l, i) => (
                          <li key={i} className="text-xs leading-relaxed">
                            <span className="num font-medium text-ink">{l.level}</span>
                            <span className="text-ink/80"> — {l.explanation}</span>
                          </li>
                        ))}
                      </ul>
                    </Section>
                  )}
                  <Section title="Setup explanation">
                    <Para text={ex.setup_explanation} />
                  </Section>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Section title="Bullish scenario">
                      <Para text={ex.bullish_scenario} />
                    </Section>
                    <Section title="Bearish scenario">
                      <Para text={ex.bearish_scenario} />
                    </Section>
                  </div>
                  <Section title="Entry conditions">
                    <Bullets items={ex.entry_conditions} dot="bg-accent" />
                  </Section>
                  <Section title="Invalidation">
                    <Bullets items={ex.invalidation_conditions} dot="bg-bear" />
                  </Section>
                  <Section title="Risk analysis">
                    <Para text={ex.risk_explanation} />
                  </Section>
                  {data?.validation && (
                    <div className="rounded-md border border-line bg-panel-2 px-3 py-2 text-[10.5px] text-muted">
                      <p>
                        <span className={data.validation.removed.length || data.validation.corrections.length ? "text-wait" : "text-bull"}>
                          {data.validation.removed.length || data.validation.corrections.length ? "▲" : "✓"}
                        </span>{" "}
                        {data.validation.numbers_verified} of {data.validation.numbers_checked} numbers verified against market data
                        {data.validation.removed.length > 0 && ` · ${data.validation.removed.length} unsupported statement(s) removed`}
                        {data.validation.corrections.length > 0 && ` · ${data.validation.corrections.length} field(s) reset to engine values`}
                      </p>
                      {(data.validation.removed.length > 0 || data.validation.corrections.length > 0) && (
                        <details className="mt-1">
                          <summary className="cursor-pointer text-faint">Details</summary>
                          <ul className="mt-1 space-y-0.5 text-faint">
                            {data.validation.corrections.map((c, i) => (
                              <li key={`c${i}`}>{c}</li>
                            ))}
                            {data.validation.removed.map((r, i) => (
                              <li key={`r${i}`}>
                                Removed from {r.field.replaceAll("_", " ")}: “{r.text}” ({r.reason})
                              </li>
                            ))}
                          </ul>
                        </details>
                      )}
                      <p className="mt-1 text-faint">
                        Status and levels come from the deterministic engines; Claude only explains them.
                        {data.model && ` ${data.cached ? "Cached" : "Generated"} by ${data.model}`}
                        {data.generated_at && ` at ${new Date(data.generated_at).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" })} ET`}
                        . Educational analysis, not financial advice.
                      </p>
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="rounded-md border border-line bg-panel-2 p-3 text-xs leading-relaxed text-muted">
                    {generating ? (
                      <p>Claude is explaining the verified analysis…</p>
                    ) : data?.not_generated ? (
                      <p>
                        No explanation for the current data yet.{" "}
                        {auto ? "Generating automatically…" : "Press “Explain with Claude” to generate one."}
                      </p>
                    ) : (
                      <>
                        <p className="font-semibold text-ink">AI explanation unavailable.</p>
                        <p className="mt-1">{data?.reason?.replace(/^AI explanation unavailable:\s*/, "")}</p>
                        <p className="mt-1 text-faint">The calculated technical analysis below and in the Trade Plan panel is unaffected.</p>
                      </>
                    )}
                  </div>
                  {/* Calculated analysis, shown without any AI. */}
                  <Section title="Calculated analysis">
                    <ul className="space-y-0.5 text-[11px] text-ink/85">
                      <li>
                        Market bias: <span className={BIAS_CLS[facts.market_bias.label]}>{facts.market_bias.label}</span> (score{" "}
                        {facts.market_bias.score}/{facts.market_bias.max_score})
                      </li>
                      {facts.vwap && (
                        <li className="num">
                          VWAP {fmtPrice(facts.vwap.value)}: {facts.vwap.relation} ({facts.vwap.distance_pct}%)
                        </li>
                      )}
                      <li>Moving averages: {String(facts.moving_averages.alignment)}</li>
                      {facts.volume.relative_volume !== null && <li className="num">Relative volume {facts.volume.relative_volume}x</li>}
                      {facts.nearest_support && (
                        <li className="num">
                          Nearest support: {facts.nearest_support.label}{" "}
                          {facts.nearest_support.price !== null
                            ? fmtPrice(facts.nearest_support.price)
                            : `${fmtPrice(facts.nearest_support.zone_low)}–${fmtPrice(facts.nearest_support.zone_high)}`}
                        </li>
                      )}
                      {facts.nearest_resistance && (
                        <li className="num">
                          Nearest resistance: {facts.nearest_resistance.label}{" "}
                          {facts.nearest_resistance.price !== null
                            ? fmtPrice(facts.nearest_resistance.price)
                            : `${fmtPrice(facts.nearest_resistance.zone_low)}–${fmtPrice(facts.nearest_resistance.zone_high)}`}
                        </li>
                      )}
                      {facts.setup?.status_reasons[0] && <li>Setup: {facts.setup.status_reasons[0]}</li>}
                    </ul>
                  </Section>
                </>
              )}
            </>
          )
        )}
        {view && !view.error && !facts && data?.reason && (
          <p className="rounded-md bg-bear/10 px-3 py-2 text-xs text-bear">{data.reason}</p>
        )}
      </div>
    </Panel>
  );
}
