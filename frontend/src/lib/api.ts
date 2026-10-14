import type {
  AIResult,
  Alert,
  Analysis,
  CandleResponse,
  DataStatus,
  FeedInfo,
  Health,
  KeyLevelReport,
  MarketStatus,
  Mode,
  Quote,
  RecordedSetup,
  SessionKind,
  StrategyEvaluation,
  PlansResponse,
  SwingEvaluation,
  SwingSortKey,
} from "./types";

export const UNAVAILABLE = "MARKET DATA UNAVAILABLE";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    /** Stable code from the backend, e.g. MARKET_DATA_UNAVAILABLE, STALE_DATA. */
    public code: string = "ERROR",
    /** Headline to show the user, e.g. "MARKET DATA UNAVAILABLE". */
    public display: string | null = null,
    public dataStatus: DataStatus | null = null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { cache: "no-store", ...init });
  } catch {
    throw new ApiError("Cannot reach the TradeScope backend. Is it running on port 8000?", 0, "BACKEND_OFFLINE");
  }
  if (!res.ok) {
    let message = res.statusText;
    let code = "ERROR";
    let display: string | null = null;
    let dataStatus: DataStatus | null = null;
    try {
      const body = await res.json();
      const d = body.detail ?? body;
      if (typeof d === "string") {
        message = d;
      } else if (d && typeof d === "object" && "message" in d) {
        message = d.message;
        code = d.code ?? code;
        display = d.display ?? null;
        dataStatus = d.data_status ?? null;
      } else {
        message = JSON.stringify(d);
      }
    } catch {
      // Non-JSON body: most likely the Next.js proxy could not reach the backend.
      if (res.status >= 500) {
        message = "Backend unavailable. Start it with ./scripts/dev.sh or see README.";
        code = "BACKEND_OFFLINE";
      }
    }
    throw new ApiError(message, res.status, code, display, dataStatus);
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
  marketStatus: () => request<{ market: MarketStatus; feed: FeedInfo; is_demo: boolean }>("/api/market/status"),
  watchlist: () =>
    request<{ is_demo: boolean; quotes: Quote[]; feed: FeedInfo; market: MarketStatus }>("/api/watchlist"),
  bars: (symbol: string, timeframe: string, session: SessionKind) =>
    request<CandleResponse>(`/api/bars/${symbol}?${q({ timeframe, session })}`),
  analysis: (symbol: string, mode: Mode, timeframe: string) =>
    request<Analysis>(`/api/analysis/${symbol}?${q({ mode, timeframe })}`),
  levels: (symbol: string, mode: Mode, timeframe: string) =>
    request<KeyLevelReport>(`/api/levels/${symbol}?${q({ mode, timeframe })}`),
  strategySetups: () => request<StrategyEvaluation>("/api/strategies/setups"),
  plans: (symbol: string, mode: Mode) => request<PlansResponse>(`/api/plans/${symbol}?${q({ mode })}`),
  swingSetups: (sort: SwingSortKey = "quality") => request<SwingEvaluation>(`/api/swing/setups?${q({ sort })}`),
  ai: (symbol: string, mode: Mode, timeframe: string, refresh = false) =>
    request<AIResult>(`/api/ai/${symbol}?${q({ mode, timeframe, refresh })}`, { method: "POST" }),
  scan: () => request<{ scanned_at: number; new_alerts: Alert[] }>("/api/scan", { method: "POST" }),
  recentSetups: (limit = 25) => request<{ setups: RecordedSetup[] }>(`/api/setups/recent?${q({ limit })}`),
  alerts: (limit = 50) => request<{ alerts: Alert[] }>(`/api/alerts?${q({ limit })}`),
  ackAlert: (id: number) => request<{ ok: boolean }>(`/api/alerts/${id}/ack`, { method: "POST" }),
};
