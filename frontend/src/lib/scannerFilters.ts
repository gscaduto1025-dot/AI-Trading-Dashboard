import type { Mode, ScannerFilters, ValidationStatus } from "./types";

/** Validation status labels and classes. Every badge carries its text, never color alone. */
export const VALIDATION_LABEL: Record<ValidationStatus, string> = {
  VALIDATED_POSITIVE: "Validated positive",
  NEGATIVE_HISTORICAL_EVIDENCE: "Negative evidence",
  INCONCLUSIVE: "Inconclusive",
  UNVALIDATED: "Unvalidated",
};

export const VALIDATION_ORDER: ValidationStatus[] = [
  "VALIDATED_POSITIVE",
  "INCONCLUSIVE",
  "UNVALIDATED",
  "NEGATIVE_HISTORICAL_EVIDENCE",
];

export function validationClasses(s: ValidationStatus) {
  if (s === "VALIDATED_POSITIVE") return "bg-bull/15 text-bull ring-bull/40";
  if (s === "NEGATIVE_HISTORICAL_EVIDENCE") return "bg-bear/10 text-bear ring-bear/40";
  if (s === "INCONCLUSIVE") return "bg-wait/10 text-wait ring-wait/30";
  return "bg-white/5 text-muted ring-line-strong";
}

/** The scanner's filter controls, as the UI holds them. */
export interface FilterState {
  mode: "all" | Mode;
  side: "all" | "bullish" | "bearish";
  statuses: { TRIGGERED: boolean; WATCHING: boolean; NO_TRADE: boolean };
  signalState: "" | "CURRENTLY_ACTIONABLE" | "HISTORICALLY_TRIGGERED";
  minRr: string;
  minQ: string;
  strategy: string;
  validation: ValidationStatus[]; // empty = any
  volume: "" | "FULL" | "VERIFIED_OR_NOT_REQUIRED";
  maxLevelAtr: string;
  minLiquidityM: string; // USD millions
}

export const DEFAULT_FILTERS: FilterState = {
  mode: "all",
  side: "all",
  statuses: { TRIGGERED: true, WATCHING: true, NO_TRADE: false },
  signalState: "",
  minRr: "",
  minQ: "",
  strategy: "",
  validation: [],
  volume: "",
  maxLevelAtr: "",
  minLiquidityM: "",
};

const num = (s: string) => (s.trim() !== "" && Number.isFinite(Number(s)) ? Number(s) : undefined);

/** UI state -> /api/scanner parameters (also what a preset stores). */
export function toFilters(f: FilterState): ScannerFilters {
  const statuses = Object.entries(f.statuses)
    .filter(([, v]) => v)
    .map(([k]) => k);
  const liq = num(f.minLiquidityM);
  return {
    mode: f.mode === "all" ? undefined : f.mode,
    side: f.side === "all" ? undefined : f.side,
    status: statuses.join(",") || "NONE",
    signal_state: f.signalState || undefined,
    min_rr: num(f.minRr),
    min_quality: num(f.minQ),
    strategy: f.strategy || undefined,
    validation: f.validation.length ? f.validation.join(",") : undefined,
    volume: f.volume === "FULL" ? "FULL" : f.volume === "VERIFIED_OR_NOT_REQUIRED" ? "FULL,NOT_REQUIRED" : undefined,
    max_level_atr: num(f.maxLevelAtr),
    min_liquidity: liq !== undefined ? liq * 1_000_000 : undefined,
  };
}

/** Stored preset parameters -> UI state (unknown or missing keys fall back to defaults). */
export function fromFilters(p: ScannerFilters): FilterState {
  const statuses = (p.status ?? "TRIGGERED,WATCHING").split(",");
  const state = (p.signal_state ?? "").split(",")[0];
  return {
    mode: p.mode ?? "all",
    side: p.side ?? "all",
    statuses: {
      TRIGGERED: statuses.includes("TRIGGERED"),
      WATCHING: statuses.includes("WATCHING"),
      NO_TRADE: statuses.includes("NO_TRADE"),
    },
    signalState: state === "CURRENTLY_ACTIONABLE" || state === "HISTORICALLY_TRIGGERED" ? state : "",
    minRr: p.min_rr !== undefined ? String(p.min_rr) : "",
    minQ: p.min_quality !== undefined ? String(p.min_quality) : "",
    strategy: p.strategy ?? "",
    validation: (p.validation ?? "")
      .split(",")
      .filter((v): v is ValidationStatus => (VALIDATION_ORDER as string[]).includes(v)),
    volume: p.volume === "FULL" ? "FULL" : p.volume === "FULL,NOT_REQUIRED" ? "VERIFIED_OR_NOT_REQUIRED" : "",
    maxLevelAtr: p.max_level_atr !== undefined ? String(p.max_level_atr) : "",
    minLiquidityM: p.min_liquidity !== undefined ? String(p.min_liquidity / 1_000_000) : "",
  };
}

/** Drop undefined keys (what the API and the preset store accept). */
export const compact = (f: ScannerFilters): ScannerFilters =>
  Object.fromEntries(Object.entries(f).filter(([, v]) => v !== undefined)) as ScannerFilters;

export const fmtUsdCompact = (v: number | null | undefined) =>
  v === null || v === undefined
    ? "—"
    : v >= 1e9
      ? `$${(v / 1e9).toFixed(1)}B`
      : v >= 1e6
        ? `$${(v / 1e6).toFixed(0)}M`
        : `$${Math.round(v).toLocaleString("en-US")}`;
