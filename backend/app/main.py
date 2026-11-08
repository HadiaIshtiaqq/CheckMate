from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import account_routes, mock_paypal, routes
from app.core.config import get_settings
from app.database.session import init_db
from app.orchestrator.service import ServiceError
from app.orchestrator.state_machine import InvalidTransition
from app.security.auth import (
    JsonFormatter,
    RateLimitMiddleware,
    RedactSecretsFilter,
    SecurityHeadersMiddleware,
    TracingMiddleware,
)

log = logging.getLogger("checkmate")


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactSecretsFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # httpx logs full request URLs at INFO; SerpAPI takes its key as a query parameter.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()  # uvicorn re-configures logging at startup
    init_db()
    s = get_settings()
    for problem in s.problems():
        log.warning("Configuration: %s", problem)
    from app.services.container import get_container

    llm = get_container().llm.status()
    log.info("LLM provider: %s (%s), PayPal: %s", llm["provider"], llm.get("model") or "deterministic only",
             s.paypal_mode)
    from app.orchestrator import jobs
    from app.security.signing import key_id

    log.info("Signing key id %s", key_id())

    jobs.start()
    yield
    jobs.stop()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="CHECKMATE API", version="0.1.0", lifespan=lifespan,
                  description="Autonomous AI commerce & purchase integrity platform")
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware, per_minute=s.rate_limit_per_minute)
    app.add_middleware(CORSMiddleware, allow_origins=list(s.allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "traceparent"])
    app.add_middleware(TracingMiddleware)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        detail = exc.detail if isinstance(exc.detail, (dict, list)) else str(exc.detail)
        return JSONResponse({"detail": detail}, status_code=exc.status_code)

    @app.exception_handler(InvalidTransition)
    async def _invalid_transition(_: Request, exc: InvalidTransition):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Invalid request", "errors": [
            {"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]}, status_code=422)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(routes.router)
    app.include_router(account_routes.router)
    if s.paypal_mode == "mock":
        app.include_router(mock_paypal.router)
    return app


app = create_app()
