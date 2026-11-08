"use client";

import Link from "next/link";
import { Fragment, useState, type ReactNode } from "react";
import type { AgentRun, Condition, Contract, Evaluation, Payment, Suggestion, Verification, WfEvent, Workflow } from "@/lib/api";
import { Card, Field, Notice, TriBadge, TriIcon, fmtTime, money, plural, shortHash } from "@/components/ui";
import {
  Alert, ArrowRight, Award, Ban, Bot, Check, ChevronDown, Clock, CreditCard, Edit, Eye, FileText, Hash, Loader,
  Lock, Package, Question, Receipt, Refresh, Scale, Search, Shield, ShieldCheck, Sparkles, Store, X, Zap,
} from "@/components/icons";

/* =================================================================== stepper */
const STAGES: { label: string; states: string[]; icon: ReactNode }[] = [
  { label: "Understand", states: ["CREATED", "NEEDS_CLARIFICATION", "INTENT_PARSED"], icon: <Sparkles size={15} /> },
  { label: "Discover", states: ["DISCOVERING"], icon: <Search size={15} /> },
  { label: "Evaluate", states: ["EVALUATING", "EVIDENCE_INCOMPLETE"], icon: <Scale size={15} /> },
  { label: "Contract", states: ["CONTRACT_CREATED"], icon: <FileText size={15} /> },
  { label: "Verify", states: ["CHECKOUT_VERIFICATION", "CHECKOUT_BLOCKED"], icon: <ShieldCheck size={15} /> },
  { label: "Pay", states: ["PAYMENT_PENDING_APPROVAL", "PAYMENT_FAILED", "PAYMENT_CAPTURED"], icon: <CreditCard size={15} /> },
  { label: "Reconcile", states: ["RECONCILIATION", "RECONCILIATION_REQUIRED"], icon: <Receipt size={15} /> },
  { label: "Passport", states: ["PASSPORT_GENERATED", "COMPLETED"], icon: <Award size={15} /> },
];
const WARN = new Set(["EVIDENCE_INCOMPLETE", "NEEDS_CLARIFICATION", "RECONCILIATION_REQUIRED"]);
const BAD = new Set(["CHECKOUT_BLOCKED", "PAYMENT_FAILED"]);
const RUNNING = new Set(["CREATED", "INTENT_PARSED", "DISCOVERING", "EVALUATING", "CHECKOUT_VERIFICATION", "RECONCILIATION"]);

export function Stepper({ state }: { state: string }) {
  const idx = STAGES.findIndex((s) => s.states.includes(state));
  const done = state === "COMPLETED";
  return (
    <ol className="card flex items-start gap-1 overflow-x-auto p-4" aria-label="Purchase lifecycle">
      {STAGES.map((s, i) => {
        const current = i === idx && !done;
        const passed = i < idx || done;
        const tone = current && BAD.has(state) ? "bad" : current && WARN.has(state) ? "warn" : current ? "now" : passed ? "done" : "todo";
        const dot = {
          done: "bg-emerald-500 text-white",
          now: "bg-indigo-600 text-white ring-4 ring-indigo-100 dark:ring-indigo-900/60",
          warn: "bg-amber-400 text-amber-950 ring-4 ring-amber-100 dark:ring-amber-900/50",
          bad: "bg-rose-500 text-white ring-4 ring-rose-100 dark:ring-rose-900/50",
          todo: "bg-slate-100 text-slate-400 dark:bg-slate-800",
        }[tone];
        return (
          <li key={s.label} className="flex min-w-[76px] flex-1 flex-col items-center gap-2 text-center" aria-current={current ? "step" : undefined}>
            <div className="flex w-full items-center">
              <div className={`h-0.5 flex-1 rounded ${i === 0 ? "opacity-0" : passed || current ? "bg-emerald-400" : "bg-slate-200 dark:bg-slate-800"}`} />
              <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-full transition ${dot}`}>
                {tone === "done" ? <Check size={15} strokeWidth={3} /> : tone === "bad" ? <X size={15} strokeWidth={3} /> :
                  tone === "warn" ? <Alert size={15} /> : current && RUNNING.has(state) ? <Loader size={15} /> : s.icon}
              </span>
              <div className={`h-0.5 flex-1 rounded ${i === STAGES.length - 1 ? "opacity-0" : passed ? "bg-emerald-400" : "bg-slate-200 dark:bg-slate-800"}`} />
            </div>
            <span className={`text-[11px] font-semibold ${current ? "text-slate-900 dark:text-white" : passed ? "text-slate-600 dark:text-slate-300" : "text-slate-400"}`}>{s.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

/* ============================================================= why not */
export function blockingConditions(e: Evaluation): Condition[] {
  const order = { FAIL: 0, UNKNOWN: 1, PASS: 2 } as const;
  return e.conditions.filter((c) => c.mandatory && c.result !== "PASS").sort((a, b) => order[a.result] - order[b.result]);
}

function reasonText(c: Condition, cur?: string | null) {
  if (c.key === "budget" && c.observed && c.result === "FAIL") {
    const total = c.observed.replace(" (includes estimated tax)", "");
    return `Total ${money(total, cur)} — over your ${money(c.approved?.replace("<= ", "") ?? null, cur)} limit`;
  }
  if (c.key === "specifications") return c.reason.split(";")[0];
  return `${c.label}: ${c.reason.replace(/\.$/, "")}`;
}

const SUGGESTION_ICON: Record<string, ReactNode> = {
  raise_budget: <CreditCard size={18} />, relax_specs: <Package size={18} />, accept_limited_seller: <Store size={18} />,
  lower_warranty: <Shield size={18} />,
};

/* =========================================================== next step */
type Actions = {
  approve: () => void; verify: () => void; pay: () => void; retry: () => void; cancel: () => void;
  revise: (s: Suggestion) => void; clarify: (text: string) => void; selectAlt: (id: string) => void;
  switchToBuy: () => void;
};

function Banner({ tone, icon, title, children }: { tone: "indigo" | "amber" | "rose" | "emerald" | "slate" | "sky"; icon: ReactNode; title: string; children?: ReactNode }) {
  const { grad, bg } = {
    indigo: { grad: "from-indigo-500 to-violet-500", bg: "bg-indigo-50/60 dark:bg-indigo-950/30" },
    amber: { grad: "from-amber-400 to-orange-500", bg: "bg-amber-50/70 dark:bg-amber-950/30" },
    rose: { grad: "from-rose-500 to-pink-500", bg: "bg-rose-50/70 dark:bg-rose-950/30" },
    emerald: { grad: "from-emerald-500 to-teal-500", bg: "bg-emerald-50/70 dark:bg-emerald-950/30" },
    slate: { grad: "from-slate-400 to-slate-500", bg: "bg-slate-50 dark:bg-slate-900" },
    sky: { grad: "from-sky-500 to-cyan-500", bg: "bg-sky-50/70 dark:bg-sky-950/30" },
  }[tone];
  return (
    <section className="card animate-fade-up relative overflow-hidden border-0 p-0 ring-1 ring-slate-200/80 dark:ring-slate-800">
      <div className={`absolute inset-y-0 left-0 w-1.5 bg-gradient-to-b ${grad}`} />
      <div className={`${bg} p-5 pl-7`}>
        <div className="flex items-start gap-4">
          <span className={`grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-gradient-to-br text-white shadow-sm ${grad}`}>{icon}</span>
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-bold leading-tight">{title}</h2>
            <div className="mt-1.5 text-sm text-slate-600 dark:text-slate-300">{children}</div>
          </div>
        </div>
      </div>
    </section>
  );
}

export function NextStep({ wf, lastMessage, busy, actions }: { wf: Workflow; lastMessage?: string; busy: boolean; actions: Actions }) {
  const [clar, setClar] = useState("");
  const c = wf.contract;
  const v = wf.verification;
  const pay = wf.payments.find((p) => p.verification_id === v?.id && !["CANCELLED", "FAILED"].includes(p.payment_status));
  const firstLine = wf.request.split("\n")[0].replace(/ \[revised:.*\]$/, "");

  const watching = wf.autonomy_mode === "MONITORING_ONLY";
  if (watching && ["CONTRACT_CREATED", "CHECKOUT_BLOCKED", "PAYMENT_PENDING_APPROVAL", "PAYMENT_FAILED"].includes(wf.state)) {
    return (
      <Banner tone="indigo" icon={<Eye size={22} />} title="You're watching this offer — ready to buy it?">
        <p>This workflow was started in <b>Just watch</b> mode, so payment is switched off. The Monitoring Agent keeps checking
          {c ? <> <b>{c.terms.product_name}</b> ({money(c.terms.expected_total, c.terms.currency)} expected, limit {money(c.terms.max_total, c.terms.currency)})</> : " the offer"} for price and terms changes.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <button className="btn-primary" disabled={busy} onClick={actions.switchToBuy}><CreditCard size={16} /> Switch to buying</button>
          <a href="#contract" className="btn-secondary">Review contract</a>
        </div>
        <p className="mt-2 text-[11px] text-slate-500">Switching keeps every safeguard: you still approve the contract, the checkout must PASS, and you approve on PayPal.</p>
      </Banner>
    );
  }

  if (RUNNING.has(wf.state)) {
    return (
      <Banner tone="sky" icon={<Loader size={22} />} title={wf.state === "RECONCILIATION" ? "Reconciling your payment…" :
        wf.state === "CHECKOUT_VERIFICATION" ? "Checking the final checkout…" : "Your agents are on it…"}>
        <p>{lastMessage ?? "Starting up."}</p>
      </Banner>
    );
  }

  switch (wf.state) {
    case "NEEDS_CLARIFICATION":
      return (
        <Banner tone="amber" icon={<Question size={22} />} title="One quick question before we search">
          <ul className="mb-3 space-y-1">
            {[...(wf.intent?.clarifications_needed ?? []), ...(wf.intent?.conflicts ?? [])].map((q: string) => (
              <li key={q} className="flex gap-2"><ArrowRight size={14} className="mt-0.5 shrink-0" />{q}</li>
            ))}
          </ul>
          <form className="flex flex-col gap-2 sm:flex-row" onSubmit={(e) => { e.preventDefault(); if (clar.trim()) actions.clarify(clar); }}>
            <input className="input" value={clar} onChange={(e) => setClar(e.target.value)} placeholder="e.g. USD, budget $110" autoFocus />
            <button className="btn-primary" disabled={busy || !clar.trim()}>Continue <ArrowRight size={16} /></button>
          </form>
        </Banner>
      );

    case "EVIDENCE_INCOMPLETE": {
      if (!wf.error && wf.evaluations.length === 0) {
        return (
          <Banner tone="amber" icon={<Search size={22} />} title={`We couldn't find any "${wf.intent?.product_description ?? "matching products"}"`}>
            <p>None of the supported stores returned a listing for this request. Try describing the product differently (for example "office chair" instead of a brand-only name).</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Link href={`/?q=${encodeURIComponent(firstLine)}`} className="btn-primary"><Edit size={15} /> Edit request</Link>
              <button className="btn-ghost" disabled={busy} onClick={actions.retry}><Refresh size={15} /> Search again</button>
            </div>
          </Banner>
        );
      }
      const seen = new Set<string>();
      const evals = wf.evaluations.filter((e) => (seen.has(e.candidate.id) ? false : (seen.add(e.candidate.id), true)));
      const closest = [...evals].sort((a, b) => blockingConditions(a).length - blockingConditions(b).length).slice(0, 4);
      return (
        <Banner tone="amber" icon={<Shield size={22} />} title="No offer met all of your requirements">
          {wf.error ? (
            <p>An agent failed while gathering evidence, so nothing could be verified. Nothing was purchased.</p>
          ) : (
            <p>CHECKMATE won't contract an offer that breaks your rules or that it can't verify. Here's what came closest and why each was rejected:</p>
          )}
          {closest.length > 0 && (
            <ul className="mt-3 grid gap-2 sm:grid-cols-2">
              {closest.map((e) => {
                const why = blockingConditions(e);
                return (
                  <li key={e.candidate.id} className="rounded-xl bg-white/80 p-3 ring-1 ring-slate-200 dark:bg-slate-900/60 dark:ring-slate-800">
                    <div className="flex items-start justify-between gap-2">
                      <span className="font-semibold text-slate-900 dark:text-white">{e.candidate.product_name}</span>
                      <span className="shrink-0 text-xs font-semibold tabular-nums">{e.cost?.total ? money(e.cost.total, e.cost.currency) : "total unknown"}</span>
                    </div>
                    <ul className="mt-1.5 space-y-1 text-xs">
                      {why.slice(0, 2).map((c) => (
                        <li key={c.key} className="flex items-start gap-1.5"><TriIcon value={c.result} /><span>{reasonText(c, wf.intent?.currency)}</span></li>
                      ))}
                      {why.length > 2 && <li className="pl-6 text-slate-400">+{why.length - 2} more</li>}
                    </ul>
                  </li>
                );
              })}
            </ul>
          )}
          {wf.suggestions.length > 0 && (
            <div className="mt-4">
              <div className="mb-2 text-xs font-bold uppercase tracking-wider text-slate-500">Would one of these work for you?</div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {wf.suggestions.map((s) => (
                  <button key={s.label + s.product_id} disabled={busy} onClick={() => actions.revise(s)}
                          className="group flex items-start gap-3 rounded-xl bg-white p-3 text-left ring-1 ring-indigo-200 transition hover:-translate-y-0.5 hover:shadow-md hover:ring-indigo-400 disabled:opacity-50 dark:bg-slate-900 dark:ring-indigo-900">
                    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-indigo-50 text-indigo-600 dark:bg-indigo-950 dark:text-indigo-300">{SUGGESTION_ICON[s.kind]}</span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-1 font-semibold text-slate-900 dark:text-white">{s.label}<ArrowRight size={14} className="text-indigo-500 transition group-hover:translate-x-0.5" /></span>
                      <span className="block text-xs text-slate-500">{s.detail}</span>
                    </span>
                  </button>
                ))}
              </div>
              <p className="mt-2 text-[11px] text-slate-500">Each option starts a new, separately audited search. Nothing else about your rules changes.</p>
            </div>
          )}
          <div className="mt-4 flex flex-wrap gap-2">
            <Link href={`/?q=${encodeURIComponent(firstLine)}`} className="btn-secondary"><Edit size={15} /> Edit request</Link>
            <button className={wf.error ? "btn-primary" : "btn-ghost"} disabled={busy} onClick={actions.retry}><Refresh size={15} /> Search again</button>
          </div>
        </Banner>
      );
    }

    case "CONTRACT_CREATED":
      if (!c?.approved) {
        return (
          <Banner tone="indigo" icon={<FileText size={22} />} title="Review and approve your purchase contract">
            <p>The agents found <b>{c?.terms.product_name}</b> for an expected <b>{money(c?.terms.expected_total, c?.terms.currency)}</b> (your limit: {money(c?.terms.max_total, c?.terms.currency)}). Approving locks these terms; any change will need your approval again.</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <button className="btn-primary" disabled={busy} onClick={actions.approve}><Check size={16} /> Approve contract v{c?.version}</button>
              <a href="#contract" className="btn-secondary">Review terms</a>
            </div>
          </Banner>
        );
      }
      return (
        <Banner tone="indigo" icon={<ShieldCheck size={22} />} title="Contract approved — verify the final checkout">
          <p>The Verification Agent compares the merchant's final checkout to your contract, line by line, before any money moves.</p>
          <div className="mt-3"><button className="btn-primary" onClick={actions.verify}><ShieldCheck size={16} /> Choose checkout & verify</button></div>
        </Banner>
      );

    case "CHECKOUT_BLOCKED":
      return (
        <Banner tone="rose" icon={<Ban size={22} />} title="Payment blocked to protect you">
          <p>The checkout didn't match your contract, so the policy engine refused payment.</p>
          {v && (
            <ul className="mt-2 space-y-1">
              {v.conditions.filter((x) => x.result !== "PASS").map((x) => (
                <li key={x.key} className="flex items-start gap-2"><TriIcon value={x.result} /><span><b>{x.label}:</b> {x.reason}</span></li>
              ))}
            </ul>
          )}
          {wf.alternatives.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {wf.alternatives.map((a) => (
                <button key={a.product_id} className="btn-secondary" disabled={busy} onClick={() => actions.selectAlt(a.product_id)}>
                  Switch to {a.product_name} · {money(a.total, a.currency)} <ArrowRight size={14} />
                </button>
              ))}
            </div>
          )}
          <div className="mt-2"><button className="btn-ghost" onClick={actions.verify}><Refresh size={15} /> Re-check checkout</button></div>
        </Banner>
      );

    case "PAYMENT_PENDING_APPROVAL":
      return (
        <Banner tone="emerald" icon={<Check size={22} strokeWidth={3} />} title="Checkout verified — every condition passed">
          <p>Final total <b>{money(v?.checkout_snapshot?.stated_total, v?.checkout_snapshot?.currency)}</b>. Approve the payment on PayPal; capture only happens after the policy engine re-authorises it.</p>
          <div className="mt-3">
            {pay?.payment_status === "APPROVAL_PENDING" && pay.approve_url
              ? <a className="btn-paypal" href={pay.approve_url}>Continue to PayPal</a>
              : <button className="btn-paypal" disabled={busy} onClick={actions.pay}>Pay with <span className="italic">PayPal</span></button>}
          </div>
        </Banner>
      );

    case "PAYMENT_FAILED":
      return (
        <Banner tone="rose" icon={<CreditCard size={22} />} title="Payment didn't go through">
          <p>{wf.payments[0]?.error ?? "PayPal declined or the order could not be created."} No money was captured.</p>
          <div className="mt-3"><button className="btn-paypal" disabled={busy} onClick={actions.pay}>Try PayPal again</button></div>
        </Banner>
      );

    case "COMPLETED":
      return wf.has_discrepancy ? (
        <Banner tone="amber" icon={<Alert size={22} />} title="Paid — but the merchant's receipt doesn't match">
          <p>PayPal's record is authoritative for what you were charged. The discrepancy is recorded in your Purchase Truth Passport so you can raise it with the merchant.</p>
          {wf.passport_id && <div className="mt-3"><Link className="btn-primary" href={`/passports/${wf.passport_id}`}><Award size={16} /> Open passport</Link></div>}
        </Banner>
      ) : (
        <Banner tone="emerald" icon={<Award size={22} />} title="Purchase complete and verified">
          <p>Intent, contract, checkout, PayPal capture and receipt all agree.</p>
          {wf.passport_id && <div className="mt-3"><Link className="btn-primary" href={`/passports/${wf.passport_id}`}><Award size={16} /> View Purchase Truth Passport</Link></div>}
        </Banner>
      );

    case "CANCELLED":
      return <Banner tone="slate" icon={<X size={22} />} title="Workflow cancelled">All approvals were revoked. Nothing will be purchased.</Banner>;
    case "EXPIRED":
      return <Banner tone="slate" icon={<Clock size={22} />} title="Contract expired">Start a new search to get fresh offers.</Banner>;
    default:
      return null;
  }
}

/* ============================================================ intent */
export function IntentSummary({ intent }: { intent: Record<string, any> }) {
  const rules: { icon: ReactNode; label: string; value: string }[] = [
    { icon: <CreditCard size={15} />, label: "Max total", value: money(intent.max_budget, intent.currency) },
    { icon: <Shield size={15} />, label: "Warranty", value: intent.warranty_requirement.min_months ? `≥ ${intent.warranty_requirement.min_months} months` : intent.warranty_requirement.required ? "Required" : "Any" },
    { icon: <Refresh size={15} />, label: "Returns", value: intent.return_requirement.required ? (intent.return_requirement.free_returns ? "Free returns" : "Required") : "Any" },
    { icon: <Ban size={15} />, label: "Subscriptions", value: intent.subscription_allowed ? "Allowed" : "Not allowed" },
    { icon: <Store size={15} />, label: "Seller evidence", value: String(intent.seller_preferences.min_evidence_level).replace("_EVIDENCE", "").toLowerCase().replace(/^./, (s: string) => s.toUpperCase()) + "+" },
    { icon: <Package size={15} />, label: "Quantity", value: String(intent.quantity) },
  ];
  const specs = Object.entries(intent.specifications as Record<string, string>);
  return (
    <Card title="Your rules" icon={<Scale size={15} />} right={<span className="chip bg-slate-100 text-slate-500 dark:bg-slate-800">{intent.parser === "rules" ? "rule parser" : "LLM + rules"}</span>}>
      <p className="mb-4 text-base font-semibold capitalize">{intent.product_description}</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {rules.map((r) => (
          <div key={r.label} className="rounded-xl bg-slate-50 p-3 ring-1 ring-inset ring-slate-100 dark:bg-slate-800/60 dark:ring-slate-800">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{r.icon}{r.label}</div>
            <div className="mt-1 text-sm font-bold">{r.value}</div>
          </div>
        ))}
      </div>
      {(specs.length > 0 || intent.preferred_variant) && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {specs.map(([k, v]) => <span key={k} className="chip bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/10 dark:bg-indigo-950 dark:text-indigo-300">{k.replace(/_/g, " ")}: {v}</span>)}
          {intent.preferred_variant && <span className="chip bg-indigo-50 text-indigo-700 ring-1 ring-inset ring-indigo-600/10 dark:bg-indigo-950 dark:text-indigo-300">colour: {intent.preferred_variant}</span>}
        </div>
      )}
      {intent.assumptions?.length > 0 && (
        <p className="mt-3 flex gap-1.5 text-xs text-slate-500"><Lock size={13} className="mt-0.5 shrink-0" />Safe defaults: {intent.assumptions.join(" ")}</p>
      )}
    </Card>
  );
}

/* ============================================================== offers */
const EVIDENCE_STYLE: Record<string, string> = {
  STRONG_EVIDENCE: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  LIMITED_EVIDENCE: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  INSUFFICIENT_EVIDENCE: "bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300",
};

export function OfferCards({ evaluations, selectedId }: { evaluations: Evaluation[]; selectedId?: string }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!evaluations.length) return null;
  const seen = new Set<string>();
  const rank = { PASS: 0, UNKNOWN: 1, FAIL: 2 } as const;
  const rows = evaluations
    .filter((e) => (seen.has(e.candidate.id) ? false : (seen.add(e.candidate.id), true)))
    .sort((a, b) => Number(b.candidate.id === selectedId) - Number(a.candidate.id === selectedId) || rank[a.status] - rank[b.status]);
  const passing = rows.filter((r) => r.status === "PASS").length;
  return (
    <Card title="Offers evaluated" icon={<Search size={15} />} right={<span className="text-xs text-slate-500">{passing} of {plural(rows.length, "offer")} qualify</span>}>
      <div className="grid gap-3">
        {rows.map((e) => {
          const c = e.candidate;
          const selected = c.id === selectedId;
          const isOpen = open === c.id;
          const lvl = e.seller?.evidence_level ?? "INSUFFICIENT_EVIDENCE";
          const shown = e.conditions.filter((x) => x.key !== "currency");
          return (
            <article key={c.id} className={`rounded-2xl border p-4 transition ${selected ? "border-indigo-300 bg-indigo-50/40 ring-2 ring-indigo-200 dark:border-indigo-800 dark:bg-indigo-950/20 dark:ring-indigo-900" : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"} ${e.status !== "PASS" && !selected ? "opacity-90" : ""}`}>
              <div className="flex flex-wrap items-start gap-3">
                <span className={`grid h-12 w-12 shrink-0 place-items-center rounded-xl ${e.status === "PASS" ? "bg-gradient-to-br from-indigo-500 to-violet-500 text-white" : "bg-slate-100 text-slate-400 dark:bg-slate-800"}`}>
                  <Package size={22} />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="font-bold">{c.product_name}</h3>
                    {selected && <span className="chip bg-indigo-600 text-white"><Check size={12} strokeWidth={3} /> In your contract</span>}
                    {c.simulated && <span className="chip bg-slate-100 text-[10px] font-semibold uppercase text-slate-500 dark:bg-slate-800" title="Generated by the controlled demo merchant for this request">Simulated listing</span>}
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
                    <span>{c.variant ?? "—"}</span><span>·</span>
                    <span className="flex items-center gap-1"><Store size={12} />{e.seller?.seller_name ?? c.listing_seller_name}</span>
                    <span className={`chip px-2 py-0.5 text-[10px] font-bold uppercase ${EVIDENCE_STYLE[lvl]}`}>{lvl.replace("_EVIDENCE", "")} evidence</span>
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-lg font-extrabold tabular-nums">{e.cost?.total ? money(e.cost.total, e.cost.currency) : <span className="text-amber-600">Unknown</span>}</div>
                  <div className="text-[11px] text-slate-500">{e.cost?.total ? `total · item ${money(c.price, c.currency)}` : `≥ ${money(e.cost?.known_subtotal, e.cost?.currency)} known`}</div>
                </div>
                <TriBadge value={e.status} />
              </div>

              <div className="mt-3 flex flex-wrap gap-1.5">
                {shown.map((x) => (
                  <span key={x.key} title={x.reason}
                        className={`chip ring-1 ring-inset ${x.result === "PASS" ? "bg-emerald-50 text-emerald-700 ring-emerald-600/15 dark:bg-emerald-950/50 dark:text-emerald-300" : x.result === "FAIL" ? "bg-rose-50 text-rose-700 ring-rose-600/15 dark:bg-rose-950/50 dark:text-rose-300" : "bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-950/50 dark:text-amber-300"} ${!x.mandatory ? "border border-dashed border-current" : ""}`}>
                    {x.result === "PASS" ? <Check size={11} strokeWidth={3} /> : x.result === "FAIL" ? <X size={11} strokeWidth={3} /> : <Question size={11} />}
                    {x.label}
                  </span>
                ))}
              </div>

              <button onClick={() => setOpen(isOpen ? null : c.id)} className="mt-3 flex items-center gap-1 text-xs font-semibold text-indigo-600 hover:text-indigo-500 dark:text-indigo-400" aria-expanded={isOpen}>
                <ChevronDown size={14} className={`transition ${isOpen ? "rotate-180" : ""}`} />{isOpen ? "Hide evidence" : "Show evidence"}
              </button>
              {isOpen && (
                <div className="mt-3 grid gap-4 border-t border-slate-100 pt-3 text-xs dark:border-slate-800 md:grid-cols-2">
                  <ul className="space-y-1.5">
                    {e.conditions.map((x) => (
                      <li key={x.key} className="flex gap-2"><TriIcon value={x.result} /><span><b>{x.label}</b>{!x.mandatory && " (advisory)"} — {x.reason}</span></li>
                    ))}
                  </ul>
                  <div className="space-y-2.5">
                    {e.terms?.injection_flags.length ? (
                      <Notice tone="rose"><b>Hidden instructions in the listing were detected and ignored:</b> {e.terms.injection_flags.slice(0, 3).map((f) => `“${f}”`).join(", ")}</Notice>
                    ) : null}
                    {e.seller && <p><b>Seller evidence:</b> {e.seller.summary} <span className="italic text-slate-400">{e.seller.disclaimer}</span></p>}
                    {e.cost && (
                      <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/50">
                        {e.cost.lines.map((l, i) => (
                          <div key={i} className="flex justify-between gap-2 py-0.5">
                            <span>{l.label}{l.recurring && <span className="text-rose-600"> / {l.interval}</span>}</span>
                            <span className={`tabular-nums ${l.amount === null ? "text-amber-600" : ""}`}>{money(l.amount, l.currency)}<span className="ml-1 text-[10px] uppercase text-slate-400">{l.status.toLowerCase()}</span></span>
                          </div>
                        ))}
                      </div>
                    )}
                    <a href={c.product_url} className="inline-flex items-center gap-1 text-indigo-600 hover:underline" target="_blank" rel="noreferrer">Listing <ArrowRight size={12} /></a>
                  </div>
                </div>
              )}
            </article>
          );
        })}
      </div>
      <p className="mt-3 text-xs text-slate-500">Dashed chips are advisory. Unknown information is never treated as a pass.</p>
    </Card>
  );
}

/* ============================================================ contract */
export function ContractCard({ contract, state, busy, onApprove, onRevoke }: {
  contract: Contract; state: string; busy: boolean; onApprove: () => void; onRevoke: () => void;
}) {
  const t = contract.terms;
  const editable = ["CONTRACT_CREATED", "CHECKOUT_BLOCKED", "PAYMENT_PENDING_APPROVAL", "PAYMENT_FAILED"].includes(state);
  return (
    <section id="contract" className="card animate-fade-up scroll-mt-24 overflow-hidden p-0">
      <div className="flex items-center justify-between bg-gradient-to-r from-slate-900 to-indigo-950 px-5 py-3 text-white">
        <span className="flex items-center gap-2 text-xs font-bold uppercase tracking-widest"><FileText size={15} /> Purchase contract</span>
        <span className="chip bg-white/15 text-white">v{contract.version}</span>
      </div>
      <div className="p-5">
        <div className="text-lg font-bold leading-tight">{t.product_name}</div>
        <div className="text-sm text-slate-500">{t.model} · {t.exact_variant ?? "—"} · from {t.seller_name}</div>
        <div className="my-4 grid grid-cols-2 gap-2">
          <div className="rounded-xl bg-indigo-50 p-3 dark:bg-indigo-950/40">
            <div className="text-[11px] font-semibold uppercase text-indigo-600 dark:text-indigo-300">Maximum total</div>
            <div className="text-xl font-extrabold tabular-nums">{money(t.max_total, t.currency)}</div>
          </div>
          <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/50">
            <div className="text-[11px] font-semibold uppercase text-slate-500">Expected</div>
            <div className="text-xl font-extrabold tabular-nums">{money(t.expected_total, t.currency)}</div>
          </div>
        </div>
        <Field label="Quantity">{t.quantity}</Field>
        <Field label="Warranty">{t.warranty_min_months ? `≥ ${t.warranty_min_months} months` : t.warranty_required ? "Required" : "Not required"}</Field>
        <Field label="Returns">{t.return_required ? `${t.free_returns_required ? "Free returns" : "Returns"}${t.min_return_days ? ` ≥ ${t.min_return_days} days` : ""}` : "Not required"}</Field>
        <Field label="Subscription">{t.subscription_allowed ? "Allowed" : <span className="text-rose-600">Not allowed</span>}</Field>
        <Field label="Add-ons">{t.approved_addons.length ? t.approved_addons.join(", ") : "None permitted"}</Field>
        <Field label="Valid until">{new Date(contract.expires_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</Field>
        <div className="mt-3 flex items-center gap-2 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500 dark:bg-slate-800/50" title={contract.contract_hash}>
          <Hash size={13} /> SHA-256 <span className="mono text-slate-700 dark:text-slate-300">{shortHash(contract.contract_hash)}</span>
        </div>

        <div className="mt-4">
          {contract.approved ? (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="chip bg-emerald-50 py-1.5 text-sm font-bold text-emerald-700 ring-1 ring-inset ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300"><Check size={14} strokeWidth={3} /> Approved by you</span>
              {editable && <button className="btn-danger py-1.5" disabled={busy} onClick={onRevoke}>Revoke</button>}
            </div>
          ) : editable ? (
            <button className="btn-primary w-full py-2.5" disabled={busy} onClick={onApprove}><Check size={16} /> Approve contract v{contract.version}</button>
          ) : (
            <span className="chip bg-slate-100 text-slate-600 dark:bg-slate-800">Status: {contract.approval_status.toLowerCase()}</span>
          )}
          {!contract.approved && contract.approval_status !== "PENDING" && editable && (
            <p className="mt-2 text-xs text-amber-700">Previous approval {contract.approval_status.toLowerCase()} — the contract changed or was revoked.</p>
          )}
        </div>

        {contract.versions.length > 1 && (
          <div className="mt-5 border-t border-slate-100 pt-4 dark:border-slate-800">
            <div className="mb-2 text-[11px] font-bold uppercase tracking-wider text-slate-500">Version history</div>
            <ol className="relative space-y-3 border-l border-slate-200 pl-4 dark:border-slate-700">
              {contract.versions.slice().reverse().map((v) => (
                <li key={v.version} className="text-xs">
                  <span className={`absolute -left-[5px] mt-1 h-2.5 w-2.5 rounded-full ${v.version === contract.version ? "bg-indigo-500" : "bg-slate-300 dark:bg-slate-600"}`} />
                  <b>v{v.version}</b> <span className="mono text-slate-400">{shortHash(v.contract_hash)}</span>
                  <div className="text-slate-500">{v.reason}</div>
                </li>
              ))}
            </ol>
            <p className="mt-2 text-[11px] text-slate-400">A material change always invalidates earlier approvals.</p>
          </div>
        )}
      </div>
    </section>
  );
}

/* ======================================================== verification */
const SCENARIO_UI: Record<string, { title: string; tone: string; icon: ReactNode }> = {
  clean: { title: "Honest checkout", tone: "emerald", icon: <Check size={15} strokeWidth={3} /> },
  subscription_addon: { title: "Sneaky subscription", tone: "rose", icon: <Refresh size={15} /> },
  variant_swap: { title: "Wrong variant", tone: "rose", icon: <Package size={15} /> },
  seller_swap: { title: "Different seller", tone: "rose", icon: <Store size={15} /> },
  currency_swap: { title: "Currency switch", tone: "rose", icon: <CreditCard size={15} /> },
  hidden_fee: { title: "Hidden fee", tone: "amber", icon: <Eye size={15} /> },
  warranty_removed: { title: "Warranty dropped", tone: "amber", icon: <Shield size={15} /> },
  receipt_discrepancy: { title: "Receipt mismatch", tone: "violet", icon: <Receipt size={15} /> },
};
const TONE_RING: Record<string, string> = {
  emerald: "text-emerald-600 bg-emerald-50 dark:bg-emerald-950", rose: "text-rose-600 bg-rose-50 dark:bg-rose-950",
  amber: "text-amber-600 bg-amber-50 dark:bg-amber-950", violet: "text-violet-600 bg-violet-50 dark:bg-violet-950",
};

export function VerificationCard({ verification, scenarios, canVerify, busy, onVerify }: {
  verification: Verification | null; scenarios: Record<string, string>; canVerify: boolean; busy: boolean;
  onVerify: (s: string) => void;
}) {
  const [scenario, setScenario] = useState("clean");
  const v = verification;
  return (
    <Card id="verify" title="Checkout verification" icon={<ShieldCheck size={15} />}
          right={<span className="chip bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-600/20 dark:bg-amber-950 dark:text-amber-300">Demo merchant</span>}>
      <p className="mb-3 text-sm text-slate-500">Pick how the demo merchant behaves at checkout, then let the Verification Agent compare it with your contract.</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" role="radiogroup" aria-label="Checkout scenario">
        {Object.entries(scenarios).map(([k, desc]) => {
          const ui = SCENARIO_UI[k] ?? { title: k, tone: "amber", icon: <Zap size={15} /> };
          const on = scenario === k;
          return (
            <button key={k} role="radio" aria-checked={on} title={desc} onClick={() => setScenario(k)}
                    className={`flex flex-col items-start gap-1.5 rounded-xl border p-3 text-left text-xs transition ${on ? "border-indigo-400 bg-indigo-50/60 ring-2 ring-indigo-200 dark:border-indigo-700 dark:bg-indigo-950/30 dark:ring-indigo-900" : "border-slate-200 hover:border-slate-300 dark:border-slate-800"}`}>
              <span className={`grid h-7 w-7 place-items-center rounded-lg ${TONE_RING[ui.tone]}`}>{ui.icon}</span>
              <span className="font-bold text-slate-800 dark:text-slate-100">{ui.title}</span>
              <span className="line-clamp-2 text-[11px] leading-snug text-slate-500">{desc}</span>
            </button>
          );
        })}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={!canVerify || busy} onClick={() => onVerify(scenario)}>
          {busy ? <Loader size={16} /> : <ShieldCheck size={16} />} Verify final checkout
        </button>
        {!canVerify && <span className="text-xs text-slate-500">{v ? "Verification isn't available in the current step." : "Approve the contract first."}</span>}
      </div>

      {v && (
        <div className="mt-5 space-y-4 border-t border-slate-100 pt-5 dark:border-slate-800">
          <div className={`flex items-center gap-3 rounded-2xl p-4 ${v.status === "PASS" ? "bg-emerald-50 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200" : v.status === "FAIL" ? "bg-rose-50 text-rose-900 dark:bg-rose-950/40 dark:text-rose-200" : "bg-amber-50 text-amber-900 dark:bg-amber-950/40 dark:text-amber-200"}`}>
            <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-full text-white ${v.status === "PASS" ? "bg-emerald-500" : v.status === "FAIL" ? "bg-rose-500" : "bg-amber-500"}`}>
              {v.status === "PASS" ? <Check size={20} strokeWidth={3} /> : v.status === "FAIL" ? <Ban size={20} /> : <Question size={20} />}
            </span>
            <div>
              <div className="font-extrabold">{v.status === "PASS" ? "PASS — payment eligible" : `${v.status} — payment blocked`}</div>
              <div className="text-xs opacity-80">Scenario: {SCENARIO_UI[v.scenario ?? ""]?.title ?? v.scenario} · against contract v{v.contract_version}</div>
            </div>
          </div>

          <div className="overflow-x-auto rounded-xl ring-1 ring-slate-200 dark:ring-slate-800">
            <table className="w-full min-w-[520px] text-left text-sm">
              <thead className="bg-slate-50 text-[11px] uppercase tracking-wider text-slate-500 dark:bg-slate-800/50">
                <tr><th className="px-3 py-2">Condition</th><th className="px-3 py-2">Approved</th><th className="px-3 py-2">Checkout</th><th className="px-3 py-2 text-right">Result</th></tr>
              </thead>
              <tbody>
                {v.conditions.map((c) => (
                  <tr key={c.key} title={c.reason} className={`border-t border-slate-100 dark:border-slate-800 ${c.result !== "PASS" ? (c.result === "FAIL" ? "bg-rose-50/50 dark:bg-rose-950/20" : "bg-amber-50/50 dark:bg-amber-950/20") : ""}`}>
                    <td className="px-3 py-2 font-medium">{c.label}</td>
                    <td className="px-3 py-2 text-xs text-slate-600 dark:text-slate-300">{c.approved ?? "—"}</td>
                    <td className="px-3 py-2 text-xs text-slate-600 dark:text-slate-300">{c.observed ?? "—"}</td>
                    <td className="px-3 py-2 text-right"><TriBadge value={c.result} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {v.checkout_snapshot && (
            <div className="rounded-xl border border-dashed border-slate-300 p-4 text-sm dark:border-slate-700">
              <div className="mb-2 flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500"><Receipt size={13} /> Merchant checkout</div>
              {v.checkout_snapshot.line_items.map((l, i) => (
                <div key={i} className={`flex justify-between gap-3 py-0.5 ${l.recurring || l.amount === null ? "font-semibold text-rose-600" : ""}`}>
                  <span>{l.label}{l.recurring && ` (recurring / ${l.interval})`}</span>
                  <span className="tabular-nums">{money(l.amount, l.currency)}</span>
                </div>
              ))}
              <div className="mt-2 flex justify-between border-t border-slate-200 pt-2 font-extrabold dark:border-slate-700">
                <span>Total</span><span className="tabular-nums">{money(v.checkout_snapshot.stated_total, v.checkout_snapshot.currency)}</span>
              </div>
              <div className="mt-2 text-[11px] text-slate-400">{v.checkout_snapshot.source_label}</div>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

/* ============================================================= payment */
export function PaymentCard({ wf, payment, busy, onPay, onCapture }: {
  wf: Workflow; payment?: Payment; busy: boolean; onPay: () => void; onCapture: (id: string) => void;
}) {
  const v = wf.verification;
  const canPay = ["PAYMENT_PENDING_APPROVAL", "PAYMENT_FAILED"].includes(wf.state) && v?.status === "PASS";
  const st = payment?.payment_status;
  const steps = [
    { label: "Order created", done: !!payment?.paypal_order_id, sub: payment?.paypal_order_id },
    { label: "You approved on PayPal", done: ["APPROVED", "CAPTURING", "CAPTURED", "PENDING"].includes(st ?? "") || !!payment?.paypal_capture_id },
    { label: "Payment captured", done: st === "CAPTURED", sub: payment?.paypal_capture_id ?? undefined },
  ];
  return (
    <Card id="pay" title="PayPal payment" icon={<CreditCard size={15} />}
          right={payment && <span className="chip bg-slate-100 text-slate-600 dark:bg-slate-800">{payment.environment === "sandbox" ? "Sandbox" : "Simulated"}</span>}>
      {v?.status === "PASS" && v.checkout_snapshot ? (
        <div className="mb-4 rounded-2xl bg-gradient-to-br from-slate-900 to-indigo-950 p-4 text-white">
          <div className="text-[11px] uppercase tracking-wider text-indigo-200">Verified order total</div>
          <div className="text-3xl font-extrabold tabular-nums">{money(payment?.captured_amount ?? v.checkout_snapshot.stated_total, payment?.captured_currency ?? v.checkout_snapshot.currency)}</div>
          <div className="mt-1 truncate text-xs text-indigo-200">{wf.contract?.terms.product_name} · {wf.contract?.terms.seller_name}</div>
        </div>
      ) : (
        <p className="mb-3 flex items-start gap-2 text-sm text-slate-500"><Lock size={15} className="mt-0.5 shrink-0" />Payment unlocks only after the checkout verification passes.</p>
      )}

      {payment && (
        <ol className="mb-4 space-y-2.5">
          {steps.map((s) => (
            <li key={s.label} className="flex items-start gap-2.5 text-sm">
              <span className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full ${s.done ? "bg-emerald-500 text-white" : "bg-slate-200 text-slate-400 dark:bg-slate-700"}`}>{s.done ? <Check size={12} strokeWidth={3} /> : null}</span>
              <span className="min-w-0"><span className={s.done ? "font-medium" : "text-slate-500"}>{s.label}</span>
                {s.sub && <span className="mono block truncate text-slate-400">{s.sub}</span>}</span>
            </li>
          ))}
        </ol>
      )}
      {payment?.error && <p className="mb-3 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950/40 dark:text-rose-300">{payment.error}</p>}

      {canPay && (!payment || ["CANCELLED", "FAILED"].includes(st ?? "") || payment.verification_id !== v?.id) && (
        <button className="btn-paypal w-full" disabled={busy} onClick={onPay}>Pay with <span className="italic">PayPal</span></button>
      )}
      {st === "APPROVAL_PENDING" && payment?.approve_url && <a className="btn-paypal w-full" href={payment.approve_url}>Continue to PayPal</a>}
      {(st === "UNCERTAIN" || st === "APPROVED") && payment && (
        <button className="btn-secondary w-full" disabled={busy} onClick={() => onCapture(payment.id)}>
          <Refresh size={15} /> {st === "UNCERTAIN" ? "Re-check PayPal & complete" : "Complete capture"}
        </button>
      )}
      <p className="mt-3 flex gap-1.5 text-[11px] text-slate-400"><ShieldCheck size={13} className="shrink-0" />The policy engine re-authorises every capture. Retries can never charge you twice.</p>
    </Card>
  );
}

/* ====================================================== agent timeline */
const AGENTS: Record<string, { name: string; cls: string; icon: ReactNode }> = {
  intent_agent: { name: "Intent", cls: "bg-violet-500", icon: <Sparkles size={13} /> },
  discovery_agent: { name: "Discovery", cls: "bg-sky-500", icon: <Search size={13} /> },
  seller_trust_agent: { name: "Seller Trust", cls: "bg-amber-500", icon: <Store size={13} /> },
  terms_agent: { name: "Terms", cls: "bg-teal-500", icon: <FileText size={13} /> },
  cost_auditor: { name: "Cost Auditor", cls: "bg-emerald-500", icon: <Scale size={13} /> },
  contract_agent: { name: "Contract", cls: "bg-indigo-500", icon: <FileText size={13} /> },
  checkout_agent: { name: "Verification", cls: "bg-rose-500", icon: <ShieldCheck size={13} /> },
  payment_agent: { name: "Payment", cls: "bg-blue-600", icon: <CreditCard size={13} /> },
  reconciliation_agent: { name: "Reconciliation", cls: "bg-fuchsia-500", icon: <Receipt size={13} /> },
  monitoring_agent: { name: "Monitoring", cls: "bg-slate-500", icon: <Eye size={13} /> },
};
const NOISY = new Set(["seller.assessed", "terms.extracted", "cost.calculated", "workflow.state_changed"]);

export function AgentTimeline({ events, runs, chainOk }: { events: WfEvent[]; runs: AgentRun[]; chainOk: boolean | null }) {
  const [all, setAll] = useState(false);
  const stats = Object.entries(AGENTS).map(([id, a]) => {
    const r = runs.filter((x) => x.agent_id === id);
    return { id, ...a, n: r.length, failed: r.filter((x) => x.status !== "OK").length };
  }).filter((a) => a.n > 0);
  const list = (all ? events : events.filter((e) => !NOISY.has(e.type) || e.message.includes("detected"))).slice().reverse();
  return (
    <Card title="Agent activity" icon={<Bot size={15} />} right={chainOk !== null && (
      <span className={`chip ring-1 ring-inset ${chainOk ? "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300" : "bg-rose-50 text-rose-700 ring-rose-600/20"}`} title="Every event is hash-chained to the previous one">
        {chainOk ? <Lock size={12} /> : <Alert size={12} />}{chainOk ? "Tamper-evident" : "Chain broken"}
      </span>)}>
      <div className="mb-4 flex flex-wrap gap-1.5">
        {stats.map((a) => (
          <span key={a.id} className="chip bg-slate-50 py-1 pl-1 text-slate-700 ring-1 ring-inset ring-slate-200 dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-700" title={`${plural(a.n, "run")}${a.failed ? `, ${a.failed} failed` : ""}`}>
            <span className={`grid h-5 w-5 place-items-center rounded-full text-white ${a.cls}`}>{a.icon}</span>
            {a.name}<span className="text-slate-400">×{a.n}</span>{a.failed > 0 && <span className="text-rose-600">!</span>}
          </span>
        ))}
      </div>
      <ol className="relative max-h-[520px] space-y-3 overflow-y-auto pr-1">
        {list.map((e, i) => {
          const a = e.agent_id ? AGENTS[e.agent_id] : undefined;
          const bad = e.type.includes("failed") || e.type.includes("blocked");
          return (
            <li key={e.seq} className="relative flex gap-3">
              {i < list.length - 1 && <span className="absolute left-[11px] top-7 h-[calc(100%-4px)] w-px bg-slate-200 dark:bg-slate-800" />}
              <span className={`relative z-10 grid h-6 w-6 shrink-0 place-items-center rounded-full text-white ${bad ? "bg-rose-500" : a?.cls ?? "bg-slate-300 dark:bg-slate-700"}`}>
                {bad ? <X size={12} strokeWidth={3} /> : a?.icon ?? <Clock size={12} />}
              </span>
              <div className="min-w-0 flex-1 pb-1">
                <div className="flex items-center justify-between gap-2 text-[11px] text-slate-400">
                  <span className="font-semibold text-slate-600 dark:text-slate-300">{a?.name ?? "System"}</span>
                  <span className="tabular-nums">{fmtTime(e.at)}</span>
                </div>
                <p className={`text-[13px] leading-snug ${bad ? "font-medium text-rose-700 dark:text-rose-300" : ""}`}>{e.message}</p>
              </div>
            </li>
          );
        })}
      </ol>
      <button className="mt-3 text-xs font-semibold text-indigo-600 hover:text-indigo-500 dark:text-indigo-400" onClick={() => setAll(!all)}>
        {all ? "Show key events" : `Show all ${plural(events.length, "event")}`}
      </button>
    </Card>
  );
}

export { Fragment };
