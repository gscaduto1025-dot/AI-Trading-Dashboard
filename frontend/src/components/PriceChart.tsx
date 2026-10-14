"use client";

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Analysis, Bar, KeyLevel, KeyLevelReport, StrategySetup } from "@/lib/types";
import { type Band, ZoneBands } from "./chartPrimitives";

export interface ChartToggles {
  zones: boolean;
  liquidity: boolean;
  prevHighLow: boolean;
  session: boolean;
  vwap: boolean;
  ema9: boolean;
  ema20: boolean;
  ema50: boolean;
  ema200: boolean;
  swings: boolean;
  breakouts: boolean;
  plan: boolean;
  volume: boolean;
}

/** One visual language per level family (also used by the legend chips). */
export const LEVEL_STYLE = {
  support: { fill: "rgba(34,197,94,0.10)", strong: "rgba(34,197,94,0.17)", border: "rgba(34,197,94,0.55)" },
  resistance: { fill: "rgba(239,68,68,0.10)", strong: "rgba(239,68,68,0.17)", border: "rgba(239,68,68,0.55)" },
  liquidity: { fill: "rgba(192,132,252,0.10)", border: "rgba(192,132,252,0.7)" },
  pd: "#22d3ee", // previous day high/low
  pw: "#fb923c", // previous week high/low
  session: "#cbd5e1", // session open + opening range
  vwap: "#f5b942",
  ema9: "#4f9cf9",
  ema20: "#c084fc",
  ema50: "#2dd4bf",
  ema200: "#f1f5f9",
  breakout: "#22c55e",
  breakdown: "#ef4444",
  swing: "#94a3b8",
};

const COLORS = {
  bull: "#22c55e",
  bear: "#ef4444",
  grid: "#121a24",
  text: "#8494a7",
  border: "#1c2633",
};

const ts = (t: number) => t as UTCTimestamp;
type LineKey = "ema9" | "ema20" | "ema50" | "ema200" | "vwap";

const MAX_ZONES_PER_SIDE = 3;
const MIN_ZONE_SCORE = 35;
const MAX_SWING_MARKERS = 12;

/** Nearest qualifying zones each side of price (the panel lists everything). */
function nearest(levels: KeyLevel[], price: number, perSide: number): KeyLevel[] {
  const above = levels.filter((l) => l.low > price).sort((a, b) => a.low - b.low);
  const below = levels.filter((l) => l.high < price).sort((a, b) => b.high - a.high);
  const inside = levels.filter((l) => l.low <= price && l.high >= price);
  return [...inside, ...above.slice(0, perSide), ...below.slice(0, perSide)];
}

function zoneBands(report: KeyLevelReport, toggles: ChartToggles): Band[] {
  const bands: Band[] = [];
  const price = report.last_price;
  if (toggles.zones) {
    const zones = report.levels.filter((l) => l.valid && l.category === "zone" && l.strength.score >= MIN_ZONE_SCORE);
    for (const l of nearest(zones, price, MAX_ZONES_PER_SIDE)) {
      const sty = l.type === "SUPPORT_ZONE" ? LEVEL_STYLE.support : LEVEL_STYLE.resistance;
      const htf = l.timeframes.includes("1Week") || l.timeframes.includes("1Day");
      const tf = l.timeframes.includes("1Week") ? "W" : l.timeframes.includes("1Day") ? "D" : l.timeframe.replace("Hour", "H");
      bands.push({
        low: l.low,
        high: l.high,
        fill: htf ? sty.strong : sty.fill,
        border: sty.border,
        label: `${tf} ${l.type === "SUPPORT_ZONE" ? "support" : "resistance"} · ${l.strength.score}`,
      });
    }
  }
  if (toggles.liquidity) {
    const pools = report.levels.filter((l) => l.valid && l.category === "liquidity");
    for (const l of nearest(pools, price, 1)) {
      bands.push({
        low: l.low,
        high: l.high,
        fill: LEVEL_STYLE.liquidity.fill,
        border: LEVEL_STYLE.liquidity.border,
        dashed: true,
        label: l.label.startsWith("Buy") ? "BSL (potential)" : "SSL (potential)",
      });
    }
  }
  return bands;
}

export default function PriceChart({
  bars,
  analysis,
  keyLevels,
  toggles,
  isDemo,
  lastComplete,
  viewKey,
  focus = null,
}: {
  bars: Bar[];
  analysis: Analysis | null;
  keyLevels: KeyLevelReport | null;
  /** A selected strategy setup: its plan, levels and evidence replace the general plan. */
  focus?: StrategySetup | null;
  toggles: ChartToggles;
  isDemo: boolean;
  /** False when the newest candle is still forming; it is drawn translucent. */
  lastComplete: boolean;
  /** Changes when symbol/timeframe/session change; the view is re-fitted only then. */
  viewKey: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const linesRef = useRef<Record<string, ISeriesApi<"Line">>>({});
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const bandsRef = useRef<ZoneBands | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);

  // Create the chart once.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: COLORS.text,
        fontSize: 11,
        attributionLogo: true,
      },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: COLORS.border },
      timeScale: {
        borderColor: COLORS.border,
        timeVisible: true,
        secondsVisible: false,
        // Axis ticks in New York time (the default would use the viewer's timezone).
        tickMarkFormatter: (time: number, tickType: TickMarkType) => {
          const d = new Date(time * 1000);
          const opts: Intl.DateTimeFormatOptions =
            tickType === TickMarkType.Year
              ? { year: "numeric" }
              : tickType === TickMarkType.Month
                ? { month: "short" }
                : tickType === TickMarkType.DayOfMonth
                  ? { month: "short", day: "numeric" }
                  : { hour: "numeric", minute: "2-digit" };
          return d.toLocaleString("en-US", { timeZone: "America/New_York", ...opts });
        },
      },
      localization: {
        timeFormatter: (time: number) =>
          new Date(time * 1000).toLocaleString("en-US", {
            timeZone: "America/New_York",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          }),
      },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.bull,
      downColor: COLORS.bear,
      borderUpColor: COLORS.bull,
      borderDownColor: COLORS.bear,
      wickUpColor: COLORS.bull,
      wickDownColor: COLORS.bear,
    });
    candlesRef.current = candles;
    volumeRef.current = chart.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const bands = new ZoneBands();
    candles.attachPrimitive(bands);
    bandsRef.current = bands;
    markersRef.current = createSeriesMarkers(candles, []);
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      candlesRef.current = null;
      volumeRef.current = null;
      bandsRef.current = null;
      markersRef.current = null;
      linesRef.current = {};
      priceLinesRef.current = [];
    };
  }, []);

  // Candles + volume.
  useEffect(() => {
    const candles = candlesRef.current;
    const volume = volumeRef.current;
    if (!candles || !volume) return;
    candles.setData(
      bars.map((b, i) => {
        const point = { time: ts(b.time), open: b.open, high: b.high, low: b.low, close: b.close };
        if (i === bars.length - 1 && !lastComplete) {
          const c = b.close >= b.open ? "rgba(34,197,94,0.45)" : "rgba(239,68,68,0.45)";
          return { ...point, color: c, borderColor: c, wickColor: c };
        }
        return point;
      }),
    );
    volume.setData(
      toggles.volume
        ? bars.map((b) => ({
            time: ts(b.time),
            value: b.volume,
            color: b.close >= b.open ? "rgba(34,197,94,0.28)" : "rgba(239,68,68,0.28)",
          }))
        : [],
    );
  }, [bars, toggles.volume, lastComplete]);

  // Fit the view when the symbol/timeframe/session changes, not on every live refresh.
  const fittedKey = useRef<string | null>(null);
  useEffect(() => {
    if (bars.length && fittedKey.current !== viewKey) {
      chartRef.current?.timeScale().fitContent();
      fittedKey.current = viewKey;
    }
  }, [bars, viewKey]);

  // EMA 9/20/50/200 and session VWAP lines from the key-level engine.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const ind = keyLevels?.indicators;
    const wanted: Record<string, { color: string; on: boolean; style?: LineStyle; width?: 1 | 2 | 3 }> = {
      ema9: { color: LEVEL_STYLE.ema9, on: toggles.ema9, width: 1 },
      ema20: { color: LEVEL_STYLE.ema20, on: toggles.ema20, width: 1 },
      ema50: { color: LEVEL_STYLE.ema50, on: toggles.ema50, width: 2 },
      ema200: { color: LEVEL_STYLE.ema200, on: toggles.ema200, width: 2 },
      vwap: { color: LEVEL_STYLE.vwap, on: toggles.vwap, style: LineStyle.Dashed, width: 2 },
    };
    for (const [key, series] of Object.entries(linesRef.current)) {
      if (!wanted[key]?.on || !ind?.[key as keyof typeof ind]) {
        chart.removeSeries(series);
        delete linesRef.current[key];
      }
    }
    if (!ind) return;
    for (const [key, cfg] of Object.entries(wanted)) {
      const data = ind[key as LineKey];
      if (!cfg.on || !data?.length) continue;
      let series = linesRef.current[key];
      if (!series) {
        series = chart.addSeries(LineSeries, {
          color: cfg.color,
          lineWidth: cfg.width ?? 1,
          lineStyle: cfg.style ?? LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        linesRef.current[key] = series;
      }
      series.setData(data.map((p) => ({ time: ts(p.time), value: p.value })));
    }
  }, [keyLevels, toggles.ema9, toggles.ema20, toggles.ema50, toggles.ema200, toggles.vwap]);

  // Zones and liquidity as shaded bands.
  useEffect(() => {
    const bands = keyLevels ? zoneBands(keyLevels, toggles) : [];
    for (const l of focus?.supporting_levels ?? []) {
      if (l.high - l.low > 1e-9) {
        bands.push({ low: l.low, high: l.high, fill: "rgba(79,156,249,0.10)", border: "rgba(79,156,249,0.8)", label: `Setup level: ${l.label}` });
      }
    }
    bandsRef.current?.setBands(bands);
  }, [keyLevels, toggles, focus]);

  // Swing markers and breakout markers.
  useEffect(() => {
    const plugin = markersRef.current;
    if (!plugin) return;
    const times = new Set(bars.map((b) => b.time));
    const markers: SeriesMarker<Time>[] = [];
    if (keyLevels?.indicators && toggles.swings) {
      for (const s of keyLevels.indicators.swings.slice(-MAX_SWING_MARKERS)) {
        if (!times.has(s.time)) continue;
        markers.push({
          time: ts(s.time),
          position: s.kind === "high" ? "aboveBar" : "belowBar",
          shape: "circle",
          color: LEVEL_STYLE.swing,
          size: 0.4,
        });
      }
    }
    if (keyLevels && toggles.breakouts) {
      for (const l of keyLevels.levels) {
        if (l.category !== "breakout") continue;
        const t = (l.evidence.details as { breakout_candle?: number }).breakout_candle;
        if (!t || !times.has(t)) continue;
        const up = l.type === "BREAKOUT";
        markers.push({
          time: ts(t),
          position: up ? "belowBar" : "aboveBar",
          shape: up ? "arrowUp" : "arrowDown",
          color: l.valid ? (up ? LEVEL_STYLE.breakout : LEVEL_STYLE.breakdown) : LEVEL_STYLE.swing,
          text: `${up ? "BO" : "BD"}${l.valid ? "" : "✕"}`,
        });
      }
    }
    for (const e of focus?.evidence ?? []) {
      if (!times.has(e.time)) continue;
      const up = focus?.direction === "LONG";
      markers.push({
        time: ts(e.time),
        position: up ? "belowBar" : "aboveBar",
        shape: e.kind.includes("confirm") || e.kind.includes("close") || e.kind === "breakout" ? (up ? "arrowUp" : "arrowDown") : "square",
        color: "#4f9cf9",
        text: e.kind.split(" ")[0],
      });
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    plugin.setMarkers(markers);
  }, [bars, keyLevels, toggles.swings, toggles.breakouts, focus]);

  // Price lines: previous highs/lows, session levels, breakout levels, trade plan.
  useEffect(() => {
    const candles = candlesRef.current;
    if (!candles) return;
    priceLinesRef.current.forEach((l) => candles.removePriceLine(l));
    priceLinesRef.current = [];
    const add = (price: number, color: string, title: string, style = LineStyle.Dotted, width = 1) =>
      priceLinesRef.current.push(
        candles.createPriceLine({
          price,
          color,
          title,
          lineStyle: style,
          lineWidth: width as 1 | 2,
          axisLabelVisible: true,
        }),
      );
    for (const l of keyLevels?.levels ?? []) {
      if ((l.type === "PDH" || l.type === "PDL") && toggles.prevHighLow) add(l.price, LEVEL_STYLE.pd, l.type, LineStyle.Dotted, 1);
      if ((l.type === "PWH" || l.type === "PWL") && toggles.prevHighLow) add(l.price, LEVEL_STYLE.pw, l.type, LineStyle.Dashed, 2);
      if (l.type === "SESSION_OPEN" && toggles.session) add(l.price, LEVEL_STYLE.session, "Open", LineStyle.SparseDotted, 1);
      if ((l.type === "ORH" || l.type === "ORL") && toggles.session) add(l.price, LEVEL_STYLE.session, l.type, LineStyle.Dashed, 1);
      if (l.category === "breakout" && l.valid && toggles.breakouts)
        add(l.price, l.type === "BREAKOUT" ? LEVEL_STYLE.breakout : LEVEL_STYLE.breakdown, l.type === "BREAKOUT" ? "BO" : "BD", LineStyle.Solid, 2);
    }
    if (focus) {
      for (const l of focus.supporting_levels) {
        if (l.high - l.low <= 1e-9) add(l.low, "rgba(79,156,249,0.8)", l.label.slice(0, 18), LineStyle.Dotted, 1);
      }
      if (focus.entry !== null && focus.stop !== null) {
        add(focus.entry, "#4f9cf9", `${focus.direction} entry`, LineStyle.Solid, 2);
        add(focus.stop, COLORS.bear, "Stop", LineStyle.Solid, 2);
        if (focus.target1 !== null) add(focus.target1, COLORS.bull, "T1", LineStyle.LargeDashed, 2);
        if (focus.target2 !== null) add(focus.target2, COLORS.bull, "T2", LineStyle.LargeDashed, 2);
      }
      return; // the selected setup replaces the general trade plan
    }
    const s = analysis?.setup;
    if (s && toggles.plan && s.status !== "NO_TRADE" && s.entry !== null && s.stop !== null) {
      add(s.entry, "#4f9cf9", "Entry", LineStyle.Solid, 2);
      add(s.stop, COLORS.bear, "Stop", LineStyle.Solid, 2);
      s.targets.forEach((t, i) => add(t, COLORS.bull, `T${i + 1}`, LineStyle.LargeDashed, 2));
    }
  }, [analysis, keyLevels, toggles.prevHighLow, toggles.session, toggles.breakouts, toggles.plan, focus]);

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="absolute inset-0" />
      {isDemo && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <span className="select-none text-5xl font-black uppercase tracking-[0.3em] text-white/[0.04] sm:text-7xl">
            Demo data
          </span>
        </div>
      )}
    </div>
  );
}
