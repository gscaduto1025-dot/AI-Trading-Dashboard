export type Mode = "day" | "swing";
export type SetupStatus = "ACTIONABLE" | "FORMING" | "NO_TRADE" | "TRIGGERED" | "WATCHING";
export type StrategyStatus = "TRIGGERED" | "WATCHING" | "NO_TRADE";

export type Latency = "real-time" | "delayed" | "historical" | "demo" | "unavailable";
export type Freshness = "fresh" | "stale" | "unavailable" | "demo";
export type SessionKind = "regular" | "extended";

export interface FeedInfo {
  id: "iex" | "sip" | "delayed_sip" | "demo";
  label: string;
  latency: Latency;
  coverage: string;
  delay_seconds: number;
  source: "alpaca" | "demo";
  adjustment: string | null;
}

/** Provenance and quality label attached to every candle response. */
export interface DataStatus {
  source: "alpaca" | "demo";
  feed: string;
  feed_label: string;
  coverage: string;
  latency: Latency;
  freshness: Freshness;
  reason: string | null;
  as_of: string;
  data_cutoff: string;
  latest_bar_time: string | null;
  latest_bar_complete: boolean;
}

export interface MarketStatus {
  phase: "pre-market" | "regular" | "after-hours" | "closed";
  label: string;
  is_open: boolean;
  is_extended_hours: boolean;
  trading_day: boolean;
  holiday: string | null;
  early_close: string | null;
  today: { date: string; open: string; close: string; session_open: string; session_close: string; early_close: boolean } | null;
  next_open: string;
  next_close: string;
  now: string;
  timezone: string;
  calendar_source: "alpaca" | "nyse-rules";
  calendar_error: string | null;
  data_cutoff: string;
}

export interface Quality {
  ok: boolean;
  checked: number;
  dropped: number;
  errors: string[];
  warnings: string[];
  info: string[];
}

export interface CandleResponse {
  symbol: string;
  timeframe: string;
  session: SessionKind;
  is_demo: boolean;
  bars: Bar[];
  last_bar_complete: boolean;
  completed_count: number;
  status: DataStatus;
  quality: Quality;
}

export interface Health {
  status: string;
  data_mode: "demo" | "alpaca";
  is_demo: boolean;
  feed: FeedInfo;
  all_timeframes: { key: string; label: string }[];
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
  available: boolean;
  price: number | null;
  prev_close: number | null;
  change: number | null;
  change_pct: number | null;
  volume: number | null;
  price_time: string | null;
  session_date: string;
  stale: boolean;
  reason: string | null;
  latency: Latency;
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
  data_status: DataStatus;
  analyzed_through: number;
  excluded_forming_candle: boolean;
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

export type LevelCategory = "reference" | "zone" | "swing" | "indicator" | "liquidity" | "breakout";

/** One automatically detected technical level (see backend app/levels). */
export interface KeyLevel {
  id: string;
  ticker: string;
  type: string;
  label: string;
  category: LevelCategory;
  timeframe: string;
  timeframes: string[];
  price: number;
  low: number;
  high: number;
  is_zone: boolean;
  side: "support" | "resistance" | "at price";
  calculated_at: string;
  formed_at: string;
  valid: boolean;
  status: string;
  evidence: { summary: string; details: Record<string, unknown>; events: { time: number; price: number; kind: string; timeframe: string }[] };
  strength: {
    score: number;
    label: "weak" | "moderate" | "strong";
    components: { reactions: number; recency: number; timeframe: number; confluence: number };
    inputs: { reactions: number; days_since_last_reaction: number; highest_timeframe: string; overlapping_levels: string[] };
    note: string;
  };
}

export interface KeyLevelReport {
  ticker: string;
  mode: Mode;
  method_version: string;
  calculated_at: string;
  current_session: string;
  previous_session: string;
  last_price: number;
  timeframes_used: Record<string, { candles: number; from: string | null; to: string | null; label: string }>;
  atr: Record<string, number>;
  adjustment: string | null;
  splits_detected: { date: string; ratio: string }[];
  warnings: string[];
  notes: string[];
  strength_note: string;
  levels: KeyLevel[];
  indicators?: {
    ema9: LinePoint[];
    ema20: LinePoint[];
    ema50: LinePoint[];
    ema200: LinePoint[];
    vwap?: LinePoint[];
    swings: { time: number; price: number; kind: "high" | "low" }[];
  };
  is_demo: boolean;
  replay: boolean;
}

export interface StrategyCondition {
  name: string;
  passed: boolean | null;
  required: boolean;
  detail: string;
  at: number | null;
}

export interface StrategyLevelRef {
  label: string;
  type: string;
  low: number;
  high: number;
  timeframe: string;
  strength: number | null;
}

/** One day-trading setup from the strategy engine (backend app/strategies). */
export interface StrategySetup {
  id: string;
  ticker: string;
  strategy: string;
  strategy_name: string;
  direction: "LONG" | "SHORT";
  timeframe: string;
  status: StrategyStatus;
  htf_bias: { label: string; daily: string; hourly: string; detail: string };
  evaluated_at: string;
  session_date: string;
  session_state: "open" | "closed";
  entry: number | null;
  stop: number | null;
  target1: number | null;
  target2: number | null;
  risk_reward: number | null;
  risk_per_share: number | null;
  supporting_levels: StrategyLevelRef[];
  confirmation: StrategyCondition[];
  invalidation: string[];
  evidence: { time: number; kind: string; price: number }[];
  reasons: string[];
  trigger_time: number | null;
  trigger_time_et: string | null;
  confirmed_at: number | null;
  outcome: { state: string; time: number | null; bars_after_trigger: number | null } | null;
  targets_basis: string[];
  triggers_today: { time: number; entry: number; stop: number; risk_reward: number; accepted: boolean; blocked: string | null }[];
  is_demo: boolean;
}

export interface StrategyEvaluation {
  evaluated_at: string;
  replay: boolean;
  universe: string[];
  timeframes: string[];
  symbols: Record<string, { status: "ok" | "unavailable"; error?: string; gate_reasons?: string[]; triggered?: number; watching?: number }>;
  is_demo: boolean;
  setups: StrategySetup[];
  strategies: { id: string; name: string }[];
}

/** What the chart needs to draw a selected setup (day or swing). */
export interface ChartFocus {
  direction: "LONG" | "SHORT";
  entry: number | null;
  stop: number | null;
  target1: number | null;
  target2: number | null;
  entry_trigger?: number | null;
  entry_zone?: [number, number] | null;
  supporting_levels: StrategyLevelRef[];
  evidence: { time: number; kind: string; price: number }[];
}

export type SwingSortKey = "quality" | "ticker" | "strategy" | "direction" | "risk_reward" | "distance";

export interface SwingKeyLevelRef extends StrategyLevelRef {
  position: "above" | "below" | "at price";
}

/** One swing-trading setup from the swing engine (backend app/swing). */
export interface SwingSetup {
  id: string;
  ticker: string;
  mode: "swing";
  strategy: string;
  strategy_name: string;
  direction: "LONG" | "SHORT";
  timeframe: string;
  status: StrategyStatus;
  quality: { score: number; components: Record<string, number>; note: string };
  market_structure: {
    timeframe?: string;
    structure?: string;
    swing_highs?: number[];
    swing_lows?: number[];
    ema_stack?: string;
    ema20?: number | null;
    ema50?: number | null;
    ema200?: number | null;
    higher_timeframe?: string;
    higher_timeframe_detail?: string;
    supporting?: Record<string, string>;
  };
  daily_levels: SwingKeyLevelRef[];
  weekly_levels: SwingKeyLevelRef[];
  entry_trigger: number | null;
  entry_zone: [number, number] | null;
  entry: number | null;
  stop: number | null;
  target1: number | null;
  target2: number | null;
  risk_reward: number | null;
  risk_per_share: number | null;
  /** Signed % move still needed to reach the trigger (negative = already beyond it). */
  distance_to_trigger_pct: number | null;
  distance_to_trigger_atr: number | null;
  confirmation: StrategyCondition[];
  invalidation: string[];
  evidence: { time: number; kind: string; price: number }[];
  supporting_levels: StrategyLevelRef[];
  reasons: string[];
  targets_basis: string[];
  trigger_time: number | null;
  confirmed_at: number | null;
  outcome: { state: string; time: number | null; bars_after_trigger: number | null } | null;
  triggers: { time: number; entry_trigger: number; entry: number; stop: number; risk_reward: number; accepted: boolean; blocked: string | null }[];
  last_close: number | null;
  last_candle: string | null;
  htf_bias: string;
  evaluated_at: string;
  is_demo: boolean;
}

export interface SwingEvaluation {
  evaluated_at: string;
  replay: boolean;
  mode: "swing";
  universe: string[];
  timeframes: string[];
  supporting_timeframes: string[];
  symbols: Record<string, { status: "ok" | "unavailable"; error?: string; gate_reasons?: string[]; triggered?: number; watching?: number }>;
  sort: SwingSortKey;
  sort_keys: SwingSortKey[];
  is_demo: boolean;
  setups: SwingSetup[];
  strategies: { id: string; name: string }[];
}
