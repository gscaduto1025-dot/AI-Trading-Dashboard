import type {
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

export interface Band {
  low: number;
  high: number;
  fill: string;
  border: string;
  dashed?: boolean;
  label?: string;
  labelColor?: string;
}

/**
 * Horizontal price bands drawn behind the candles: support/resistance and liquidity zones
 * are ranges, not single prices, so they are shaded rather than drawn as lines.
 */
export class ZoneBands implements ISeriesPrimitive<Time> {
  private bands: Band[] = [];
  private series: ISeriesApi<SeriesType> | null = null;
  private requestUpdate: (() => void) | null = null;

  attached(param: SeriesAttachedParameter<Time>) {
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }

  detached() {
    this.series = null;
    this.requestUpdate = null;
  }

  setBands(bands: Band[]) {
    this.bands = bands;
    this.requestUpdate?.();
  }

  paneViews(): readonly IPrimitivePaneView[] {
    const series = this.series;
    const bands = this.bands;
    const renderer: IPrimitivePaneRenderer = {
      draw: () => {},
      drawBackground: (target) => {
        if (!series) return;
        target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, verticalPixelRatio: vr, horizontalPixelRatio: hr }) => {
          for (const b of bands) {
            const y1 = series.priceToCoordinate(b.high);
            const y2 = series.priceToCoordinate(b.low);
            if (y1 === null || y2 === null) continue;
            const top = Math.round(Math.min(y1, y2) * vr);
            const h = Math.max(Math.round(Math.abs(y2 - y1) * vr), Math.round(2 * vr));
            ctx.fillStyle = b.fill;
            ctx.fillRect(0, top, bitmapSize.width, h);
            ctx.strokeStyle = b.border;
            ctx.lineWidth = Math.max(1, Math.round(vr));
            ctx.setLineDash(b.dashed ? [6 * hr, 4 * hr] : []);
            ctx.beginPath();
            ctx.moveTo(0, top + 0.5);
            ctx.lineTo(bitmapSize.width, top + 0.5);
            ctx.moveTo(0, top + h - 0.5);
            ctx.lineTo(bitmapSize.width, top + h - 0.5);
            ctx.stroke();
            if (b.label) {
              ctx.setLineDash([]);
              ctx.font = `${Math.round(10 * vr)}px ui-sans-serif, system-ui, sans-serif`;
              ctx.fillStyle = b.labelColor ?? b.border;
              ctx.fillText(b.label, Math.round(8 * hr), top + Math.round(11 * vr));
            }
          }
        });
      },
    };
    return [{ zOrder: (): PrimitivePaneViewZOrder => "bottom", renderer: () => renderer }];
  }
}
