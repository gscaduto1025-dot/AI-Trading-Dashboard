import { describe, expect, it } from "vitest";
import {
  DEFAULT_FILTERS,
  VALIDATION_LABEL,
  compact,
  fmtUsdCompact,
  fromFilters,
  toFilters,
  validationClasses,
} from "@/lib/scannerFilters";

describe("scanner filter presets", () => {
  it("default state maps to the original default query", () => {
    expect(compact(toFilters(DEFAULT_FILTERS))).toEqual({ status: "TRIGGERED,WATCHING" });
  });
  it("round-trips every control through a preset", () => {
    const state = {
      ...DEFAULT_FILTERS,
      mode: "day" as const,
      side: "bearish" as const,
      statuses: { TRIGGERED: true, WATCHING: false, NO_TRADE: true },
      signalState: "CURRENTLY_ACTIONABLE" as const,
      minRr: "2",
      minQ: "60",
      strategy: "breakout",
      validation: ["INCONCLUSIVE" as const, "UNVALIDATED" as const],
      volume: "VERIFIED_OR_NOT_REQUIRED" as const,
      maxLevelAtr: "0.5",
      minLiquidityM: "250",
    };
    const params = compact(toFilters(state));
    expect(params).toEqual({
      mode: "day",
      side: "bearish",
      status: "TRIGGERED,NO_TRADE",
      signal_state: "CURRENTLY_ACTIONABLE",
      min_rr: 2,
      min_quality: 60,
      strategy: "breakout",
      validation: "INCONCLUSIVE,UNVALIDATED",
      volume: "FULL,NOT_REQUIRED",
      max_level_atr: 0.5,
      min_liquidity: 250_000_000,
    });
    expect(fromFilters(params)).toEqual(state);
  });
  it("ignores blank and invalid numbers and unknown statuses", () => {
    const p = toFilters({ ...DEFAULT_FILTERS, minRr: "abc", maxLevelAtr: " " });
    expect(p.min_rr).toBeUndefined();
    expect(p.max_level_atr).toBeUndefined();
    expect(fromFilters({ validation: "GOOD,INCONCLUSIVE" }).validation).toEqual(["INCONCLUSIVE"]);
    expect(toFilters({ ...DEFAULT_FILTERS, statuses: { TRIGGERED: false, WATCHING: false, NO_TRADE: false } }).status).toBe("NONE");
  });
});

describe("validation labels", () => {
  it("names every status in words and never colors negative evidence as positive", () => {
    expect(VALIDATION_LABEL.NEGATIVE_HISTORICAL_EVIDENCE).toBe("Negative evidence");
    expect(validationClasses("NEGATIVE_HISTORICAL_EVIDENCE")).toContain("text-bear");
    expect(validationClasses("INCONCLUSIVE")).not.toContain("text-bull");
    expect(validationClasses("UNVALIDATED")).not.toContain("text-bull");
    expect(validationClasses("VALIDATED_POSITIVE")).toContain("text-bull");
  });
  it("formats liquidity compactly", () => {
    expect(fmtUsdCompact(null)).toBe("—");
    expect(fmtUsdCompact(2.34e9)).toBe("$2.3B");
    expect(fmtUsdCompact(450e6)).toBe("$450M");
  });
});
