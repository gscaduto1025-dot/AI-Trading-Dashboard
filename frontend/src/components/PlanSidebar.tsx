"use client";

import { useState, type ReactNode } from "react";
import { STATUS_LABEL } from "@/lib/format";
import type { PlansResponse, TradePlan } from "@/lib/types";
import TradePlanView from "./TradePlanPanel";
import { DataUnavailable, DemoTag, Empty, Panel } from "./ui";

const TF: Record<string, string> = { "5Min": "5m", "15Min": "15m", "1Day": "D", "1Week": "W" };

const chip = (p: TradePlan, active: boolean) =>
  `rounded px-2 py-1 text-left text-[10px] ring-1 transition ${
    active ? "bg-accent/15 text-ink ring-accent/50" : "text-muted ring-line-strong hover:text-ink"
  }`;

/**
 * Right sidebar. With a selected setup: its trade plan and (tab) the strategy details.
 * Otherwise: every plan for the current symbol and mode, best first.
 */
export default function PlanSidebar({
  selected,
  details,
  onClose,
  plans,
  error,
  isDemo,
}: {
  selected: TradePlan | null;
  details: ReactNode | null;
  onClose: () => void;
  plans: PlansResponse | null;
  error: string | null;
  isDemo: boolean;
}) {
  const [tab, setTab] = useState<"plan" | "details">("plan");
  const [picked, setPicked] = useState(0);

  if (selected) {
    return (
      <Panel
        title="Trade Plan"
        right={
          <span className="flex items-center gap-1">
            {(["plan", "details"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`rounded px-2 py-0.5 text-[10px] ring-1 ${tab === t ? "bg-accent text-bg ring-accent" : "text-muted ring-line-strong hover:text-ink"}`}
              >
                {t === "plan" ? "Plan" : "Setup details"}
              </button>
            ))}
            <button onClick={onClose} className="ml-1 rounded px-2 py-0.5 text-[10px] text-muted ring-1 ring-line-strong hover:text-ink">
              ✕
            </button>
          </span>
        }
        bodyClassName="overflow-y-auto"
      >
        {tab === "plan" ? <TradePlanView plan={selected} /> : details}
      </Panel>
    );
  }

  const list = plans?.plans ?? [];
  const active = list.filter((p) => p.status !== "NO_TRADE");
  const shown = active.length ? active : list.slice(0, 6);
  const current = shown[Math.min(picked, Math.max(shown.length - 1, 0))] ?? null;
  return (
    <Panel title="Trade Plan" right={isDemo ? <DemoTag /> : undefined} bodyClassName="overflow-y-auto">
      {error ? (
        <DataUnavailable compact message={error} />
      ) : !plans ? (
        <Empty>Building trade plans…</Empty>
      ) : (
        <>
          <div className="border-b border-line px-3 py-2">
            <p className="text-[11px] text-muted">
              {plans.symbol} · {plans.mode === "day" ? "day trading" : "swing trading"} ·{" "}
              {active.length ? `${active.length} active plan${active.length > 1 ? "s" : ""}` : "no active plan"}
            </p>
            {plans.status === "NO_TRADE" && plans.reason && (
              <p className="mt-1 text-[11px] leading-snug text-ink/85">
                <span className="font-semibold text-muted">NO TRADE.</span> {plans.reason}
              </p>
            )}
            {shown.length > 1 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {shown.map((p, i) => (
                  <button key={`${p.setup_id}-${i}`} onClick={() => setPicked(i)} className={chip(p, p === current)}>
                    <span className={p.direction === "LONG" ? "text-bull" : "text-bear"}>{p.direction === "LONG" ? "▲" : "▼"}</span>{" "}
                    {p.strategy_name} {TF[p.timeframe] ?? p.timeframe} · {STATUS_LABEL[p.status]}
                  </button>
                ))}
              </div>
            )}
            {!active.length && list.length > 0 && (
              <p className="mt-1 text-[10px] text-faint">Showing the closest rejected plans so their calculations can be inspected.</p>
            )}
          </div>
          {current ? <TradePlanView plan={current} /> : <Empty>No setups for this symbol in this mode.</Empty>}
        </>
      )}
    </Panel>
  );
}
