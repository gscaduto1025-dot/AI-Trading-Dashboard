"use client";

import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { STATUS_LABEL, statusClasses } from "@/lib/format";
import { validationClasses } from "@/lib/scannerFilters";
import type { JournalAccount, Mode, RulesExplanation as RX, ScannerRow } from "@/lib/types";

const SECTIONS: [keyof RX, string][] = [
  ["detected", "Detected setup"],
  ["why_it_qualifies", "Why it qualifies"],
  ["status_reason", "Why this status"],
  ["key_levels", "Important key levels"],
  ["confirmation_required", "Confirmation required"],
  ["invalidation", "What invalidates it"],
  ["trade_plan", "Entry, stop and targets"],
  ["risk_reward", "Risk / reward"],
  ["evidence", "Historical strategy evidence"],
  ["market_context", "Market context"],
];

/** Deterministic, rules-based explanation (computed by the engines; not AI). */
export default function RulesExplanation({ x }: { x: RX }) {
  const dir = x.detected[0]?.includes("SHORT") ? "short" : "long";
  const evStatus = x.evidence_status;
  return (
    <div className="space-y-2.5 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded px-1.5 py-px text-[10px] font-bold uppercase ring-1 ${statusClasses(x.status, dir)}`}>{STATUS_LABEL[x.status]}</span>
        {evStatus && (
          <span className={`rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${validationClasses(evStatus)}`}>{x.evidence_label}</span>
        )}
        <span className="text-[10px] text-faint">
          {x.symbol} · {x.mode} · data as of{" "}
          {new Date(x.data_timestamp).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} ET ·{" "}
          {x.data_source}
        </span>
      </div>
      {SECTIONS.map(([k, title]) => {
        const items = x[k] as string[];
        if (!items || !items.length) return null;
        return (
          <div key={k}>
            <h4 className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">{title}</h4>
            <ul className="space-y-0.5">
              {items.map((t, i) => (
                <li key={i} className="leading-relaxed text-ink/85">
                  {t}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
      <p className="text-[10px] text-faint">{x.source} Educational analysis, not financial advice.</p>
    </div>
  );
}

const nowLocal = () => {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
};
const inp = "rounded bg-white/5 px-2 py-1 ring-1 ring-line-strong";

/**
 * Actions on a detected setup. Saving keeps it for review (never a trade). Logging a trade
 * records YOUR fill, pre-filled with the engine's plan; nothing is executed.
 */
export function SetupActions({ row, source, onWatchlist }: { row: ScannerRow; source: "watchlist" | "universe"; onWatchlist?: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [log, setLog] = useState<null | { account: JournalAccount; qty: string; price: string; fees: string; at: string }>(null);
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
    } catch (e) {
      setMsg(e instanceof ApiError || e instanceof Error ? e.message : String(e));
    }
  };
  const record = () =>
    run(
      () =>
        api.journalCreate(log!.account, {
          ticker: row.ticker,
          direction: row.direction,
          mode: row.mode as Mode,
          strategy: row.strategy,
          setup_id: row.id,
          planned_stop: row.stop,
          planned_target: row.target1,
          planned_target2: row.target2,
          quality: row.quality,
          notes: `From ${source} scanner setup ${row.setup} (${row.timeframe}), status ${row.status} at logging.`,
          fills: [{ side: row.direction === "LONG" ? "BUY" : "SELL", quantity: log!.qty, price: log!.price, fees: log!.fees || 0, executed_at: new Date(log!.at).toISOString() }],
        }),
      `Recorded a ${log!.account} trade. See the Trade log.`,
    ).then(() => setLog(null));
  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex flex-wrap gap-1.5">
        <button onClick={() => run(() => api.saveSetup(row.id, source), "Saved to your saved setups (not a trade).")} className="rounded px-2 py-1 font-semibold text-accent ring-1 ring-accent/40 hover:bg-accent/10">
          Save setup
        </button>
        {source === "universe" && (
          <button onClick={() => run(() => api.addSymbol(row.ticker).then(() => onWatchlist?.()), `${row.ticker} added to the watchlist.`)} className="rounded px-2 py-1 text-muted ring-1 ring-line-strong hover:text-ink">
            Add {row.ticker} to watchlist
          </button>
        )}
        {(["paper", "actual"] as const).map((a) => (
          <button
            key={a}
            onClick={() => setLog({ account: a, qty: "", price: String(row.entry ?? row.entry_trigger ?? ""), fees: "0", at: nowLocal() })}
            className="rounded px-2 py-1 text-muted ring-1 ring-line-strong hover:text-ink"
          >
            Log {a} trade…
          </button>
        ))}
      </div>
      {log && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            record();
          }}
          className="flex flex-wrap items-end gap-1.5 rounded-md bg-panel-2 p-2 ring-1 ring-line"
        >
          <span className="w-full text-faint">
            Record YOUR {log.account} {row.direction === "LONG" ? "buy" : "short sale"} of {row.ticker}. Stop {row.stop ?? "—"}, targets {row.target1 ?? "—"} / {row.target2 ?? "—"} come from the plan. TradeScope does not place orders.
          </span>
          <label className="flex flex-col"><span className="text-faint">Quantity</span><input aria-label="Log quantity" required value={log.qty} onChange={(e) => setLog({ ...log, qty: e.target.value })} className={`${inp} w-20`} /></label>
          <label className="flex flex-col"><span className="text-faint">Fill price</span><input aria-label="Log price" required value={log.price} onChange={(e) => setLog({ ...log, price: e.target.value })} className={`${inp} w-24`} /></label>
          <label className="flex flex-col"><span className="text-faint">Fees</span><input aria-label="Log fees" value={log.fees} onChange={(e) => setLog({ ...log, fees: e.target.value })} className={`${inp} w-16`} /></label>
          <label className="flex flex-col"><span className="text-faint">Executed at</span><input aria-label="Log time" type="datetime-local" value={log.at} onChange={(e) => setLog({ ...log, at: e.target.value })} className={inp} /></label>
          <button className="rounded bg-accent/15 px-2 py-1 font-semibold text-accent ring-1 ring-accent/40">Record</button>
          <button type="button" onClick={() => setLog(null)} className="text-muted">Cancel</button>
        </form>
      )}
      {msg && <p className="text-faint">{msg}</p>}
    </div>
  );
}
