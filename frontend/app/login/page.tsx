"use client";

import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { ArrowRight, Loader, Lock, ShieldCheck } from "@/components/icons";
import { LogoMark } from "@/components/Logo";

export default function LoginPage() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [exists, setExists] = useState(false);
  const [missing, setMissing] = useState(false);
  const [waking, setWaking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [next, setNext] = useState("/");

  useEffect(() => {
    const n = new URLSearchParams(window.location.search).get("next");
    if (n && n.startsWith("/") && !n.startsWith("//")) setNext(n);
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setExists(false);
    setMissing(false);
    if (mode === "register" && password !== confirm) {
      setError("The two passwords don't match.");
      return;
    }
    setBusy(true);
    // The free host sleeps when idle; while the backend wakes up, requests fail with 502-504
    // or hang. Retry for up to two minutes instead of leaving the button spinning.
    const deadline = Date.now() + 120_000;
    for (;;) {
      try {
        await api(`/api/v1/auth/${mode === "login" ? "login" : "register"}`, {
          method: "POST", body: JSON.stringify({ email, password }), signal: AbortSignal.timeout(20_000),
        });
        window.location.href = next;
        return;
      } catch (err) {
        const waking = !(err instanceof ApiError) || [502, 503, 504].includes(err.status);
        if (waking && Date.now() < deadline) {
          setWaking(true);
          await new Promise((r) => setTimeout(r, 3000));
          continue;
        }
        setWaking(false);
        setError(waking ? "CHECKMATE didn't wake up in time. Please try again in a minute."
                 : err instanceof ApiError ? err.message : "Could not reach CHECKMATE.");
        setExists(mode === "register" && err instanceof ApiError && err.status === 409);
        setMissing(mode === "login" && err instanceof ApiError && err.status === 401);
        setBusy(false);
        return;
      }
    }
  };

  const switchTo = (m: "login" | "register", keepEmail = false) => {
    setMode(m);
    setError(null);
    setExists(false);
    setMissing(false);
    setPassword("");
    setConfirm("");
    if (!keepEmail) setEmail("");
  };

  return (
    <div className="mx-auto grid max-w-4xl items-center gap-8 py-8 md:grid-cols-2">
      <div className="hidden md:block">
        <LogoMark size={56} className="mb-5 drop-shadow" />
        <h1 className="text-3xl font-extrabold leading-tight tracking-tight">
          Your shopping team,<br /><span className="bg-gradient-to-r from-brand-600 to-plum-600 bg-clip-text text-transparent">your rules.</span>
        </h1>
        <ul className="mt-6 space-y-3 text-sm text-slate-600 dark:text-slate-300">
          <li className="flex gap-2"><ShieldCheck size={18} className="shrink-0 text-brand-600" />Searches real stores and checks every offer against your rules.</li>
          <li className="flex gap-2"><ShieldCheck size={18} className="shrink-0 text-brand-600" />Never invents products; anything a listing doesn't show stays unknown.</li>
          <li className="flex gap-2"><ShieldCheck size={18} className="shrink-0 text-brand-600" />Signed purchase plans and a tamper-evident passport for every purchase.</li>
        </ul>
      </div>
      <form onSubmit={submit} className="card space-y-4 p-6">
        <div className="flex rounded-xl bg-slate-100 p-1 text-sm font-semibold dark:bg-slate-800" role="tablist">
          {(["login", "register"] as const).map((m) => (
            <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => mode !== m && switchTo(m)}
                    className={`flex-1 rounded-lg py-2 transition ${mode === m ? "bg-white text-brand-700 shadow-sm dark:bg-slate-700 dark:text-white" : "text-slate-500"}`}>
              {m === "login" ? "Sign in" : "Create account"}
            </button>
          ))}
        </div>
        <label className="block text-sm font-medium">Email
          <input key={`email-${mode}`} className="input mt-1" type="email" required value={email}
                 name={mode === "login" ? "email" : "new-email"} autoComplete={mode === "login" ? "username" : "off"}
                 onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="block text-sm font-medium">Password
          <input key={`pw-${mode}`} className="input mt-1" type="password" required
                 name={mode === "login" ? "password" : "new-password"} aria-label="Password" minLength={mode === "register" ? 10 : 1}
                 autoComplete={mode === "login" ? "current-password" : "new-password"} value={password}
                 onChange={(e) => setPassword(e.target.value)} />
          {mode === "register" && <span className="mt-1 block text-xs text-slate-500">At least 10 characters.</span>}
        </label>
        {mode === "register" && (
          <label className="block text-sm font-medium">Confirm password
            <input className="input mt-1" type="password" required minLength={10} name="confirm-password" aria-label="Confirm password"
                   autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </label>
        )}
        {error && (
          <div className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950/40 dark:text-rose-300" role="alert">
            {error}
            {exists && (
              <button type="button" onClick={() => switchTo("login", true)} className="ml-1 font-semibold underline underline-offset-2">
                Sign in instead
              </button>
            )}
            {missing && (
              <span className="mt-1 block text-xs">
                Demo accounts are cleared when the server restarts, so an account made earlier may be gone.{" "}
                <button type="button" onClick={() => switchTo("register", true)} className="font-semibold underline underline-offset-2">
                  Create it again
                </button>
              </span>
            )}
          </div>
        )}
        {waking && busy && (
          <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950/40 dark:text-amber-300" role="status">
            CHECKMATE is waking up (free hosting sleeps when idle). This can take about a minute; we'll keep trying.
          </p>
        )}
        <button className="btn-primary w-full py-2.5" disabled={busy}>
          {busy ? <Loader size={16} /> : <Lock size={16} />} {mode === "login" ? "Sign in" : "Create account"} {!busy && <ArrowRight size={16} />}
        </button>
        <p className="text-center text-[11px] text-slate-500">Passwords are stored as salted scrypt hashes. Sessions use a secure, HttpOnly cookie.</p>
      </form>
    </div>
  );
}
