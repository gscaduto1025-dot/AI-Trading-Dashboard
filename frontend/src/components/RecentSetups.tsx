"use client";

import { STATUS_LABEL, directionColor, fmtPrice, statusClasses, timeAgo } from "@/lib/format";
import type { RecordedSetup } from "@/lib/types";
import { Empty, Panel } from "./ui";

export default function RecentSetups({
  setups,
  onSelect,
}: {
  setups: RecordedSetup[] | null;
  onSelect: (symbol: string, mode: RecordedSetup["mode"], timeframe: string) => void;
}) {
  return (
    <Panel title="Recently Detected Setups" bodyClassName="overflow-y-auto">
      {!setups?.length ? (
        <Empty>No setups recorded yet. The scanner runs on startup and on an interval.</Empty>
      ) : (
        <ul className="divide-y divide-line">
          {setups.map((s) => (
            <li key={s.id}>
              <button
                onClick={() => onSelect(s.symbol, s.mode, s.timeframe)}
                className="w-full px-3 py-2 text-left hover:bg-white/[0.03]"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-2">
                    <span className="text-xs font-bold">{s.symbol}</span>
                    <span className={`rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${statusClasses(s.status, s.direction)}`}>
                      {STATUS_LABEL[s.status]}
                    </span>
                    {s.is_demo && <span className="text-[9px] font-bold uppercase text-wait">demo</span>}
                  </span>
                  <span className="text-[10px] text-faint">{timeAgo(s.detected_at)}</span>
                </div>
                <p className={`mt-0.5 truncate text-[11px] ${directionColor(s.direction)}`}>{s.title}</p>
                <p className="num mt-0.5 text-[10px] text-faint">
                  {s.mode} · {s.timeframe} · E {fmtPrice(s.entry)} · S {fmtPrice(s.stop)} · T {fmtPrice(s.target1)} ·{" "}
                  {s.risk_reward?.toFixed(2)}R
                </p>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
