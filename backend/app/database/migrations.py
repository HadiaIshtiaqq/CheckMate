"""Versioned schema migrations.

create_all() creates new tables and ensure_columns() adds new nullable columns.
Anything else (indexes on existing tables, renames, type changes, data backfills)
is an explicit, ordered, run-once migration recorded in schema_migrations."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.util import utcnow


def _m0001_indexes(conn: Connection) -> None:
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflows_user_created ON workflows (user_id, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_payments_status_updated "
                      "ON payment_transactions (payment_status, updated_at)"))


def _m0002_approval_source_backfill(conn: Connection) -> None:
    conn.execute(text("UPDATE contract_approvals SET source = 'user' WHERE source IS NULL"))


MIGRATIONS: list[tuple[str, Callable[[Connection], None]]] = [
    ("0001_indexes", _m0001_indexes),
    ("0002_approval_source_backfill", _m0002_approval_source_backfill),
]


def run_migrations(engine) -> list[str]:
    applied: list[str] = []
    with engine.begin() as conn:
        done = {r[0] for r in conn.execute(text("SELECT id FROM schema_migrations"))}
        for mid, fn in MIGRATIONS:
            if mid in done:
                continue
            fn(conn)
            conn.execute(text("INSERT INTO schema_migrations (id, applied_at) VALUES (:id, :at)"),
                         {"id": mid, "at": utcnow()})
            applied.append(mid)
    return applied
