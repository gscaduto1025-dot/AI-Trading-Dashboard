"use client";

import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { STATUS_LABEL } from "@/lib/format";
import {
  DEFAULT_FILTERS,
  VALIDATION_LABEL,
  VALIDATION_ORDER,
  compact,
  fromFilters,
  toFilters,
  type FilterState,
} from "@/lib/scannerFilters";
import type { ScannerPreset } from "@/lib/types";

const PATTERNS = ["breakout", "retest", "pullback", "reversal"];
const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

export function Seg<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="flex rounded-md bg-panel-2 p-0.5 ring-1 ring-line">
      {options.map(([v, label]) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          className={`rounded px-2 py-0.5 text-[11px] font-medium ${value === v ? "bg-accent text-bg" : "text-muted hover:text-ink"}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

/** Scanner filters and saved presets, shared by the watchlist and universe scanners. */
export default function FilterBar({
  f,
  setF,
  strategies,
}: {
  f: FilterState;
  setF: (next: FilterState | ((cur: FilterState) => FilterState)) => void;
  strategies: string[];
}) {
  const set = (patch: Partial<FilterState>) => {
    setF((cur) => ({ ...cur, ...patch }));
    setPresetId("");
  };
  const { mode, side, statuses, minRr, minQ, strategy } = f;
  const [presets, setPresets] = useState<ScannerPreset[]>([]);
  const [presetId, setPresetId] = useState<string>("");
  const [presetMsg, setPresetMsg] = useState<string | null>(null);
  const filters = compact(toFilters(f));

  useEffect(() => {
    api
      .presets()
      .then((r) => setPresets(r.presets))
      .catch(() => {});
  }, []);

  const applyPreset = (id: string) => {
    setPresetId(id);
    setPresetMsg(null);
    const p = presets.find((x) => String(x.id) === id);
    if (p) setF(fromFilters(p.filters));
  };
  const savePreset = async () => {
    const current = presets.find((x) => String(x.id) === presetId);
    const name = window.prompt("Save these scanner filters as preset:", current?.name ?? "")?.trim();
    if (!name) return;
    try {
      const saved = await api.savePreset(name, filters);
      const list = (await api.presets()).presets;
      setPresets(list);
      setPresetId(String(saved.id));
      setPresetMsg(`Saved "${saved.name}"`);
    } catch (e) {
      setPresetMsg(errText(e));
    }
  };
  const deletePreset = async () => {
    const p = presets.find((x) => String(x.id) === presetId);
    if (!p || !window.confirm(`Delete preset "${p.name}"?`)) return;
    try {
      await api.deletePreset(p.id);
      setPresets(presets.filter((x) => x.id !== p.id));
      setPresetId("");
      setPresetMsg(`Deleted "${p.name}"`);
    } catch (e) {
      setPresetMsg(errText(e));
    }
  };

  return (
    <>
          {/* Filters */}
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <Seg value={mode} onChange={(v) => set({ mode: v })} options={[["all", "All"], ["day", "Day trading"], ["swing", "Swing trading"]]} />
        <Seg value={side} onChange={(v) => set({ side: v })} options={[["all", "Both"], ["bullish", "Bullish"], ["bearish", "Bearish"]]} />
        {(["TRIGGERED", "WATCHING", "NO_TRADE"] as const).map((s) => (
          <label key={s} className="flex items-center gap-1 text-muted">
            <input
              type="checkbox"
              checked={statuses[s]}
              onChange={(e) => set({ statuses: { ...statuses, [s]: e.target.checked } })}
              className="accent-[#4f9cf9]"
            />
            {s === "NO_TRADE" ? "Fails requirements" : STATUS_LABEL[s]}
          </label>
        ))}
        <label className="flex items-center gap-1 text-muted">
          Min R:R
          <input value={minRr} onChange={(e) => set({ minRr: e.target.value })} inputMode="decimal" placeholder="any"
            className="w-14 rounded bg-panel-2 px-1.5 py-0.5 ring-1 ring-line-strong outline-none focus:ring-accent" />
        </label>
        <label className="flex items-center gap-1 text-muted">
          Min setup quality
          <input value={minQ} onChange={(e) => set({ minQ: e.target.value })} inputMode="numeric" placeholder="any"
            className="w-14 rounded bg-panel-2 px-1.5 py-0.5 ring-1 ring-line-strong outline-none focus:ring-accent" />
        </label>
        <label className="flex items-center gap-1 text-muted">
          Strategy
          <select value={strategy} onChange={(e) => set({ strategy: e.target.value })}
            className="rounded bg-panel-2 px-1 py-0.5 text-ink ring-1 ring-line-strong">
            <option value="">All</option>
            {PATTERNS.map((p) => (
              <option key={p} value={p}>
                {p[0].toUpperCase() + p.slice(1)} setups
              </option>
            ))}
            {strategies.map((s) => (
              <option key={s} value={s}>
                {s.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <label className="flex items-center gap-1 text-muted">
          Entry state
          <select value={f.signalState} onChange={(e) => set({ signalState: e.target.value as FilterState["signalState"] })}
            className="rounded bg-panel-2 px-1 py-0.5 text-ink ring-1 ring-line-strong">
            <option value="">Any</option>
            <option value="CURRENTLY_ACTIONABLE">Entry conditions met now</option>
            <option value="HISTORICALLY_TRIGGERED">Triggered earlier (not a fresh entry)</option>
          </select>
        </label>
        <span className="flex items-center gap-1 text-muted" title="The strategy's frozen historical-validation status">
          Evidence
          {VALIDATION_ORDER.map((v) => (
            <label key={v} className="flex items-center gap-0.5">
              <input
                type="checkbox"
                checked={f.validation.includes(v)}
                onChange={(e) =>
                  set({ validation: e.target.checked ? [...f.validation, v] : f.validation.filter((x) => x !== v) })
                }
                className="accent-[#4f9cf9]"
              />
              {VALIDATION_LABEL[v]}
            </label>
          ))}
        </span>
        <label className="flex items-center gap-1 text-muted">
          Volume
          <select value={f.volume} onChange={(e) => set({ volume: e.target.value as FilterState["volume"] })}
            className="rounded bg-panel-2 px-1 py-0.5 text-ink ring-1 ring-line-strong">
            <option value="">Any</option>
            <option value="FULL">Confirmed on consolidated volume</option>
            <option value="VERIFIED_OR_NOT_REQUIRED">Confirmed or not required</option>
          </select>
        </label>
        <label className="flex items-center gap-1 text-muted" title="Nearest key-level support/resistance within this many ATRs of price">
          Key level within
          <input value={f.maxLevelAtr} onChange={(e) => set({ maxLevelAtr: e.target.value })} inputMode="decimal" placeholder="any"
            className="w-12 rounded bg-panel-2 px-1.5 py-0.5 ring-1 ring-line-strong outline-none focus:ring-accent" />
          ATR
        </label>
        <label className="flex items-center gap-1 text-muted" title="Consolidated (SIP) average daily dollar volume, last 20 sessions">
          Min liquidity $
          <input value={f.minLiquidityM} onChange={(e) => set({ minLiquidityM: e.target.value })} inputMode="decimal" placeholder="any"
            className="w-14 rounded bg-panel-2 px-1.5 py-0.5 ring-1 ring-line-strong outline-none focus:ring-accent" />
          M/day
        </label>
        <span className="ml-auto flex items-center gap-1">
          <select value={presetId} onChange={(e) => applyPreset(e.target.value)} aria-label="Scanner preset"
            className="rounded bg-panel-2 px-1 py-0.5 text-ink ring-1 ring-line-strong">
            <option value="">Presets…</option>
            {presets.map((p) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </select>
          <button onClick={savePreset} className="rounded px-2 py-0.5 font-semibold text-accent ring-1 ring-accent/40 hover:bg-accent/10">
            Save preset
          </button>
          {presetId && (
            <button onClick={deletePreset} className="rounded px-2 py-0.5 text-muted ring-1 ring-line-strong hover:text-bear">
              Delete
            </button>
          )}
          <button onClick={() => { setF(DEFAULT_FILTERS); setPresetId(""); }} className="rounded px-2 py-0.5 text-muted ring-1 ring-line-strong hover:text-ink">
            Reset
          </button>
          {presetMsg && <span className="text-faint">{presetMsg}</span>}
        </span>
      </div>
    </>
  );
}
