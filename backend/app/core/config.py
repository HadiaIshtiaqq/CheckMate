"""Environment-based configuration. Secrets are read from the environment only and
are never returned by any API response."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(BACKEND_DIR.parent / ".env")

PAYPAL_SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _parse_users(raw: str) -> dict[str, str]:
    """"alice:token-a,bob:token-b" -> {token: user_id}"""
    users: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user_id, token = pair.split(":", 1)
        users[token.strip()] = user_id.strip()
    return users


DEFAULT_SECRETS = {"dev-mock-webhook-secret", "dev-payment-authorization-secret",
                   "change-me-payment-authorization-secret", "change-me-mock-webhook-secret"}
DEFAULT_DEMO_USERS = "alice:dev-token-alice,bob:dev-token-bob"


def _csv(raw: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x.strip() for x in (raw or "").split(",") if x.strip()))


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
    agent_timeout_seconds: float = 30.0
    rate_limit_per_minute: int = 240
    app_env: str = "development"
    llm_provider: str = "auto"  # auto | gemini | openai | none
    gemini_api_keys: tuple[str, ...] = ()
    gemini_models: tuple[str, ...] = ("gemini-2.5-flash",)
    gemini_max_concurrency: int = 2
    llm_timeout_seconds: float = 20.0
    demo_controls_enabled: bool = False
    jobs_enabled: bool = True
    jobs_interval_seconds: float = 60.0

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def secret_values(self) -> list[str]:
        """Everything that must never appear in logs or responses."""
        return [v for v in (self.paypal_client_secret, self.openai_api_key, self.authorization_secret,
                            self.mock_webhook_secret, *self.gemini_api_keys, *self.demo_users.keys()) if v]

    def problems(self) -> list[str]:
        out = []
        if self.authorization_secret in DEFAULT_SECRETS:
            out.append("PAYMENT_AUTHORIZATION_SECRET is a default value")
        if self.paypal_mode == "mock" and self.mock_webhook_secret in DEFAULT_SECRETS:
            out.append("MOCK_WEBHOOK_SECRET is a default value")
        if set(self.demo_users) & {"dev-token-alice", "dev-token-bob"}:
            out.append("CHECKMATE_DEMO_USERS uses the public default tokens")
        if self.paypal_mode == "sandbox" and not self.paypal_webhook_id:
            out.append("PAYPAL_WEBHOOK_ID is not set; all PayPal webhooks will be rejected")
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
    frontend = _env("FRONTEND_URL", "http://localhost:3000")
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
        mock_webhook_secret=_env("MOCK_WEBHOOK_SECRET", "dev-mock-webhook-secret"),
        authorization_secret=_env("PAYMENT_AUTHORIZATION_SECRET", "dev-payment-authorization-secret"),
        backend_public_url=_env("BACKEND_PUBLIC_URL", "http://localhost:8000"),
        frontend_url=frontend,
        demo_users=_parse_users(_env("CHECKMATE_DEMO_USERS", DEFAULT_DEMO_USERS)),
        cors_origins=tuple(o.strip() for o in (_env("CORS_ORIGINS", frontend) or "").split(",") if o.strip()),
        contract_ttl_hours=int(_env("CONTRACT_TTL_HOURS", "24")),
        agent_timeout_seconds=float(_env("AGENT_TIMEOUT_SECONDS", "45")),
        rate_limit_per_minute=int(_env("RATE_LIMIT_PER_MINUTE", "240")),
        app_env=app_env,
        llm_provider=provider,
        gemini_api_keys=tuple(dict.fromkeys(gemini_keys)),
        gemini_models=_csv(_env("GEMINI_MODELS") or _env("GEMINI_MODEL"))
        or ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"),
        gemini_max_concurrency=int(_env("GEMINI_MAX_CONCURRENCY", "2")),
        llm_timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS", "15")),
        demo_controls_enabled=(_env("DEMO_CONTROLS_ENABLED", "false") or "false").lower() == "true",
        jobs_enabled=(_env("JOBS_ENABLED", "true") or "true").lower() == "true",
        jobs_interval_seconds=float(_env("JOBS_INTERVAL_SECONDS", "60")),
    )
    if not settings.demo_users:
        raise RuntimeError("CHECKMATE_DEMO_USERS must define at least one user:token pair")
    if settings.is_production:
        fatal = [p for p in settings.problems() if "default" in p]
        if fatal:
            raise RuntimeError("Refusing to start in production: " + "; ".join(fatal))
    return settings
