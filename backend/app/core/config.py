"""Environment-based configuration. Secrets are read from the environment only and
are never returned by any API response."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(BACKEND_DIR.parent / ".env")

PAYPAL_SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

# Placeholder values that must never be used as real secrets.
DEFAULT_SECRETS = {"dev-mock-webhook-secret", "dev-payment-authorization-secret",
                   "change-me-payment-authorization-secret", "change-me-mock-webhook-secret"}
PUBLIC_DEMO_TOKENS = {"dev-token-alice", "dev-token-bob", "replace-with-a-long-random-token",
                      "a-long-random-token"}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is not None:
        # Values pasted into a hosting dashboard often carry stray spaces, newlines or quotes.
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    return (_env(name, "true" if default else "false") or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_users(raw: str) -> dict[str, str]:
    """"user:token,other:token2" -> {token: user_id}. Bearer-token access is for tests/automation only."""
    users: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user_id, token = pair.split(":", 1)
        users[token.strip()] = user_id.strip()
    return users


def _csv(raw: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x.strip() for x in (raw or "").split(",") if x.strip()))


_GENERATED: dict[str, str] = {}


def _secret(name: str) -> str:
    """A configured secret, or a random one generated once for this process. Both
    payment authorisations and simulated webhooks are signed and verified within one
    process, so a per-process random secret is safe and never a known default."""
    value = _env(name)
    if value and value not in DEFAULT_SECRETS:
        return value
    return _GENERATED.setdefault(name, secrets.token_hex(32))


@dataclass(frozen=True)
class Settings:
    database_url: str
    openai_api_key: str | None
    openai_model: str
    paypal_client_id: str | None
    paypal_client_secret: str | None
    paypal_webhook_id: str | None
    paypal_mode: str  # "sandbox" | "mock"
    mock_webhook_secret: str
    authorization_secret: str
    backend_public_url: str
    frontend_url: str
    demo_users: dict[str, str] = field(default_factory=dict)
    cors_origins: tuple[str, ...] = ()
    contract_ttl_hours: int = 24
    agent_timeout_seconds: float = 45.0
    rate_limit_per_minute: int = 240
    app_env: str = "development"
    llm_provider: str = "auto"  # auto | gemini | openai | none
    gemini_api_keys: tuple[str, ...] = ()
    gemini_models: tuple[str, ...] = ("gemini-2.5-flash",)
    gemini_max_concurrency: int = 2
    llm_timeout_seconds: float = 15.0
    demo_controls_enabled: bool = False
    serpapi_key: str | None = None
    serpapi_cache_seconds: float = 3600.0
    serpapi_max_results: int = 20
    serpapi_daily_limit: int = 0          # 0 = no daily cap on real searches
    serpapi_reserve: int = 0              # searches public use may never spend
    max_search_depth: str = "wide"        # quick | standard | wide
    requests_per_user_per_day: int = 0    # 0 = unlimited shopping requests per account
    demo_catalog_enabled: bool = False
    jobs_enabled: bool = True
    jobs_interval_seconds: float = 60.0
    allow_registration: bool = True
    session_days: int = 7
    secure_cookies: bool = False
    trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1")
    signing_key: str | None = None
    signing_key_path: str = str(BACKEND_DIR / ".keys" / "signing.key")
    receipt_max_bytes: int = 5 * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.cors_origins, self.frontend_url)))

    def secret_values(self) -> list[str]:
        """Everything that must never appear in logs or responses."""
        return [v for v in (self.paypal_client_secret, self.openai_api_key, self.authorization_secret,
                            self.mock_webhook_secret, self.serpapi_key, self.signing_key, *self.gemini_api_keys,
                            *self.demo_users.keys()) if v]

    def problems(self) -> list[str]:
        out = []
        if set(self.demo_users) & PUBLIC_DEMO_TOKENS:
            out.append("CHECKMATE_DEMO_USERS uses a publicly known default token")
        if not self.serpapi_key and not self.demo_catalog_enabled:
            out.append("No product source: set SERPAPI_KEY to search real stores via Google Shopping")
        if self.paypal_mode == "sandbox" and not self.paypal_webhook_id:
            out.append("PAYPAL_WEBHOOK_ID is not set; all PayPal webhooks will be rejected")
        if self.is_production and not self.secure_cookies:
            out.append("SECURE_COOKIES is off in production; serve CHECKMATE over HTTPS and enable it")
        return out


@lru_cache
def get_settings() -> Settings:
    client_id = _env("PAYPAL_CLIENT_ID")
    secret = _env("PAYPAL_CLIENT_SECRET")
    mode = _env("PAYPAL_MODE") or ("sandbox" if client_id and secret else "mock")
    if mode not in ("sandbox", "mock"):
        raise RuntimeError("PAYPAL_MODE must be 'sandbox' or 'mock'. Live payments are not supported.")
    if mode == "sandbox" and not (client_id and secret):
        raise RuntimeError("PAYPAL_MODE=sandbox requires PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.")
    # RENDER_EXTERNAL_URL is set automatically on Render (the site's public https address).
    frontend = _env("FRONTEND_URL") or _env("RENDER_EXTERNAL_URL") or "http://localhost:3000"
    app_env = (_env("APP_ENV", "development") or "development").lower()
    gemini_keys = _csv(_env("GEMINI_API_KEYS")) + _csv(_env("GEMINI_API_KEY")) + _csv(_env("GOOGLE_API_KEY"))
    provider = (_env("LLM_PROVIDER", "auto") or "auto").lower()
    if provider not in ("auto", "gemini", "openai", "none"):
        raise RuntimeError("LLM_PROVIDER must be auto, gemini, openai or none")
    settings = Settings(
        database_url=_env("DATABASE_URL", f"sqlite:///{(BACKEND_DIR / 'checkmate.db').as_posix()}"),
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
        paypal_client_id=client_id,
        paypal_client_secret=secret,
        paypal_webhook_id=_env("PAYPAL_WEBHOOK_ID"),
        paypal_mode=mode,
        mock_webhook_secret=_secret("MOCK_WEBHOOK_SECRET"),
        authorization_secret=_secret("PAYMENT_AUTHORIZATION_SECRET"),
        backend_public_url=_env("BACKEND_PUBLIC_URL", "http://localhost:8000"),
        frontend_url=frontend,
        demo_users=_parse_users(_env("CHECKMATE_DEMO_USERS", "") or ""),
        cors_origins=tuple(o.strip() for o in (_env("CORS_ORIGINS", frontend) or "").split(",") if o.strip()),
        contract_ttl_hours=int(_env("CONTRACT_TTL_HOURS", "24")),
        agent_timeout_seconds=float(_env("AGENT_TIMEOUT_SECONDS", "45")),
        rate_limit_per_minute=int(_env("RATE_LIMIT_PER_MINUTE", "240")),
        app_env=app_env,
        llm_provider=provider,
        gemini_api_keys=tuple(dict.fromkeys(gemini_keys)),
        gemini_models=_csv(_env("GEMINI_MODELS") or _env("GEMINI_MODEL"))
        or ("gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-flash-latest"),
        gemini_max_concurrency=int(_env("GEMINI_MAX_CONCURRENCY", "2")),
        llm_timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS", "15")),
        demo_controls_enabled=_bool("DEMO_CONTROLS_ENABLED", False),
        jobs_enabled=_bool("JOBS_ENABLED", True),
        serpapi_key=_env("SERPAPI_KEY") or _env("SERPAPI_API_KEY"),
        serpapi_cache_seconds=float(_env("SERPAPI_CACHE_SECONDS", "3600")),
        serpapi_max_results=int(_env("SERPAPI_MAX_RESULTS", "20")),
        serpapi_daily_limit=int(_env("SERPAPI_DAILY_LIMIT", "0")),
        serpapi_reserve=int(_env("SERPAPI_RESERVE", "0")),
        max_search_depth=(_env("MAX_SEARCH_DEPTH", "wide") or "wide").lower(),
        requests_per_user_per_day=int(_env("REQUESTS_PER_USER_PER_DAY", "0")),
        demo_catalog_enabled=_bool("DEMO_CATALOG_ENABLED", False),
        jobs_interval_seconds=float(_env("JOBS_INTERVAL_SECONDS", "60")),
        allow_registration=_bool("ALLOW_REGISTRATION", True),
        session_days=int(_env("SESSION_DAYS", "7")),
        secure_cookies=_bool("SECURE_COOKIES", app_env == "production"),
        trusted_proxies=_csv(_env("TRUSTED_PROXIES", "127.0.0.1,::1")),
        signing_key=_env("SIGNING_KEY"),
        signing_key_path=_env("SIGNING_KEY_PATH", str(BACKEND_DIR / ".keys" / "signing.key")),
        receipt_max_bytes=int(_env("RECEIPT_MAX_BYTES", str(5 * 1024 * 1024))),
    )
    if settings.is_production:
        if set(settings.demo_users) & PUBLIC_DEMO_TOKENS:
            raise RuntimeError("Refusing to start in production with a publicly known demo token.")
    return settings
