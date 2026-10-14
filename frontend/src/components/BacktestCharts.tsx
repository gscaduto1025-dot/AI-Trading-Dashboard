"use client";

import { useEffect, useRef, useState } from "react";

// Chart palette (validated on the panel surface #0d1219: lightness band, chroma, CVD and
// contrast all pass). Blue = gains / the series, red = losses / drawdown.
export const C = {
  series: "#3987e5",
  gain: "#3987e5",
  loss: "#e66767",
  grid: "#1c2633",
  axis: "#2a3747",
  muted: "#8494a7",
  ink: "#e6edf5",
  surface: "#0d1219",
};

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((e) => setW(Math.max(240, Math.floor(e[0].contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  // Round the domain OUTWARD so the first/last tick always contain the data.
  const out = [];
  const first = Math.floor(min / step + 1e-9) * step;
  const last = Math.ceil(max / step - 1e-9) * step;
  for (let v = first; v <= last + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}

const fmtDay = (t: number) =>
  new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit", timeZone: "America/New_York" });

function Tip({ x, y, w, children }: { x: number; y: number; w: number; children: React.ReactNode }) {
  const left = Math.min(Math.max(x + 12, 0), w - 170);
  return (
    <div
      className="pointer-events-none absolute z-10 w-[160px] rounded-md border border-line-strong bg-panel-2 px-2 py-1.5 text-[11px] shadow-lg"
      style={{ left, top: Math.max(0, y - 10) }}
    >
      {children}
    </div>
  );
}

const PAD = { l: 44, r: 12, t: 10, b: 24 };

/** Fixed-fractional equity curve (single series: no legend; the title names it). */
export function EquityChart({
  points,
  splitTime,
  height = 200,
}: {
  points: { time: number; equity: number; drawdown_pct: number }[];
  splitTime?: number | null;
  height?: number;
}) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  if (!points.length) return <p className="py-8 text-center text-xs text-faint">No closed trades.</p>;
  const data = [{ time: points[0].time, equity: 100, drawdown_pct: 0 }, ...points];
  const t0 = data[0].time;
  const t1 = data[data.length - 1].time || t0 + 1;
  const vals = data.map((p) => p.equity);
  const lo = Math.min(...vals, 100);
  const hi = Math.max(...vals, 100);
  const ticks = niceTicks(lo, hi === lo ? hi + 1 : hi, 4);
  const y0 = ticks[0];
  const y1 = ticks[ticks.length - 1];
  const iw = w - PAD.l - PAD.r;
  const ih = height - PAD.t - PAD.b;
  const X = (t: number) => PAD.l + (t1 === t0 ? iw / 2 : ((t - t0) / (t1 - t0)) * iw);
  const Y = (v: number) => PAD.t + (1 - (v - y0) / (y1 - y0 || 1)) * ih;
  const path = data.map((p, i) => `${i ? "L" : "M"}${X(p.time).toFixed(1)},${Y(p.equity).toFixed(1)}`).join("");
  const h = hover != null ? data[hover] : null;
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    let best = 0;
    data.forEach((p, i) => {
      if (Math.abs(X(p.time) - px) < Math.abs(X(data[best].time) - px)) best = i;
    });
    setHover(best);
  };
  return (
    <div ref={ref} className="relative">
      <svg width={w} height={height} onPointerMove={onMove} onPointerLeave={() => setHover(null)} role="img"
        aria-label={`Equity curve from 100 to ${data[data.length - 1].equity.toFixed(1)}`}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={w - PAD.r} y1={Y(t)} y2={Y(t)} stroke={C.grid} strokeWidth={1} />
            <text x={PAD.l - 6} y={Y(t) + 3} textAnchor="end" fontSize={10} fill={C.muted} className="tabular-nums">
              {t.toFixed(t % 1 ? 1 : 0)}
            </text>
          </g>
        ))}
        <line x1={PAD.l} x2={w - PAD.r} y1={Y(100)} y2={Y(100)} stroke={C.axis} strokeWidth={1} />
        {splitTime && splitTime > t0 && splitTime < t1 && (
          <g>
            <line x1={X(splitTime)} x2={X(splitTime)} y1={PAD.t} y2={PAD.t + ih} stroke={C.axis} strokeWidth={1} />
            <text x={X(splitTime) + 4} y={PAD.t + 10} fontSize={10} fill={C.muted}>
              out-of-sample →
            </text>
          </g>
        )}
        <path d={path} fill="none" stroke={C.series} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        <text x={PAD.l} y={height - 6} fontSize={10} fill={C.muted}>
          {fmtDay(t0)}
        </text>
        <text x={w - PAD.r} y={height - 6} fontSize={10} fill={C.muted} textAnchor="end">
          {fmtDay(t1)}
        </text>
        {h && (
          <g>
            <line x1={X(h.time)} x2={X(h.time)} y1={PAD.t} y2={PAD.t + ih} stroke={C.muted} strokeWidth={1} />
            <circle cx={X(h.time)} cy={Y(h.equity)} r={4} fill={C.series} stroke={C.surface} strokeWidth={2} />
          </g>
        )}
      </svg>
      {h && hover != null && (
        <Tip x={X(h.time)} y={Y(h.equity)} w={w}>
          <p className="font-semibold text-ink">{h.equity.toFixed(2)}</p>
          <p className="text-muted">equity (start 100)</p>
          <p className="mt-0.5 text-ink">−{h.drawdown_pct.toFixed(2)}% <span className="text-muted">from peak</span></p>
          <p className="text-faint">{hover === 0 ? "start" : `after trade ${hover} · ${fmtDay(h.time)}`}</p>
        </Tip>
      )}
    </div>
  );
}

/** Drawdown from the running peak (its own chart - never a second axis on the equity). */
export function DrawdownChart({ points, height = 120 }: { points: { time: number; drawdown_pct: number }[]; height?: number }) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  if (!points.length) return null;
  const t0 = points[0].time;
  const t1 = points[points.length - 1].time || t0 + 1;
  const max = Math.max(1, ...points.map((p) => p.drawdown_pct));
  const ticks = niceTicks(0, max, 3);
  const top = ticks[ticks.length - 1];
  const iw = w - PAD.l - PAD.r;
  const ih = height - PAD.t - PAD.b;
  const X = (t: number) => PAD.l + (t1 === t0 ? iw / 2 : ((t - t0) / (t1 - t0)) * iw);
  const Y = (v: number) => PAD.t + (v / top) * ih;
  const line = points.map((p, i) => `${i ? "L" : "M"}${X(p.time).toFixed(1)},${Y(p.drawdown_pct).toFixed(1)}`).join("");
  const area = `${line}L${X(points[points.length - 1].time)},${Y(0)}L${X(points[0].time)},${Y(0)}Z`;
  const h = hover != null ? points[hover] : null;
  return (
    <div ref={ref} className="relative">
      <svg width={w} height={height} role="img" aria-label={`Maximum drawdown ${max.toFixed(1)}%`}
        onPointerLeave={() => setHover(null)}
        onPointerMove={(e) => {
          const px = e.clientX - e.currentTarget.getBoundingClientRect().left;
          let best = 0;
          points.forEach((p, i) => {
            if (Math.abs(X(p.time) - px) < Math.abs(X(points[best].time) - px)) best = i;
          });
          setHover(best);
        }}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={w - PAD.r} y1={Y(t)} y2={Y(t)} stroke={t === 0 ? C.axis : C.grid} strokeWidth={1} />
            <text x={PAD.l - 6} y={Y(t) + 3} textAnchor="end" fontSize={10} fill={C.muted} className="tabular-nums">
              {t === 0 ? "0" : `−${t}%`}
            </text>
          </g>
        ))}
        <path d={area} fill={C.loss} fillOpacity={0.1} />
        <path d={line} fill="none" stroke={C.loss} strokeWidth={2} strokeLinejoin="round" />
        {h && <line x1={X(h.time)} x2={X(h.time)} y1={PAD.t} y2={PAD.t + ih} stroke={C.muted} strokeWidth={1} />}
      </svg>
      {h && (
        <Tip x={X(h.time)} y={Y(h.drawdown_pct)} w={w}>
          <p className="font-semibold text-ink">−{h.drawdown_pct.toFixed(2)}%</p>
          <p className="text-muted">below the running peak · {fmtDay(h.time)}</p>
        </Tip>
      )}
    </div>
  );
}

type Col = { key: string; label: string; value: number; sub: string; positive: boolean };

/** Columns from a zero baseline, colored by polarity (gain / loss) with a legend. */
function Columns({ cols, height, yLabel, fmt, legend }: {
  cols: Col[];
  height: number;
  yLabel: string;
  fmt: (v: number) => string;
  legend: [string, string];
}) {
  const [ref, w] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  if (!cols.length) return <p className="py-8 text-center text-xs text-faint">No data.</p>;
  const vals = cols.map((c) => c.value);
  const lo = Math.min(0, ...vals);
  const hi = Math.max(0, ...vals);
  const ticks = niceTicks(lo, hi === lo ? lo + 1 : hi, 3);
  const y0 = Math.min(ticks[0], lo);
  const y1 = Math.max(ticks[ticks.length - 1], hi);
  const iw = w - PAD.l - PAD.r;
  const ih = height - PAD.t - PAD.b - 12;
  const band = iw / cols.length;
  const bw = Math.min(24, Math.max(4, band - 2)); // <= 24px, >= 2px surface gap
  const Y = (v: number) => PAD.t + (1 - (v - y0) / (y1 - y0 || 1)) * ih;
  const r = 4;
  return (
    <div ref={ref} className="relative">
      <div className="mb-1 flex gap-3 text-[10px] text-muted">
        <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-sm" style={{ background: C.gain }} />{legend[0]}</span>
        <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-sm" style={{ background: C.loss }} />{legend[1]}</span>
        <span className="ml-auto text-faint">{yLabel}</span>
      </div>
      <svg width={w} height={height - 12} role="img" aria-label={yLabel} onPointerLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={w - PAD.r} y1={Y(t)} y2={Y(t)} stroke={t === 0 ? C.axis : C.grid} strokeWidth={1} />
            <text x={PAD.l - 6} y={Y(t) + 3} textAnchor="end" fontSize={10} fill={C.muted} className="tabular-nums">
              {fmt(t)}
            </text>
          </g>
        ))}
        {cols.map((c, i) => {
          const cx = PAD.l + band * i + band / 2;
          const top = Y(Math.max(0, c.value));
          const bottom = Y(Math.min(0, c.value));
          const hgt = Math.max(1, bottom - top);
          const up = c.value >= 0;
          // rounded data-end (4px), square at the baseline
          const x = cx - bw / 2;
          const rr = Math.min(r, hgt, bw / 2);
          const d = up
            ? `M${x},${bottom}V${top + rr}Q${x},${top} ${x + rr},${top}H${x + bw - rr}Q${x + bw},${top} ${x + bw},${top + rr}V${bottom}Z`
            : `M${x},${top}V${bottom - rr}Q${x},${bottom} ${x + rr},${bottom}H${x + bw - rr}Q${x + bw},${bottom} ${x + bw},${bottom - rr}V${top}Z`;
          return (
            <g key={c.key} onPointerEnter={() => setHover(i)}>
              <rect x={PAD.l + band * i} y={PAD.t} width={band} height={ih} fill="transparent" />
              <path d={d} fill={c.positive ? C.gain : C.loss} opacity={hover == null || hover === i ? 1 : 0.55} />
              {c.label && (
                <text x={cx} y={height - 16} textAnchor="middle" fontSize={9} fill={C.muted}>
                  {c.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {hover != null && (
        <Tip x={PAD.l + band * hover + band / 2} y={Y(cols[hover].value)} w={w}>
          <p className="font-semibold text-ink">{cols[hover].sub}</p>
          <p className="text-muted">{cols[hover].label}</p>
        </Tip>
      )}
    </div>
  );
}

/** Distribution of realized R multiples (0.5 R bins; tails folded at -3 R / +5 R). */
export function RHistogram({ rs, height = 170 }: { rs: number[]; height?: number }) {
  if (!rs.length) return <p className="py-8 text-center text-xs text-faint">No closed trades.</p>;
  const lo = -3;
  const hi = 5;
  const step = 0.5;
  const bins: Col[] = [];
  for (let a = lo; a < hi; a += step) {
    const b = a + step;
    const n = rs.filter((r) => (a === lo ? r < b : a + step >= hi ? r >= a : r >= a && r < b)).length;
    const name = a === lo ? `< ${b}R` : b >= hi ? `≥ ${a}R` : `${a} to ${b}R`;
    bins.push({ key: String(a), label: Number.isInteger(a) ? (a === lo ? `<${a + step}R` : `${a}R`) : "", value: n, sub: `${n} trade${n === 1 ? "" : "s"} · ${name}`, positive: a >= 0 });
  }
  return <Columns cols={bins} height={height} yLabel="trades per 0.5 R bin" fmt={(v) => `${v}`} legend={["Winning (R ≥ 0)", "Losing (R < 0)"]} />;
}

/** Expectancy per quality-score bucket: does a higher score go with better outcomes? */
export function QualityChart({ buckets, height = 170 }: { buckets: { range: string; trades: number; expectancy_r: number | null }[]; height?: number }) {
  const cols: Col[] = buckets.map((b) => ({
    key: b.range,
    label: `${b.range} (${b.trades})`,
    value: b.expectancy_r ?? 0,
    sub: b.expectancy_r == null ? "no trades" : `${b.expectancy_r >= 0 ? "+" : ""}${b.expectancy_r.toFixed(2)} R per trade · ${b.trades} trades`,
    positive: (b.expectancy_r ?? 0) >= 0,
  }));
  return <Columns cols={cols} height={height} yLabel="expectancy (R) by quality score (trades)" fmt={(v) => `${v.toFixed(1)}R`} legend={["Positive expectancy", "Negative expectancy"]} />;
}

/** Inline diverging bar for table cells (value in R around zero). */
export function DivergingBar({ value, max }: { value: number | null | undefined; max: number }) {
  if (value == null) return <span className="text-faint">—</span>;
  const pct = Math.min(1, Math.abs(value) / (max || 1)) * 50;
  return (
    <span className="relative inline-block h-2 w-24 align-middle" aria-hidden>
      <span className="absolute left-1/2 top-0 h-2 w-px" style={{ background: C.axis }} />
      <span
        className="absolute top-0 h-2"
        style={{
          background: value >= 0 ? C.gain : C.loss,
          left: value >= 0 ? "50%" : `${50 - pct}%`,
          width: `${pct}%`,
          borderRadius: value >= 0 ? "0 2px 2px 0" : "2px 0 0 2px",
        }}
      />
    </span>
  );
}
