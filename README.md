# CHECKMATE

**Your AI shopping team. Your rules. Your money protected.**

CHECKMATE turns a shopper's natural-language request into a hashed, versioned **Purchase Contract**, has specialised agents gather seller and terms evidence, verifies the final checkout against that contract, and only then lets a PayPal Sandbox payment proceed, with buyer approval. Every purchase ends with a **Purchase Truth Passport**: a tamper-evident audit record.

> Agents reason; a deterministic policy engine decides. No LLM output can authorise a payment.

---

## Quick start (local, no credentials needed)

Requirements: Python 3.12+ and Node 20+.

```bash
# backend
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run dev                     # http://localhost:3000
```

With no `.env`, CHECKMATE runs with:

- **Simulated PayPal**: a local, clearly labelled stand-in for the PayPal Orders v2 flow, including a hosted approval page, `PayPal-Request-Id` idempotency and `ORDER_NOT_APPROVED` errors. No money moves.
- **Rule-based agents**: deterministic parsing. Add free Gemini keys (below) to enable LLM extraction.
- **SQLite** at `backend/checkmate.db`.

### Using free Gemini API keys

1. Create one or more free keys at https://aistudio.google.com/apikey.
2. In `.env` at the repo root, set `GEMINI_API_KEYS=key1,key2` (one key also works).
3. Restart the backend. The header shows `LLM: Gemini · <model>`, and the startup log names the provider.

How the free tier is handled:
- **Key rotation:** a key that hits the rate limit (429) cools down for 60 seconds while the next key is used. A rejected key is disabled.
- **Model fallback:** models in `GEMINI_MODELS` are tried in order, so a retired model (404) falls through to the next.
- **Low concurrency, plus caching:** identical prompts are not re-sent.
- **Deterministic fallback:** if every key is exhausted, or Gemini is slow or returns invalid output, the agents keep working with rule-based parsing.
- **Keys stay out of logs:** keys travel in the `x-goog-api-key` header, never the URL, and are redacted from logs.

**What the LLM can and cannot do.** It can add descriptive detail and fill in facts that are *verifiably present* in the source text (a number it reports must appear in the text). It can only make a yes/no term *stricter*: "subscription required" or "returns not free". It cannot raise a budget: a disagreement with the rule parser forces a clarification question. It cannot allow a subscription, loosen a requirement, or decide anything about payment. The free tier may use prompts to improve Google products. CHECKMATE sends only the request text and merchant policy text, never payment data. `OPENAI_API_KEY` remains supported as an alternative (`LLM_PROVIDER=openai`).

### Using the real PayPal Sandbox

1. Create a REST app at developer.paypal.com (Sandbox), then copy `.env.example` to `.env` at the repo root.
2. Set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. The app switches to `api-m.sandbox.paypal.com` automatically. Live mode is deliberately unsupported.
3. For webhooks, expose `POST /api/v1/webhooks/paypal` publicly (e.g. with a tunnel), subscribe to `CHECKOUT.ORDER.APPROVED` and `PAYMENT.CAPTURE.*`, and set `PAYPAL_WEBHOOK_ID`. Signatures are verified through PayPal's `verify-webhook-signature` API; without a webhook ID, every webhook is rejected.
4. Approve the payment with a Sandbox *personal* buyer account.

### Docker (PostgreSQL)

```bash
docker compose up --build       # frontend :3000, API :8000, Postgres :5432
```

---

## Demo script: the three scenarios

Choose **Buy it** and submit the first example request:
*"Find wireless headphones under $110 … at least a one-year warranty, free returns, no subscription … reliable seller."*

| | Steps | Expected |
|---|---|---|
| **A: Safe purchase** | Approve contract v1 → verify with scenario `clean` → **Pay with PayPal** → approve on PayPal | PASS → capture of $105.00 → reconciliation RECONCILED → Passport |
| **B: Manipulated checkout** | New workflow → approve → verify with `subscription_addon` | FAIL (total $120 > $110; pre-selected $15/month subscription) → **PAYMENT BLOCKED** → alternative found (Lumen Nova 2, $109.12) → "Use this offer" creates contract v2, which needs fresh approval |
| **C: Post-payment discrepancy** | New workflow → approve → verify with `receipt_discrepancy` → pay | PayPal capture stays $105.00; the simulated merchant receipt says $115.00 → discrepancy recorded in the Passport, with no claim that PayPal charged $115 |

Further checkout behaviours to try: `variant_swap`, `seller_swap`, `currency_swap` (all FAIL), and `hidden_fee`, `warranty_removed` (both UNKNOWN, which still blocks payment).

The evaluation step also demonstrates:
- **Prompt injection:** the EchoBeat listing tells "AI agents" to raise the budget to $500. The text is flagged, ignored, and the offer fails on its required subscription.
- **UNKNOWN is never PASS:** Tempo Lite has an undisclosed warranty and tax, so it cannot be contracted.
- **Monitoring:** `POST /api/v1/demo/offers/hp-aurora-x {"price":"92.00"}` after approval. The next verification detects the change, creates contract v2 and invalidates the old approval.

---

## How it maps to the specification

| Spec | Implementation |
|---|---|
| 10 agents (TRD 2.3) | `backend/app/agents/*`: each has an `agent_id`, a **tool allowlist** (`ScopedTools` raises `ToolPermissionError`) and typed pydantic output wrapped in an `AgentEnvelope` (workflow id, agent id, timestamp, evidence, confidence, missing info, error) |
| Deterministic policy engine (3.5) | `app/policies/engine.py`: candidate evaluation, checkout verification (PASS/FAIL/UNKNOWN), payment authorisation. It issues an HMAC-signed `PaymentAuthorization`, and the Payment Agent refuses to act without one |
| Cost auditor (FR-05) | `app/policies/cost.py`: `Decimal` only. Confirmed, estimated, unknown and recurring charges are kept apart; mixed currencies are never combined |
| Contract versioning + hash (FR-06) | `app/policies/contracts.py`: SHA-256 of canonical JSON. Approval binds to version + hash; any material change → new version, old approvals `INVALIDATED` |
| Workflow states (3.4) | `app/orchestrator/state_machine.py`: invalid transitions raise. Adds `NEEDS_CLARIFICATION` and `CHECKOUT_BLOCKED` |
| Persistent, resumable workflows | Every step is persisted; interrupted workflows resume on startup (`resume_incomplete`) |
| Audit log | `workflow_events` is **hash-chained** (each event commits to the previous one); edits are detectable and the chain status is shown in the UI and the Passport |
| PayPal (FR-08, 2.7) | `app/integrations/paypal/client.py`: OAuth2 client credentials, `PayPal-Request-Id` on create/capture, timeouts treated as *unknown* (order state is re-queried, never assumed failed), webhook signature verification and deduplication, and webhook claims re-confirmed against `GET /v2/checkout/orders/{id}` |
| Merchant adapters (4.7) | `app/integrations/merchants/`: a standard interface where each adapter declares its supported operations. The controlled demo merchant exposes exact checkout snapshots. `Channel3Adapter` is a placeholder declaring no operations |
| Purchase Truth Passport (FR-10) | `build_passport` in `app/orchestrator/service.py`; JSON export at `GET /api/v1/passports/{id}/export` |
| API (2.6) | All listed endpoints, plus `/workflows/{id}/clarify|retry|cancel|monitor`, `/payments/{id}/cancel`, `/system` |
| Screens (3.2) | Dashboard + workspace (`/`), workflow page with Agent Activity Centre, Product Comparison, Contract, Checkout Verification and PayPal panels (`/workflows/[id]`), and the Passport (`/passports/[id]`) |

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest      # all tests run offline; Gemini and PayPal are faked
```

The tests cover Testing Document sections 5.2–5.7: the cost and intent tables, every checkout-verification scenario, contract hashing and versioning, agent permission enforcement, failed agents never passing, resume after restart, PayPal create/approve/capture/cancel/decline, timeout-then-reconcile, idempotent retries, valid, invalid and duplicate webhooks, delayed-webhook reconciliation, user isolation, prompt injection, revoked and expired authorisations, duplicate-capture prevention, secret redaction, the three end-to-end scenarios, and the Gemini client: key rotation, model fallback, schema fallback, caching, and a *hostile* fake Gemini that tries to loosen terms and inflate the budget.

## Honest limits of this MVP

- **Mode B (bounded autonomous mandates)** is not implemented. True unattended payment needs PayPal vaulting / billing agreements, which are out of scope. Modes A (Assisted) and C (Monitoring only) work.
- **Production mode** (`APP_ENV=production`) refuses to start with default secrets or demo tokens, and disables demo controls. It is still not a substitute for real identity.
- **Authentication** uses demo bearer tokens from `CHECKMATE_DEMO_USERS`. Swap in a real identity provider before any real use.
- **Background execution** uses FastAPI background tasks and threads, not Redis/Celery. The agent and orchestrator boundaries are designed so a queue can be added without changing them.
- **Rate limiting** is in-process (per API instance).
- **Products, sellers and receipts** come from a controlled, fictional demo merchant. CHECKMATE does not claim it can inspect arbitrary retailers' checkouts.
- **Channel3** is a placeholder adapter, not an integration.
- **Any product can be requested.** Headphones, laptops, keyboards and coffee makers come from a curated catalogue. For anything else (a chair, a desk lamp, running shoes) the controlled demo merchant generates five *simulated* listings anchored to your budget. These are deliberately mixed: one good fit, one over budget, one with a hidden subscription, one with undisclosed terms, and one with a short warranty. They are labelled "Simulated listing" in the UI.
- **Just watch mode** keeps payment off. The Monitoring Agent re-checks the offer every `JOBS_INTERVAL_SECONDS`, and you can switch the workflow to **Buy it** at any time without losing your safeguards.
- **Single-process safeguards.** Per-user request serialisation, the payment sweeper and the rate limiter run inside one API process. Multiple instances would need row locks and a shared store such as Redis.
- **Schema changes** that only add columns are applied automatically at startup. Renames and type changes need Alembic.
- **Seller assessments** report evidence levels; they are not guarantees.
