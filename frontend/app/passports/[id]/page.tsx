"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { api, API_URL, getToken } from "@/lib/api";
import { Card, Field, TriIcon, fmtTime, money, shortHash } from "@/components/ui";
import { Alert, ArrowLeft, Award, Check, CreditCard, Download, FileText, Hash, Lock, Package, Receipt, ShieldCheck, Sparkles, Store } from "@/components/icons";

type Passport = { id: string; passport_hash: string; body: Record<string, any> };

function Fact({ icon, label, value, sub }: { icon: ReactNode; label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-2xl bg-white/70 p-4 ring-1 ring-slate-200 dark:bg-slate-900/60 dark:ring-slate-800">
      <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-slate-500">{icon}{label}</div>
      <div className="mt-1 truncate text-base font-extrabold">{value}</div>
      {sub && <div className="truncate text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export default function PassportPage({ params }: { params: { id: string } }) {
  const [pp, setPp] = useState<Passport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api<Passport>(`/api/v1/passports/${params.id}`).then(setPp).catch((e) => setErr(e.message));
  }, [params.id]);

  const download = async () => {
    const r = await fetch(`${API_URL}/api/v1/passports/${params.id}/export`, { headers: { Authorization: `Bearer ${getToken()}` } });
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${params.id}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const copyHash = async () => {
    try {
      await navigator.clipboard.writeText(pp!.passport_hash);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard unavailable */ }
  };

  if (err) return <div className="card mx-auto max-w-lg py-10 text-center"><Alert size={28} className="mx-auto text-rose-500" /><p className="mt-3 font-semibold">{err}</p></div>;
  if (!pp) return <div className="space-y-4">{[220, 120, 300].map((h) => <div key={h} className="card animate-pulse" style={{ height: h }} />)}</div>;

  const b = pp.body;
  const rec = b.reconciliation;
  const pay = b.payment;
  const c = b.contract;
  const ok = rec.status === "RECONCILED";
  const store = b.purchase_channel === "external_store";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link href={`/workflows/${b.workflow_id}`} className="inline-flex items-center gap-1 text-xs font-semibold text-indigo-600 hover:text-indigo-500 dark:text-indigo-400"><ArrowLeft size={14} /> Back to workflow</Link>
        <button className="btn-secondary" onClick={download}><Download size={15} /> Export audit (JSON)</button>
      </div>

      {/* ------------------------------------------------------ certificate */}
      <section className={`relative overflow-hidden rounded-3xl p-[1.5px] shadow-xl ${ok ? "bg-gradient-to-br from-emerald-400 via-indigo-500 to-violet-500 shadow-indigo-500/20" : "bg-gradient-to-br from-amber-400 via-orange-500 to-rose-500 shadow-amber-500/20"}`}>
        <div className="relative rounded-[22px] bg-gradient-to-br from-white to-slate-50 p-6 dark:from-slate-900 dark:to-slate-950 sm:p-8">
          <div className="pointer-events-none absolute right-6 top-6 hidden h-28 w-28 place-items-center rounded-full border-4 border-dashed border-indigo-200 text-indigo-300 sm:grid dark:border-indigo-900 dark:text-indigo-800">
            <Award size={52} />
          </div>
          <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.2em] text-indigo-600 dark:text-indigo-400"><Sparkles size={14} /> Purchase Truth Passport</div>
          <h1 className="mt-2 max-w-2xl text-2xl font-extrabold tracking-tight sm:text-3xl">{b.selected_product.product_name}</h1>
          <p className="mt-1 text-sm text-slate-500">{b.selected_product.variant} · issued {new Date(b.generated_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</p>

          <div className={`mt-5 inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-bold ${ok ? "bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300" : "bg-amber-50 text-amber-800 ring-1 ring-amber-600/20 dark:bg-amber-950 dark:text-amber-300"}`}>
            {ok ? <Check size={16} strokeWidth={3} /> : <Alert size={16} />}
            {ok ? "Reconciled — every record agrees" : `${rec.status === "DISCREPANCY" ? "Discrepancy recorded" : "Evidence incomplete"}`}
          </div>

          <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {store ? (
              <Fact icon={<CreditCard size={13} />} label={`Paid at ${pay.store}`} value={money(pay.captured_amount, pay.captured_currency)} sub="reported by you" />
            ) : (
              <Fact icon={<CreditCard size={13} />} label="Charged via PayPal" value={money(pay.captured_amount, pay.captured_currency)} sub={pay.environment === "sandbox" ? "PayPal Sandbox" : "Simulated PayPal"} />
            )}
            <Fact icon={<FileText size={13} />} label="Contract limit" value={money(c.terms.max_total, c.terms.currency)} sub={`v${c.version} · approved`} />
            <Fact icon={<Store size={13} />} label="Seller" value={c.terms.seller_name} sub={(b.seller_evidence?.evidence_level ?? "").replace("_EVIDENCE", " evidence").toLowerCase()} />
            <Fact icon={<ShieldCheck size={13} />} label={store ? "Checklist" : "Checkout check"}
                  value={({ CONFIRMED_BY_YOU: "Confirmed by you", NOT_MET: "Not met", INCOMPLETE: "Incomplete" } as Record<string, string>)[b.checkout_verification.result] ?? b.checkout_verification.result}
                  sub={`${b.checkout_verification.conditions.length} ${store ? "items" : "conditions"}`} />
          </div>

          <button onClick={copyHash} className="mt-5 flex max-w-full items-center gap-2 rounded-xl bg-slate-100 px-3 py-2 text-left text-xs text-slate-600 transition hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300" title="Copy passport hash">
            <Hash size={14} className="shrink-0" /><span className="shrink-0 font-semibold">Passport SHA-256</span>
            <span className="mono truncate">{pp.passport_hash}</span>
            <span className="shrink-0 text-indigo-600">{copied ? "copied" : "copy"}</span>
          </button>
        </div>
      </section>

      {!ok && b.discrepancies.length > 0 && (
        <Card title="Discrepancies" icon={<Alert size={15} />} className="border-amber-200 dark:border-amber-900">
          <ul className="space-y-3">
            {b.discrepancies.map((d: any) => (
              <li key={d.code} className="flex gap-3 rounded-xl bg-amber-50 p-3 text-sm dark:bg-amber-950/40">
                <Alert size={16} className="mt-0.5 shrink-0 text-amber-600" />
                <div><div className="text-xs font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">{d.code.replaceAll("_", " ")} · {d.severity}</div><p className="mt-0.5">{d.description}</p></div>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-3">
        <Card title="Original request" icon={<Sparkles size={15} />}>
          <blockquote className="border-l-4 border-indigo-300 pl-3 text-sm italic text-slate-700 dark:border-indigo-700 dark:text-slate-300">“{b.original_intent.raw_text.split("\n")[0]}”</blockquote>
        </Card>
        <Card title="Cost breakdown" icon={<Receipt size={15} />}>
          {b.cost_breakdown.verified_checkout_lines.map((l: any, i: number) => (
            <Field key={i} label={l.label}>{money(l.amount, l.currency)}</Field>
          ))}
          {store && (
            <Field label={`Listed price${b.cost_breakdown.listed_price_as_of ? ` (${new Date(b.cost_breakdown.listed_price_as_of).toLocaleDateString()})` : ""}`}>
              {money(b.cost_breakdown.listed_price, b.cost_breakdown.currency)}
            </Field>
          )}
          <div className="mt-2 flex justify-between border-t border-slate-200 pt-2 text-sm font-extrabold dark:border-slate-700">
            <span>{store ? "Total you paid" : "Verified total"}</span><span>{money(b.cost_breakdown.verified_checkout_total, b.cost_breakdown.currency)}</span>
          </div>
          {store && <p className="mt-2 text-[11px] text-slate-500">Self-reported; CHECKMATE did not process this payment.</p>}
        </Card>
        <Card title="Purchase terms" icon={<Package size={15} />}>
          <Field label="Warranty">{b.purchase_terms?.warranty_months != null ? `${b.purchase_terms.warranty_months} months` : "Unknown"}</Field>
          <Field label="Return window">{b.purchase_terms?.return_window_days != null ? `${b.purchase_terms.return_window_days} days` : "Unknown"}</Field>
          <Field label="Free returns">{b.purchase_terms?.free_returns == null ? "Unknown" : b.purchase_terms.free_returns ? "Yes" : "No"}</Field>
          <Field label="Subscription">{b.purchase_terms?.subscription_required == null ? "Unknown" : b.purchase_terms.subscription_required ? "Required" : "None"}</Field>
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title={store ? "Confirmed at the store" : "Checkout verification"} icon={<ShieldCheck size={15} />}>
          {store && <p className="mb-3 text-xs text-slate-500">{b.checkout_verification.explanation}</p>}
          <ul className="space-y-2 text-sm">
            {b.checkout_verification.conditions.map((x: any) => (
              <li key={x.key} className="flex gap-2.5"><TriIcon value={x.result} /><span><b>{x.label}</b> — <span className="text-slate-600 dark:text-slate-300">{x.reason}</span></span></li>
            ))}
          </ul>
        </Card>
        <Card title="Reconciliation" icon={<Receipt size={15} />}>
          <ul className="space-y-2 text-sm">
            {rec.checks.map((x: any) => (
              <li key={x.key} className="flex gap-2.5"><TriIcon value={x.result} /><span><b>{x.label}</b> — <span className="text-slate-600 dark:text-slate-300">{x.reason}</span></span></li>
            ))}
          </ul>
          {store ? (
            <div className="mt-4 grid gap-2 text-xs sm:grid-cols-2">
              <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/50"><div className="font-bold text-slate-500">Store</div><div className="truncate">{pay.store}</div></div>
              <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/50"><div className="font-bold text-slate-500">Order number</div><div className="mono truncate">{pay.order_reference ?? "not given"}</div></div>
            </div>
          ) : (
            <div className="mt-4 grid gap-2 text-xs sm:grid-cols-2">
              <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/50"><div className="font-bold text-slate-500">PayPal order</div><div className="mono truncate">{pay.paypal_order_id}</div></div>
              <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/50"><div className="font-bold text-slate-500">PayPal capture</div><div className="mono truncate">{pay.paypal_capture_id}</div></div>
            </div>
          )}
          {b.receipt_evidence && !store && (
            <p className="mt-3 text-xs text-slate-500">Receipt {b.receipt_evidence.receipt_id}: <b>{money(b.receipt_evidence.total, b.receipt_evidence.currency)}</b> — {b.receipt_evidence.source_label}</p>
          )}
        </Card>
      </div>

      <Card title="Audit trail" icon={<Lock size={15} />} right={
        <span className={`chip ring-1 ring-inset ${b.audit_chain.verified ? "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300" : "bg-rose-50 text-rose-700 ring-rose-600/20"}`}>
          {b.audit_chain.verified ? <Check size={12} strokeWidth={3} /> : <Alert size={12} />} {b.audit_chain.events} hash-chained events · head {shortHash(b.audit_chain.head_hash)}
        </span>}>
        <ol className="relative max-h-[420px] space-y-2 overflow-y-auto border-l border-slate-200 pl-5 dark:border-slate-800">
          {b.event_timeline.map((e: any) => (
            <li key={e.seq} className="relative text-sm">
              <span className="absolute -left-[25px] top-1.5 h-2 w-2 rounded-full bg-indigo-400" />
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="tabular-nums text-xs text-slate-400">{fmtTime(e.at)}</span>
                <span className="mono text-[11px] text-slate-500">{e.type}</span>
              </div>
              <p className="leading-snug">{e.message}</p>
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}
