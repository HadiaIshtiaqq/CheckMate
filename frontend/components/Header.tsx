"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, post, type Account, type SystemInfo } from "@/lib/api";
import { Bot, Lock, ShieldCheck, Store } from "@/components/icons";

export default function Header() {
  const [sys, setSys] = useState<SystemInfo | null | undefined>(undefined);
  const [account, setAccount] = useState<Account | null | undefined>(undefined);
  const onLogin = typeof window !== "undefined" && window.location.pathname === "/login";

  useEffect(() => {
    api<SystemInfo>("/api/v1/system").then(setSys).catch(() => setSys(null));
    if (window.location.pathname !== "/login") {
      api<Account>("/api/v1/auth/me").then(setAccount).catch(() => setAccount(null));
    } else {
      setAccount(null);
    }
  }, []);

  const signOut = async () => {
    await post("/api/v1/auth/logout").catch(() => null);
    window.location.href = "/login";
  };

  const llm = sys?.llm_enabled
    ? `${sys.llm_provider === "gemini" ? "Gemini" : "OpenAI"} · ${sys.llm_model}`
    : "Rule-based";

  return (
    <header className="sticky top-0 z-30 border-b border-slate-200/70 bg-white/75 backdrop-blur-xl dark:border-slate-800 dark:bg-slate-950/75">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <Link href="/" className="group flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-lg shadow-indigo-500/30 transition group-hover:scale-105">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M5 12.5 10 17 19 7" />
            </svg>
          </span>
          <span>
            <span className="block text-lg font-extrabold tracking-tight">CHECK<span className="bg-gradient-to-r from-indigo-600 to-violet-600 bg-clip-text text-transparent">MATE</span></span>
            <span className="hidden text-[11px] text-slate-500 sm:block">Your AI shopping team. Your rules. Your money protected.</span>
          </span>
        </Link>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {sys === null && (
            <span className="chip bg-rose-50 text-rose-700 ring-1 ring-inset ring-rose-600/20">
              <span className="h-1.5 w-1.5 rounded-full bg-rose-500" /> API offline
            </span>
          )}
          {sys && (
            <>
              <span className={`chip ring-1 ring-inset ${sys.real_stores_connected
                ? "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300"
                : "bg-rose-50 text-rose-700 ring-rose-600/20 dark:bg-rose-950 dark:text-rose-300"}`}
                    title={sys.product_sources.join(" · ") || "No product source connected"}>
                <Store size={12} /> {sys.real_stores_connected ? "Real stores · Google Shopping" : "No store source connected"}
              </span>
              {sys.demo_store_enabled && <span className={`chip ring-1 ring-inset ${sys.paypal_environment === "sandbox"
                ? "bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-950 dark:text-sky-300"
                : "bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-950 dark:text-amber-300"}`}
                    title={sys.paypal_environment === "sandbox" ? "Real PayPal Sandbox" : "Simulated PayPal — no money moves"}>
                <Lock size={12} /> Demo store · PayPal {sys.paypal_environment === "sandbox" ? "Sandbox" : "Simulated"}
              </span>}
              <span className="chip bg-slate-100 text-slate-700 ring-1 ring-inset ring-slate-500/10 dark:bg-slate-800 dark:text-slate-300"
                    title="Language model used by the agents (rules always enforce the final decision)">
                <Bot size={12} /> {llm}
              </span>
            </>
          )}
          {account && (
            <>
              <Link href="/mandates" className="chip bg-white text-slate-700 ring-1 ring-inset ring-slate-200 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-200 dark:ring-slate-700">
                <ShieldCheck size={12} /> Mandates
              </Link>
              <span className="chip max-w-[180px] truncate bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300" title={account.email ?? ""}>{account.email}</span>
              <button onClick={signOut} className="chip bg-white text-slate-600 ring-1 ring-inset ring-slate-200 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-300 dark:ring-slate-700">Sign out</button>
            </>
          )}
          {account === null && !onLogin && <Link href="/login" className="btn-primary py-1.5 text-xs">Sign in</Link>}
        </div>
      </div>
    </header>
  );
}
