"use client";

import { LATENCY_LABEL, fmtIsoNy, fmtVolume } from "@/lib/format";
import type { CandleResponse, FeedInfo, MarketStatus, Quote } from "@/lib/types";

const PHASE_STYLE: Record<MarketStatus["phase"], string> = {
  regular: "text-bull ring-bull/40 bg-bull/10",
  "pre-market": "text-accent ring-accent/40 bg-accent/10",
  "after-hours": "text-accent ring-accent/40 bg-accent/10",
  closed: "text-muted ring-line-strong bg-white/5",
};

const LATENCY_STYLE: Record<string, string> = {
  "real-time": "text-bull ring-bull/40",
  delayed: "text-wait ring-wait/40",
  historical: "text-muted ring-line-strong",
  demo: "text-wait ring-wait/40",
  unavailable: "text-bear ring-bear/50",
};

function Item({ label, children, title }: { label: string; children: React.ReactNode; title?: string }) {
  return (
    <div className="flex min-w-0 flex-col" title={title}>
      <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-faint">{label}</span>
      <span className="truncate text-xs text-ink">{children}</span>
    </div>
  );
}

/** Market session, feed provenance and candle freshness for the selected chart. */
export default function MarketStatusBar({
  market,
  feed,
  candles,
  candlesError,
  quote,
}: {
  market: MarketStatus | null;
  feed: FeedInfo | null;
  candles: CandleResponse | null;
  candlesError: string | null;
  quote: Quote | undefined;
}) {
  const status = candles?.status;
  const latency = candlesError ? "unavailable" : (status?.latency ?? feed?.latency ?? "unavailable");
  const stale = status?.freshness === "stale";
  const sessionDetail = market
    ? market.holiday
      ? market.holiday
      : market.early_close
        ? `Early close 1:00 PM (${market.early_close})`
        : market.phase === "regular"
          ? `Closes ${fmtIsoNy(market.next_close, false)}`
          : `Opens ${fmtIsoNy(market.next_open)}`
    : "";

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line bg-panel/60 px-4 py-2">
      <div className="flex items-center gap-2" title={market ? `Calendar: ${market.calendar_source}` : undefined}>
        <span
          className={`rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ring-1 ${
            market ? PHASE_STYLE[market.phase] : "text-faint ring-line"
          }`}
        >
          {market?.label ?? "Session —"}
        </span>
        <span className="text-[11px] text-muted">{sessionDetail}</span>
      </div>

      <div className="flex items-center gap-2" title={feed?.coverage}>
        <span className="text-[9px] font-semibold uppercase tracking-[0.14em] text-faint">Feed</span>
        <span className="rounded bg-panel-2 px-1.5 py-0.5 text-[11px] font-semibold text-ink ring-1 ring-line">
          {feed ? (feed.source === "alpaca" ? `Alpaca · ${feed.label}` : "DEMO") : "—"}
        </span>
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ring-1 ${LATENCY_STYLE[latency]}`}>
          {LATENCY_LABEL[latency] ?? latency}
        </span>
        {stale && (
          <span className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase text-bear ring-1 ring-bear/50" title={status?.reason ?? ""}>
            Stale
          </span>
        )}
      </div>

      <Item label="Latest candle" title={status?.reason ?? undefined}>
        {candlesError ? (
          <span className="font-semibold text-bear">Unavailable</span>
        ) : status?.latest_bar_time ? (
          <>
            <span className="num">
              {candles?.timeframe === "1Week"
                ? `Week of ${new Date(status.latest_bar_time).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" })}`
                : candles?.timeframe === "1Day"
                  ? new Date(status.latest_bar_time).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", year: "numeric" })
                  : fmtIsoNy(status.latest_bar_time)}
            </span>{" "}
            <span className={status.latest_bar_complete ? "text-muted" : "text-wait"}>
              {status.latest_bar_complete ? "· closed" : "· forming"}
            </span>
          </>
        ) : (
          "—"
        )}
      </Item>

      <Item label="Session volume" title={feed?.id === "iex" ? "IEX-only volume: a small share of consolidated volume" : undefined}>
        <span className="num">{quote?.available ? fmtVolume(quote.volume) : "—"}</span>
        {feed?.id === "iex" && quote?.available && <span className="text-faint"> (IEX only)</span>}
      </Item>

      <Item label="Last trade">
        <span className="num">{quote?.available ? fmtIsoNy(quote.price_time) : "—"}</span>
        {quote?.stale && <span className="text-bear"> · stale</span>}
      </Item>

      {feed && feed.delay_seconds > 0 && market && (
        <Item label="Data through">
          <span className="num">{fmtIsoNy(market.data_cutoff, false)}</span>
        </Item>
      )}
    </div>
  );
}
