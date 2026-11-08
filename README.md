# CHECKMATE

**Your AI shopping team. Your rules. Your money protected.**

CHECKMATE searches real stores, checks every offer against your rules, and turns the best one into a hashed, signed **purchase plan** that you approve. After you buy, it reconciles the purchase and issues a tamper-evident **Purchase Truth Passport**. It never invents products: anything a listing doesn't state stays *unknown*, and unknown is never treated as a pass.

> Agents reason; a deterministic policy engine decides. No LLM output can approve, loosen a rule, or authorise a payment.

---

## Quick start

Requirements: Python 3.12+ and Node 20+.

```bash
cp .env.example .env            # then add SERPAPI_KEY (and optionally GEMINI_API_KEYS)

# backend
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows; `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run build && npm start      # http://localhost:3000; forwards /api to BACKEND_URL (default http://localhost:8000)
```

Open http://localhost:3000, **create an account**, and describe what you want to buy.

### Keys

| Setting | What it does | Where to get it |
|---|---|---|
| `SERPAPI_KEY` | **Required to find products.** Real listings from Google Shopping (thousands of stores) | serpapi.com, free plan: 250 searches a month |
| `GEMINI_API_KEYS` | Optional LLM extraction (comma-separate several free keys to rotate them) | aistudio.google.com/apikey |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | Real PayPal **Sandbox** for the demo-store checkout | developer.paypal.com |

Secrets you don't set (payment authorisation, simulated webhooks) are generated randomly at startup. The signing key is created once in `backend/.keys/signing.key`. Keep it out of the database and back it up.

---

## How a purchase works

**Real stores (default).**
1. **Search:** discovery searches Google Shopping via SerpAPI.
2. **Evaluate:** each listing is checked against your rules using only what the listing states (title, store, price, delivery, notes such as "Free 30-day returns").
3. **Plan:** the best offer with no failed rule becomes a **purchase plan**. Everything the listing couldn't prove (warranty, return policy, subscriptions, final total with tax) becomes a checklist.
4. **Approve:** you approve the plan. The approval is bound to the plan's version and SHA-256 hash, and signed with the server key.
5. **Buy:** you open the listing and buy on the store's site. "Refresh price" re-checks the listing and uses one search.
6. **Report:** you record what you paid, answer the checklist, and can attach your receipt (PDF or image; its SHA-256 goes into the passport).
7. **Reconcile:** CHECKMATE reconciles your report against the plan and issues a signed passport, labelled *self-reported*, because it can't see a real store's payment.

**Demo store (`DEMO_CATALOG_ENABLED=true`).** A fictional store whose checkout CHECKMATE *can* inspect, used for tests and to demonstrate the full flow: checkout verification (PASS/FAIL/UNKNOWN), then PayPal approval and capture, then reconciliation. It's off by default and labelled "Demo store (fictional)".

**Modes.**
- **Buy it:** you approve every plan.
- **My mandate (Mode B):** CHECKMATE approves plans that fit a mandate you set: a per-purchase limit, a monthly cap, a store/keyword allow-list, established stores only, and never subscriptions. Payment still needs you.
- **Just watch:** monitoring only. You can switch to buying later.

---

## Security model

- **Accounts:** email + password (scrypt). Sessions use an HttpOnly, SameSite=Lax cookie, and only a hash of the token is stored. State-changing requests must come from an allowed Origin. Sign-in is throttled per IP and per email.
- **One origin:** the browser only talks to the frontend's own origin, and `app/api/[...path]/route.ts` forwards to the backend. No credentials are compiled into the JavaScript bundle.
- **Signed records:** approvals, passports and the audit-chain head are HMAC-signed with a key kept outside the database. `GET /api/v1/passports/{id}/verify` re-checks the hash, signatures and event chain, and the passport page shows the result.
- **Multi-process coordination:** rate limits live in the database (shared by every API process). Per-user action locks use PostgreSQL advisory locks (in-process on SQLite). Background jobs (payment sweeper, offer monitoring) run on one elected leader.
- **No direct browser contact with image hosts:** listing images are served through `/api/v1/images/{id}`.
- **Logs:** structured JSON with W3C `traceparent` trace ids. Configured secrets, plus key/token/password/cookie parameters, are redacted.
- **HTTPS:** `docker compose --profile https up` adds a Caddy front door. Set `SECURE_COOKIES=true` (always on when `APP_ENV=production`).

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest      # API, agents, policy engine, PayPal, SerpAPI, Gemini (all offline)
cd frontend && npm run build && npm run test:e2e   # real browser (Microsoft Edge) against a real backend
```

The browser suite signs up, runs a purchase through the simulated PayPal approval page to a verified passport, checks that a manipulated checkout is blocked, and checks account isolation. Backend tests fake SerpAPI, Gemini and PayPal at the HTTP layer, so they spend no credits.

## Limits that code can't remove

- **Real stores' checkouts can't be inspected.** No store exposes its checkout to third parties. For real-store purchases *you* confirm the checklist, and the passport says so. Only the demo store demonstrates automatic checkout blocking.
- **Real-store payments aren't processed by CHECKMATE**, so the paid amount is self-reported (the receipt upload is your evidence). PayPal covers the demo store only.
- **Coverage is Google Shopping's index:** thousands of retailers, but not every website. One results page (about 40 items) per search; the top 20 are evaluated.
- **Store trust from a listing is a name match** against established retailers. Marketplace sellers ("Walmart - SellerName", "eBay - …") count as limited evidence. Star ratings shown are product ratings.
- **Signatures protect against database edits, not against someone holding both the database and the signing key.** External timestamping would be the next step.
- **The real PayPal Sandbox and PostgreSQL/Docker paths are covered by tests and config checks**, but haven't been exercised end to end on this machine.
- **The free Gemini tier rate-limits quickly.** CHECKMATE then falls back to its rule-based parser, which is English-only.
- **Channel3 isn't integrated** (no API access); the placeholder was removed rather than left pretending.
