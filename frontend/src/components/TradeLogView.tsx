"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { validationClasses } from "@/lib/scannerFilters";
import type { JournalAccount, JournalResponse, JournalTrade, SavedSetup } from "@/lib/types";
import { Empty, Panel } from "./ui";

const money = (v: number | null | undefined) =>
  v == null ? "—" : `${v < 0 ? "−" : v > 0 ? "+" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const n = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));
const when = (t: number | null) =>
  t == null ? "—" : new Date(t * 1000).toLocaleString("en-US", { timeZone: "America/New_York", dateStyle: "short", timeStyle: "short" });
/** Local datetime-input value -> ISO with the browser's offset. */
const iso = (local: string) => (local ? new Date(local).toISOString() : "");
const nowLocal = () => {
  const d = new Date();
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  return d.toISOString().slice(0, 16);
};
const input = "rounded bg-white/5 px-2 py-1 ring-1 ring-line-strong";
const dur = (s: number | null | undefined) =>
  s == null ? "—" : s < 3600 ? `${Math.round(s / 60)}m` : s < 86400 ? `${(s / 3600).toFixed(1)}h` : `${(s / 86400).toFixed(1)}d`;

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-3 py-2">
      <p className="text-[10px] text-muted">{label}</p>
      <p className="text-lg font-semibold text-ink">{value}</p>
      {sub && <p className="text-[10px] text-faint">{sub}</p>}
    </div>
  );
}

function NewTrade({ account, onDone }: { account: JournalAccount; onDone: () => void }) {
  const [f, setF] = useState({ ticker: "", direction: "LONG", qty: "", price: "", fees: "0", at: nowLocal(), stop: "", target: "", target2: "", strategy: "", notes: "", mode: "", asset: "stock", optionSymbol: "", multiplier: "100", quality: "" });
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      await api.journalCreate(account, {
        ticker: f.ticker,
        direction: f.direction,
        planned_stop: f.stop || null,
        planned_target: f.target || null,
        planned_target2: f.target2 || null,
        strategy: f.strategy || null,
        notes: f.notes,
        mode: f.mode || null,
        asset_type: f.asset,
        option_symbol: f.asset === "option" ? f.optionSymbol : null,
        multiplier: f.asset === "option" ? f.multiplier : null,
        quality: f.quality || null,
        fills: [{ side: f.direction === "LONG" ? "BUY" : "SELL", quantity: f.qty, price: f.price, fees: f.fees || 0, executed_at: iso(f.at) }],
      });
      setF({ ...f, ticker: "", qty: "", price: "", stop: "", target: "", target2: "", notes: "", optionSymbol: "", quality: "" });
      onDone();
    } catch (x) {
      setErr((x as ApiError).message);
    }
  };
  return (
    <form onSubmit={submit} className="grid gap-2 p-3 text-[11px] md:grid-cols-6">
      <label className="flex flex-col gap-0.5"><span className="text-faint">Ticker</span><input aria-label="Ticker" value={f.ticker} onChange={set("ticker")} className={input} required /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Direction</span>
        <select aria-label="Direction" value={f.direction} onChange={set("direction")} className={input}><option value="LONG">Long</option><option value="SHORT">Short</option></select></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Entry quantity</span><input aria-label="Quantity" value={f.qty} onChange={set("qty")} className={input} inputMode="decimal" required /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Entry price</span><input aria-label="Price" value={f.price} onChange={set("price")} className={input} inputMode="decimal" required /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Fees</span><input aria-label="Fees" value={f.fees} onChange={set("fees")} className={input} inputMode="decimal" /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Executed at (your time)</span><input aria-label="Executed at" type="datetime-local" value={f.at} onChange={set("at")} className={input} required /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Planned stop (for R)</span><input aria-label="Planned stop" value={f.stop} onChange={set("stop")} className={input} inputMode="decimal" /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Target 1</span><input aria-label="Planned target" value={f.target} onChange={set("target")} className={input} inputMode="decimal" /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Target 2</span><input aria-label="Planned target 2" value={f.target2} onChange={set("target2")} className={input} inputMode="decimal" /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Trading mode</span>
        <select aria-label="Trading mode" value={f.mode} onChange={set("mode")} className={input}><option value="">—</option><option value="day">Day trade</option><option value="swing">Swing trade</option></select></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Instrument</span>
        <select aria-label="Instrument" value={f.asset} onChange={set("asset")} className={input}><option value="stock">Stock</option><option value="option">Option</option></select></label>
      {f.asset === "option" && (
        <>
          <label className="flex flex-col gap-0.5"><span className="text-faint">Option contract</span><input aria-label="Option contract" value={f.optionSymbol} onChange={set("optionSymbol")} className={input} placeholder="e.g. NVDA261120C00180000" /></label>
          <label className="flex flex-col gap-0.5"><span className="text-faint">Multiplier</span><input aria-label="Multiplier" value={f.multiplier} onChange={set("multiplier")} className={input} inputMode="decimal" /></label>
        </>
      )}
      <label className="flex flex-col gap-0.5"><span className="text-faint">Setup quality (0-100)</span><input aria-label="Setup quality" value={f.quality} onChange={set("quality")} className={input} inputMode="decimal" /></label>
      <label className="flex flex-col gap-0.5"><span className="text-faint">Strategy / setup</span><input aria-label="Strategy" value={f.strategy} onChange={set("strategy")} className={input} /></label>
      <label className="flex flex-col gap-0.5 md:col-span-2"><span className="text-faint">Notes</span><input aria-label="Notes" value={f.notes} onChange={set("notes")} className={input} /></label>
      <div className="flex items-end gap-2">
        <button className="rounded bg-accent/15 px-3 py-1.5 font-semibold text-accent ring-1 ring-accent/40">Record {account} trade</button>
      </div>
      {err && <p className="text-bear md:col-span-6">{err}</p>}
    </form>
  );
}

function TradeRow({ t, account, onChange }: { t: JournalTrade; account: JournalAccount; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  const exitSide = t.direction === "LONG" ? "SELL" : "BUY";
  const [fill, setFill] = useState({ side: exitSide, qty: "", price: "", fees: "0", at: nowLocal() });
  const [err, setErr] = useState<string | null>(null);
  const p = t.pnl;
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
      onChange();
    } catch (x) {
      setErr((x as ApiError).message);
    }
  };
  return (
    <>
      <tr onClick={() => setOpen(!open)} className="cursor-pointer tabular-nums hover:bg-white/[0.03]">
        <td className="px-2 py-1">{when(p.opened_at)}</td>
        <td className="px-2 py-1 font-sans font-semibold">{t.ticker}{t.asset_type === "option" && <span className="ml-1 text-[9px] font-normal text-faint" title={t.option_symbol ?? ""}>OPT</span>}</td>
        <td className="px-2 py-1">{t.mode === "day" ? "Day" : t.mode === "swing" ? "Swing" : "—"}</td>
        <td className="px-2 py-1">{t.direction === "LONG" ? "Long" : "Short"}</td>
        <td className="px-2 py-1 font-sans">{t.strategy ?? "—"}</td>
        <td className="px-2 py-1">{p.status}{p.status === "open" ? ` (${p.open_quantity})` : ""}</td>
        <td className="px-2 py-1">{n(p.max_position, 0)}</td>
        <td className="px-2 py-1">{n(p.avg_entry, 4)}</td>
        <td className="px-2 py-1">{n(p.avg_exit, 4)}</td>
        <td className="px-2 py-1">{t.planned_stop ?? "—"}</td>
        <td className="px-2 py-1">{t.planned_target ?? "—"}{t.planned_target2 != null ? ` / ${t.planned_target2}` : ""}</td>
        <td className="px-2 py-1">{money(p.realized_gross)}</td>
        <td className="px-2 py-1">{money(p.fees)}</td>
        <td className={`px-2 py-1 font-semibold ${p.realized_net >= 0 ? "text-bull" : "text-bear"}`}>{money(p.realized_net)}</td>
        <td className="px-2 py-1">{p.unrealized == null ? "—" : money(p.unrealized)}</td>
        <td className="px-2 py-1">{p.r_multiple == null ? "—" : `${p.r_multiple.toFixed(2)}R`}</td>
        <td className="px-2 py-1">{dur(p.duration_seconds)}</td>
        <td className={`px-2 py-1 font-sans ${p.result === "win" ? "text-bull" : p.result === "loss" ? "text-bear" : "text-muted"}`}>{p.result ?? "—"}</td>
        <td className="px-2 py-1 font-sans">
          {t.evidence_status ? <span className={`rounded px-1 text-[9px] font-bold uppercase ring-1 ${validationClasses(t.evidence_status)}`} title="Strategy evidence when the trade was logged">{t.evidence_label}</span> : "—"}
          {t.quality != null && <span className="ml-1 text-faint" title="Setup quality (rules score)">q{t.quality.toFixed(0)}</span>}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={20} className="bg-white/[0.02] px-3 py-2 text-[11px]">
            {t.problem && <p className="text-bear">{t.problem}</p>}
            {p.status === "open" && t.mark_note && <p className="text-faint">{t.mark_note}</p>}
            <table className="mb-2 mt-1 w-full max-w-[720px]">
              <thead className="text-left text-[10px] uppercase text-faint">
                <tr>{["Time", "Side", "Qty", "Price", "Fees", "Note", ""].map((h) => <th key={h} className="px-2 py-0.5 font-medium">{h}</th>)}</tr>
              </thead>
              <tbody className="tabular-nums">
                {t.fills.map((f) => (
                  <tr key={f.id}>
                    <td className="px-2 py-0.5">{when(f.executed_at)}</td>
                    <td className="px-2 py-0.5">{f.side}</td>
                    <td className="px-2 py-0.5">{f.quantity}</td>
                    <td className="px-2 py-0.5">{f.price}</td>
                    <td className="px-2 py-0.5">{f.fees}</td>
                    <td className="px-2 py-0.5 font-sans">{f.note}</td>
                    <td className="px-2 py-0.5">
                      <button onClick={() => act(() => api.journalDeleteFill(account, t.id, f.id))} className="text-muted hover:text-bear" title="Delete fill">✕</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                act(() => api.journalAddFill(account, t.id, { side: fill.side, quantity: fill.qty, price: fill.price, fees: fill.fees || 0, executed_at: iso(fill.at) }));
              }}
              className="flex flex-wrap items-center gap-1.5"
            >
              <select aria-label="Fill side" value={fill.side} onChange={(e) => setFill({ ...fill, side: e.target.value })} className={input}>
                <option value={exitSide}>{exitSide} (exit / partial exit)</option>
                <option value={exitSide === "SELL" ? "BUY" : "SELL"}>{exitSide === "SELL" ? "BUY" : "SELL"} (add to position)</option>
              </select>
              <input aria-label="Fill quantity" placeholder="qty" value={fill.qty} onChange={(e) => setFill({ ...fill, qty: e.target.value })} className={`${input} w-20`} />
              <input aria-label="Fill price" placeholder="price" value={fill.price} onChange={(e) => setFill({ ...fill, price: e.target.value })} className={`${input} w-24`} />
              <input aria-label="Fill fees" placeholder="fees" value={fill.fees} onChange={(e) => setFill({ ...fill, fees: e.target.value })} className={`${input} w-16`} />
              <input aria-label="Fill time" type="datetime-local" value={fill.at} onChange={(e) => setFill({ ...fill, at: e.target.value })} className={input} />
              <button className="rounded bg-accent/15 px-2 py-1 font-semibold text-accent ring-1 ring-accent/40">Add fill</button>
              <button type="button" onClick={() => { if (confirm("Delete this trade and all its fills?")) act(() => api.journalDelete(account, t.id)); }} className="ml-auto text-muted hover:text-bear">
                Delete trade
              </button>
            </form>
            {p.status === "open" && <CloseForm t={t} account={account} act={act} />}
            <EditForm t={t} account={account} act={act} />
            {err && <p className="mt-1 text-bear">{err}</p>}
            <p className="mt-1 text-faint">
              Planned stop {t.planned_stop ?? "—"} · targets {t.planned_target ?? "—"}{t.planned_target2 != null ? ` / ${t.planned_target2}` : ""} · initial risk {money(p.initial_risk)}
              {t.asset_type === "option" ? ` · option ${t.option_symbol ?? ""} ×${t.multiplier}` : ""}{t.setup_id ? ` · from setup ${t.setup_id}` : ""} · {t.notes}
            </p>
          </td>
        </tr>
      )}
    </>
  );
}

function CloseForm({ t, account, act }: { t: JournalTrade; account: JournalAccount; act: (fn: () => Promise<unknown>) => void }) {
  const [c, setC] = useState({ price: "", fees: "0", at: nowLocal() });
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        act(() => api.journalClose(account, t.id, { price: c.price, fees: c.fees || 0, executed_at: iso(c.at) }));
      }}
      className="mt-2 flex flex-wrap items-center gap-1.5"
    >
      <span className="text-faint">Close the remaining {t.pnl.open_quantity} at</span>
      <input aria-label="Close price" placeholder="exit price" required value={c.price} onChange={(e) => setC({ ...c, price: e.target.value })} className={`${input} w-24`} />
      <input aria-label="Close fees" placeholder="fees" value={c.fees} onChange={(e) => setC({ ...c, fees: e.target.value })} className={`${input} w-16`} />
      <input aria-label="Close time" type="datetime-local" value={c.at} onChange={(e) => setC({ ...c, at: e.target.value })} className={input} />
      <button className="rounded bg-bull/15 px-2 py-1 font-semibold text-bull ring-1 ring-bull/40">Close trade</button>
    </form>
  );
}

function EditForm({ t, account, act }: { t: JournalTrade; account: JournalAccount; act: (fn: () => Promise<unknown>) => void }) {
  const init = () => ({
    strategy: t.strategy ?? "", mode: t.mode ?? "", stop: t.planned_stop?.toString() ?? "", t1: t.planned_target?.toString() ?? "",
    t2: t.planned_target2?.toString() ?? "", quality: t.quality?.toString() ?? "", notes: t.notes ?? "",
  });
  const [e, setE] = useState<ReturnType<typeof init> | null>(null);
  if (!e) return <button onClick={() => setE(init())} className="mt-2 text-accent hover:underline">Edit details</button>;
  return (
    <form
      onSubmit={(ev) => {
        ev.preventDefault();
        act(() =>
          api.journalUpdate(account, t.id, {
            strategy: e.strategy, mode: e.mode || null, planned_stop: e.stop || null, planned_target: e.t1 || null,
            planned_target2: e.t2 || null, quality: e.quality || null, notes: e.notes,
          }),
        );
        setE(null);
      }}
      className="mt-2 flex flex-wrap items-end gap-1.5"
    >
      {([["strategy", "Strategy", "w-36"], ["stop", "Stop", "w-20"], ["t1", "Target 1", "w-20"], ["t2", "Target 2", "w-20"], ["quality", "Quality", "w-14"], ["notes", "Notes", "w-64"]] as const).map(([k, label, w]) => (
        <label key={k} className="flex flex-col"><span className="text-faint">{label}</span>
          <input aria-label={`Edit ${label}`} value={e[k]} onChange={(x) => setE({ ...e, [k]: x.target.value })} className={`${input} ${w}`} /></label>
      ))}
      <label className="flex flex-col"><span className="text-faint">Mode</span>
        <select aria-label="Edit mode" value={e.mode} onChange={(x) => setE({ ...e, mode: x.target.value })} className={input}><option value="">—</option><option value="day">Day</option><option value="swing">Swing</option></select></label>
      <button className="rounded bg-accent/15 px-2 py-1 font-semibold text-accent ring-1 ring-accent/40">Save changes</button>
      <button type="button" onClick={() => setE(null)} className="text-muted">Cancel</button>
    </form>
  );
}

function SavedSetups() {
  const [list, setList] = useState<SavedSetup[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(() => {
    api.savedSetups().then((r) => setList(r.setups)).catch((e: ApiError) => setErr(e.message));
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  return (
    <Panel title="Saved setups · detected by the scanners, kept for review · NOT trades (no fills, excluded from all P&L)" bodyClassName="overflow-x-auto">
      {err && <p className="p-3 text-xs text-bear">{err}</p>}
      {!list ? (
        <Empty>Loading…</Empty>
      ) : !list.length ? (
        <Empty>No saved setups. Use “Save setup” on a scanner setup.</Empty>
      ) : (
        <table className="w-full text-[11px]">
          <thead className="text-left text-[10px] uppercase tracking-wider text-faint">
            <tr>{["Saved", "Source", "Ticker", "Mode", "Setup", "Dir", "Status at save", "Entry trigger", "Stop", "Targets", "R:R", "Quality", "Evidence", ""].map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
          </thead>
          <tbody className="divide-y divide-line tabular-nums">
            {list.map((x) => (
              <tr key={x.id}>
                <td className="px-2 py-1">{when(x.saved_at)}</td>
                <td className="px-2 py-1 font-sans">{x.source}</td>
                <td className="px-2 py-1 font-sans font-semibold">{x.ticker}</td>
                <td className="px-2 py-1 font-sans">{x.mode} <span className="text-faint">{x.timeframe}</span></td>
                <td className="px-2 py-1 font-sans">{x.payload.setup ?? x.strategy}</td>
                <td className="px-2 py-1">{x.direction === "LONG" ? "Long" : "Short"}</td>
                <td className="px-2 py-1 font-sans">{x.status_at_save}</td>
                <td className="px-2 py-1">{x.payload.entry_trigger ?? "—"}</td>
                <td className="px-2 py-1">{x.payload.stop ?? "—"}</td>
                <td className="px-2 py-1">{x.payload.target1 ?? "—"}{x.payload.target2 != null ? ` / ${x.payload.target2}` : ""}</td>
                <td className="px-2 py-1">{x.payload.risk_reward ?? "—"}</td>
                <td className="px-2 py-1">{x.payload.quality ?? "—"}</td>
                <td className="px-2 py-1 font-sans">{x.payload.validation ? <span className={`rounded px-1 text-[9px] font-bold uppercase ring-1 ${validationClasses(x.payload.validation.status)}`}>{x.payload.validation.label}</span> : "—"}</td>
                <td className="px-2 py-1"><button onClick={() => api.deleteSavedSetup(x.id).then(load)} className="text-muted hover:text-bear" title="Remove">✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

export default function TradeLogView() {
  const [tab, setTab] = useState<JournalAccount | "saved">("actual");
  const account: JournalAccount = tab === "saved" ? "paper" : tab;
  const [data, setData] = useState<{ account: JournalAccount; res: JournalResponse } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api
      .journal(account)
      .then((res) => {
        setData({ account, res });
        setError(null);
      })
      .catch((e: ApiError) => setError(e.message));
  }, [account]);
  useEffect(() => {
    load();
  }, [load]);
  const res = data?.account === account ? data.res : null;
  const s = res?.stats;
  return (
    <main className="space-y-3 p-3">
      <section className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-xs">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Trade log</span>
        <div className="flex rounded-md bg-panel-2 p-0.5 ring-1 ring-line" role="tablist" aria-label="Account">
          {(["actual", "paper", "saved"] as const).map((a) => (
            <button key={a} role="tab" aria-selected={tab === a} onClick={() => setTab(a)}
              className={`rounded px-3 py-1 text-xs font-semibold ${tab === a ? "bg-ink text-bg" : "text-muted hover:text-ink"}`}>
              {a === "actual" ? "Actual trades" : a === "paper" ? "Paper trades" : "Saved setups"}
            </button>
          ))}
        </div>
        <a href={`/api/journal/${account}/export.csv`} className="rounded px-2 py-1 text-muted ring-1 ring-line-strong hover:text-ink">Export CSV</a>
        <span className="ml-auto text-[10px] text-faint">
          Your own manually recorded trades. Kept separate from paper trades and from simulated backtests. TradeScope never places orders.
        </span>
      </section>
      {error && <p className="rounded border border-bear/30 bg-bear/10 px-3 py-2 text-xs text-bear">{error}</p>}
      {tab === "saved" && <SavedSetups />}

      {tab !== "saved" && s && (
        <section className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">
          <Tile label="Closed trades" value={`${s.closed_trades}`} sub={`${s.open_trades} open · ${s.sample_note}`} />
          <Tile label="Net realized P&L" value={money(s.net_pnl)} sub={`after ${money(s.fees)} fees`} />
          <Tile label="Win rate" value={s.win_rate == null ? "—" : `${s.win_rate.toFixed(1)}%`} />
          <Tile label="Average win / loss" value={`${money(s.avg_win)} / ${money(s.avg_loss)}`} />
          <Tile label="Profit factor" value={n(s.profit_factor)} />
          <Tile label="Expectancy per trade" value={money(s.expectancy)} />
          <Tile label="Average R" value={s.avg_r == null ? "—" : `${s.avg_r.toFixed(2)}R`} sub={`${s.r_trades} trades with a planned stop`} />
        </section>
      )}

      {tab !== "saved" && (<>
      <Panel title={`Record a new ${account} trade (opening fill)`}>
        <NewTrade account={account} onDone={load} />
      </Panel>

      <Panel title={`${account === "actual" ? "Actual" : "Paper"} trades · click a trade for fills, partial exits and adds`} bodyClassName="overflow-x-auto">
        {!res ? (
          <Empty>Loading…</Empty>
        ) : !res.trades.length ? (
          <Empty>No {account} trades recorded yet.</Empty>
        ) : (
          <table className="w-full text-[11px]">
            <thead className="text-left text-[10px] uppercase tracking-wider text-faint">
              <tr>{["Opened", "Ticker", "Mode", "Dir", "Strategy", "Status", "Size", "Avg entry", "Avg exit", "Stop", "Targets", "Gross", "Fees", "Net", "Unrealized", "R", "Duration", "Result", "Evidence"].map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
            </thead>
            <tbody className="divide-y divide-line">
              {res.trades.map((t) => <TradeRow key={t.id} t={t} account={account} onChange={load} />)}
            </tbody>
          </table>
        )}
      </Panel>
      </>)}
    </main>
  );
}
