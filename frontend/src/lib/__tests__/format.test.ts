import { describe, expect, it } from "vitest";
import { fmtIsoNy, fmtPct, fmtPrice, fmtSigned, fmtVolume, statusClasses } from "@/lib/format";

describe("formatters never invent values", () => {
  it("renders missing numbers as an em dash", () => {
    for (const f of [fmtPrice, fmtPct, fmtSigned, fmtVolume]) {
      expect(f(null)).toBe("—");
      expect(f(undefined)).toBe("—");
    }
    expect(fmtPrice(Number.NaN)).toBe("—");
    expect(fmtIsoNy(null)).toBe("—");
  });
  it("formats prices, signs and volume", () => {
    expect(fmtPrice(1234.5)).toBe("1,234.50");
    expect(fmtPrice(0.1234, 4)).toBe("0.1234");
    expect(fmtPct(1.234)).toBe("+1.23%");
    expect(fmtPct(-0.5)).toBe("-0.50%");
    expect(fmtSigned(0)).toBe("0.00");
    expect(fmtVolume(1_234_567)).toBe("1.23M");
    expect(fmtVolume(2_500_000_000)).toBe("2.50B");
    expect(fmtVolume(999)).toBe("999");
  });
  it("shows times in New York time", () => {
    expect(fmtIsoNy("2026-03-10T15:02:00Z")).toBe("Mar 10, 11:02 AM ET");
  });
  it("colors status by direction", () => {
    expect(statusClasses("TRIGGERED", "short")).toContain("text-bear");
    expect(statusClasses("TRIGGERED", "long")).toContain("text-bull");
    expect(statusClasses("NO_TRADE", "long")).toContain("text-muted");
  });
});
