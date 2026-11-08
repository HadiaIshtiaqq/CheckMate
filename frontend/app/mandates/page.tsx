"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, post, type Mandate } from "@/lib/api";
import { Card, Notice, money } from "@/components/ui";
import { ArrowLeft, Check, FileText, Loader, ShieldCheck, X } from "@/components/icons";

const csv = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);

export default function MandatesPage() {
  const [rows, setRows] = useState<Mandate[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [form, setForm] = useState({ max: "100", cap: "300", currency: "USD", stores: "", keywords: "", days: "30", established: true });

  const load = () => api<Mandate[]>("/api/v1/mandates").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      await post("/api/v1/mandates", {
        max_per_purchase: form.max, monthly_cap: form.cap, currency: form.currency.toUpperCase(),
        allowed_stores: csv(form.stores), allowed_keywords: csv(form.keywords),
        require_established_store: form.established, days: Number(form.days),
      });
      setMsg({ ok: true, text: "Mandate created." });
      await load();
    } catch (err) {
      setMsg({ ok: false, text: err instanceof ApiError ? err.message : "Could not create the mandate" });
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id: string) => {
    if (!window.confirm("Revoke this mandate? Approvals it granted for unfinished purchases are withdrawn.")) return;
    await post(`/api/v1/mandates/${id}/revoke`);
    load();
  };

  return (
    <div className="space-y-5">
      <Link href="/" className="inline-flex items-center gap-1 text-xs font-semibold text-brand-600"><ArrowLeft size={14} /> Dashboard</Link>
      <div>
        <h1 className="text-2xl font-extrabold tracking-tight">Purchasing mandates</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-500">
          A mandate lets CHECKMATE approve purchase plans on your behalf when they fit limits you set. It never pays on its own:
          you still approve on PayPal (demo store) or buy at the store yourself. Mandates never cover subscriptions.
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-5">
        <Card title="New mandate" icon={<FileText size={15} />} className="lg:col-span-2">
          <form className="space-y-3 text-sm" onSubmit={create}>
            <div className="grid grid-cols-3 gap-2">
              <label className="col-span-1">Per purchase<input className="input mt-1" inputMode="decimal" value={form.max} onChange={(e) => setForm({ ...form, max: e.target.value })} /></label>
              <label className="col-span-1">Monthly cap<input className="input mt-1" inputMode="decimal" value={form.cap} onChange={(e) => setForm({ ...form, cap: e.target.value })} /></label>
              <label className="col-span-1">Currency<input className="input mt-1 uppercase" maxLength={3} value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value })} /></label>
            </div>
            <label className="block">Only these stores <span className="text-slate-400">(comma-separated, optional)</span>
              <input className="input mt-1" placeholder="Best Buy, Target" value={form.stores} onChange={(e) => setForm({ ...form, stores: e.target.value })} /></label>
            <label className="block">Only products matching <span className="text-slate-400">(keywords, optional)</span>
              <input className="input mt-1" placeholder="headphones, paper towels" value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} /></label>
            <div className="grid grid-cols-2 gap-2">
              <label>Valid for (days)<input className="input mt-1" type="number" min={1} max={90} value={form.days} onChange={(e) => setForm({ ...form, days: e.target.value })} /></label>
              <label className="flex items-end gap-2 pb-2"><input type="checkbox" checked={form.established} onChange={(e) => setForm({ ...form, established: e.target.checked })} /> Established stores only</label>
            </div>
            {msg && <Notice tone={msg.ok ? "emerald" : "rose"}>{msg.text}</Notice>}
            <button className="btn-primary w-full" disabled={busy}>{busy ? <Loader size={16} /> : <ShieldCheck size={16} />} Create mandate</button>
          </form>
        </Card>

        <Card title="Your mandates" icon={<ShieldCheck size={15} />} className="lg:col-span-3">
          {rows === null && <p className="text-sm text-slate-500">Loading…</p>}
          {rows?.length === 0 && <p className="text-sm text-slate-500">No mandates yet. Purchases need your approval each time.</p>}
          <ul className="space-y-3">
            {rows?.map((m) => (
              <li key={m.id} className="rounded-2xl border border-slate-200 p-4 dark:border-slate-800">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="font-bold">{money(m.max_per_purchase, m.currency)} per purchase · {money(m.monthly_cap, m.currency)} / month</div>
                  <span className={`chip ring-1 ring-inset ${m.status === "ACTIVE" ? "bg-emerald-50 text-emerald-700 ring-emerald-600/20" : "bg-slate-100 text-slate-600 ring-slate-500/20"}`}>
                    {m.status === "ACTIVE" ? <Check size={12} strokeWidth={3} /> : <X size={12} />}{m.status.toLowerCase()}
                  </span>
                </div>
                <div className="mt-1 text-xs text-slate-500">
                  {money(m.spent_this_month, m.currency)} spent this month · until {new Date(m.expires_at).toLocaleDateString()}
                  {m.allowed_stores.length > 0 && <> · stores: {m.allowed_stores.join(", ")}</>}
                  {m.allowed_keywords.length > 0 && <> · products: {m.allowed_keywords.join(", ")}</>}
                  {m.require_established_store && <> · established stores only</>}
                </div>
                {m.status === "ACTIVE" && <button className="btn-danger mt-3 py-1 text-xs" onClick={() => revoke(m.id)}>Revoke</button>}
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}
