"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, post, type Payment, type Suggestion, type SystemInfo, type WfEvent, type Workflow } from "@/lib/api";
import { StateBadge, timeAgo } from "@/components/ui";
import { AgentTimeline, ContractCard, IntentSummary, NextStep, OfferCards, PaymentCard, Stepper, StorePurchaseCard, VerificationCard } from "@/components/WorkflowPanels";
import { Alert, ArrowLeft, Award, Check, Eye, X } from "@/components/icons";

const RUNNING = new Set(["CREATED", "INTENT_PARSED", "DISCOVERING", "EVALUATING", "CHECKOUT_VERIFICATION", "RECONCILIATION"]);
const CANCELLABLE = new Set(["CREATED", "NEEDS_CLARIFICATION", "INTENT_PARSED", "DISCOVERING", "EVALUATING",
  "EVIDENCE_INCOMPLETE", "CONTRACT_CREATED", "CHECKOUT_VERIFICATION", "CHECKOUT_BLOCKED", "PAYMENT_PENDING_APPROVAL",
  "PAYMENT_FAILED"]);

type Toast = { tone: "error" | "warn" | "ok"; text: string; reasons?: string[] };

export default function WorkflowPage({ params }: { params: { id: string } }) {
  const id = params.id;
  const router = useRouter();
  const [wf, setWf] = useState<Workflow | null>(null);
  const [events, setEvents] = useState<WfEvent[]>([]);
  const [chainOk, setChainOk] = useState<boolean | null>(null);
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const handledReturn = useRef(false);
  const lastState = useRef<string | null>(null);
  useEffect(() => {
    if (!wf) return;
    if (lastState.current && lastState.current !== wf.state && !RUNNING.has(wf.state)) {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    lastState.current = wf.state;
  }, [wf]);

  const load = useCallback(async () => {
    try {
      const [w, ev] = await Promise.all([
        api<Workflow>(`/api/v1/workflows/${id}`),
        api<{ events: WfEvent[]; chain_verified: boolean }>(`/api/v1/workflows/${id}/events`),
      ]);
      setWf(w);
      setEvents(ev.events);
      setChainOk(ev.chain_verified);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof ApiError ? (e.status === 404 ? "This workflow doesn't exist or belongs to another user." : e.message) : "Could not reach the CHECKMATE API.");
    }
  }, [id]);

  useEffect(() => {
    load();
    api<SystemInfo>("/api/v1/system").then(setSys).catch(() => null);
  }, [load]);

  // Live updates via server-sent events; polling takes over if the stream isn't available.
  const [live, setLive] = useState(false);
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const es = new EventSource(`/api/v1/workflows/${id}/stream`);
    let timer: ReturnType<typeof setTimeout> | null = null;
    es.addEventListener("workflow", () => {
      setLive(true);
      if (timer) clearTimeout(timer);
      timer = setTimeout(load, 150); // coalesce bursts of events into one refresh
    });
    es.addEventListener("done", () => es.close());
    es.onerror = () => setLive(false);
    return () => { es.close(); if (timer) clearTimeout(timer); };
  }, [id, load]);

  useEffect(() => {
    if (!wf || !RUNNING.has(wf.state) || live) return;
    const t = setTimeout(load, 900);
    return () => clearTimeout(t);
  }, [wf, load, live]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), toast.tone === "ok" ? 4000 : 9000);
    return () => clearTimeout(t);
  }, [toast]);

  const act = useCallback(async (fn: () => Promise<unknown>, success?: string) => {
    setBusy(true);
    setToast(null);
    try {
      await fn();
      if (success) setToast({ tone: "ok", text: success });
    } catch (e) {
      if (e instanceof ApiError) setToast({ tone: e.status === 403 ? "error" : "warn", text: e.message, reasons: e.reasons });
      else setToast({ tone: "error", text: "Request failed. Is the backend running?" });
    } finally {
      await load();
      setBusy(false);
    }
  }, [load]);

  // Returning from the PayPal approval page.
  useEffect(() => {
    if (handledReturn.current) return;
    const q = new URLSearchParams(window.location.search);
    const pay = q.get("payment");
    const outcome = q.get("paypal");
    if (!pay || !outcome) return;
    handledReturn.current = true;
    window.history.replaceState(null, "", `/workflows/${id}`);
    if (outcome === "approved") act(() => post(`/api/v1/payments/${pay}/capture`), "PayPal approval received — payment captured.");
    else act(() => post(`/api/v1/payments/${pay}/cancel`), "Payment cancelled on PayPal. No money moved.");
  }, [id, act]);

  if (!wf) {
    return loadError ? (
      <div className="card mx-auto max-w-lg py-12 text-center">
        <Alert size={28} className="mx-auto text-rose-500" />
        <p className="mt-3 font-semibold">{loadError}</p>
        <Link href="/" className="btn-secondary mt-4"><ArrowLeft size={15} /> Back to dashboard</Link>
      </div>
    ) : (
      <div className="space-y-4">{[64, 96, 200, 320].map((h) => <div key={h} className="card animate-pulse" style={{ height: h }} />)}</div>
    );
  }

  const contract = wf.contract;
  const payment: Payment | undefined = wf.payments.find((p) => p.verification_id === wf.verification?.id &&
    !["CANCELLED", "FAILED"].includes(p.payment_status)) ?? wf.payments[0];
  const contractStates = ["CONTRACT_CREATED", "CHECKOUT_BLOCKED", "PAYMENT_PENDING_APPROVAL", "PAYMENT_FAILED"];
  const canVerify = !!contract?.approved && contractStates.includes(wf.state) && wf.autonomy_mode !== "MONITORING_ONLY";
  const lastMessage = [...events].reverse().find((e) => e.type !== "workflow.state_changed")?.message;
  const external = contract?.terms.purchase_channel === "external_store";

  const startPayPal = () => act(async () => {
    const p = await post<Payment>("/api/v1/payments/orders", { contract_id: contract!.id });
    if (p.approve_url && ["APPROVAL_PENDING", "APPROVED"].includes(p.payment_status)) window.location.href = p.approve_url;
  });
  const approve = () => act(() => post(`/api/v1/contracts/${contract!.id}/approve`, { contract_hash: contract!.contract_hash }), "Contract approved. Next: verify the checkout.");
  const revise = (s: Suggestion) => act(async () => {
    const r = await post<{ workflow_id: string }>(`/api/v1/workflows/${id}/revise`, {
      max_budget: s.max_budget, drop_specs: s.drop_specs ?? [], clear_variant: !!s.clear_variant,
      accept_limited_seller: s.kind === "accept_limited_seller", warranty_min_months: s.warranty_min_months,
    });
    router.push(`/workflows/${r.workflow_id}`);
  });

  return (
    <div className="space-y-5">
      {/* --------------------------------------------------------- header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <Link href="/" className="inline-flex items-center gap-1 text-xs font-semibold text-brand-600 hover:text-brand-500 dark:text-brand-400"><ArrowLeft size={14} /> Dashboard</Link>
          <h1 className="mt-2 text-xl font-extrabold leading-snug tracking-tight sm:text-2xl">{wf.request.split("\n")[0].replace(/ \[revised:.*\]$/, "")}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <StateBadge state={wf.state} />
            {/\[revised: (.*)\]$/.test(wf.request) && <span className="chip bg-plum-50 text-plum-700 ring-1 ring-inset ring-plum-600/20 dark:bg-plum-950 dark:text-plum-300">Revised: {wf.request.match(/\[revised: (.*)\]$/)?.[1]}</span>}
            {wf.autonomy_mode === "MONITORING_ONLY" && <span className="chip bg-slate-100 dark:bg-slate-800"><Eye size={12} /> Watching only</span>}
            <span>Started {timeAgo(wf.created_at)}</span>
            <span className="mono text-slate-400" title="Workflow ID">{wf.id}</span>
          </div>
        </div>
        <div className="flex gap-2">
          {wf.passport_id && <Link className="btn-primary" href={`/passports/${wf.passport_id}`}><Award size={16} /> Passport</Link>}
          {CANCELLABLE.has(wf.state) && (
            <button className="btn-ghost" disabled={busy} onClick={() => { if (window.confirm("Cancel this workflow? All approvals will be revoked.")) act(() => post(`/api/v1/workflows/${id}/cancel`), "Workflow cancelled."); }}>
              <X size={15} /> Cancel
            </button>
          )}
        </div>
      </div>

      <Stepper state={wf.state} />

      <NextStep wf={wf} lastMessage={lastMessage} events={events} busy={busy} actions={{
        approve, pay: startPayPal,
        verify: () => document.getElementById("verify")?.scrollIntoView({ behavior: "smooth" }),
        retry: () => act(() => post(`/api/v1/workflows/${id}/retry`)),
        cancel: () => act(() => post(`/api/v1/workflows/${id}/cancel`)),
        revise,
        clarify: (text) => act(() => post(`/api/v1/workflows/${id}/clarify`, { text })),
        switchToBuy: () => act(() => post(`/api/v1/workflows/${id}/mode`, { autonomy_mode: "ASSISTED" }), "Switched to buying. Approve the contract to continue."),
        selectAlt: (pid) => act(() => post(`/api/v1/workflows/${id}/alternatives/${pid}/select`), "New contract version created — review and approve it."),
      }} />

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="space-y-5 lg:col-span-2">
          {wf.intent && <IntentSummary intent={wf.intent} />}
          {contract && sys && !external && wf.autonomy_mode !== "MONITORING_ONLY" && (
            <VerificationCard verification={wf.verification} scenarios={sys.scenarios} canVerify={canVerify} busy={busy}
                              onVerify={(s) => act(() => post(`/api/v1/contracts/${contract.id}/verify`, { scenario: s }))} />
          )}
          <OfferCards evaluations={wf.evaluations} selectedId={contract?.terms.product_id} busy={busy}
                      canSwitch={["CONTRACT_CREATED", "EVIDENCE_INCOMPLETE"].includes(wf.state) && wf.autonomy_mode !== "MONITORING_ONLY"}
                      onSelect={(pid, broken) => {
                        if (broken.length && !window.confirm(`This offer breaks ${broken.length === 1 ? "one of your rules" : `${broken.length} of your rules`}:\n\n• ${broken.join("\n• ")}\n\nChoose it anyway? The override will be recorded in your plan and passport.`)) return;
                        act(() => post(`/api/v1/workflows/${id}/alternatives/${pid}/select${broken.length ? "?override=true" : ""}`),
                            "Plan updated to the offer you chose. Review and approve it.");
                      }} />
          {!wf.evaluations.length && RUNNING.has(wf.state) && (
            <div className="space-y-3" aria-hidden="true">
              {[0, 1, 2].map((i) => <div key={i} className="card h-28 animate-pulse bg-white/60" />)}
            </div>
          )}
          <AgentTimeline events={events} runs={wf.agent_runs} chainOk={chainOk} />
        </div>
        <div className="space-y-5 lg:sticky lg:top-24 lg:max-h-[calc(100vh-7rem)] lg:self-start lg:overflow-y-auto lg:pb-2 lg:pr-1">
          {contract && (
            <ContractCard contract={contract} state={wf.state} busy={busy} onApprove={approve}
                          onRevoke={() => act(() => post(`/api/v1/contracts/${contract.id}/revoke`), "Approval revoked. No payment can proceed.")} />
          )}
          {contract && external && wf.autonomy_mode !== "MONITORING_ONLY" && wf.state === "CONTRACT_CREATED" && (
            <StorePurchaseCard contract={contract} state={wf.state} busy={busy}
                               onRefresh={() => act(async () => {
                                 const r = await post<{ found: boolean; changed?: boolean; price?: string }>(`/api/v1/workflows/${id}/refresh-offer`);
                                 if (!r.found) throw new ApiError(404, "This listing no longer appears in Google Shopping results; it may be sold out.");
                                 setToast({ tone: r.changed ? "warn" : "ok", text: r.changed ? `Price changed to ${r.price}. The plan was updated; approve it again.` : `Price unchanged (${r.price}).` });
                               })}
                               onReceipt={(f) => act(async () => {
                                 const fd = new FormData();
                                 fd.append("file", f);
                                 await post(`/api/v1/contracts/${contract.id}/receipts`, fd);
                               }, "Receipt attached.")}
                               onReport={(r) => act(() => post(`/api/v1/contracts/${contract.id}/report-purchase`, r), "Purchase recorded and reconciled. Your passport is ready.")} />
          )}
          {contract && !external && wf.autonomy_mode !== "MONITORING_ONLY" && (
            <PaymentCard wf={wf} payment={payment} busy={busy} onPay={startPayPal}
                         onCapture={(pid) => act(() => post(`/api/v1/payments/${pid}/capture`), "Payment captured.")} />
          )}
        </div>
      </div>

      {toast && (
        <div role="status" className={`fixed bottom-5 right-5 z-50 w-[min(420px,calc(100vw-2.5rem))] animate-fade-up rounded-2xl border p-4 shadow-2xl ${toast.tone === "ok" ? "border-emerald-200 bg-white dark:border-emerald-900 dark:bg-slate-900" : toast.tone === "warn" ? "border-amber-200 bg-white dark:border-amber-900 dark:bg-slate-900" : "border-rose-200 bg-white dark:border-rose-900 dark:bg-slate-900"}`}>
          <div className="flex items-start gap-3">
            <span className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-white ${toast.tone === "ok" ? "bg-emerald-500" : toast.tone === "warn" ? "bg-amber-500" : "bg-rose-500"}`}>
              {toast.tone === "ok" ? <Check size={15} strokeWidth={3} /> : <Alert size={15} />}
            </span>
            <div className="min-w-0 flex-1 text-sm">
              <p className="font-semibold">{toast.text}</p>
              {toast.reasons && toast.reasons.length > 0 && (
                <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-slate-600 dark:text-slate-300">{toast.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              )}
            </div>
            <button className="text-slate-400 hover:text-slate-700" onClick={() => setToast(null)} aria-label="Dismiss"><X size={16} /></button>
          </div>
        </div>
      )}
    </div>
  );
}
