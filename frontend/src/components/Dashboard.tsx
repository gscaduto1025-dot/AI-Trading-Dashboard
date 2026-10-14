"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { fmtDate, fmtPct, fmtPrice, fmtTime } from "@/lib/format";
import type { AIResult, Alert, Analysis, Bar, Health, Mode, Quote, RecordedSetup } from "@/lib/types";
import AIPanel from "./AIPanel";
import AlertsPanel from "./AlertsPanel";
import BiasPanel from "./BiasPanel";
import LevelsPanel from "./LevelsPanel";
import PriceChart, { type ChartToggles } from "./PriceChart";
import RecentSetups from "./RecentSetups";
import SetupPanel from "./SetupPanel";
import Watchlist from "./Watchlist";
import { DemoTag, Panel } from "./ui";

const FALLBACK_TIMEFRAMES: Record<Mode, string[]> = { day: ["1Min", "5Min", "15Min"], swing: ["1Hour", "1Day"] };
const FALLBACK_DEFAULT: Record<Mode, string> = { day: "5Min", swing: "1Day" };
const TF_LABEL: Record<string, string> = { "1Min": "1m", "5Min": "5m", "15Min": "15m", "1Hour": "1H", "1Day": "1D" };

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

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("SPY");
  const [mode, setMode] = useState<Mode>("day");
  const [timeframe, setTimeframe] = useState("5Min");
  const [quotes, setQuotes] = useState<Quote[] | null>(null);
  const [quotesError, setQuotesError] = useState<string | null>(null);
  const [bars, setBars] = useState<Bar[]>([]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  // Key of the symbol/mode/timeframe whose data is currently shown; loading while it lags.
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  // AI results are tied to the symbol/mode/timeframe they were generated for.
  const [aiState, setAiState] = useState<{ key: string; result: AIResult } | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [setups, setSetups] = useState<RecordedSetup[] | null>(null);
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const [notify, setNotify] = useState(false);
  const [toggles, setToggles] = useState<ChartToggles>({
    emaFast: true,
    emaSlow: true,
    vwap: true,
    smaLong: true,
    levels: true,
    plan: true,
    volume: true,
  });
  const seenAlerts = useRef<number | null>(null);
  const requestId = useRef(0);

  const aiKey = `${symbol}|${mode}|${timeframe}`;
  const ai = aiState?.key === aiKey ? aiState.result : null;
  const loading = loadedKey !== aiKey;
  const isDemo = health?.is_demo ?? true;
  const timeframes = health?.timeframes ?? FALLBACK_TIMEFRAMES;
  // Demo data is static, so poll slowly; real data refreshes more often.
  const refreshMs = isDemo ? 0 : 30_000;

  usePolling(
    () => {
      api
        .health()
        .then((h) => {
          setHealth(h);
          setBackendError(null);
        })
        .catch((e) => setBackendError(errorText(e)));
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
          setQuotesError(null);
        })
        .catch((e) => setQuotesError(errorText(e)));
    },
    isDemo ? 0 : 15_000,
    [isDemo, health !== null],
  );

  const loadChart = useCallback(() => {
    const id = ++requestId.current;
    const key = `${symbol}|${mode}|${timeframe}`;
    Promise.all([api.bars(symbol, timeframe), api.analysis(symbol, mode, timeframe)])
      .then(([b, a]) => {
        if (id !== requestId.current) return; // a newer request superseded this one
        setBars(b.bars);
        setAnalysis(a);
        setAnalysisError(null);
      })
      .catch((e) => {
        if (id === requestId.current) setAnalysisError(errorText(e));
      })
      .finally(() => {
        if (id === requestId.current) setLoadedKey(key);
      });
  }, [symbol, mode, timeframe]);

  useEffect(() => {
    loadChart();
    if (refreshMs <= 0) return;
    const t = setInterval(loadChart, refreshMs);
    return () => clearInterval(t);
  }, [loadChart, refreshMs, health !== null]); // eslint-disable-line react-hooks/exhaustive-deps

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
  const toggleList: [keyof ChartToggles, string, string, boolean][] = [
    ["emaFast", `EMA ${ind?.ema_fast_period ?? (mode === "day" ? 9 : 20)}`, "bg-[#4f9cf9]", true],
    ["emaSlow", `EMA ${ind?.ema_slow_period ?? (mode === "day" ? 21 : 50)}`, "bg-[#c084fc]", true],
    ["vwap", "VWAP", "bg-[#f5b942]", Boolean(analysis?.overlays.vwap)],
    ["smaLong", "SMA 200", "bg-[#94a3b8]", Boolean(analysis?.overlays.sma200)],
    ["levels", "S/R levels", "bg-bull", true],
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
          <span className="num text-lg">{fmtPrice(quote?.price ?? analysis?.price)}</span>
          {quote && (
            <span className={`num text-sm ${quote.change_pct >= 0 ? "text-bull" : "text-bear"}`}>
              {quote.change >= 0 ? "+" : ""}
              {fmtPrice(quote.change)} ({fmtPct(quote.change_pct)})
            </span>
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
              backendError
                ? "text-bear ring-bear/40"
                : isDemo
                  ? "text-wait ring-wait/40"
                  : "text-bull ring-bull/40"
            }`}
            title={backendError ?? `Data source: ${health?.data_mode ?? "…"}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${backendError ? "bg-bear" : isDemo ? "bg-wait" : "bg-bull"}`} />
            {backendError ? "Backend offline" : isDemo ? "Demo feed" : "Alpaca"}
          </span>
        </div>
      </header>

      {backendError && (
        <div className="border-b border-bear/30 bg-bear/10 px-4 py-2 text-xs text-bear">{backendError}</div>
      )}

      {/* Main grid */}
      <main className="grid grid-cols-1 gap-3 p-3 lg:grid-cols-[220px_minmax(0,1fr)_330px] xl:grid-cols-[240px_minmax(0,1fr)_360px]">
        <div className="lg:h-[620px]">
          <Watchlist quotes={quotes} selected={symbol} onSelect={setSymbol} isDemo={isDemo} error={quotesError} />
        </div>

        <div className="h-[480px] sm:h-[560px] lg:h-[620px]">
        <Panel
          title={
            <span className="flex items-center gap-2 normal-case tracking-normal">
              <span className="text-xs font-bold uppercase tracking-[0.12em] text-ink">{symbol}</span>
              <span className="text-faint">
                {analysis
                  ? analysis.timeframe === "1Day"
                    ? `as of ${fmtDate(analysis.as_of)} close`
                    : `as of ${fmtTime(analysis.as_of)} ET`
                  : ""}
              </span>
            </span>
          }
          right={
            <div className="flex rounded-md bg-panel-2 p-0.5 ring-1 ring-line">
              {timeframes[mode].map((tf) => (
                <button
                  key={tf}
                  onClick={() => setTimeframe(tf)}
                  className={`num rounded px-2 py-0.5 text-[11px] font-semibold ${
                    timeframe === tf ? "bg-white/10 text-ink" : "text-muted hover:text-ink"
                  }`}
                >
                  {TF_LABEL[tf] ?? tf}
                </button>
              ))}
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
            <PriceChart bars={bars} analysis={analysis} toggles={toggles} isDemo={isDemo} />
            {loading && (
              <div className="absolute inset-0 flex items-center justify-center bg-bg/40 text-xs text-muted">
                Loading {symbol} {timeframe}…
              </div>
            )}
            {analysisError && !loading && (
              <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-xs text-bear">
                {analysisError}
              </div>
            )}
          </div>
        </Panel>
        </div>

        <div className="max-h-[720px] lg:h-[620px] lg:max-h-none">
          <SetupPanel analysis={analysis} loading={loading} />
        </div>
      </main>

      {/* Bottom panels */}
      <section className="grid grid-cols-1 gap-3 px-3 pb-3 md:grid-cols-2 xl:grid-cols-[1fr_1fr_1.6fr_1.1fr_1.1fr]">
        <div className="max-h-[420px] md:h-[340px] md:max-h-none">
          <BiasPanel analysis={analysis} />
        </div>
        <div className="max-h-[420px] md:h-[340px] md:max-h-none">
          <LevelsPanel analysis={analysis} />
        </div>
        <div className="max-h-[520px] md:col-span-2 md:h-[420px] md:max-h-none xl:col-span-1 xl:h-[340px]">
          <AIPanel
            analysis={analysis}
            result={ai}
            loading={aiLoading}
            aiEnabled={health?.ai_enabled ?? false}
            onGenerate={generateAi}
          />
        </div>
        <div className="max-h-[420px] md:h-[340px] md:max-h-none">
          <RecentSetups setups={setups} onSelect={selectSetup} />
        </div>
        <div className="max-h-[420px] md:h-[340px] md:max-h-none">
          <AlertsPanel alerts={alerts} onAck={ack} notifyEnabled={notify} onToggleNotify={toggleNotify} />
        </div>
      </section>

      <footer className="border-t border-line px-4 py-2.5 text-center text-[10px] text-faint">
        {health?.disclaimer ?? "Educational analysis only — not financial advice."} Charts by{" "}
        <a href="https://www.tradingview.com/" className="underline hover:text-muted" target="_blank" rel="noreferrer">
          TradingView Lightweight Charts
        </a>
        .
      </footer>
    </div>
  );
}
