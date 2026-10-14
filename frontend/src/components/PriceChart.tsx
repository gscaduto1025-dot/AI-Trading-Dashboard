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
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Analysis, Bar } from "@/lib/types";

export interface ChartToggles {
  emaFast: boolean;
  emaSlow: boolean;
  vwap: boolean;
  smaLong: boolean;
  levels: boolean;
  plan: boolean;
  volume: boolean;
}

const COLORS = {
  bull: "#22c55e",
  bear: "#ef4444",
  emaFast: "#4f9cf9",
  emaSlow: "#c084fc",
  vwap: "#f5b942",
  sma: "#94a3b8",
  grid: "#121a24",
  text: "#8494a7",
  border: "#1c2633",
};

const ts = (t: number) => t as UTCTimestamp;

export default function PriceChart({
  bars,
  analysis,
  toggles,
  isDemo,
  lastComplete,
  viewKey,
}: {
  bars: Bar[];
  analysis: Analysis | null;
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
    candlesRef.current = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.bull,
      downColor: COLORS.bear,
      borderUpColor: COLORS.bull,
      borderDownColor: COLORS.bear,
      wickUpColor: COLORS.bull,
      wickDownColor: COLORS.bear,
    });
    volumeRef.current = chart.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      candlesRef.current = null;
      volumeRef.current = null;
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

  // Indicator overlays.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const wanted: Record<string, { color: string; on: boolean; style?: LineStyle }> = {};
    if (analysis) {
      const { ema_fast_period: f, ema_slow_period: s } = analysis.indicators;
      wanted[`ema${f}`] = { color: COLORS.emaFast, on: toggles.emaFast };
      wanted[`ema${s}`] = { color: COLORS.emaSlow, on: toggles.emaSlow };
      wanted.vwap = { color: COLORS.vwap, on: toggles.vwap, style: LineStyle.Dashed };
      wanted.sma200 = { color: COLORS.sma, on: toggles.smaLong };
    }
    for (const [key, series] of Object.entries(linesRef.current)) {
      if (!wanted[key]?.on || !analysis?.overlays[key]) {
        chart.removeSeries(series);
        delete linesRef.current[key];
      }
    }
    if (!analysis) return;
    for (const [key, cfg] of Object.entries(wanted)) {
      const data = analysis.overlays[key];
      if (!cfg.on || !data) continue;
      let series = linesRef.current[key];
      if (!series) {
        series = chart.addSeries(LineSeries, {
          color: cfg.color,
          lineWidth: 2,
          lineStyle: cfg.style ?? LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        linesRef.current[key] = series;
      }
      series.setData(data.map((p) => ({ time: ts(p.time), value: p.value })));
    }
  }, [analysis, toggles.emaFast, toggles.emaSlow, toggles.vwap, toggles.smaLong]);

  // Price lines: key levels and the trade plan.
  useEffect(() => {
    const candles = candlesRef.current;
    if (!candles) return;
    priceLinesRef.current.forEach((l) => candles.removePriceLine(l));
    priceLinesRef.current = [];
    if (!analysis) return;
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
    if (toggles.levels) {
      for (const lvl of analysis.levels) {
        add(
          lvl.price,
          lvl.kind === "support" ? "rgba(34,197,94,0.55)" : "rgba(239,68,68,0.55)",
          lvl.source === "Swing pivot" ? (lvl.kind === "support" ? "S" : "R") : lvl.source,
        );
      }
    }
    const s = analysis.setup;
    if (toggles.plan && s.status !== "NO_TRADE" && s.entry !== null && s.stop !== null) {
      add(s.entry, "#4f9cf9", "Entry", LineStyle.Solid, 2);
      add(s.stop, COLORS.bear, "Stop", LineStyle.Solid, 2);
      s.targets.forEach((t, i) => add(t, COLORS.bull, `T${i + 1}`, LineStyle.Dashed, 2));
    }
  }, [analysis, toggles.levels, toggles.plan]);

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
