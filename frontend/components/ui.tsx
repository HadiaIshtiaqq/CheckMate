import type { ReactNode } from "react";
import type { Tri } from "@/lib/api";
import { Alert, Check, Info, Question, X } from "@/components/icons";

const TRI: Record<string, { cls: string; icon: ReactNode; text: string }> = {
  PASS: { cls: "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950/60 dark:text-emerald-300", icon: <Check size={12} strokeWidth={3} />, text: "Pass" },
  FAIL: { cls: "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-950/60 dark:text-rose-300", icon: <X size={12} strokeWidth={3} />, text: "Fail" },
  UNKNOWN: { cls: "bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-950/60 dark:text-amber-300", icon: <Question size={12} strokeWidth={2.5} />, text: "Unknown" },
};

export function TriBadge({ value, compact = false }: { value: Tri | string; compact?: boolean }) {
  const t = TRI[value] ?? TRI.UNKNOWN;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide ring-1 ring-inset ${t.cls}`}>
      {t.icon}{!compact && t.text}
    </span>
  );
}

export function TriIcon({ value }: { value: Tri | string }) {
  const map: Record<string, string> = {
    PASS: "bg-emerald-500 text-white", FAIL: "bg-rose-500 text-white", UNKNOWN: "bg-amber-400 text-amber-950",
  };
  return (
    <span className={`grid h-5 w-5 shrink-0 place-items-center rounded-full ${map[value] ?? map.UNKNOWN}`}>
      {value === "PASS" ? <Check size={12} strokeWidth={3} /> : value === "FAIL" ? <X size={12} strokeWidth={3} /> : <Question size={12} strokeWidth={2.5} />}
    </span>
  );
}

const STATE: Record<string, { label: string; tone: string }> = {
  CREATED: { label: "Starting", tone: "sky" },
  NEEDS_CLARIFICATION: { label: "Needs your input", tone: "amber" },
  INTENT_PARSED: { label: "Understood", tone: "sky" },
  DISCOVERING: { label: "Searching", tone: "sky" },
  EVALUATING: { label: "Evaluating", tone: "sky" },
  EVIDENCE_INCOMPLETE: { label: "No qualifying offer", tone: "amber" },
  CONTRACT_CREATED: { label: "Contract ready", tone: "indigo" },
  CHECKOUT_VERIFICATION: { label: "Verifying checkout", tone: "sky" },
  CHECKOUT_BLOCKED: { label: "Payment blocked", tone: "rose" },
  PAYMENT_PENDING_APPROVAL: { label: "Ready to pay", tone: "indigo" },
  PAYMENT_CAPTURED: { label: "Paid", tone: "emerald" },
  PAYMENT_FAILED: { label: "Payment failed", tone: "rose" },
  RECONCILIATION: { label: "Reconciling", tone: "sky" },
  RECONCILIATION_REQUIRED: { label: "Discrepancy found", tone: "amber" },
  PASSPORT_GENERATED: { label: "Passport issued", tone: "emerald" },
  COMPLETED: { label: "Completed", tone: "emerald" },
  CANCELLED: { label: "Cancelled", tone: "slate" },
  EXPIRED: { label: "Expired", tone: "slate" },
};
const TONES: Record<string, string> = {
  emerald: "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950/60 dark:text-emerald-300",
  rose: "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-950/60 dark:text-rose-300",
  amber: "bg-amber-50 text-amber-800 ring-amber-600/25 dark:bg-amber-950/60 dark:text-amber-300",
  indigo: "bg-indigo-50 text-indigo-700 ring-indigo-600/20 dark:bg-indigo-950/60 dark:text-indigo-300",
  slate: "bg-slate-100 text-slate-600 ring-slate-500/20 dark:bg-slate-800 dark:text-slate-300",
  sky: "bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-950/60 dark:text-sky-300",
};
const DOT: Record<string, string> = {
  emerald: "bg-emerald-500", rose: "bg-rose-500", amber: "bg-amber-500", indigo: "bg-indigo-500", slate: "bg-slate-400",
  sky: "bg-sky-500 animate-pulse",
};

export function stateLabel(state: string) {
  return STATE[state]?.label ?? state.replaceAll("_", " ").toLowerCase();
}

export function StateBadge({ state }: { state: string }) {
  const s = STATE[state] ?? { label: state, tone: "sky" };
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ring-inset ${TONES[s.tone]}`} title={state}>
      <span className={`h-1.5 w-1.5 rounded-full ${DOT[s.tone]}`} />{s.label}
    </span>
  );
}

export function Card({ title, icon, children, right, className = "", id }: {
  title?: string; icon?: ReactNode; children: ReactNode; right?: ReactNode; className?: string; id?: string;
}) {
  return (
    <section id={id} className={`card animate-fade-up scroll-mt-24 ${className}`}>
      {(title || right) && (
        <div className="mb-4 flex items-start justify-between gap-3">
          {title && <h2 className="card-title">{icon}{title}</h2>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-dashed border-slate-200 py-2 text-sm last:border-0 dark:border-slate-800">
      <span className="shrink-0 text-slate-500 dark:text-slate-400">{label}</span>
      <span className="min-w-0 text-right font-medium">{children}</span>
    </div>
  );
}

type Tone = "rose" | "amber" | "emerald" | "slate" | "indigo";
const NOTICE: Record<Tone, string> = {
  rose: "border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200",
  amber: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200",
  emerald: "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200",
  slate: "border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300",
  indigo: "border-indigo-200 bg-indigo-50 text-indigo-900 dark:border-indigo-900 dark:bg-indigo-950/40 dark:text-indigo-200",
};

export function Notice({ tone = "slate", children, icon }: { tone?: Tone; children: ReactNode; icon?: ReactNode }) {
  const def = tone === "rose" || tone === "amber" ? <Alert size={16} /> : <Info size={16} />;
  return (
    <div className={`flex gap-3 rounded-xl border px-4 py-3 text-sm ${NOTICE[tone]}`}>
      <span className="mt-0.5 shrink-0">{icon ?? def}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

const SYMBOLS: Record<string, string> = { USD: "$", EUR: "€", GBP: "£" };

export function money(v: string | null | undefined, cur?: string | null) {
  if (v === null || v === undefined) return "Unknown";
  const n = Number(v);
  const s = Number.isFinite(n) ? n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : v;
  if (!cur) return `${s} (currency unknown)`;
  return SYMBOLS[cur] ? `${SYMBOLS[cur]}${s}` : `${s} ${cur}`;
}

export function shortHash(h: string | null | undefined) {
  return h ? `${h.slice(0, 8)}…${h.slice(-6)}` : "—";
}

export function fmtTime(iso: string) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

export function timeAgo(iso: string) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

export function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
