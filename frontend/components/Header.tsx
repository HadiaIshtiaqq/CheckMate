"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type SystemInfo } from "@/lib/api";
import { Bot, Lock } from "@/components/icons";

export default function Header() {
  const [sys, setSys] = useState<SystemInfo | null | undefined>(undefined);

  useEffect(() => {
    api<SystemInfo>("/api/v1/system").then(setSys).catch(() => setSys(null));
  }, []);

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
              <span className={`chip ring-1 ring-inset ${sys.paypal_environment === "sandbox"
                ? "bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-950 dark:text-sky-300"
                : "bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-950 dark:text-amber-300"}`}
                    title={sys.paypal_environment === "sandbox" ? "Real PayPal Sandbox" : "Simulated PayPal — no money moves"}>
                <Lock size={12} /> PayPal {sys.paypal_environment === "sandbox" ? "Sandbox" : "Simulated"}
              </span>
              <span className="chip bg-slate-100 text-slate-700 ring-1 ring-inset ring-slate-500/10 dark:bg-slate-800 dark:text-slate-300"
                    title="Language model used by the agents (rules always enforce the final decision)">
                <Bot size={12} /> {llm}
              </span>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
