"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, UNAVAILABLE, api } from "@/lib/api";
import { fmtPct, fmtPrice } from "@/lib/format";
import type {
  AIResult,
  Alert,
  Analysis,
  CandleResponse,
  FeedInfo,
  Health,
  KeyLevelReport,
  MarketStatus,
  Mode,
  Quote,
  RecordedSetup,
  SessionKind,
  StrategyEvaluation,
  StrategySetup,
} from "@/lib/types";
import AIPanel from "./AIPanel";
import AlertsPanel from "./AlertsPanel";
import BiasPanel from "./BiasPanel";
import LevelsPanel from "./LevelsPanel";
import MarketStatusBar from "./MarketStatusBar";
import PriceChart, { type ChartToggles } from "./PriceChart";
import RecentSetups from "./RecentSetups";
import SetupPanel from "./SetupPanel";
import StrategyDetail from "./StrategyDetail";
import StrategySetupsPanel from "./StrategySetupsPanel";
import Watchlist from "./Watchlist";
import { DataUnavailable, DemoTag, Panel } from "./ui";

const ALL_TIMEFRAMES = ["1Min", "5Min", "15Min", "30Min", "1Hour", "4Hour", "1Day", "1Week"];
const FALLBACK_DEFAULT: Record<Mode, string> = { day: "5Min", swing: "1Day" };
const DEFAULT_SYMBOLS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN"];
const TF_LABEL: Record<string, string> = {
  "1Min": "1m",
  "5Min": "5m",
  "15Min": "15m",
  "30Min": "30m",
  "1Hour": "1H",
  "4Hour": "4H",
  "1Day": "1D",
  "1Week": "1W",
};
const INTRADAY = new Set(["1Min", "5Min", "15Min", "30Min", "1Hour", "4Hour"]);

const errorText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

function usePolling(fn: () => void, ms: number, deps: unknown[]) {
  useEffect(() => {
    fn();
    if (ms <= 0) return;
    const id = setInterval(fn, ms);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** How often to refresh charts: fast while any session is live, slow when closed. */
function refreshInterval(isDemo: boolean, phase: MarketStatus["phase"] | undefined, timeframe: string) {
  if (isDemo) return 0; // demo data never changes
  if (!phase || phase === "closed") return 120_000;
  return timeframe === "1Min" ? 10_000 : 20_000;
}

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("SPY");
  const [mode, setMode] = useState<Mode>("day");
  const [timeframe, setTimeframe] = useState("5Min");
  const [session, setSession] = useState<SessionKind>("regular");
  const [quotes, setQuotes] = useState<Quote[] | null>(null);
  const [quotesError, setQuotesError] = useState<string | null>(null);
  const [market, setMarket] = useState<MarketStatus | null>(null);
  const [feed, setFeed] = useState<FeedInfo | null>(null);
  const [candles, setCandles] = useState<CandleResponse | null>(null);
  const [candlesError, setCandlesError] = useState<ApiError | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [keyLevels, setKeyLevels] = useState<KeyLevelReport | null>(null);
  const [levelsError, setLevelsError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<{ title: string; message: string } | null>(null);
  // Key of the view whose data is currently shown; loading while it lags behind.
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  // AI results are tied to the symbol/mode/timeframe they were generated for.
  const [aiState, setAiState] = useState<{ key: string; result: AIResult } | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [setups, setSetups] = useState<RecordedSetup[] | null>(null);
  const [strategies, setStrategies] = useState<StrategyEvaluation | null>(null);
  const [strategiesError, setStrategiesError] = useState<string | null>(null);
  const [selectedSetupId, setSelectedSetupId] = useState<string | null>(null);
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const [notify, setNotify] = useState(false);
  const [toggles, setToggles] = useState<ChartToggles>({
    zones: true,
    liquidity: true,
    prevHighLow: true,
    session: true,
    vwap: true,
    ema9: true,
    ema20: true,
    ema50: false,
    ema200: true,
    swings: true,
    breakouts: true,
    plan: true,
    volume: true,
  });
  const seenAlerts = useRef<number | null>(null);
  const requestId = useRef(0);

  const intraday = INTRADAY.has(timeframe);
  const effectiveSession: SessionKind = intraday ? session : "regular";
  const viewKey = `${symbol}|${mode}|${timeframe}|${effectiveSession}`;
  const aiKey = `${symbol}|${mode}|${timeframe}`;
  const ai = aiState?.key === aiKey ? aiState.result : null;
  const loading = loadedKey !== viewKey;
  const isDemo = health?.is_demo ?? true;
  const timeframes = health?.all_timeframes.map((t) => t.key) ?? ALL_TIMEFRAMES;
  const suggested = new Set(health?.timeframes[mode] ?? []);
  const phase = market?.phase;
  const chartRefreshMs = refreshInterval(isDemo, phase, timeframe);
  const quoteRefreshMs = isDemo ? 0 : phase === "closed" ? 60_000 : 10_000;

  usePolling(
    () => {
      api
        .health()
        .then((h) => {
          setHealth(h);
          setFeed(h.feed);
          setBackendError(null);
        })
        .catch((e) => setBackendError(errorText(e)));
      api
        .marketStatus()
        .then((r) => setMarket(r.market))
        .catch(() => {});
    },
    30_000,
    [],
  );

  usePolling(
    () => {
      api
        .watchlist()
        .then((r) => {
          setQuotes(r.quotes);
          setMarket(r.market);
          setQuotesError(null);
        })
        .catch((e) => {
          // Never keep showing old prices as if they were current.
          setQuotes(null);
          setQuotesError(errorText(e));
        });
    },
    quoteRefreshMs,
    [quoteRefreshMs, health !== null],
  );

  const loadChart = useCallback(() => {
    const id = ++requestId.current;
    const key = viewKey;
    Promise.allSettled([
      api.bars(symbol, timeframe, effectiveSession),
      api.analysis(symbol, mode, timeframe),
      api.levels(symbol, mode, timeframe),
    ])
      .then(([b, a, k]) => {
        if (id !== requestId.current) return; // a newer request superseded this one
        if (b.status === "fulfilled") {
          setCandles(b.value);
          setCandlesError(null);
        } else {
          setCandles(null);
          setCandlesError(
            b.reason instanceof ApiError ? b.reason : new ApiError(errorText(b.reason), 0, "ERROR", UNAVAILABLE),
          );
        }
        if (k.status === "fulfilled") {
          setKeyLevels(k.value);
          setLevelsError(null);
        } else {
          setKeyLevels(null);
          setLevelsError(errorText(k.reason));
        }
        if (a.status === "fulfilled") {
          setAnalysis(a.value);
          setBlocked(null);
        } else {
          const err = a.reason as ApiError;
          setAnalysis(null);
          setBlocked({
            title: err.display ?? (err.code === "BACKEND_OFFLINE" ? "BACKEND OFFLINE" : UNAVAILABLE),
            message: err.message,
          });
        }
      })
      .finally(() => {
        if (id === requestId.current) setLoadedKey(key);
      });
  }, [symbol, mode, timeframe, effectiveSession, viewKey]);

  useEffect(() => {
    loadChart();
    if (chartRefreshMs <= 0) return;
    const t = setInterval(loadChart, chartRefreshMs);
    return () => clearInterval(t);
  }, [loadChart, chartRefreshMs, health !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  // Strategy engine: re-evaluated server-side when a new 5m/15m candle closes.
  const strategyRefreshMs = isDemo ? 60_000 : phase === "closed" ? 120_000 : 20_000;
  usePolling(
    () => {
      api
        .strategySetups()
        .then((r) => {
          setStrategies(r);
          setStrategiesError(null);
        })
        .catch((e) => setStrategiesError(errorText(e)));
    },
    strategyRefreshMs,
    [strategyRefreshMs, health !== null],
  );
  // Resolve the selection against the latest evaluation so its status stays current.
  const selectedSetup: StrategySetup | null =
    strategies?.setups.find((x) => x.id === selectedSetupId) ?? null;

  const selectStrategySetup = (st: StrategySetup) => {
    setSelectedSetupId(st.id);
    setSymbol(st.ticker);
    setMode("day");
    setTimeframe(st.timeframe);
    setSession("regular");
  };

  const loadFeeds = useCallback(() => {
    api.recentSetups(30).then((r) => setSetups(r.setups)).catch(() => {});
    api
      .alerts(50)
      .then((r) => {
        setAlerts(r.alerts);
        const newest = r.alerts[0]?.id ?? 0;
        if (seenAlerts.current !== null && newest > seenAlerts.current && notify) {
          r.alerts
            .filter((a) => a.id > (seenAlerts.current ?? 0) && a.status === "ACTIONABLE")
            .forEach((a) => {
              try {
                new Notification(`TradeScope: ${a.symbol} actionable${a.is_demo ? " (DEMO)" : ""}`, {
                  body: a.message,
                });
              } catch {
                /* notifications unsupported */
              }
            });
        }
        seenAlerts.current = newest;
      })
      .catch(() => {});
  }, [notify]);

  usePolling(loadFeeds, 20_000, [loadFeeds]);

  const changeMode = (m: Mode) => {
    setMode(m);
    setTimeframe((health?.default_timeframes ?? FALLBACK_DEFAULT)[m]);
  };

  const selectSetup = (s: string, m: Mode, tf: string) => {
    setSymbol(s);
    setMode(m);
    setTimeframe(tf);
  };

  const runScan = async () => {
    setScanning(true);
    try {
      await api.scan();
      loadFeeds();
    } catch (e) {
      setBackendError(errorText(e));
    } finally {
      setScanning(false);
    }
  };

  const generateAi = async (refresh: boolean) => {
    setAiLoading(true);
    const key = aiKey;
    try {
      setAiState({ key, result: await api.ai(symbol, mode, timeframe, refresh) });
    } catch (e) {
      setAiState({ key, result: { available: false, reason: errorText(e) } });
    } finally {
      setAiLoading(false);
    }
  };

  const ack = async (id: number) => {
    await api.ackAlert(id).catch(() => {});
    loadFeeds();
  };

  const toggleNotify = async () => {
    if (notify) return setNotify(false);
    if (typeof Notification === "undefined") return;
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    setNotify(perm === "granted");
  };

  const quote = quotes?.find((q) => q.symbol === symbol);
  const ind = analysis?.indicators;
  const stale = candles?.status.freshness === "stale";
  const dataDown = Boolean(quotesError) || Boolean(candlesError && candlesError.code !== "INVALID_REQUEST");
  const hasVwap = Boolean(keyLevels?.indicators?.vwap?.length);
  const toggleList: [keyof ChartToggles, string, string, boolean][] = [
    ["zones", "S/R zones", "bg-gradient-to-b from-bear/70 to-bull/70", true],
    ["prevHighLow", "PDH/PDL · PWH/PWL", "bg-[#22d3ee]", true],
    ["session", "Open · OR", "bg-[#cbd5e1]", mode === "day"],
    ["vwap", "VWAP", "bg-[#f5b942]", hasVwap],
    ["ema9", "EMA 9", "bg-[#4f9cf9]", true],
    ["ema20", "EMA 20", "bg-[#c084fc]", true],
    ["ema50", "EMA 50", "bg-[#2dd4bf]", true],
    ["ema200", "EMA 200", "bg-[#f1f5f9]", true],
    ["swings", "Swings", "bg-[#94a3b8]", true],
    ["liquidity", "Liquidity", "bg-[#c084fc]/60", true],
    ["breakouts", "Breakouts", "bg-bull", true],
    ["plan", "Trade plan", "bg-accent", true],
    ["volume", "Volume", "bg-muted", true],
  ];


  return (
    <div className="flex min-h-screen flex-col">
      {/* Demo banner */}
      {isDemo && (
        <div className="border-b border-wait/30 bg-wait/10 px-4 py-1.5 text-center text-[11px] font-medium text-wait">
          <span className="font-bold uppercase tracking-wider">Demo data</span> — prices, charts,
          setups and alerts below are generated from simulated data, not real market prices. Add
          Alpaca keys in <code className="num">backend/.env</code> to use real market data.
        </div>
      )}

      {/* Header */}
      <header className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line bg-panel px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-to-br from-accent to-bull text-sm font-black text-bg">
            T
          </div>
          <div className="leading-tight">
            <h1 className="text-sm font-bold tracking-wide">
              TradeScope <span className="text-accent">AI</span>
            </h1>
            <p className="text-[10px] text-faint">Setup scanner · analysis only, no trade execution</p>
          </div>
        </div>

        <div className="flex items-baseline gap-3">
          <span className="text-lg font-bold">{symbol}</span>
          {quote?.available ? (
            <>
              <span className="num text-lg">{fmtPrice(quote.price)}</span>
              <span className={`num text-sm ${(quote.change ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>
                {(quote.change ?? 0) >= 0 ? "+" : ""}
                {fmtPrice(quote.change)} ({fmtPct(quote.change_pct)})
              </span>
            </>
          ) : quotesError || (quotes && !quote?.available) ? (
            <span className="text-xs font-black uppercase tracking-[0.15em] text-bear">{UNAVAILABLE}</span>
          ) : (
            <span className="num text-lg text-faint">—</span>
          )}
          {isDemo && <DemoTag />}
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="flex rounded-md bg-panel-2 p-0.5 ring-1 ring-line" role="tablist" aria-label="Trading style">
            {(["day", "swing"] as Mode[]).map((m) => (
              <button
                key={m}
                role="tab"
                aria-selected={mode === m}
                onClick={() => changeMode(m)}
                className={`rounded px-3 py-1 text-xs font-semibold transition ${
                  mode === m ? "bg-accent text-bg" : "text-muted hover:text-ink"
                }`}
              >
                {m === "day" ? "Day Trading" : "Swing Trading"}
              </button>
            ))}
          </div>
          <button
            onClick={runScan}
            disabled={scanning}
            className="rounded-md px-3 py-1.5 text-xs font-semibold text-ink ring-1 ring-line-strong transition hover:bg-white/5 disabled:opacity-50"
          >
            {scanning ? "Scanning…" : "Scan watchlist"}
          </button>
          <span
            className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] font-semibold uppercase ring-1 ${
              backendError || dataDown ? "text-bear ring-bear/40" : isDemo ? "text-wait ring-wait/40" : "text-bull ring-bull/40"
            }`}
            title={backendError ?? quotesError ?? feed?.coverage ?? ""}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${backendError || dataDown ? "bg-bear" : isDemo ? "bg-wait" : "bg-bull"}`}
            />
            {backendError
              ? "Backend offline"
              : isDemo
                ? "Demo feed"
                : `Alpaca ${feed?.label ?? ""}${dataDown ? " · unavailable" : ""}`}
          </span>
        </div>
      </header>

      <MarketStatusBar
        market={market}
        feed={feed}
        candles={candles}
        candlesError={candlesError?.message ?? null}
        quote={quote}
      />

      {backendError && (
        <div className="border-b border-bear/30 bg-bear/10 px-4 py-2 text-xs text-bear">{backendError}</div>
      )}

      {/* Main grid */}
      <main className="grid grid-cols-1 gap-3 p-3 lg:grid-cols-[220px_minmax(0,1fr)_330px] xl:grid-cols-[240px_minmax(0,1fr)_360px]">
        <div className="lg:h-[620px]">
          <Watchlist
            quotes={quotes}
            selected={symbol}
            onSelect={(sym) => {
              setSelectedSetupId(null);
              setSymbol(sym);
            }}
            isDemo={isDemo}
            error={quotesError}
            symbols={health?.watchlist ?? DEFAULT_SYMBOLS}
          />
        </div>

        <div className="h-[480px] sm:h-[560px] lg:h-[620px]">
          <Panel
            title={
              <span className="flex items-center gap-2 normal-case tracking-normal">
                <span className="text-xs font-bold uppercase tracking-[0.12em] text-ink">{symbol}</span>
                <span className="text-faint">{TF_LABEL[timeframe]}</span>
                {intraday && <span className="text-faint">· {effectiveSession === "regular" ? "RTH" : "ETH"}</span>}
              </span>
            }
            right={
              <div className="flex items-center gap-1.5">
                {intraday && (
                  <div className="flex rounded-md bg-panel-2 p-0.5 ring-1 ring-line" title="Regular or extended trading hours">
                    {(["regular", "extended"] as SessionKind[]).map((s) => (
                      <button
                        key={s}
                        onClick={() => setSession(s)}
                        className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                          session === s ? "bg-white/10 text-ink" : "text-muted hover:text-ink"
                        }`}
                      >
                        {s === "regular" ? "RTH" : "ETH"}
                      </button>
                    ))}
                  </div>
                )}
                <div className="flex flex-wrap rounded-md bg-panel-2 p-0.5 ring-1 ring-line">
                  {timeframes.map((tf) => (
                    <button
                      key={tf}
                      onClick={() => setTimeframe(tf)}
                      title={suggested.has(tf) ? `Suggested for ${mode} trading` : undefined}
                      className={`num rounded px-1.5 py-0.5 text-[11px] font-semibold sm:px-2 ${
                        timeframe === tf
                          ? "bg-white/10 text-ink"
                          : suggested.has(tf)
                            ? "text-muted hover:text-ink"
                            : "text-faint hover:text-ink"
                      }`}
                    >
                      {TF_LABEL[tf] ?? tf}
                    </button>
                  ))}
                </div>
              </div>
            }
            bodyClassName="flex flex-col"
          >
            <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-3 py-1.5">
              {toggleList
                .filter(([, , , available]) => available)
                .map(([key, label, swatch]) => (
                  <button
                    key={key}
                    onClick={() => setToggles((t) => ({ ...t, [key]: !t[key] }))}
                    className={`flex items-center gap-1.5 rounded px-2 py-0.5 text-[11px] ring-1 transition ${
                      toggles[key] ? "text-ink ring-line-strong" : "text-faint ring-line opacity-60"
                    }`}
                  >
                    <span className={`h-2 w-2 rounded-sm ${swatch}`} />
                    {label}
                  </button>
                ))}
              {ind && (
                <span className="num ml-auto hidden text-[11px] text-faint md:inline">
                  RSI {ind.rsi.toFixed(1)} · ATR {fmtPrice(ind.atr)}
                  {ind.vwap !== null && ` · VWAP ${fmtPrice(ind.vwap)}`}
                </span>
              )}
            </div>
            <div className="relative min-h-0 flex-1">
              <PriceChart
                bars={candles?.bars ?? []}
                analysis={analysis}
                keyLevels={keyLevels}
                focus={selectedSetup && selectedSetup.ticker === symbol && selectedSetup.timeframe === timeframe ? selectedSetup : null}
                toggles={toggles}
                isDemo={isDemo}
                lastComplete={candles?.last_bar_complete ?? true}
                viewKey={viewKey}
              />
              {stale && !loading && (
                <div className="absolute left-3 top-2 max-w-[80%] rounded bg-bear/15 px-2 py-1 text-[11px] text-bear ring-1 ring-bear/40">
                  <span className="font-bold uppercase">Stale data</span> — {candles?.status.reason}
                </div>
              )}
              {loading && (
                <div className="absolute inset-0 flex items-center justify-center bg-bg/40 text-xs text-muted">
                  Loading {symbol} {TF_LABEL[timeframe]}…
                </div>
              )}
              {candlesError && !loading && (
                <div className="absolute inset-0 flex items-center justify-center bg-bg/80">
                  <DataUnavailable
                    title={candlesError.code === "BACKEND_OFFLINE" ? "BACKEND OFFLINE" : UNAVAILABLE}
                    message={candlesError.message}
                  />
                </div>
              )}
            </div>
          </Panel>
        </div>

        <div className="max-h-[720px] lg:h-[620px] lg:max-h-none">
          {selectedSetup ? (
            <StrategyDetail setup={selectedSetup} onClose={() => setSelectedSetupId(null)} />
          ) : (
            <SetupPanel analysis={analysis} loading={loading} blocked={loading ? null : blocked} />
          )}
        </div>
      </main>

      {/* Bottom panels: context row, then signals row */}
      <section className="grid grid-cols-1 gap-3 px-3 pb-3 md:grid-cols-2 xl:grid-cols-12">
        <div className="max-h-[420px] md:h-[340px] md:max-h-none xl:col-span-3">
          <BiasPanel analysis={analysis} />
        </div>
        <div className="max-h-[420px] md:h-[340px] md:max-h-none xl:col-span-4">
          <LevelsPanel report={keyLevels} error={loading ? null : levelsError} />
        </div>
        <div className="max-h-[520px] md:col-span-2 md:h-[420px] md:max-h-none xl:col-span-5 xl:h-[340px]">
          <AIPanel
            analysis={analysis}
            result={ai}
            loading={aiLoading}
            aiEnabled={health?.ai_enabled ?? false}
            onGenerate={generateAi}
          />
        </div>
        <div className="max-h-[520px] md:col-span-2 md:h-[380px] md:max-h-none xl:col-span-6">
          <StrategySetupsPanel
            data={strategies}
            error={strategiesError}
            selectedId={selectedSetupId}
            onSelect={selectStrategySetup}
          />
        </div>
        <div className="max-h-[420px] md:h-[380px] md:max-h-none xl:col-span-3">
          <RecentSetups setups={setups} onSelect={selectSetup} />
        </div>
        <div className="max-h-[420px] md:h-[380px] md:max-h-none xl:col-span-3">
          <AlertsPanel alerts={alerts} onAck={ack} notifyEnabled={notify} onToggleNotify={toggleNotify} />
        </div>
      </section>

      <footer className="border-t border-line px-4 py-2.5 text-center text-[10px] text-faint">
        {health?.disclaimer ?? "Educational analysis only — not financial advice."}{" "}
        {feed?.source === "alpaca" && <>Market data from Alpaca ({feed.label}). </>}
        Charts by{" "}
        <a href="https://www.tradingview.com/" className="underline hover:text-muted" target="_blank" rel="noreferrer">
          TradingView Lightweight Charts
        </a>
        .
      </footer>
    </div>
  );
}
