import { describe, expect, it } from "vitest";
import { niceTicks } from "@/lib/chartMath";

describe("niceTicks", () => {
  it("always contains the data range (regression: lines drawn outside the axes)", () => {
    const cases: [number, number][] = [
      [96.7, 100.9], [0, 4.2], [-2.3, 1.1], [99_000, 100_303], [0.001, 0.0042], [-31.1, 0],
    ];
    for (const [lo, hi] of cases) {
      const t = niceTicks(lo, hi, 4);
      expect(t[0]).toBeLessThanOrEqual(lo);
      expect(t[t.length - 1]).toBeGreaterThanOrEqual(hi);
      expect(t.length).toBeLessThanOrEqual(8);
    }
  });
  it("uses round steps", () => {
    expect(niceTicks(0, 100, 4)).toEqual([0, 25, 50, 75, 100]);
    expect(niceTicks(96, 101, 4)).toEqual([96, 98, 100, 102]);
  });
  it("handles a flat series", () => {
    expect(niceTicks(5, 5)).toEqual([5]);
  });
});
