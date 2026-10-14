"use client";

import {
  CandlestickSeries,
  ColorType,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { BacktestTrade, BacktestTradeChart } from "@/lib/types";
import { Panel } from "./ui";

const COL = { bull: "#22c55e", bear: "#ef4444", accent: "#4f9cf9", ink: "#e6edf5", text: "#8494a7", grid: "#1c2633" };
const n = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));
const t2s = (t: number) =>
  new Date(t * 1000).toLocaleString("en-US", { timeZone: "America/New_York", dateStyle: "medium", timeStyle: "short" });
/** Daily candles are stamped at midnight: show the session date, not a fake time. */
const d2s = (t: number) => new Date(t * 1000).toLocaleDateString("en-US", { timeZone: "America/New_York", dateStyle: "medium" });

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 py-0.5">
      <span className="text-faint">{k}</span>
      <span className="text-right tabular-nums text-ink/90">{v}</span>
    </div>
  );
}

/** A SIMULATED backtest trade: candles with entry, stop, targets and exits. */
export default function BacktestTradeInspector({ runId, trade, onClose }: { runId: string; trade: BacktestTrade; onClose: () => void }) {
  const daily = trade.mode === "swing";
  const box = useRef<HTMLDivElement>(null);
  const [data, setData] = useState<{ id: string; chart: BacktestTradeChart | null; error: string | null } | null>(null);

  useEffect(() => {
    let live = true;
    api
      .backtestTradeChart(runId, trade.id)
      .then((c) => live && setData({ id: trade.id, chart: c, error: null }))
      .catch((e: ApiError) => live && setData({ id: trade.id, chart: null, error: e.message }));
    return () => {
      live = false;
    };
  }, [runId, trade.id]);

  const chart = data?.id === trade.id ? data.chart : null;
  useEffect(() => {
    if (!box.current || !chart?.bars.length) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: COL.text, fontSize: 11 },
      grid: { vertLines: { color: COL.grid }, horzLines: { color: COL.grid } },
      timeScale: { timeVisible: chart.timeframe !== "1Day", secondsVisible: false },
      localization: {
        // explicit: navigator.language can be an invalid tag (e.g. en-US@posix)
        locale: "en-US",
        timeFormatter: (time: number) =>
          new Date(time * 1000).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
      },
    });
    const s = c.addSeries(CandlestickSeries, {
      upColor: COL.bull, downColor: COL.bear, borderUpColor: COL.bull, borderDownColor: COL.bear,
      wickUpColor: COL.bull, wickDownColor: COL.bear,
    });
    s.setData(chart.bars.map((b) => ({ time: b.time as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close })));
    const line = (price: number | null, color: string, title: string, style = LineStyle.Dashed) =>
      price != null && s.createPriceLine({ price, color, title, lineStyle: style, lineWidth: 1, axisLabelVisible: true });
    line(trade.entry, COL.accent, "fill", LineStyle.Solid);
    line(trade.stop, COL.bear, "stop");
    line(trade.target1, COL.bull, "T1");
    if (trade.target2 != null && trade.legs.some((l) => l.target === trade.target2)) line(trade.target2, COL.bull, "T2");
    const long = trade.direction === "LONG";
    const entry: SeriesMarker<Time> = {
      time: trade.entry_time as UTCTimestamp,
      position: long ? "belowBar" : "aboveBar",
      shape: long ? "arrowUp" : "arrowDown",
      color: COL.accent,
      text: "entry",
    };
    const exits: SeriesMarker<Time>[] = trade.legs.map((l) => ({
      time: l.time as UTCTimestamp,
      position: long ? "aboveBar" : "belowBar",
      shape: long ? "arrowDown" : "arrowUp",
      color: l.reason.startsWith("target") ? COL.bull : l.reason.startsWith("stop") ? COL.bear : COL.ink,
      text: `${l.reason}${trade.legs.length > 1 ? ` ${Math.round(l.fraction * 100)}%` : ""}`,
    }));
    const markers = [entry, ...exits].sort((a, b) => (a.time as number) - (b.time as number));
    createSeriesMarkers(s, markers);
    c.timeScale().fitContent();
    return () => c.remove();
  }, [chart, trade]);

  const err = data?.id === trade.id ? data.error : null;
  return (
    <Panel
      title={`Simulated trade · ${trade.ticker} ${trade.strategy_name} ${trade.direction} (${trade.timeframe})`}
      right={<button onClick={onClose} className="text-xs text-muted hover:text-ink">Close ✕</button>}
      bodyClassName="grid gap-3 p-3 lg:grid-cols-[minmax(0,1fr)_300px]"
    >
      <div>
        <p className="mb-1 text-[10px] text-wait">
          SIMULATED backtest trade — not part of your trade log or paper trades. Candles: {chart?.timeframe ?? "…"} from{" "}
          {chart?.data_source ?? "…"}{chart?.is_demo ? " (DEMO DATA)" : ""}.
        </p>
        {err ? (
          <p className="py-10 text-center text-xs text-bear">Chart unavailable: {err}</p>
        ) : (
          <div ref={box} className="h-[320px] w-full" />
        )}
      </div>
      <div className="text-[11px]">
        <Row k="Signal (candle close)" v={t2s(trade.signal_time)} />
        <Row k={daily ? "Entry (session open)" : "Entry"} v={daily ? d2s(trade.entry_time) : t2s(trade.entry_time)} />
        <Row k="Open / fill (after costs)" v={`${n(trade.open)} / ${n(trade.entry, 4)}`} />
        <Row k="Plan entry" v={n(trade.plan_entry)} />
        <Row k="Stop / T1 / T2" v={`${n(trade.stop)} / ${n(trade.target1)} / ${n(trade.target2)}`} />
        {trade.legs.map((l, i) => (
          <Row key={i} k={`Exit ${trade.legs.length > 1 ? `${i + 1} (${Math.round(l.fraction * 100)}%)` : ""}`} v={`${n(l.exit, 4)} · ${l.reason} · ${daily ? d2s(l.time) : t2s(l.time)}`} />
        ))}
        <Row k="Candles held" v={trade.bars_held} />
        <Row k="Risk per share" v={n(trade.risk_per_share, 4)} />
        <Row k="P&L per share (net)" v={n(trade.pnl_per_share, 4)} />
        <Row k="Costs per share (spread, slippage, fees)" v={n(trade.cost_per_share, 4)} />
        <Row k="of which commission + fees" v={n(trade.fees_per_share, 4)} />
        <Row k="R net / gross" v={`${n(trade.r, 3)} / ${n(trade.gross_r, 3)}`} />
        <Row k="MAE / MFE" v={`${n(trade.mae_r, 2)}R / ${n(trade.mfe_r, 2)}R`} />
        <Row k="Shares (sized)" v={trade.shares ? `${trade.shares} (${trade.sized_by})` : "not taken (capital)"} />
        <Row k="P&L ($)" v={trade.pnl_usd == null ? "—" : trade.pnl_usd.toFixed(2)} />
        <Row k="Quality / planned R:R" v={`${n(trade.quality, 1)} / ${trade.planned_rr ?? "—"}`} />
        <Row k="Market regime" v={trade.regime ?? "—"} />
      </div>
    </Panel>
  );
}
