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
  scanner?: { scan_count: number; due: boolean; last_error: string | null; last_finished_at: string | null };
  alerts?: {
    total: number;
    unread: number;
    unread_actionable: number;
    latest_id: number | null;
    webhook: boolean;
    last_run: { at: number; scan_id: number | null; created: number; suppressed: number } | null;
  };
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

/** Deterministic fact sheet the AI explanation is checked against (backend app/ai/facts.py). */
export interface AIStrategyEvidence {
  label: string;
  status: ValidationStatus;
  summary: string;
  notice: string;
  validation_trades?: number | null;
  net_r_per_trade?: number | null;
  net_r_95ci?: [number, number] | null;
}

export interface AIFacts {
  strategy_evidence: AIStrategyEvidence;
  live_entry: {
    signal_state: string | null;
    actionable_now: boolean | null;
    checks_failed: string[];
    volume_confirmation: string | null;
  } | null;
  symbol: string;
  trading_mode: Mode;
  chart_timeframe: string;
  data_timestamp: string;
  last_completed_candle: string | null;
  data_source: string;
  last_price: number;
  market_bias: { label: "bullish" | "bearish" | "neutral"; score: number; max_score: number };
  vwap: { value: number; relation: string; distance_pct: number } | null;
  moving_averages: Record<string, number | string>;
  volume: { relative_volume: number | null; coverage: string | null };
  nearest_support: { label: string; price: number | null; zone_low: number | null; zone_high: number | null } | null;
  nearest_resistance: { label: string; price: number | null; zone_low: number | null; zone_high: number | null } | null;
  setup: {
    name: string;
    direction: "LONG" | "SHORT";
    timeframe: string;
    status: StrategyStatus;
    status_reasons: string[];
  } | null;
}

export interface AIExplanation {
  symbol: string;
  trading_mode: Mode;
  market_bias: "bullish" | "bearish" | "neutral";
  setup_name: string;
  setup_status: StrategyStatus;
  evidence_label: string;
  technical_summary: string;
  market_structure: string;
  risk_reward: string;
  historical_evidence: string;
  key_level_explanations: { level: string; explanation: string }[];
  setup_explanation: string;
  bullish_scenario: string;
  bearish_scenario: string;
  entry_conditions: string[];
  invalidation_conditions: string[];
  risk_explanation: string;
  data_timestamp: string;
}

export interface AIValidation {
  passed: boolean;
  corrections: string[];
  removed: { field: string; text: string; reason: string }[];
  numbers_checked: number;
  numbers_verified: number;
}

export interface AIResponse {
  available: boolean;
  reason?: string;
  cached?: boolean;
  not_generated?: boolean;
  status: StrategyStatus;
  setup_id?: string | null;
  facts: AIFacts | null;
  explanation?: AIExplanation;
  validation?: AIValidation;
  model?: string;
  generated_at?: string;
  is_demo?: boolean;
  budget_blocked?: boolean;
  estimated_cost_usd?: number | null;
  usage?: { input_tokens: number; output_tokens: number; cache_read_input_tokens: number; cache_creation_input_tokens: number };
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

export type AlertType =
  | "approaching_support"
  | "approaching_resistance"
  | "breakout_confirmed"
  | "retest_confirmed"
  | "setup_triggered"
  | "setup_invalidated"
  | "swing_candidate"
  | "target_reached"
  | "stop_reached"
  | "price_level"
  | "data_stale";
export type AlertSeverity = "actionable" | "info" | "warning";

/** One alert from the server-side alert engine (backend app/alerts). */
export interface Alert {
  id: number;
  type: AlertType;
  label: string;
  severity: AlertSeverity;
  /** True only for confirmed, fresh, complete TRIGGERED plans. */
  actionable: boolean;
  ticker: string;
  mode: Mode | null;
  strategy: string | null;
  setup: string | null;
  timeframe: string | null;
  direction: "LONG" | "SHORT" | null;
  price: number | null;
  key_level: {
    label: string;
    low: number | null;
    high: number | null;
    timeframe: string | null;
    strength?: number | null;
    distance_atr?: number | null;
    inside?: boolean;
  } | null;
  status: string | null;
  previous_status: string | null;
  entry_trigger: number | null;
  entry: number | null;
  entry_conditions: { name: string; passed: boolean | null; detail: string | null }[];
  invalidation: string[];
  stop: number | null;
  targets: number[];
  risk_reward: number | null;
  quality: number | null;
  setup_id: string | null;
  trigger_time: number | null;
  candle_time: string | null;
  /** The strategy's historical-validation status (setup alerts only). */
  validation?: StrategyValidation | null;
  feed_status: {
    source: string | null;
    feed: string | null;
    latency: string | null;
    delay_seconds: number | null;
    freshness: string | null;
    reason: string | null;
    as_of: string | null;
    last_candle: string | null;
    timeframe: string | null;
  };
  is_demo: boolean;
  scan_id: number | null;
  timestamp: string;
  title: string;
  message: string;
  /** Server preference: show this alert as a browser notification. */
  browser: boolean;
  acknowledged: boolean;
  created_at: number;
  delivery: string | null;
}

export interface AlertsResponse {
  alerts: Alert[];
  counts: { total: number; unread: number; unread_actionable: number; latest_id: number | null };
  last_run: { at: number; scan_id: number | null; created: number; suppressed: unknown[] } | null;
  is_demo: boolean;
}

export interface AlertPrefs {
  enabled: Record<AlertType, boolean>;
  cooldown_minutes: Record<AlertType, number>;
  modes: Mode[];
  muted_tickers: string[];
  proximity_atr: number;
  swing_candidate_min_quality: number;
  min_quality: number;
  min_risk_reward: number | null;
  browser_types: AlertType[];
  webhook_types: AlertType[];
}

export interface AlertTypeMeta {
  label: string;
  severity: AlertSeverity;
  actionable: boolean;
  cooldown_minutes: number;
  description: string;
}

export interface AlertPrefsResponse {
  preferences: AlertPrefs;
  types: Record<AlertType, AlertTypeMeta>;
  webhook: { configured: boolean; host: string | null };
}

export interface PriceLevel {
  id: number;
  ticker: string;
  price: number;
  condition: "above" | "below";
  note: string;
  created_at: number;
  triggered_at: number | null;
  triggered_price: number | null;
  active: boolean;
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
  triggers_today: { time: number; entry: number; stop: number | null; risk_reward: number | null; accepted: boolean; blocked: string | null }[];
  is_demo: boolean;
  plan: TradePlan | null;
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
  quality: { score: number; components: QualityComponent[]; conflict_penalty: number; conflicts: string[]; note: string };
  plan: TradePlan | null;
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
  triggers: { time: number; entry_trigger: number; entry: number; stop: number | null; risk_reward: number | null; accepted: boolean; blocked: string | null }[];
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

/** A price with the reason it exists (trade-planning engine). */
export interface PlanPrice {
  price: number;
  basis: string;
  source?: string;
  timeframe?: string | null;
  strength?: number | null;
  kind?: string;
}

export interface PlanCheck {
  name: string;
  passed: boolean | null;
  detail: string;
}

export interface QualityComponent {
  name: string;
  weight: number;
  value: number;
  points: number;
  detail: string;
}

/** Complete conditional trade plan from backend app/planning. */
export interface TradePlan {
  setup_id?: string;
  ticker: string;
  mode: "day" | "swing";
  strategy: string;
  strategy_name: string;
  direction: "LONG" | "SHORT";
  timeframe: string;
  status: StrategyStatus;
  reasons: string[];
  entry_trigger: PlanPrice | null;
  entry: PlanPrice | null;
  entry_zone: { low: number; high: number; basis: string } | null;
  stop: (PlanPrice & { anchor: number; buffer_atr: number; buffer: number }) | null;
  target1: PlanPrice | null;
  target2: PlanPrice | null;
  risk_per_share: number | null;
  reward_per_share_t1: number | null;
  reward_per_share_t2: number | null;
  risk_reward_t1: number | null;
  risk_reward_t2: number | null;
  risk_reward_worst_fill: number | null;
  min_rr: number;
  atr: { value: number | null; label: string };
  confirmation: StrategyCondition[];
  invalidation: string[];
  conflicts: string[];
  checks: PlanCheck[];
  calculations: string[];
  signal_time: number | null;
  quality: {
    score: number;
    components: QualityComponent[];
    conflict_penalty: number;
    conflicts: string[];
    note: string;
  } | null;
}

export interface PlansResponse {
  symbol: string;
  mode: "day" | "swing";
  plans: TradePlan[];
  status: StrategyStatus;
  reason: string | null;
  evaluated_at?: string;
  min_rr?: number;
  is_demo: boolean;
}

// ------------------------------------------------------------- opportunity scanner

export interface ScannerRow {
  id: string;
  ticker: string;
  mode: Mode;
  price: number | null;
  market_bias: string | null;
  setup: string;
  strategy: string;
  pattern: "breakout" | "retest" | "pullback" | "reversal" | "other";
  timeframe: string;
  direction: "LONG" | "SHORT";
  side: "bullish" | "bearish";
  entry_trigger: number | null;
  entry: number | null;
  stop: number | null;
  target1: number | null;
  target2: number | null;
  risk_reward: number | null;
  quality: number | null;
  status: StrategyStatus;
  meets_requirements: boolean;
  actionable: boolean;
  /** TRIGGERED: whether it is a fresh entry now (backend scanner entry_checks). */
  signal_state: "CURRENTLY_ACTIONABLE" | "HISTORICALLY_TRIGGERED" | StrategyStatus;
  /** Weakest verification of the signal's required volume conditions. */
  volume_confirmation: "FULL" | "LIMITED" | "UNVERIFIED" | "UNAVAILABLE" | "NOT_REQUIRED";
  entry_checks: { name: string; passed: boolean; detail: string }[];
  live_price: number | null;
  live_price_time: string | null;
  live_risk_reward: number | null;
  live_checks_failed: string[];
  requirement_issues: string[];
  reason: string;
  distance_to_trigger_pct: number | null;
  last_updated: string | null;
  data_freshness: Freshness | "invalid" | null;
  data_as_of: string | null;
  last_candle: string | null;
  /** The strategy's frozen historical-validation status (not a property of this setup). */
  validation: StrategyValidation;
  /** Nearest key-level support/resistance to price (key-level engine). */
  nearest_level: {
    side: "support" | "resistance";
    label: string;
    low: number;
    high: number;
    timeframe: string | null;
    distance_pct: number | null;
    distance_atr: number | null;
    near: boolean;
    inside: boolean;
  } | null;
  /** Consolidated (SIP) average daily dollar volume, last 20 sessions. */
  liquidity_adv: number | null;
}

export type ValidationStatus = "VALIDATED_POSITIVE" | "NEGATIVE_HISTORICAL_EVIDENCE" | "INCONCLUSIVE" | "UNVALIDATED";

export interface StrategyValidation {
  status: ValidationStatus;
  label: string;
  notice: string;
  summary: string;
  n: number;
  net_r: number | null;
  net_ci: [number, number] | null;
  profit_factor: number | null;
  gross_r: number | null;
  timeframe: { timeframe: string; n: number; net_r: number; net_ci: [number, number] | null } | null;
  baseline: string | null;
}

export interface ValidationEvidence {
  n: number;
  win_rate: number;
  win_ci: [number, number] | null;
  exp_r: number;
  exp_ci: [number, number] | null;
  pf: number | null;
  max_dd_r: number;
  gross_exp_r: number;
  mean_cost_r: number;
  verdict: string;
  cost_sensitivity?: Record<string, { exp_r: number; exp_ci: [number, number] | null; pf: number | null; max_dd_r: number }>;
}

export interface ResearchValidation {
  baseline: string | null;
  frozen_on: string;
  rules_hash: string;
  source: string;
  criteria: Record<ValidationStatus, string>;
  costs: string;
  modes: Record<Mode, {
    sample: string;
    independence: string;
    overall: ValidationEvidence;
    preregistered: {
      p_two_sided: Record<string, number | null>;
      p_holm: Record<string, number>;
      h2_htf_aligned: ValidationEvidence;
      h3_quality_ge_70: ValidationEvidence;
    };
  }>;
  strategies: Record<string, {
    mode: Mode;
    status: ValidationStatus;
    label: string;
    summary: string;
    note: string | null;
    evidence: ValidationEvidence | null;
    by_timeframe: Record<string, ValidationEvidence>;
  }>;
}

export interface ScannerPreset {
  id: number;
  name: string;
  filters: ScannerFilters;
  created_at: number;
  updated_at: number;
}

export interface ScannerLevel {
  label: string;
  low: number;
  high: number;
  timeframe: string | null;
  strength: number | null;
  distance_pct: number | null;
  distance_atr: number | null;
  near: boolean;
  inside: boolean;
}

export interface ScannerTicker {
  ticker: string;
  price: number | null;
  change_pct: number | null;
  data: Record<Mode, { freshness: string; reason: string | null; as_of: string | null; last_candle: string | null; timeframe: string }>;
  bias: Partial<Record<Mode, string>>;
  levels: Partial<Record<Mode, { support: ScannerLevel | null; resistance: ScannerLevel | null }>>;
  flags: string[];
  flagged: boolean;
  counts: Record<StrategyStatus, number>;
}

export interface ScannerSnapshot {
  scan_id: number;
  trigger: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  is_demo: boolean;
  feed: FeedInfo;
  market_phase: string;
  watchlist: string[];
  tickers: ScannerTicker[];
  rows: ScannerRow[];
  total_rows: number;
  errors: { symbol: string; mode: string; code: string; error: string }[];
  strategies: string[];
  signature: (string | number)[];
  due: boolean;
  scan_count: number;
  poll_seconds: number;
  last_error: string | null;
}

export interface ScannerFilters {
  mode?: Mode;
  side?: "bullish" | "bearish";
  status?: string;
  min_rr?: number;
  min_quality?: number;
  strategy?: string;
  include_no_trade?: boolean;
  /** Comma lists. */
  signal_state?: string;
  validation?: string;
  volume?: string;
  max_level_atr?: number;
  max_level_pct?: number;
  /** Consolidated average daily dollar volume, USD. */
  min_liquidity?: number;
}

// ------------------------------------------------------------------ options analysis

export interface OptionsFeed {
  id: "indicative" | "opra";
  label: string;
  real_time: boolean;
  actual_quotes: boolean;
  trade_delay_seconds: number;
  requirement: string;
  coverage: string;
}

export interface OptionCheck {
  id: string;
  label: string;
  severity: "block" | "warn" | "info";
  detail: string;
}

export interface OptionEstimate {
  scenario: string;
  reliability: "low" | "moderate";
  underlying: number;
  move: number;
  value: number;
  change_per_contract: number;
  pnl_from_ask_per_contract: number | null;
}

/** One contract as evaluated by the backend. Every price/Greek is Alpaca's value or null. */
export interface OptionContractRow {
  symbol: string;
  type: "call" | "put";
  strike: number;
  expiration: string;
  dte: number;
  monthly: boolean;
  style: string | null;
  multiplier: number;
  bid: number | null;
  ask: number | null;
  bid_size: number | null;
  ask_size: number | null;
  mid: number | null;
  spread: number | null;
  spread_pct: number | null;
  quote_time: number | null;
  quote_age_seconds: number | null;
  last: number | null;
  last_time: number | null;
  volume: number | null;
  volume_session: string | null;
  open_interest: number | null;
  open_interest_date: string | null;
  iv: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  rho: number | null;
  moneyness: "ITM" | "ATM" | "OTM" | null;
  intrinsic: number | null;
  extrinsic: number | null;
  breakeven: number | null;
  premium_per_contract: number | null;
  spread_cost_per_contract: number | null;
  sensitivity: {
    per_1_usd_move: number | null;
    delta_change_per_1_usd: number | null;
    theta_per_day_per_contract: number | null;
    theta_pct_of_mid_per_day: number | null;
    vega_per_vol_point_per_contract: number | null;
    at_stock_target1: OptionEstimate | null;
    at_stock_stop: OptionEstimate | null;
    estimated_option_rr: number | null;
  };
  checks: OptionCheck[];
  eligible: boolean;
  score: { total: number; points: Record<string, number>; weights: Record<string, number>; note: string } | null;
  has_data: boolean;
}

export interface OptionExpiration {
  date: string;
  dte: number;
  weekday: string;
  monthly: boolean;
  in_window: boolean;
  short_dated: boolean;
  calls: number;
  puts: number;
}

export interface OptionsCapabilities {
  provider: string;
  configured: boolean;
  feed: OptionsFeed | null;
  feeds: Record<string, OptionsFeed>;
  fields: Record<string, string[]>;
  restrictions: string[];
  order_execution: false;
}

export interface OptionsAnalysis {
  available: boolean;
  symbol: string;
  mode: Mode;
  reason?: string;
  code?: string;
  capabilities?: OptionsCapabilities;
  order_execution: false;
  feed?: OptionsFeed;
  as_of?: string;
  setup?: {
    id: string;
    strategy: string;
    strategy_name: string;
    direction: "LONG" | "SHORT";
    status: StrategyStatus;
    timeframe: string;
    qualifies: boolean;
  } | null;
  option_type?: "call" | "put";
  type_rationale?: string;
  underlying?: { price: number | null; price_time: string | null; stale: boolean; reason: string | null; latency?: string | null };
  market_open?: boolean;
  stock_risk?: {
    status: StrategyStatus;
    direction: "LONG" | "SHORT";
    entry_trigger: number | null;
    entry: number | null;
    stop: number | null;
    target1: number | null;
    target2: number | null;
    risk_per_share: number | null;
    reward_per_share_t1: number | null;
    risk_reward_t1: number | null;
    risk_per_100_shares: number | null;
    invalidation: string[];
    note: string;
  };
  expirations?: OptionExpiration[];
  selected_expiration?: string | null;
  dte_window?: [number, number];
  contracts?: OptionContractRow[];
  contracts_total?: number;
  candidates?: OptionContractRow[];
  eligible_count?: number;
  blocked_by?: Record<string, number>;
  check_labels?: Record<string, string>;
  recommendation?: { contract: OptionContractRow | null; reasons: string[]; label: string };
  option_risk?: {
    contract: string;
    premium_per_contract: number | null;
    max_loss_per_contract: number | null;
    max_loss_note: string;
    spread_cost_per_contract: number | null;
    breakeven_at_expiration: number | null;
    estimated_at_stock_stop: OptionEstimate | null;
    estimated_at_stock_target1: OptionEstimate | null;
    estimated_option_rr: number | null;
    theta_per_day_per_contract: number | null;
    vega_per_vol_point_per_contract: number | null;
    dte: number;
  } | null;
  risk_notes?: Record<"stop_vs_option" | "time_decay" | "short_dated" | "iv" | "estimates", string[]>;
  data_limitations?: string[];
  thresholds?: {
    max_spread_pct: number;
    min_volume: number;
    min_open_interest: number;
    max_quote_age_seconds: number;
    delta_band: [number, number];
    target_delta: number;
    dte_window: [number, number];
    weights: Record<string, number>;
  };
  chain_fetched_at?: number | null;
}

// ------------------------------------------------------------------------ backtesting

/** Summary statistics for a set of hypothetical trades (backend app/backtest/metrics). */
export interface BacktestStats {
  trades: number;
  sample_note?: string;
  wins?: number;
  losses?: number;
  win_rate?: number | null;
  avg_win_r?: number | null;
  avg_loss_r?: number | null;
  avg_win_pct?: number | null;
  avg_loss_pct?: number | null;
  avg_planned_rr?: number | null;
  realized_win_loss_ratio?: number | null;
  profit_factor?: number | null;
  expectancy_r?: number | null;
  expectancy_pct?: number | null;
  expectancy_ci95_r?: [number | null, number | null] | null;
  std_r?: number | null;
  total_r?: number;
  max_drawdown_r?: number;
  max_drawdown_pct?: number;
  final_equity_pct?: number;
  avg_bars_held?: number;
  avg_cost_r?: number | null;
  exits?: Record<string, number>;
  until?: string;
  from?: string;
  period?: [string, string];
}

export interface BacktestTrade {
  id: string;
  ticker: string;
  mode: Mode;
  strategy: string;
  strategy_name: string;
  timeframe: string;
  direction: "LONG" | "SHORT";
  signal_time: number;
  entry_time: number;
  exit_time: number;
  plan_entry: number | null;
  open: number;
  entry: number;
  stop: number;
  target1: number;
  target2: number | null;
  exit: number;
  exit_reason: string;
  legs: { fraction: number; target: number; exit: number; reason: string; time: number }[];
  risk_per_share: number;
  pnl_per_share: number;
  r: number;
  gross_r: number;
  cost_per_share: number;
  return_pct: number;
  mae_r: number;
  mfe_r: number;
  bars_held: number;
  planned_rr: number | null;
  quality: number | null;
  fees_per_share: number;
  regime?: string;
  shares?: number;
  notional?: number;
  risk_usd?: number;
  pnl_usd?: number;
  equity_after?: number;
  sized_by?: string;
}

export interface BacktestPortfolio {
  starting_capital: number;
  final_equity: number;
  total_pnl: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  max_drawdown_usd: number;
  trades_taken: number;
  skipped: Record<string, number>;
  max_concurrent: number;
  curve: { time: number; equity: number; drawdown_pct: number }[];
  assumptions: string;
}

export interface BacktestTradeChart {
  trade: BacktestTrade;
  timeframe: string;
  data_source: string;
  is_demo: boolean;
  simulated: true;
  bars: { time: number; open: number; high: number; low: number; close: number; volume: number }[];
}

export interface BacktestReport {
  filters: Record<string, string | null>;
  overall: BacktestStats;
  in_sample: BacktestStats;
  out_of_sample: BacktestStats;
  walk_forward: {
    folds: {
      fold: number;
      train: [string, string];
      test_period: [string, string];
      train_trades: number;
      min_quality: number;
      train_expectancy_r: number | null;
      test: BacktestStats;
    }[];
    oos: BacktestStats;
    method: string;
  };
  by_ticker: Record<string, BacktestStats>;
  by_strategy: Record<string, BacktestStats>;
  by_timeframe: Record<string, BacktestStats>;
  by_direction: Record<string, BacktestStats>;
  by_mode: Record<string, BacktestStats>;
  by_regime: Record<string, BacktestStats>;
  portfolio: BacktestPortfolio;
  quality: {
    trades: number;
    spearman: number | null;
    t_stat: number | null;
    buckets: { range: string; trades: number; win_rate: number | null; expectancy_r: number | null }[];
    verdict: string;
  };
  equity: { points: { time: number; equity: number; drawdown_pct: number }[]; max_drawdown_pct: number; final_return_pct: number };
  verdict: { label: string; positive_evidence: boolean; reasons: string[] };
  skipped_reasons: Record<string, number>;
  trades: BacktestTrade[];
}

export interface BacktestRequest {
  mode: Mode | "both";
  data_source: "market" | "bundled";
  symbols?: string[];
  start: string;
  end: string;
  settings?: Record<string, unknown>;
  universe?: { symbol: string; start?: string; end?: string }[] | null;
}

export interface BacktestRunRow {
  id: string;
  created_at: number;
  finished_at: number | null;
  status: "running" | "done" | "failed" | "interrupted";
  request: BacktestRequest;
  summary: {
    trades: number;
    win_rate: number | null;
    expectancy_r: number | null;
    profit_factor: number | null;
    verdict: string;
    is_demo: boolean;
    duration_s: number;
  } | null;
  error: string | null;
}

export interface BacktestRunDetail extends BacktestRunRow {
  live?: { progress: number; message: string };
  run?: {
    mode: Mode | "both";
    regime_basis?: string;
    symbols: string[];
    start: string;
    end: string;
    data_source: string;
    data_description: string;
    is_demo: boolean;
    survivorship_biased: boolean;
    settings: Record<string, unknown>;
    not_live: { ticker: string; strategy: string; timeframe: string; direction: string; confirmed_at: number; reason: string }[];
    data_integrity: { symbol: string; timeframe: string; bars: number; first: string | null; last: string | null; issues: string[] }[];
    data_errors: { symbol: string; error: string }[];
    engine_evaluations: number;
    limitations: string[];
  };
  report?: BacktestReport;
  skipped?: { ticker: string; strategy: string; timeframe: string; direction: string; seen_at: number; reason: string }[];
}

export interface BacktestDefaults {
  settings: Record<string, unknown> & {
    half_spread_bps: Record<Mode, number>;
    slippage_bps: Record<Mode, number>;
    commission_per_share: number;
    entry_delay_bars: number;
    exit_policy: string;
    max_hold_bars: Record<Mode, number | null>;
  };
  limitations: string[];
  max_days: Record<Mode, number>;
  sources: Record<"market" | "bundled", { label: string; is_demo: boolean; modes: (Mode | "both")[]; symbols: string[] }>;
  order_execution: false;
}

// ------------------------------------------------------------------------- trade log

export type JournalAccount = "actual" | "paper";

export interface JournalFill {
  id: number;
  trade_id: number;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  fees: number;
  executed_at: number;
  note: string;
}

export interface JournalPnl {
  status: "open" | "closed" | "empty" | "invalid";
  open_quantity: number;
  avg_entry: number | null;
  avg_exit: number | null;
  entered_quantity: number;
  exited_quantity: number;
  max_position: number;
  partial_exits: number;
  realized_gross: number;
  fees: number;
  realized_net: number;
  unrealized: number | null;
  initial_risk: number | null;
  r_multiple: number | null;
  opened_at: number | null;
  closed_at: number | null;
}

export interface JournalTrade {
  id: number;
  account: JournalAccount;
  ticker: string;
  direction: "LONG" | "SHORT";
  strategy: string | null;
  setup_id: string | null;
  planned_stop: number | null;
  planned_target: number | null;
  notes: string;
  fills: JournalFill[];
  pnl: JournalPnl;
  problem: string | null;
  mark_note: string | null;
}

export interface JournalResponse {
  account: JournalAccount;
  trades: JournalTrade[];
  stats: {
    closed_trades: number;
    open_trades: number;
    win_rate: number | null;
    net_pnl: number;
    fees: number;
    avg_win: number | null;
    avg_loss: number | null;
    profit_factor: number | null;
    expectancy: number | null;
    avg_r: number | null;
    r_trades: number;
    sample_note: string;
  };
  simulated: false;
  order_execution: false;
}
