"use client";

import type { AIResult, Analysis } from "@/lib/types";
import { DemoTag, Panel, Skeleton } from "./ui";

const VERDICT: Record<string, { label: string; cls: string }> = {
  TAKE_SETUP: { label: "Setup valid", cls: "bg-bull/15 text-bull ring-bull/40" },
  WAIT: { label: "Wait", cls: "bg-wait/10 text-wait ring-wait/30" },
  NO_TRADE: { label: "No trade", cls: "bg-white/5 text-muted ring-line-strong" },
};

function List({ title, items, dot }: { title: string; items?: string[]; dot: string }) {
  if (!items?.length) return null;
  return (
    <div>
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h4>
      <ul className="space-y-1">
        {items.map((t, i) => (
          <li key={i} className="flex gap-2 text-xs leading-relaxed text-ink/85">
            <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
            {t}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function AIPanel({
  analysis,
  result,
  loading,
  aiEnabled,
  onGenerate,
}: {
  analysis: Analysis | null;
  result: AIResult | null;
  loading: boolean;
  aiEnabled: boolean;
  onGenerate: (refresh: boolean) => void;
}) {
  const button = (
    <button
      onClick={() => onGenerate(Boolean(result?.available))}
      disabled={loading || !analysis || !aiEnabled}
      className="rounded-md bg-accent/15 px-2.5 py-1 text-[11px] font-semibold text-accent ring-1 ring-accent/40 transition hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {loading ? "Analyzing…" : result?.available ? "Regenerate" : "Explain with Claude"}
    </button>
  );
  return (
    <Panel title="Claude AI Analysis" right={button} bodyClassName="overflow-y-auto">
      <div className="space-y-3 p-3">
        {!aiEnabled && (
          <div className="rounded-md border border-line bg-panel-2 p-3 text-xs leading-relaxed text-muted">
            <p className="font-semibold text-ink">Claude is not connected.</p>
            <p className="mt-1">
              Add <code className="num text-accent">ANTHROPIC_API_KEY</code> to{" "}
              <code className="num">backend/.env</code> and restart the backend. The setup engine
              works without it; Claude explains the engine&apos;s output in plain language.
            </p>
          </div>
        )}
        {aiEnabled && !result && !loading && (
          <p className="text-xs leading-relaxed text-muted">
            Claude will review the computed levels, bias, setup and checklist for{" "}
            <span className="text-ink">{analysis?.symbol ?? "this symbol"}</span> and explain the
            bullish case, bearish case, what to watch and the risks. Claude never invents prices
            and can only be more cautious than the engine.
          </p>
        )}
        {loading && (
          <div className="space-y-2">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-16" />
            <Skeleton className="h-24" />
          </div>
        )}
        {!loading && result && !result.available && (
          <p className="rounded-md border border-bear/30 bg-bear/5 p-2.5 text-xs text-bear">{result.reason}</p>
        )}
        {!loading && result?.available && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              {result.verdict && (
                <span className={`rounded px-2 py-0.5 text-[11px] font-bold uppercase ring-1 ${VERDICT[result.verdict].cls}`}>
                  {VERDICT[result.verdict].label}
                </span>
              )}
              {result.is_demo && <DemoTag />}
              <span className="text-[10px] text-faint">
                {result.model}
                {result.cached ? " · cached" : ""}
              </span>
            </div>
            <p className="text-sm font-semibold text-ink">{result.headline}</p>
            <p className="text-xs leading-relaxed text-ink/85">{result.summary}</p>
            <div className="grid gap-3 sm:grid-cols-2">
              <List title="Bullish case" items={result.bullish_case} dot="bg-bull" />
              <List title="Bearish case" items={result.bearish_case} dot="bg-bear" />
            </div>
            {result.key_levels_commentary && (
              <div>
                <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">Levels</h4>
                <p className="text-xs leading-relaxed text-ink/85">{result.key_levels_commentary}</p>
              </div>
            )}
            <List title="What to watch" items={result.what_to_watch} dot="bg-accent" />
            <List title="Risk notes" items={result.risk_notes} dot="bg-wait" />
            <p className="text-[10px] text-faint">AI-generated explanation. Educational only — not financial advice.</p>
          </>
        )}
      </div>
    </Panel>
  );
}
