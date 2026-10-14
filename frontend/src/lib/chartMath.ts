/** Round axis ticks. The domain is rounded OUTWARD so the first and last tick always
 * contain [min, max] (a line never leaves the plot). */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  const first = Math.floor(min / step + 1e-9) * step;
  const last = Math.ceil(max / step - 1e-9) * step;
  for (let v = first; v <= last + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}
