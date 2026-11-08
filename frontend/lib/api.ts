export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Demo access token for this single-user build. It is configured at build time and is not a substitute
// for real sign-in; replace with an identity provider before exposing CHECKMATE to other people.
const DEMO_TOKEN = process.env.NEXT_PUBLIC_DEMO_TOKEN ?? "dev-token-alice";

export function getToken(): string {
  return DEMO_TOKEN;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : (detail as { message?: string })?.message ?? `HTTP ${status}`);
  }
  get reasons(): string[] {
    const d = this.detail as { reasons?: string[]; changes?: string[] };
    return d?.reasons ?? d?.changes ?? [];
  }
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...(init.headers ?? {}),
    },
    cache: "no-store",
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? body);
  return body as T;
}

export const post = <T = unknown>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

// ------------------------------------------------------------------ types ---
export type Tri = "PASS" | "FAIL" | "UNKNOWN";

export interface Condition {
  key: string;
  label: string;
  approved: string | null;
  observed: string | null;
  result: Tri;
  reason: string;
  mandatory: boolean;
}

export interface Charge {
  label: string;
  kind: string;
  amount: string | null;
  currency: string;
  status: string;
  recurring: boolean;
  interval: string | null;
  preselected: boolean;
}

export interface Evaluation {
  candidate: {
    simulated?: boolean;
    id: string; product_name: string; brand: string; model: string; variant: string | null; price: string;
    currency: string; seller_id: string; listing_seller_name: string; product_url: string; gaps: string[];
    match_score: number;
  };
  seller: { seller_name: string | null; evidence_level: string; summary: string; missing_evidence: string[];
    disclaimer: string } | null;
  terms: { warranty_months: number | null; return_window_days: number | null; free_returns: boolean | null;
    subscription_required: boolean | null; unknown_fields: string[]; injection_flags: string[];
    inconsistencies: string[]; shipping_cost: string | null } | null;
  cost: { total: string | null; known_subtotal: string | null; is_complete: boolean; includes_estimates: boolean;
    unknown_items: string[]; recurring_items: string[]; lines: Charge[]; currency: string; error: string | null } | null;
  conditions: Condition[];
  status: Tri;
  agent_errors: string[];
}

export interface ContractTerms {
  product_id: string; product_name: string; brand: string; model: string; exact_variant: string | null;
  seller_id: string; seller_name: string; quantity: number; unit_price: string; expected_total: string | null;
  max_total: string; currency: string; warranty_min_months: number | null; warranty_required: boolean;
  return_required: boolean; free_returns_required: boolean; min_return_days: number | null;
  subscription_allowed: boolean; approved_addons: string[]; seller_min_evidence: string;
  max_delivery_days: number | null; source_reference: string; expires_at: string;
}

export interface Contract {
  id: string; version: number; contract_hash: string; approval_status: string; approved: boolean;
  expires_at: string; terms: ContractTerms;
  versions: { version: number; contract_hash: string; reason: string; created_at: string }[];
  approvals: { id: string; version: number; status: string; reason: string | null; created_at: string }[];
}

export interface Verification {
  id: string; status: Tri; conditions: Condition[]; reasons: string[]; explanation: string; scenario: string | null;
  contract_version: number;
  checkout_snapshot: { line_items: Charge[]; stated_total: string | null; currency: string; seller_name: string;
    variant: string | null; source_label: string } | null;
}

export interface Payment {
  id: string; environment: string; paypal_order_id: string | null; paypal_capture_id: string | null;
  approve_url: string | null; amount: string; currency: string; captured_amount: string | null;
  captured_currency: string | null; payment_status: string; idempotency_key: string; error: string | null;
  verification_id: string; contract_version: number;
}

export interface AgentRun {
  id: string; agent_id: string; subject: string | null; status: string; missing_information: string[] | null;
  confidence: number | null; error: string | null; duration_ms: number; started_at: string;
}

export interface Suggestion {
  kind: "raise_budget" | "relax_specs" | "accept_limited_seller" | "lower_warranty";
  label: string;
  detail: string;
  product_id: string;
  product_name: string;
  max_budget?: string;
  drop_specs?: string[];
  clear_variant?: boolean;
  warranty_min_months?: number;
}

export interface WorkflowSummary {
  id: string; state: string; autonomy_mode: string; request: string; product_description: string | null;
  max_budget: string | null; currency: string | null; has_discrepancy: boolean; blocked_reason: string | null;
  created_at: string; updated_at: string;
}

export interface Workflow extends WorkflowSummary {
  intent: Record<string, any> | null;
  evaluations: Evaluation[];
  alternatives: { product_id: string; product_name: string; variant: string | null; seller_name: string;
    total: string; currency: string }[];
  error: string | null;
  suggestions: Suggestion[];
  contract: Contract | null;
  verification: Verification | null;
  payments: Payment[];
  passport_id: string | null;
  agent_runs: AgentRun[];
}

export interface WfEvent {
  seq: number; type: string; agent_id: string | null; state: string; message: string; payload: any; at: string;
  hash: string;
}

export interface SystemInfo {
  paypal_environment: string; llm_enabled: boolean; llm_provider: string; llm_model: string | null;
  demo_controls_enabled: boolean; scenarios: Record<string, string>;
}
