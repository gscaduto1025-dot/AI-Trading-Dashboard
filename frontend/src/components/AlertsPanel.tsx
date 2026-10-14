"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { fmtIsoNy, fmtPrice, timeAgo } from "@/lib/format";
import type { Alert, AlertPrefs, AlertPrefsResponse, AlertSeverity, AlertType, PriceLevel } from "@/lib/types";
import { Empty, Panel } from "./ui";

type Tab = "alerts" | "levels" | "settings";
type Filter = "all" | "actionable" | "levels" | "warnings";

const SEVERITY: Record<AlertSeverity, string> = {
  actionable: "bg-accent/15 text-accent ring-accent/40",
  info: "bg-white/5 text-ink/80 ring-line-strong",
  warning: "bg-wait/10 text-wait ring-wait/30",
};

const LEVEL_TYPES: AlertType[] = ["approaching_support", "approaching_resistance", "price_level", "target_reached"];

function matches(a: Alert, f: Filter) {
  if (f === "actionable") return a.actionable;
  if (f === "levels") return LEVEL_TYPES.includes(a.type);
  if (f === "warnings") return a.severity === "warning";
  return true;
}

const zone = (lv: Alert["key_level"]) =>
  !lv ? "—" : lv.low === lv.high || lv.high == null ? fmtPrice(lv.low) : `${fmtPrice(lv.low)}–${fmtPrice(lv.high)}`;

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-faint">{k}</dt>
      <dd className="num text-ink/90">{children}</dd>
    </>
  );
}

function AlertDetail({ a, onOpen }: { a: Alert; onOpen?: (a: Alert) => void }) {
  const f = a.feed_status;
  return (
    <div className="mt-1.5 rounded bg-white/[0.03] p-2 text-[10.5px]">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        <Row k="Ticker">
          {a.ticker} {a.mode && <span className="text-faint">· {a.mode} trading</span>}
        </Row>
        {a.setup && (
          <Row k="Strategy">
            {a.setup} {a.timeframe && <span className="text-faint">({a.timeframe})</span>}
          </Row>
        )}
        {a.direction && (
          <Row k="Direction">
            <span className={a.direction === "LONG" ? "text-bull" : "text-bear"}>{a.direction}</span>
          </Row>
        )}
        <Row k="Current price">{fmtPrice(a.price)}</Row>
        <Row k="Key level">
          {a.key_level ? (
            <>
              {a.key_level.label} {zone(a.key_level)}
              {a.key_level.timeframe && <span className="text-faint"> · {a.key_level.timeframe}</span>}
            </>
          ) : (
            "—"
          )}
        </Row>
        <Row k="Setup status">
          {a.previous_status && a.previous_status !== a.status ? `${a.previous_status} → ` : ""}
          {a.status ?? "—"}
        </Row>
        {a.entry_trigger != null && <Row k="Entry trigger">{fmtPrice(a.entry_trigger)}</Row>}
        {a.entry != null && <Row k="Entry">{fmtPrice(a.entry)}</Row>}
        {a.stop != null && <Row k="Stop-loss">{fmtPrice(a.stop)}</Row>}
        {a.targets.length > 0 && <Row k="Targets">{a.targets.map((t, i) => `T${i + 1} ${fmtPrice(t)}`).join(" · ")}</Row>}
        {a.risk_reward != null && <Row k="Risk/reward">{a.risk_reward.toFixed(2)}:1</Row>}
        {a.quality != null && <Row k="Quality">{a.quality}/100 (rules score, not a win probability)</Row>}
        <Row k="Timestamp">{fmtIsoNy(a.timestamp)}</Row>
        <Row k="Candle">{fmtIsoNy(a.candle_time)}</Row>
        <Row k="Data feed">
          <span className={f.freshness === "fresh" || f.freshness === "demo" ? "" : "text-wait"}>
            {f.feed ?? f.source ?? "—"} · {f.freshness ?? "unknown"}
            {f.latency && f.latency !== f.freshness ? ` · ${f.latency}` : ""}
          </span>
          {f.reason && <span className="block text-faint">{f.reason}</span>}
        </Row>
        {a.delivery && <Row k="Delivery">{a.delivery}</Row>}
      </dl>
      {a.entry_conditions.length > 0 && (
        <div className="mt-1.5">
          <p className="text-faint">Entry conditions</p>
          <ul className="mt-0.5 space-y-0.5">
            {a.entry_conditions.map((c) => (
              <li key={c.name} className="flex gap-1.5">
                <span className={c.passed ? "text-bull" : c.passed === false ? "text-bear" : "text-faint"}>
                  {c.passed ? "✓" : c.passed === false ? "✗" : "·"}
                </span>
                <span>
                  {c.name}
                  {c.detail && <span className="text-faint"> — {c.detail}</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {a.invalidation.length > 0 && (
        <p className="mt-1.5 text-faint">
          Invalidation: <span className="text-ink/80">{a.invalidation.join(" ")}</span>
        </p>
      )}
      {onOpen && (
        <button onClick={() => onOpen(a)} className="mt-1.5 text-[10.5px] font-semibold text-accent hover:underline">
          Open chart &amp; analysis →
        </button>
      )}
    </div>
  );
}

function AlertList({
  alerts,
  onAck,
  onOpen,
}: {
  alerts: Alert[] | null;
  onAck: (id: number) => void;
  onOpen?: (a: Alert) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<number | null>(null);
  const shown = (alerts ?? []).filter((a) => matches(a, filter));
  return (
    <>
      <div className="flex gap-1 border-b border-line px-3 py-1.5">
        {(["all", "actionable", "levels", "warnings"] as Filter[]).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`rounded px-1.5 py-0.5 text-[10px] capitalize ${
              filter === f ? "bg-white/10 text-ink" : "text-muted hover:text-ink"
            }`}
          >
            {f}
          </button>
        ))}
      </div>
      {!alerts ? (
        <Empty>Loading alerts…</Empty>
      ) : !shown.length ? (
        <Empty>
          No alerts{filter !== "all" ? " in this view" : " yet"}. The server checks every new candle, even
          when this page is closed.
        </Empty>
      ) : (
        <ul className="divide-y divide-line">
          {shown.map((a) => (
            <li key={a.id} className={`px-3 py-2 ${a.acknowledged ? "opacity-55" : ""}`}>
              <div className="flex items-center justify-between gap-2">
                <span className="flex min-w-0 items-center gap-1.5">
                  <span
                    className={`shrink-0 rounded px-1.5 py-px text-[9px] font-bold uppercase ring-1 ${SEVERITY[a.severity]}`}
                  >
                    {a.label}
                  </span>
                  <span className="text-xs font-bold">{a.ticker}</span>
                  {a.mode && <span className="text-[9px] uppercase text-faint">{a.mode}</span>}
                  {a.is_demo && <span className="text-[9px] font-bold uppercase text-wait">demo</span>}
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="text-[10px] text-faint" title={fmtIsoNy(a.timestamp)}>
                    {timeAgo(a.created_at)}
                  </span>
                  {!a.acknowledged && (
                    <button onClick={() => onAck(a.id)} className="text-[10px] text-muted hover:text-ink" title="Mark as read">
                      ✓
                    </button>
                  )}
                </span>
              </div>
              <button className="mt-0.5 block w-full text-left" onClick={() => setOpen(open === a.id ? null : a.id)}>
                <p className="text-[11px] font-medium leading-snug text-ink/90">{a.title.replace(/^\[DEMO\] /, "")}</p>
                <p className="text-[11px] leading-snug text-ink/70">{a.message}</p>
              </button>
              {open === a.id && <AlertDetail a={a} onOpen={onOpen} />}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function LevelsTab({ watchlist }: { watchlist: string[] }) {
  const [levels, setLevels] = useState<PriceLevel[] | null>(null);
  const [ticker, setTicker] = useState(watchlist[0] ?? "SPY");
  const [price, setPrice] = useState("");
  const [condition, setCondition] = useState<"auto" | "above" | "below">("auto");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api
      .priceLevels()
      .then((r) => setLevels(r.levels))
      .catch((e: ApiError) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const p = Number(price);
    if (!(p > 0)) return setError("Enter a positive price.");
    try {
      await api.addPriceLevel({ ticker, price: p, note, condition: condition === "auto" ? undefined : condition });
      setPrice("");
      setNote("");
      load();
    } catch (err) {
      setError((err as ApiError).message);
    }
  };

  return (
    <div className="space-y-2 p-3 text-[11px]">
      <p className="text-faint">
        One-shot alerts when price reaches your level, checked server-side on every scan with the latest trade price
        (only while that ticker&apos;s data is fresh).
      </p>
      <form onSubmit={add} className="flex flex-wrap items-center gap-1.5">
        <select
          value={ticker}
          onChange={(e) => setTicker(e.target.value)}
          className="rounded bg-white/5 px-1.5 py-1 ring-1 ring-line-strong"
          aria-label="Ticker"
        >
          {watchlist.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select
          value={condition}
          onChange={(e) => setCondition(e.target.value as typeof condition)}
          className="rounded bg-white/5 px-1.5 py-1 ring-1 ring-line-strong"
          aria-label="Condition"
        >
          <option value="auto">crosses</option>
          <option value="above">at/above</option>
          <option value="below">at/below</option>
        </select>
        <input
          value={price}
          onChange={(e) => setPrice(e.target.value)}
          inputMode="decimal"
          placeholder="Price"
          aria-label="Price"
          className="num w-20 rounded bg-white/5 px-1.5 py-1 ring-1 ring-line-strong"
        />
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Note (optional)"
          aria-label="Note"
          className="min-w-0 flex-1 rounded bg-white/5 px-1.5 py-1 ring-1 ring-line-strong"
        />
        <button className="rounded bg-accent/15 px-2 py-1 font-semibold text-accent ring-1 ring-accent/40">Add</button>
      </form>
      {error && <p className="text-bear">{error}</p>}
      {!levels?.length ? (
        <p className="text-faint">No price levels yet.</p>
      ) : (
        <ul className="divide-y divide-line rounded ring-1 ring-line">
          {levels.map((l) => (
            <li key={l.id} className={`flex items-center justify-between gap-2 px-2 py-1.5 ${l.active ? "" : "opacity-60"}`}>
              <span>
                <span className="font-bold">{l.ticker}</span> {l.condition}{" "}
                <span className="num">{fmtPrice(l.price)}</span>
                {l.note && <span className="text-faint"> · {l.note}</span>}
                {!l.active && l.triggered_at && (
                  <span className="text-faint">
                    {" "}
                    · reached {timeAgo(l.triggered_at)} at <span className="num">{fmtPrice(l.triggered_price)}</span>
                  </span>
                )}
              </span>
              <button
                onClick={() => api.deletePriceLevel(l.id).then(load)}
                className="text-muted hover:text-bear"
                title="Delete level"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SettingsTab() {
  const [data, setData] = useState<AlertPrefsResponse | null>(null);
  const [draft, setDraft] = useState<AlertPrefs | null>(null);
  const [muted, setMuted] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const apply = (r: AlertPrefsResponse) => {
    setData(r);
    setDraft(r.preferences);
    setMuted(r.preferences.muted_tickers.join(", "));
  };
  useEffect(() => {
    api
      .alertPrefs()
      .then(apply)
      .catch((e: ApiError) => setMsg({ text: e.message, ok: false }));
  }, []);

  if (!data || !draft) return <Empty>{msg?.text ?? "Loading preferences…"}</Empty>;
  const set = (patch: Partial<AlertPrefs>) => setDraft({ ...draft, ...patch });
  const toggleIn = (list: AlertType[], t: AlertType) => (list.includes(t) ? list.filter((x) => x !== t) : [...list, t]);
  const num = (v: string, fallback: number) => (v === "" || Number.isNaN(Number(v)) ? fallback : Number(v));

  const save = async () => {
    setMsg(null);
    try {
      apply(
        await api.saveAlertPrefs({
          ...draft,
          muted_tickers: muted
            .split(/[\s,]+/)
            .map((s) => s.trim())
            .filter(Boolean),
        }),
      );
      setMsg({ text: "Saved.", ok: true });
    } catch (e) {
      setMsg({ text: (e as ApiError).message, ok: false });
    }
  };

  return (
    <div className="space-y-3 p-3 text-[11px]">
      <table className="w-full">
        <thead className="text-left text-[10px] uppercase tracking-wider text-faint">
          <tr>
            <th className="pb-1 font-medium">Alert type</th>
            <th className="pb-1 font-medium" title="Create this alert">On</th>
            <th className="pb-1 font-medium" title="Browser notification">Notify</th>
            <th className="pb-1 font-medium" title="Minutes before the same alert can repeat">Cooldown</th>
          </tr>
        </thead>
        <tbody>
          {(Object.keys(data.types) as AlertType[]).map((t) => (
            <tr key={t} title={data.types[t].description}>
              <td className="py-0.5 pr-2">{data.types[t].label}</td>
              <td>
                <input
                  type="checkbox"
                  aria-label={`${data.types[t].label} enabled`}
                  checked={draft.enabled[t]}
                  onChange={(e) => set({ enabled: { ...draft.enabled, [t]: e.target.checked } })}
                />
              </td>
              <td>
                <input
                  type="checkbox"
                  aria-label={`${data.types[t].label} browser notification`}
                  checked={draft.browser_types.includes(t)}
                  onChange={() => set({ browser_types: toggleIn(draft.browser_types, t) })}
                />
              </td>
              <td>
                <input
                  aria-label={`${data.types[t].label} cooldown minutes`}
                  className="num w-14 rounded bg-white/5 px-1 py-0.5 ring-1 ring-line-strong"
                  inputMode="numeric"
                  value={draft.cooldown_minutes[t]}
                  onChange={(e) =>
                    set({ cooldown_minutes: { ...draft.cooldown_minutes, [t]: num(e.target.value, 0) } })
                  }
                />{" "}
                <span className="text-faint">min</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-0.5">
          <span className="text-faint">Modes</span>
          <span className="flex gap-2">
            {(["day", "swing"] as const).map((m) => (
              <label key={m} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={draft.modes.includes(m)}
                  onChange={() =>
                    set({ modes: draft.modes.includes(m) ? draft.modes.filter((x) => x !== m) : [...draft.modes, m] })
                  }
                />
                {m}
              </label>
            ))}
          </span>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-faint">Approaching = within (ATR)</span>
          <input
            className="num rounded bg-white/5 px-1.5 py-0.5 ring-1 ring-line-strong"
            value={draft.proximity_atr}
            inputMode="decimal"
            onChange={(e) => set({ proximity_atr: num(e.target.value, draft.proximity_atr) })}
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-faint">Swing candidate min quality</span>
          <input
            className="num rounded bg-white/5 px-1.5 py-0.5 ring-1 ring-line-strong"
            value={draft.swing_candidate_min_quality}
            inputMode="numeric"
            onChange={(e) => set({ swing_candidate_min_quality: num(e.target.value, 70) })}
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-faint">Trigger alerts: min quality</span>
          <input
            className="num rounded bg-white/5 px-1.5 py-0.5 ring-1 ring-line-strong"
            value={draft.min_quality}
            inputMode="numeric"
            onChange={(e) => set({ min_quality: num(e.target.value, 0) })}
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-faint">Trigger alerts: min R:R (blank = planner&apos;s)</span>
          <input
            className="num rounded bg-white/5 px-1.5 py-0.5 ring-1 ring-line-strong"
            value={draft.min_risk_reward ?? ""}
            inputMode="decimal"
            onChange={(e) => set({ min_risk_reward: e.target.value === "" ? null : num(e.target.value, 0) })}
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-faint">Muted tickers</span>
          <input
            className="rounded bg-white/5 px-1.5 py-0.5 ring-1 ring-line-strong"
            value={muted}
            placeholder="e.g. TSLA, AMD"
            onChange={(e) => setMuted(e.target.value)}
          />
        </label>
      </div>
      <p className="text-faint">
        Webhook (alerts while the dashboard is closed):{" "}
        {data.webhook.configured ? (
          <span className="text-bull">configured → {data.webhook.host}</span>
        ) : (
          <span>not configured — set ALERT_WEBHOOK_URL in backend/.env (see docs/DEPLOYMENT.md)</span>
        )}
      </p>
      <div className="flex items-center gap-2">
        <button onClick={save} className="rounded bg-accent/15 px-2 py-1 font-semibold text-accent ring-1 ring-accent/40">
          Save preferences
        </button>
        <button
          onClick={() => api.resetAlertPrefs().then(apply).then(() => setMsg({ text: "Defaults restored.", ok: true }))}
          className="rounded px-2 py-1 text-muted ring-1 ring-line-strong hover:text-ink"
        >
          Reset
        </button>
        {msg && <span className={msg.ok ? "text-muted" : "text-bear"}>{msg.text}</span>}
      </div>
    </div>
  );
}

export default function AlertsPanel({
  alerts,
  unread,
  onAck,
  onAckAll,
  onOpen,
  notifyEnabled,
  onToggleNotify,
  watchlist,
}: {
  alerts: Alert[] | null;
  unread: number;
  onAck: (id: number) => void;
  onAckAll: () => void;
  onOpen?: (a: Alert) => void;
  notifyEnabled: boolean;
  onToggleNotify: () => void;
  watchlist: string[];
}) {
  const [tab, setTab] = useState<Tab>("alerts");
  return (
    <Panel
      title={
        <span className="flex items-center gap-2">
          Alerts
          {unread > 0 && <span className="num rounded-full bg-accent px-1.5 text-[10px] font-bold text-bg">{unread}</span>}
        </span>
      }
      right={
        <span className="flex items-center gap-1">
          {(["alerts", "levels", "settings"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded px-1.5 py-0.5 text-[10px] capitalize ${
                tab === t ? "bg-white/10 text-ink" : "text-muted hover:text-ink"
              }`}
            >
              {t}
            </button>
          ))}
          {tab === "alerts" && unread > 0 && (
            <button onClick={onAckAll} className="px-1 text-[10px] text-muted hover:text-ink" title="Mark all as read">
              ✓ all
            </button>
          )}
          <button
            onClick={onToggleNotify}
            className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ${
              notifyEnabled ? "bg-accent/15 text-accent ring-accent/40" : "text-muted ring-line-strong hover:text-ink"
            }`}
            title="Browser notifications (types chosen in Settings) while this page is open"
          >
            {notifyEnabled ? "🔔" : "🔕"}
          </button>
        </span>
      }
      bodyClassName="overflow-y-auto"
    >
      {tab === "alerts" && <AlertList alerts={alerts} onAck={onAck} onOpen={onOpen} />}
      {tab === "levels" && <LevelsTab watchlist={watchlist} />}
      {tab === "settings" && <SettingsTab />}
    </Panel>
  );
}
