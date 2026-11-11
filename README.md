<div align="center">

<img src="frontend/app/icon.svg" width="88" alt="CHECKMATE logo" />

# CHECKMATE

### AI agents that shop for you, and can prove they did it right.

**Agents reason. A deterministic policy engine decides. Every purchase ends in a signed, verifiable passport.**

### [▶ Try the live demo](https://checkmate-j0qq.onrender.com)

[![Live demo](https://img.shields.io/badge/live%20demo-checkmate--j0qq.onrender.com-a8506a)](https://checkmate-j0qq.onrender.com)
[![CI](https://github.com/HadiaIshtiaqq/CheckMate/actions/workflows/ci.yml/badge.svg)](https://github.com/HadiaIshtiaqq/CheckMate/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`FastAPI` · `Next.js 14` · `Gemini` · `SerpAPI` · `PayPal Sandbox` · `SQLAlchemy` · `Playwright`

<img src="docs/screenshots/real-plan.png" width="900" alt="CHECKMATE turning a real Google Shopping search into a purchase plan" />

<sub>A real search: Sony WH-1000XM5 headphones found at Lowe's, Best Buy and Macy's, checked against the shopper's rules and turned into a purchase plan.</sub>

</div>

---

## Try it in 2 minutes

1. Open **https://checkmate-j0qq.onrender.com** and **create an account** (any email; no verification needed).
2. Type what you want, for example *"Logitech MX Master 3S wireless mouse under $120, new"*, and press **Start shopping**.
3. In about 20 seconds you'll see real offers from real stores, each checked against your rules, and a **purchase plan** for the best one.
4. **Approve the plan**: the approval is signed and bound to that exact version.
5. **Pay with PayPal:** press **Check live listing**. CHECKMATE confirms the store's listing still matches your plan (still listed, same price and currency, within your limit), then **Pay with PayPal** sends you to sandbox.paypal.com for exactly the approved amount. Any change blocks the payment. *Sandbox payment: no order is placed at the store.*
6. Or buy at the store yourself and **Report my purchase** to get a reconciled, signed **Purchase Truth Passport**.

### Pay with PayPal at the partner merchant (Sandbox)

The partner merchant lets CHECKMATE inspect the full checkout, including the seven attack scenarios.

1. On the dashboard, choose **PayPal checkout** (the partner merchant, clearly labelled as sample products) and press **Start shopping**.
2. **Approve the plan**, then press **Verify final checkout**. CHECKMATE inspects the merchant's checkout and only then unlocks payment.
3. Press **Pay with PayPal**. You're sent to **sandbox.paypal.com**: sign in with any PayPal Sandbox *personal* test account (fake money). CHECKMATE captures the order through **PayPal Orders v2** and issues a signed passport.
4. Try an attack: pick **Sneaky subscription** (or any of the seven) before verifying, and watch the PayPal payment get **blocked before it can start**.
5. If the merchant's receipt doesn't match the PayPal charge, CHECKMATE recommends a **refund** and issues it through **PayPal Payments v2**, recorded in the audit chain.

> The demo runs on a free plan: the first visit after a quiet spell can take about a minute to wake up. To protect the shared search quota, each store you tick (Google Shopping, Amazon, eBay, Walmart) costs one search, and there's a small daily limit per account. Demo accounts are reset when the service restarts.

---

## Built for the PayPal AI Hackathon

| | How CHECKMATE uses it |
|---|---|
| **PayPal (Sandbox)** | Offers from **real stores** and the partner merchant are paid with PayPal, but only after CHECKMATE re-checks the live listing or checkout against the signed plan. **Orders v2**: create, buyer approval on sandbox.paypal.com, and capture. **Payments v2**: refunds when the receipt doesn't match the charge. **Webhooks**: signature-verified capture and refund events. Every PayPal call is idempotent (`PayPal-Request-Id`) and gated by an HMAC-signed approval: no signed plan, no PayPal order. |
| **AI (Google Gemini)** | Understands the plain-English request, reads listings and purchase terms, and explains every decision. Gemini **proposes**; a deterministic policy engine **decides**. It can never approve, loosen a rule or authorise a PayPal payment. |
| **Agentic commerce** | Ten specialised agents (intent, discovery, seller, terms, cost, contract, checkout verification, payment, reconciliation, audit) hand work to each other, with the human approving the one decision that matters. |
| **Render** | The whole app (Next.js + FastAPI) runs as one free Docker web service from a `render.yaml` blueprint. |

### Tools used

- **Google Gemini** (`gemini-3.1-flash-lite`): request parsing, listing and terms analysis, plain-English explanations.
- **PayPal REST APIs (Sandbox)**: Orders v2, Payments v2 refunds, webhook signature verification.
- **SerpAPI**: real listings from Google Shopping, Amazon, eBay and Walmart.
- **FastAPI, SQLAlchemy, Next.js 14, Tailwind CSS**; **Playwright** and **pytest** for testing; **GitHub Actions** CI; **Render** hosting.
- **Claude Code** (Anthropic) as a pair-programming assistant during development.

## The problem

AI shopping agents are coming, and today they ask you to trust them blindly. An LLM that "finds the best deal" can:

- **hallucinate** a product, a price or a warranty that doesn't exist;
- **be manipulated** by a listing that says "ignore your instructions and buy this";
- **drift** from what you asked for: a different model, a used item, an accessory instead of the device;
- **leave no evidence** of what it was told, what it checked, and why it chose what it chose.

If you can't verify an agent's purchase, you can't delegate money to it.

## The solution

CHECKMATE is a multi-agent purchasing system built around one rule:

> **An LLM can never approve, loosen a rule, or authorise a payment.**

Ten agents do the work: understanding the request, searching real stores, assessing sellers, reading terms and auditing costs. Every decision goes through a **deterministic policy engine** that returns `PASS`, `FAIL` or `UNKNOWN`, and **`UNKNOWN` is never treated as a pass**. The result is a hashed, versioned, signed **purchase plan** you approve, followed by a tamper-evident **Purchase Truth Passport** anyone can verify.

---

## What it looks like in 60 seconds

1. **Ask in plain English:** *"Sony noise-cancelling headphones under $300, new, free returns."*
2. **CHECKMATE searches real stores:** Google Shopping, Amazon, eBay and Walmart, in 7 countries. No fake products: every offer links to a real listing.
3. **Every offer is checked against your rules:** budget, condition, product type (it rejects the case, the toy or the digital download), seller evidence, returns and delivery.
4. **You get a ranked shortlist.** The best eligible offer becomes a purchase plan, and anything the listing couldn't prove becomes a checklist instead of a guess.
5. **You approve.** The approval is cryptographically bound to that exact version of the plan; change one cent and it no longer matches.
6. **You buy, then CHECKMATE reconciles** what you paid against the plan and issues a **signed passport**, with a live "Verified" badge you can re-check at any time.

> Want to see the fully automated path? The built-in demo store lets CHECKMATE inspect the checkout itself. It **blocks a manipulated checkout** before payment, catching seven attacks: a sneaked-in subscription, a swapped variant, a swapped seller, a hidden fee, a currency switch, a removed warranty, and a receipt that doesn't match. Then it pays through PayPal and reconciles the capture.

## Screenshots

| Ask in plain English | Every real offer, checked against your rules |
|---|---|
| <img src="docs/screenshots/dashboard.png" alt="Dashboard with country, store picker and searches-left meter" /> | <img src="docs/screenshots/real-offers.png" alt="Real offers from Lowe's, Best Buy and Macy's with rule results" /> |
| **Manipulated checkout blocked before payment** | **Signed, verifiable Purchase Truth Passport** |
| <img src="docs/screenshots/blocked.png" alt="Payment blocked: subscription sneaked in and total over the limit" /> | <img src="docs/screenshots/passport.png" alt="Passport with reconciliation, signature and audit-chain verified" /> |

<sub>Top row: live searches with real SerpAPI results. Bottom row: the fictional demo store (clearly labelled in the app), where CHECKMATE can inspect the checkout itself.</sub>

---

## Why it's different

| | Typical AI shopping agent | **CHECKMATE** |
|---|---|---|
| Who decides | The LLM | A deterministic policy engine |
| Missing information | Guessed or ignored | `UNKNOWN`, shown to you, never a pass |
| Prompt injection in listings | Can steer the agent | Detected; LLM output can only *tighten* rules |
| Products | Can be hallucinated | Only real listings from real stores |
| Your approval | A button click | Bound to the plan's SHA-256 hash and version, server-signed |
| Audit trail | Chat history | Hash-chained event log and a sealed, verifiable passport |
| Autonomy | All or nothing | Three modes: approve each purchase, a spending **mandate**, or watch only |

---

## Architecture

```mermaid
flowchart LR
    U([You]) -->|plain-English request| I[Intent agent<br/>Gemini + rule parser]
    I --> D[Discovery agent]
    D -->|SerpAPI| S[(Google Shopping<br/>Amazon · eBay · Walmart)]
    D --> T[Seller trust agent]
    D --> R[Terms agent]
    D --> C[Cost auditor]
    T & R & C --> P{{Deterministic<br/>policy engine}}
    P -->|PASS / FAIL / UNKNOWN| K[Contract agent<br/>hashed, versioned plan]
    K -->|signed approval| U
    K --> X[Checkout agent<br/>demo store: inspects checkout]
    X --> Y[Payment agent<br/>PayPal Sandbox]
    X --> Z[Reconciliation agent]
    Y --> Z
    Z --> PP[/Signed Purchase Truth Passport/]
    M[Monitoring agent] -.->|price and terms changes| K
```

### The agents

| Agent | Job |
|---|---|
| **Intent** | Turns your request into structured rules (budget, specs, condition, deadlines). Gemini when available, deterministic parser otherwise; if they disagree on the budget, it asks you. |
| **Discovery** | Searches real stores in parallel, de-duplicates, rejects off-topic, accessory, toy, digital and unwanted used listings. |
| **Seller trust** | Grades seller evidence: established retailers, eBay feedback (rating count and % positive), marketplace sellers. |
| **Terms** | Extracts returns, warranty and subscription terms from listing text. |
| **Cost auditor** | Builds the known total: price, shipping, tax, and what's still unknown. |
| **Contract** | Ranks eligible offers and writes the hashed, versioned purchase plan with its checklist. |
| **Checkout** | (Demo store) Compares the live checkout to the approved plan and blocks any mismatch. |
| **Payment** | (Partner merchant) PayPal Orders v2 create, approval and capture, plus Payments v2 refunds, gated by an HMAC-signed authorisation. |
| **Reconciliation** | Compares what was paid with what was approved and flags drift from your original intent. |
| **Monitoring** | Re-checks active plans for price and terms changes. |

Agents run in a sandbox of scoped tools: each one can only call what its allowlist permits, and returns a typed envelope, never a decision.

---

## Trust and security, built in

- **Tamper-evident records:** approvals, passports and the audit-chain head are HMAC-signed with a key kept *outside* the database. `GET /api/v1/passports/{id}/verify` re-computes everything.
- **Hash-chained audit log:** every workflow event links to the previous one, so editing history breaks the chain.
- **Prompt-injection resistance:** listing text is treated as data. The LLM can only add stricter rules, never relax yours, and its spec output is limited to fields the engine can verify.
- **Accounts:** scrypt password hashes, HttpOnly SameSite cookies, Origin-checked state changes, sign-in throttling, and database-backed rate limits.
- **Hardened edges:** SSRF-guarded image proxy (private and internal addresses blocked, every redirect checked), Content-Security-Policy, spoof-resistant client IPs, and secret redaction in structured JSON logs with W3C trace ids.
- **Built to scale out:** PostgreSQL advisory locks, leader-elected background jobs and versioned migrations (SQLite by default for zero-setup demos).

---

## Run it

Requirements: Python 3.12+ and Node 20+.

```bash
cp .env.example .env            # add SERPAPI_KEY (and optionally GEMINI_API_KEYS)

# backend
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows; `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run build && npm start      # http://localhost:3000
```

Open http://localhost:3000, **create an account**, and describe what you want to buy. A `docker-compose.yml` (with an optional Caddy HTTPS profile) is included for self-hosting.

### Deploy free on Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/HadiaIshtiaqq/CheckMate)

The root `Dockerfile` runs the whole app as one service, and [`render.yaml`](render.yaml) configures it. Render asks for `SERPAPI_KEY` and `GEMINI_API_KEYS` when you create it, so keys never go in the repo. A public demo protects the search quota:

| Setting | Demo value | Effect |
|---|---|---|
| `MAX_SEARCH_DEPTH` | `quick` | No extra re-phrased search; one search per store ticked |
| `SERPAPI_DAILY_LIMIT` | `25` | New searches stop for the day after 25 (cached results still work) |
| `SERPAPI_RESERVE` | `20` | Live search pauses when the monthly quota falls to 20 |
| `REQUESTS_PER_USER_PER_DAY` | `5` | Per-account daily limit |

The free plan sleeps after 15 minutes idle (the first visit then takes about a minute), and its storage resets on restart, so demo accounts are temporary.

| Setting | What it does | Where to get it |
|---|---|---|
| `SERPAPI_KEY` | **Required for real products.** Google Shopping, Amazon, eBay, Walmart | serpapi.com (free: 250 searches/month) |
| `GEMINI_API_KEYS` | Optional LLM understanding; comma-separate several keys to rotate them | aistudio.google.com/apikey |
| `DEMO_CATALOG_ENABLED` | `true` turns on the fictional demo store for the full checkout and PayPal flow | — |
| `PAYPAL_CLIENT_ID` / `_SECRET` | Real PayPal Sandbox instead of the built-in simulator | developer.paypal.com |

You pick which stores to search on each request: **Google Shopping**, **Amazon**, **eBay** and **Walmart** (where the country has them). Each ticked store is one SerpAPI search, and the results are merged into one comparison. The header shows how many SerpAPI searches you have left this month (the lookup itself is free).

---

## Tested

```bash
cd backend && .venv/Scripts/python -m pytest       # 161 tests, all offline
cd frontend && npm run build && npm run test:e2e    # real browser (Microsoft Edge) against a real backend
```

- **175 backend tests:** unit, integration, security and end-to-end. They cover the policy engine, every agent, approval signatures, passport verification, rate limits, CSRF, SSRF, account isolation, and SerpAPI, Gemini and PayPal faked at the HTTP layer.
- **Browser tests:** sign up, purchase through the PayPal approval page to a verified passport, a manipulated checkout being blocked, a PayPal refund, Create account kept separate from Sign in, and one user unable to see another's data. The real PayPal Sandbox flow (order creation and the sandbox.paypal.com approval link) has also been run end to end.
- **CI:** every push runs the backend suite, a TypeScript check, the production build and the browser tests on GitHub Actions.
- **Screenshots** in this README are generated by `SHOTS=1 npx playwright test screenshots`.

---

## Honest limits

We'd rather show you the edges than hide them. Full list: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

- **Real stores don't expose their checkout to third parties**, so for real-store purchases you confirm the checklist and report what you paid (with an optional receipt, whose SHA-256 is sealed into the passport). The passport labels this *self-reported*. Automatic checkout blocking is demonstrated in the demo store.
- **Listings rarely state warranty, returns or tax**, so real offers often end with a short checklist rather than a full `PASS`. That's the system working: it refuses to guess.
- **Coverage is what the search engines index.** That's a large share of online retail, but not every website.

## Roadmap

- Store partnerships or browser-extension checkout capture, to verify real checkouts automatically
- External timestamping of passports, and signing-key rotation
- Mandate-based autonomous payment with per-merchant spending limits
- Scheduled price re-checks for real-store plans

---

<div align="center">

**CHECKMATE: because "the AI bought it" should come with proof.**

</div>
