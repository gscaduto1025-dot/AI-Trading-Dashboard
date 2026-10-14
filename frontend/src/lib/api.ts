import type { AIResult, Alert, Analysis, Bar, Health, Mode, Quote, RecordedSetup } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { cache: "no-store", ...init });
  } catch {
    throw new ApiError("Cannot reach the TradeScope backend. Is it running on port 8000?", 0);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    if (res.status >= 500 && res.status !== 502) {
      detail = "Backend unavailable. Start it with ./scripts/dev.sh or see README.";
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

const q = (params: Record<string, string | number | boolean | undefined>) =>
  new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined)
      .map(([k, v]) => [k, String(v)]),
  ).toString();

export const api = {
  health: () => request<Health>("/api/health"),
  watchlist: () => request<{ is_demo: boolean; quotes: Quote[] }>("/api/watchlist"),
  bars: (symbol: string, timeframe: string) =>
    request<{ bars: Bar[]; is_demo: boolean }>(`/api/bars/${symbol}?${q({ timeframe })}`),
  analysis: (symbol: string, mode: Mode, timeframe: string) =>
    request<Analysis>(`/api/analysis/${symbol}?${q({ mode, timeframe })}`),
  ai: (symbol: string, mode: Mode, timeframe: string, refresh = false) =>
    request<AIResult>(`/api/ai/${symbol}?${q({ mode, timeframe, refresh })}`, { method: "POST" }),
  scan: () => request<{ scanned_at: number; new_alerts: Alert[] }>("/api/scan", { method: "POST" }),
  recentSetups: (limit = 25) =>
    request<{ setups: RecordedSetup[] }>(`/api/setups/recent?${q({ limit })}`),
  alerts: (limit = 50) => request<{ alerts: Alert[] }>(`/api/alerts?${q({ limit })}`),
  ackAlert: (id: number) => request<{ ok: boolean }>(`/api/alerts/${id}/ack`, { method: "POST" }),
};
