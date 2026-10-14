export type Mode = "day" | "swing";
export type SetupStatus = "ACTIONABLE" | "FORMING" | "NO_TRADE";

export interface Health {
  status: string;
  data_mode: "demo" | "alpaca";
  is_demo: boolean;
  ai_enabled: boolean;
  ai_model: string | null;
  watchlist: string[];
  timeframes: Record<Mode, string[]>;
  default_timeframes: Record<Mode, string>;
  last_scan: number | null;
  scan_interval_seconds: number;
  live_trading: boolean;
  disclaimer: string;
}

export interface Quote {
  symbol: string;
  price: number;
  prev_close: number;
  change: number;
  change_pct: number;
  as_of: number;
}

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Level {
  price: number;
  kind: "support" | "resistance";
  source: string;
  touches: number;
  strength: number;
  last_touch: number | null;
}

export interface Check {
  label: string;
  passed: boolean;
  required: boolean;
  detail: string;
}

export interface TradeSetup {
  status: SetupStatus;
  setup_type: string;
  title: string;
  direction: "long" | "short" | null;
  entry: number | null;
  stop: number | null;
  targets: number[];
  risk_reward: number | null;
  risk_per_share: number | null;
  confidence: number;
  triggered: boolean;
  checklist: Check[];
  reasons: string[];
  invalidation: string;
}

export interface BiasFactor {
  label: string;
  value: number;
  signal: "bullish" | "bearish" | "neutral";
  detail: string;
}

export interface Bias {
  label: "Bullish" | "Bearish" | "Neutral";
  score: number;
  max_score: number;
  strength: number;
  factors: BiasFactor[];
}

export interface LinePoint {
  time: number;
  value: number;
}

export interface Analysis {
  symbol: string;
  mode: Mode;
  timeframe: string;
  as_of: number;
  price: number;
  is_demo: boolean;
  indicators: {
    atr: number;
    rsi: number;
    ema_fast: number;
    ema_slow: number;
    ema_fast_period: number;
    ema_slow_period: number;
    vwap: number | null;
    sma_long: number | null;
    volume_ratio: number;
  };
  bias: Bias;
  levels: Level[];
  setup: TradeSetup;
  alternatives: TradeSetup[];
  overlays: Record<string, LinePoint[]>;
}

export interface AIResult {
  available: boolean;
  reason?: string;
  cached?: boolean;
  as_of?: number;
  headline?: string;
  verdict?: "TAKE_SETUP" | "WAIT" | "NO_TRADE";
  summary?: string;
  bullish_case?: string[];
  bearish_case?: string[];
  key_levels_commentary?: string;
  what_to_watch?: string[];
  risk_notes?: string[];
  model?: string;
  engine_status?: SetupStatus;
  is_demo?: boolean;
}

export interface RecordedSetup {
  id: number;
  symbol: string;
  mode: Mode;
  timeframe: string;
  bar_time: number;
  status: SetupStatus;
  setup_type: string;
  direction: "long" | "short" | null;
  title: string;
  entry: number | null;
  stop: number | null;
  target1: number | null;
  target2: number | null;
  risk_reward: number | null;
  confidence: number | null;
  is_demo: boolean;
  detected_at: number;
}

export interface Alert {
  id: number;
  symbol: string;
  mode: Mode;
  timeframe: string;
  status: SetupStatus;
  direction: "long" | "short" | null;
  title: string;
  message: string;
  is_demo: boolean;
  created_at: number;
  acknowledged: boolean;
}
