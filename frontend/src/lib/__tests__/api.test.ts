import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";

const respond = (status: number, body: unknown, json = true) =>
  vi.fn(async () =>
    json
      ? new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
      : new Response(String(body), { status }),
  );

afterEach(() => vi.unstubAllGlobals());

describe("api error handling", () => {
  it("surfaces MARKET DATA UNAVAILABLE from the backend", async () => {
    vi.stubGlobal("fetch", respond(503, { detail: { code: "MARKET_DATA_UNAVAILABLE", message: "Alpaca 503", display: "MARKET DATA UNAVAILABLE" } }));
    const err = await api.health().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
    expect(err.code).toBe("MARKET_DATA_UNAVAILABLE");
    expect(err.display).toBe("MARKET DATA UNAVAILABLE");
    expect(err.message).toBe("Alpaca 503");
  });
  it("reports an offline backend instead of inventing data", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("fetch failed"); }));
    const err = await api.watchlist().catch((e) => e);
    expect(err.code).toBe("BACKEND_OFFLINE");
  });
  it("treats a non-JSON 5xx (proxy cannot reach backend) as offline", async () => {
    vi.stubGlobal("fetch", respond(502, "Bad Gateway", false));
    const err = await api.health().catch((e) => e);
    expect(err.code).toBe("BACKEND_OFFLINE");
  });
  it("passes plain-string validation errors through (e.g. trade-log over-close)", async () => {
    vi.stubGlobal("fetch", respond(400, { detail: "SELL of 100 exceeds the open position of 60" }));
    const err = await api.journalAddFill("actual", 1, {}).catch((e) => e);
    expect(err.status).toBe(400);
    expect(err.message).toContain("exceeds the open position");
  });
  it("calls the separate trade-log and backtest endpoints", async () => {
    const f = respond(200, { trades: [] });
    vi.stubGlobal("fetch", f);
    await api.journal("paper");
    await api.backtests();
    expect(f.mock.calls.map((c) => String((c as unknown[])[0]))).toEqual(["/api/journal/paper", "/api/backtests"]);
  });
});
