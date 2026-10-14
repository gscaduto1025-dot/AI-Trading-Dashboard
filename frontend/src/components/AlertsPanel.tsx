"use client";

import { statusClasses, STATUS_LABEL, timeAgo } from "@/lib/format";
import type { Alert } from "@/lib/types";
import { Empty, Panel } from "./ui";

export default function AlertsPanel({
  alerts,
  onAck,
  notifyEnabled,
  onToggleNotify,
}: {
  alerts: Alert[] | null;
  onAck: (id: number) => void;
  notifyEnabled: boolean;
  onToggleNotify: () => void;
}) {
  const unread = alerts?.filter((a) => !a.acknowledged).length ?? 0;
  return (
    <Panel
      title={
        <span className="flex items-center gap-2">
          Alerts
          {unread > 0 && (
            <span className="num rounded-full bg-accent px-1.5 text-[10px] font-bold text-bg">{unread}</span>
          )}
        </span>
      }
      right={
        <button
          onClick={onToggleNotify}
          className={`rounded px-2 py-0.5 text-[10px] font-semibold ring-1 ${
            notifyEnabled ? "bg-accent/15 text-accent ring-accent/40" : "text-muted ring-line-strong hover:text-ink"
          }`}
          title="Browser notifications for new actionable setups"
        >
          {notifyEnabled ? "🔔 On" : "🔕 Notify"}
        </button>
      }
      bodyClassName="overflow-y-auto"
    >
      {!alerts?.length ? (
        <Empty>No alerts yet. You&apos;ll be alerted when a setup starts forming or becomes actionable.</Empty>
      ) : (
        <ul className="divide-y divide-line">
          {alerts.map((a) => (
            <li key={a.id} className={`px-3 py-2 ${a.acknowledged ? "opacity-50" : ""}`}>
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${statusClasses(a.status, a.direction)}`}>
                    {STATUS_LABEL[a.status]}
                  </span>
                  <span className="text-xs font-bold">{a.symbol}</span>
                  {a.is_demo && <span className="text-[9px] font-bold uppercase text-wait">demo</span>}
                </span>
                <span className="flex items-center gap-2">
                  <span className="text-[10px] text-faint">{timeAgo(a.created_at)}</span>
                  {!a.acknowledged && (
                    <button onClick={() => onAck(a.id)} className="text-[10px] text-muted hover:text-ink" title="Mark as read">
                      ✓
                    </button>
                  )}
                </span>
              </div>
              <p className="mt-0.5 text-[11px] leading-snug text-ink/80">{a.message}</p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
