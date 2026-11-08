"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, post, type SystemInfo, type WorkflowSummary } from "@/lib/api";
import { StateBadge, money, plural, timeAgo } from "@/components/ui";
import {
  Alert, ArrowRight, Award, Ban, Clock, CreditCard, Eye, FileText, Loader, Search, ShieldCheck, Sparkles, Zap,
} from "@/components/icons";

const EXAMPLES = [
  { label: "Headphones under $110", text: "Find wireless headphones under $110. They must have at least a one-year warranty, free returns, no subscription, and come from a reliable seller." },
  { label: "Laptop under $800", text: "Find a laptop under $800 with 16GB RAM, 512GB SSD and at least a one-year warranty." },
  { label: "Mechanical keyboard", text: "Find a black mechanical keyboard under $90 with free returns and no subscription." },
  { label: "Office chair", text: "Find an office chair under $150 with free returns, at least a one-year warranty, from a reliable seller." },
  { label: "Coffee maker", text: "Find a coffee maker under $70 with free returns and at least a one-year warranty." },
];

const STEPS = [
  { icon: <Search size={18} />, title: "Search real stores", text: "Live Google Shopping listings across thousands of retailers." },
  { icon: <ShieldCheck size={18} />, title: "Check your rules", text: "Price, store, specs, returns, subscriptions. Unknowns stay unknown." },
  { icon: <FileText size={18} />, title: "Approve a plan", text: "A hashed purchase plan with a checklist to confirm at the store." },
  { icon: <Award size={18} />, title: "Prove", text: "Report what you paid; get a reconciled, tamper-evident passport." },
];

const ACTIVE = new Set(["CREATED", "INTENT_PARSED", "DISCOVERING", "EVALUATING", "CHECKOUT_VERIFICATION",
  "RECONCILIATION"]);
const NEEDS_YOU = new Set(["CONTRACT_CREATED", "PAYMENT_PENDING_APPROVAL", "PAYMENT_FAILED", "NEEDS_CLARIFICATION",
  "EVIDENCE_INCOMPLETE"]);

export default function Dashboard() {
  const router = useRouter();
  const [text, setText] = useState(EXAMPLES[0].text);
  const [mode, setMode] = useState("ASSISTED");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rows, setRows] = useState<WorkflowSummary[] | null>(null);
  const [filter, setFilter] = useState<"all" | "you" | "done" | "blocked">("all");
  const [sys, setSys] = useState<SystemInfo | null>(null);

  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("q");
    if (q) {
      setText(q);
      window.history.replaceState(null, "", "/");
      document.getElementById("composer")?.focus();
    }
    api<SystemInfo>("/api/v1/system").then(setSys).catch(() => setSys(null));
    const load = () => api<WorkflowSummary[]>("/api/v1/workflows").then(setRows).catch(() => setRows([]));
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ workflow_id: string }>("/api/v1/shopping/intents", { text, autonomy_mode: mode });
      router.push(`/workflows/${r.workflow_id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not reach the CHECKMATE API. Is the backend running on port 8000?");
      setBusy(false);
    }
  };

  const count = (pred: (w: WorkflowSummary) => boolean) => rows?.filter(pred).length ?? 0;
  const stats = [
    { label: "In progress", value: count((w) => ACTIVE.has(w.state)), icon: <Zap size={18} />, cls: "from-sky-500 to-cyan-500" },
    { label: "Needs you", value: count((w) => NEEDS_YOU.has(w.state)), icon: <Clock size={18} />, cls: "from-indigo-500 to-violet-500" },
    { label: "Blocked checkouts", value: count((w) => w.state === "CHECKOUT_BLOCKED"), icon: <Ban size={18} />, cls: "from-rose-500 to-pink-500" },
    { label: "Completed", value: count((w) => w.state === "COMPLETED"), icon: <CreditCard size={18} />, cls: "from-emerald-500 to-teal-500" },
    { label: "Discrepancies", value: count((w) => w.has_discrepancy), icon: <Alert size={18} />, cls: "from-amber-500 to-orange-500" },
  ];

  const shown = (rows ?? []).filter((w) =>
    filter === "all" ? true : filter === "you" ? NEEDS_YOU.has(w.state) : filter === "done" ? w.state === "COMPLETED"
      : w.state === "CHECKOUT_BLOCKED" || w.has_discrepancy);

  return (
    <div className="space-y-8">
      {/* ----------------------------------------------------------- hero */}
      <section className="relative overflow-hidden rounded-3xl bg-gradient-to-br from-indigo-600 via-violet-600 to-fuchsia-600 p-6 text-white shadow-xl shadow-indigo-500/20 sm:p-8">
        <div className="pointer-events-none absolute -right-24 -top-24 h-72 w-72 rounded-full bg-white/10 blur-2xl" />
        <div className="pointer-events-none absolute -bottom-32 left-1/3 h-72 w-72 rounded-full bg-fuchsia-300/20 blur-3xl" />
        <div className="relative grid gap-8 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <span className="chip bg-white/15 text-white ring-1 ring-inset ring-white/25"><Sparkles size={13} /> 10 specialised agents · 1 rule engine</span>
            <h1 className="mt-4 text-3xl font-extrabold leading-tight tracking-tight sm:text-4xl">
              Tell us what to buy.<br />We'll make sure it's exactly what you pay for.
            </h1>
            <p className="mt-3 max-w-xl text-sm text-indigo-100">
              CHECKMATE searches real stores, checks every offer against your rules, shows exactly what it could and
              couldn't verify, and turns the best one into a purchase plan you approve before buying.
            </p>

            <div className="mt-6 rounded-2xl bg-white p-3 text-slate-900 shadow-2xl shadow-indigo-900/30 dark:bg-slate-900 dark:text-slate-100">
              <label htmlFor="composer" className="sr-only">Shopping request</label>
              <textarea id="composer" className="min-h-[88px] w-full resize-none rounded-xl border-0 bg-transparent p-2 text-[15px] leading-relaxed outline-none placeholder:text-slate-400"
                        value={text} onChange={(e) => setText(e.target.value)} maxLength={2000}
                        placeholder="e.g. Find wireless headphones under $110 with free returns and no subscription"
                        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }} />
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 px-1 pt-3 dark:border-slate-800">
                <div className="flex rounded-xl bg-slate-100 p-1 text-xs font-semibold dark:bg-slate-800" role="radiogroup" aria-label="Autonomy mode">
                  {[{ v: "ASSISTED", l: "Buy it", t: "Find, verify and buy with your approval", i: <CreditCard size={14} /> }, { v: "MANDATE", l: "My mandate", t: "Approve automatically if it fits a purchasing mandate you set (you still pay yourself)", i: <ShieldCheck size={14} /> }, { v: "MONITORING_ONLY", l: "Just watch", t: "Track the offer without buying (you can switch to buying later)", i: <Eye size={14} /> }].map((m) => (
                    <button key={m.v} role="radio" aria-checked={mode === m.v} onClick={() => setMode(m.v)} title={m.t}
                            className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 transition ${mode === m.v ? "bg-white text-indigo-700 shadow-sm dark:bg-slate-700 dark:text-white" : "text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"}`}>
                      {m.i}{m.l}
                    </button>
                  ))}
                </div>
                <button className="btn-primary px-5" onClick={submit} disabled={busy || text.trim().length < 3}>
                  {busy ? <><Loader size={16} /> Starting agents…</> : <>Start shopping <ArrowRight size={16} /></>}
                </button>
              </div>
            </div>
            {error && <p className="mt-3 rounded-lg bg-rose-500/20 px-3 py-2 text-sm text-white ring-1 ring-rose-200/40">{error}</p>}
            <div className="mt-4 flex flex-wrap gap-2">
              {EXAMPLES.map((ex) => (
                <button key={ex.label} onClick={() => setText(ex.text)}
                        className={`chip ring-1 ring-inset transition ${text === ex.text ? "bg-white text-indigo-700 ring-white" : "bg-white/10 text-white ring-white/25 hover:bg-white/20"}`}>
                  {ex.label}
                </button>
              ))}
            </div>
          </div>

          <ol className="grid content-center gap-3 lg:col-span-2">
            {STEPS.map((s, i) => (
              <li key={s.title} className="flex gap-3 rounded-2xl bg-white/10 p-4 ring-1 ring-inset ring-white/15 backdrop-blur">
                <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-white/20">{s.icon}</span>
                <div>
                  <div className="text-sm font-bold">{i + 1}. {s.title}</div>
                  <div className="text-xs text-indigo-100">{s.text}</div>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      {sys && !sys.real_stores_connected && (
        <section className="card flex flex-col gap-3 border-amber-200 bg-amber-50/70 sm:flex-row sm:items-start dark:border-amber-900 dark:bg-amber-950/30">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-amber-400 text-amber-950"><Alert size={18} /></span>
          <div className="text-sm">
            <p className="font-bold">Connect real stores to search for products</p>
            <p className="mt-1 text-slate-600 dark:text-slate-300">CHECKMATE only shows real listings. It searches Google Shopping (thousands of stores) through SerpAPI:</p>
            <ol className="mt-2 list-decimal space-y-1 pl-5 text-slate-600 dark:text-slate-300">
              <li>Create a free account at <a className="font-semibold text-indigo-600 underline" href="https://serpapi.com/users/sign_up" target="_blank" rel="noreferrer">serpapi.com</a> (free plan: 250 searches a month).</li>
              <li>Copy your API key into <code className="rounded bg-white px-1 dark:bg-slate-800">.env</code> as <code className="rounded bg-white px-1 dark:bg-slate-800">SERPAPI_KEY=…</code></li>
              <li>Restart the backend.</li>
            </ol>
          </div>
        </section>
      )}

      {/* ---------------------------------------------------------- stats */}
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {stats.map((s) => (
          <div key={s.label} className="card flex items-center gap-3 p-4">
            <span className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br text-white shadow-sm ${s.cls}`}>{s.icon}</span>
            <div>
              <div className="text-2xl font-extrabold tabular-nums leading-none">{rows ? s.value : "–"}</div>
              <div className="mt-1 text-xs text-slate-500">{s.label}</div>
            </div>
          </div>
        ))}
      </section>

      {/* ------------------------------------------------------ workflows */}
      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-bold">Your shopping workflows</h2>
          <div className="flex rounded-xl bg-white p-1 text-xs font-semibold ring-1 ring-slate-200 dark:bg-slate-900 dark:ring-slate-800">
            {([["all", "All"], ["you", "Needs you"], ["blocked", "Blocked / issues"], ["done", "Completed"]] as const).map(([k, l]) => (
              <button key={k} onClick={() => setFilter(k)}
                      className={`rounded-lg px-3 py-1.5 ${filter === k ? "bg-indigo-600 text-white" : "text-slate-500 hover:text-slate-800 dark:hover:text-slate-200"}`}>{l}</button>
            ))}
          </div>
        </div>
        {rows === null && (
          <div className="grid gap-3 md:grid-cols-2">{[0, 1, 2, 3].map((i) => <div key={i} className="card h-24 animate-pulse" />)}</div>
        )}
        {rows && shown.length === 0 && (
          <div className="card flex flex-col items-center py-12 text-center">
            <span className="grid h-12 w-12 place-items-center rounded-2xl bg-indigo-50 text-indigo-600 dark:bg-indigo-950"><Search size={22} /></span>
            <p className="mt-3 font-semibold">{rows.length === 0 ? "No workflows yet" : "Nothing in this view"}</p>
            <p className="text-sm text-slate-500">{rows.length === 0 ? "Describe what you want to buy above to start your first one." : "Try another filter."}</p>
          </div>
        )}
        <ul className="grid gap-3 md:grid-cols-2">
          {shown.map((w) => (
            <li key={w.id}>
              <Link href={`/workflows/${w.id}`}
                    className="card group flex h-full flex-col gap-3 p-4 transition hover:-translate-y-0.5 hover:border-indigo-200 hover:shadow-lg dark:hover:border-indigo-900">
                <div className="flex items-start justify-between gap-3">
                  <p className="line-clamp-2 text-sm font-semibold leading-snug">{w.request.split("\n")[0]}</p>
                  <ArrowRight size={16} className="mt-0.5 shrink-0 text-slate-300 transition group-hover:translate-x-0.5 group-hover:text-indigo-500" />
                </div>
                <div className="mt-auto flex flex-wrap items-center gap-2 text-xs text-slate-500">
                  <StateBadge state={w.state} />
                  {w.has_discrepancy && <span className="chip bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-600/20"><Alert size={12} /> discrepancy</span>}
                  {w.max_budget && <span className="chip bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">max {money(w.max_budget, w.currency)}</span>}
                  {w.autonomy_mode === "MONITORING_ONLY" && <span className="chip bg-slate-100 text-slate-600 dark:bg-slate-800"><Eye size={12} /> watching</span>}
                  <span className="ml-auto">{timeAgo(w.created_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
        {rows && rows.length > 0 && <p className="mt-3 text-xs text-slate-400">{plural(rows.length, "workflow")} · refreshes automatically</p>}
      </section>
    </div>
  );
}
