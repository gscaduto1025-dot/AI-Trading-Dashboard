"use client";

import { Fragment, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { VALIDATION_LABEL, validationClasses } from "@/lib/scannerFilters";
import type { Mode, ResearchValidation, ValidationEvidence, ValidationStatus } from "@/lib/types";
import { DataUnavailable, Empty, Panel } from "./ui";

const COST_KEYS: [string, string][] = [
  ["x0", "No costs"],
  ["x0.25", "x0.25"],
  ["x0.5", "x0.5"],
  ["x1", "Frozen (x1)"],
  ["x2", "x2"],
];
const ENDPOINT_LABEL: Record<string, string> = {
  gross_all: "Gross edge, all trades",
  gross_h2_htf_aligned: "H2: higher timeframe aligned (gross)",
  gross_h3_quality70: "H3: setup quality ≥ 70 (gross)",
  "net_measured_cost_x0.2": "Net at measured cost (x0.2)",
};

const r2 = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}`);
const ci = (c: [number, number] | null | undefined) => (c ? `(${r2(c[0])} to ${r2(c[1])})` : "");
const pval = (p: number | null | undefined) => (p === null || p === undefined ? "—" : p < 0.001 ? "<0.001" : p.toFixed(3));

const MARK: Record<ValidationStatus, string> = {
  VALIDATED_POSITIVE: "var(--color-bull)",
  NEGATIVE_HISTORICAL_EVIDENCE: "var(--color-bear)",
  INCONCLUSIVE: "var(--color-wait)",
  UNVALIDATED: "var(--color-muted)",
};

function Badge({ s }: { s: ValidationStatus }) {
  return (
    <span className={`whitespace-nowrap rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${validationClasses(s)}`}>
      {VALIDATION_LABEL[s]}
    </span>
  );
}

/** Net expectancy per strategy with its 95% CI against zero (frozen costs). */
function ForestPlot({ data }: { data: ResearchValidation }) {
  const rows = Object.entries(data.strategies)
    .filter(([, s]) => s.evidence && s.evidence.n > 0 && s.evidence.exp_ci)
    .map(([id, s]) => ({ id, ...s, ev: s.evidence as ValidationEvidence }))
    .sort((a, b) => (a.mode === b.mode ? a.ev.exp_r - b.ev.exp_r : a.mode === "day" ? -1 : 1));
  if (!rows.length) return <Empty>No strategy has validation trades.</Empty>;
  const lo = Math.min(0, ...rows.map((r) => r.ev.exp_ci![0]));
  const hi = Math.max(0, ...rows.map((r) => r.ev.exp_ci![1]));
  const W = 760, left = 230, right = 120, rowH = 26, top = 24;
  const H = top + rows.length * rowH + 28;
  const x = (v: number) => left + ((v - lo) / (hi - lo || 1)) * (W - left - right);
  const ticks = [-1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1].filter((t) => t >= lo - 1e-9 && t <= hi + 1e-9);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Net expectancy per strategy with 95% confidence interval" className="w-full max-w-[820px]">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={x(t)} x2={x(t)} y1={top - 6} y2={H - 24} stroke={t === 0 ? "var(--color-muted)" : "var(--color-line)"} strokeWidth={t === 0 ? 1.5 : 1} />
          <text x={x(t)} y={H - 8} textAnchor="middle" fontSize="10" fill="var(--color-faint)">
            {t > 0 ? `+${t}` : t}R
          </text>
        </g>
      ))}
      <text x={x(0) + 4} y={top - 10} fontSize="10" fill="var(--color-muted)">
        break-even
      </text>
      {rows.map((r, i) => {
        const y = top + i * rowH + rowH / 2;
        const [a, b] = r.ev.exp_ci!;
        return (
          <g key={r.id}>
            <title>{`${r.id} (${r.mode}): ${r2(r.ev.exp_r)}R per trade after costs, 95% CI ${r2(a)} to ${r2(b)}R, ${r.ev.n} trades — ${r.label}`}</title>
            <rect x={0} y={y - rowH / 2} width={W} height={rowH} fill="transparent" />
            <text x={left - 10} y={y + 4} textAnchor="end" fontSize="11" fill="var(--color-ink)">
              {r.id.replaceAll("_", " ")}
            </text>
            <text x={8} y={y + 4} fontSize="9" fill="var(--color-faint)">
              {r.mode.toUpperCase()}
            </text>
            <line x1={x(a)} x2={x(b)} y1={y} y2={y} stroke={MARK[r.status]} strokeWidth="2" strokeLinecap="round" />
            <circle cx={x(r.ev.exp_r)} cy={y} r="4.5" fill={MARK[r.status]} stroke="var(--color-panel)" strokeWidth="2" />
            <text x={W - right + 8} y={y + 4} fontSize="10" fill="var(--color-muted)">
              {`${r2(r.ev.exp_r)}R · n=${r.ev.n.toLocaleString("en-US")}`}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function ModeCard({ mode, m }: { mode: Mode; m: ResearchValidation["modes"][Mode] }) {
  const o = m.overall;
  return (
    <div className="rounded-md bg-panel-2 p-3 ring-1 ring-line">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-muted">{mode === "day" ? "Day trading" : "Swing trading"}</p>
      <p className="num mt-1 text-lg font-bold text-bear">{r2(o.exp_r)}R / trade</p>
      <p className="num text-[11px] text-muted">95% CI {ci(o.exp_ci)} after frozen costs</p>
      <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
        <dt className="text-faint">Trades</dt>
        <dd className="num text-right">{o.n.toLocaleString("en-US")}</dd>
        <dt className="text-faint">Win rate</dt>
        <dd className="num text-right">{o.win_rate}%</dd>
        <dt className="text-faint">Profit factor</dt>
        <dd className="num text-right">{o.pf ?? "—"}</dd>
        <dt className="text-faint">Before costs</dt>
        <dd className="num text-right">{r2(o.gross_exp_r)}R</dd>
        <dt className="text-faint">Max drawdown</dt>
        <dd className="num text-right">{o.max_dd_r.toLocaleString("en-US")}R</dd>
      </dl>
      <p className="mt-2 text-[10px] text-faint">
        {m.sample} · {m.independence}
      </p>
    </div>
  );
}

/**
 * Research dashboard: the frozen historical validation behind the scanner's strategy
 * labels. Read-only; kept apart from the scanner and from any forward results.
 */
export default function ResearchView() {
  const [data, setData] = useState<ResearchValidation | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api
      .researchValidation()
      .then(setData)
      .catch((e) => setError(e instanceof ApiError || e instanceof Error ? e.message : String(e)));
  }, []);
  if (error) return <div className="p-3"><DataUnavailable message={error} /></div>;
  if (!data) return <div className="p-3 text-xs text-muted">Loading research results…</div>;
  const strategies = Object.entries(data.strategies);
  return (
    <div className="space-y-3 p-3">
      <Panel title="Strategy research — frozen historical validation">
        <div className="space-y-3 p-3 text-[11px]">
          <p className="text-muted">
            No current strategy has statistically supported positive expectancy after costs. These are the results of the frozen,
            pre-registered 50-stock validation (baseline <span className="font-semibold text-ink">{data.baseline}</span>, frozen{" "}
            {data.frozen_on}, rules hash <span className="num">{data.rules_hash.slice(0, 12)}</span>). They are kept separate from the
            scanner&rsquo;s technical setups and from forward paper testing. The sealed period from May 1, 2026 is untouched.
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            {(["day", "swing"] as Mode[]).map((m) => data.modes[m] && <ModeCard key={m} mode={m} m={data.modes[m]} />)}
          </div>
          <p className="text-faint">Costs: {data.costs}</p>
        </div>
      </Panel>

      <Panel title="Net expectancy per strategy (95% confidence interval, frozen costs)" bodyClassName="p-3">
        <ForestPlot data={data} />
        <p className="mt-1 text-[10px] text-faint">
          Point = mean R per trade after costs; line = 95% bootstrap interval. A strategy is only labelled validated positive when the
          whole interval is above zero and a pre-registered confirmatory test agrees.
        </p>
      </Panel>

      <Panel title="Per-strategy evidence and cost sensitivity" bodyClassName="overflow-auto">
        <table className="w-full min-w-[1100px] text-left text-[11px]">
          <thead className="bg-panel text-[9px] uppercase tracking-wider text-faint">
            <tr>
              <th className="px-2 py-1.5 pl-3 font-medium">Strategy</th>
              <th className="px-2 font-medium">Status</th>
              <th className="px-2 text-right font-medium">Trades</th>
              <th className="px-2 text-right font-medium">Win rate</th>
              <th className="px-2 text-right font-medium">Net R / trade (95% CI)</th>
              <th className="px-2 text-right font-medium">Profit factor</th>
              <th className="px-2 text-right font-medium">Max DD (R)</th>
              {COST_KEYS.map(([k, label]) => (
                <th key={k} className="px-2 text-right font-medium" title={`Net R per trade with modelled costs scaled ${k}`}>
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {strategies.map(([id, s]) => {
              const e = s.evidence;
              const tfs = Object.entries(s.by_timeframe);
              return (
                <Fragment key={id}>
                  <tr title={[s.summary, s.note].filter(Boolean).join("\n")}>
                    <td className="py-1.5 pl-3 pr-2">
                      <span className="font-semibold">{id.replaceAll("_", " ")}</span>{" "}
                      <span className="text-[9px] uppercase text-faint">{s.mode}</span>
                    </td>
                    <td className="px-2"><Badge s={s.status} /></td>
                    <td className="num px-2 text-right">{e ? e.n.toLocaleString("en-US") : 0}</td>
                    <td className="num px-2 text-right">{e ? `${e.win_rate}%` : "—"}</td>
                    <td className="num px-2 text-right">{e ? `${r2(e.exp_r)} ${ci(e.exp_ci)}` : "—"}</td>
                    <td className="num px-2 text-right">{e?.pf ?? "—"}</td>
                    <td className="num px-2 text-right">{e ? e.max_dd_r.toLocaleString("en-US") : "—"}</td>
                    {COST_KEYS.map(([k]) => (
                      <td key={k} className="num px-2 text-right">{r2(e?.cost_sensitivity?.[k]?.exp_r)}</td>
                    ))}
                  </tr>
                  {tfs.map(([tf, t]) => (
                    <tr key={tf} className="text-muted">
                      <td className="py-1 pl-7 pr-2 text-[10px]">{tf}</td>
                      <td className="px-2 text-[10px] text-faint">{t.verdict}</td>
                      <td className="num px-2 text-right">{t.n.toLocaleString("en-US")}</td>
                      <td className="num px-2 text-right">{t.win_rate}%</td>
                      <td className="num px-2 text-right">{`${r2(t.exp_r)} ${ci(t.exp_ci)}`}</td>
                      <td className="num px-2 text-right">{t.pf ?? "—"}</td>
                      <td className="num px-2 text-right">{t.max_dd_r.toLocaleString("en-US")}</td>
                      <td colSpan={COST_KEYS.length} />
                    </tr>
                  ))}
                  {s.note && (
                    <tr>
                      <td colSpan={7 + COST_KEYS.length} className="pb-1.5 pl-7 pr-2 text-[10px] text-faint">{s.note}</td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </Panel>

      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Transaction-cost sensitivity (all trades)" bodyClassName="overflow-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="bg-panel text-[9px] uppercase tracking-wider text-faint">
              <tr>
                <th className="px-2 py-1.5 pl-3 font-medium">Cost multiplier</th>
                <th className="px-2 text-right font-medium">Day net R (95% CI)</th>
                <th className="px-2 text-right font-medium">Swing net R (95% CI)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {["x0", "x0.2", "x0.25", "x0.5", "x1", "x2"].map((k) => (
                <tr key={k}>
                  <td className="py-1 pl-3 pr-2">{k === "x0.2" ? "x0.2 (≈ measured quote cost)" : k === "x1" ? "x1 (frozen model)" : k}</td>
                  {(["day", "swing"] as Mode[]).map((m) => {
                    const c = data.modes[m]?.overall.cost_sensitivity?.[k];
                    return (
                      <td key={m} className="num px-2 text-right">
                        {c ? `${r2(c.exp_r)} ${ci(c.exp_ci)}` : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
        <Panel title="Pre-registered endpoints (Holm-adjusted)" bodyClassName="overflow-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="bg-panel text-[9px] uppercase tracking-wider text-faint">
              <tr>
                <th className="px-2 py-1.5 pl-3 font-medium">Mode</th>
                <th className="px-2 font-medium">Endpoint</th>
                <th className="px-2 text-right font-medium">p</th>
                <th className="px-2 text-right font-medium">p (Holm)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {(["day", "swing"] as Mode[]).flatMap((m) =>
                Object.entries(data.modes[m]?.preregistered.p_two_sided ?? {}).map(([k, p]) => (
                  <tr key={`${m}-${k}`}>
                    <td className="py-1 pl-3 pr-2 capitalize">{m}</td>
                    <td className="px-2">{ENDPOINT_LABEL[k] ?? k}</td>
                    <td className="num px-2 text-right">{pval(p)}</td>
                    <td className="num px-2 text-right">{pval(data.modes[m].preregistered.p_holm[k])}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
          <p className="px-3 py-2 text-[10px] text-faint">
            A small day-trading gross edge is statistically supported but far too small to cover realistic costs; the net-at-measured-cost
            test is significant because it is negative.
          </p>
        </Panel>
      </div>

      <Panel title="How statuses are assigned" bodyClassName="p-3">
        <ul className="space-y-1 text-[11px]">
          {(Object.keys(VALIDATION_LABEL) as ValidationStatus[]).map((k) => (
            <li key={k} className="flex items-start gap-2">
              <Badge s={k} />
              <span className="text-muted">{data.criteria[k]}</span>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-[10px] text-faint">
          Source: {data.source}. The next-generation research protocol (wider stops, longer holds, realistic execution costs) is a
          design only: docs/RESEARCH_PROTOCOL_V2.md.
        </p>
      </Panel>
    </div>
  );
}
