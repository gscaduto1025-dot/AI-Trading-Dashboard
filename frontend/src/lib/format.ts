import type { SetupStatus } from "./types";

export const fmtPrice = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const fmtPct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;

export const fmtSigned = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;

export function fmtTime(unix: number | null | undefined, withDate = true) {
  if (!unix) return "—";
  return new Date(unix * 1000).toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: withDate ? "short" : undefined,
    day: withDate ? "numeric" : undefined,
    hour: "numeric",
    minute: "2-digit",
  });
}

export const fmtDate = (unix: number) =>
  new Date(unix * 1000).toLocaleDateString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    year: "numeric",
  });

export function timeAgo(unix: number) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - unix));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const STATUS_LABEL: Record<SetupStatus, string> = {
  ACTIONABLE: "Actionable",
  FORMING: "Forming",
  NO_TRADE: "No Trade",
};

/** Tailwind classes per status: green/red for direction, amber/gray for incomplete. */
export function statusClasses(status: SetupStatus, direction: "long" | "short" | null) {
  if (status === "ACTIONABLE") {
    return direction === "short"
      ? "bg-bear/15 text-bear ring-bear/40"
      : "bg-bull/15 text-bull ring-bull/40";
  }
  if (status === "FORMING") return "bg-wait/10 text-wait ring-wait/30";
  return "bg-white/5 text-muted ring-line-strong";
}

export const directionColor = (d: "long" | "short" | null) =>
  d === "long" ? "text-bull" : d === "short" ? "text-bear" : "text-muted";
