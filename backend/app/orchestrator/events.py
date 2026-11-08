"""Hash-chained workflow event log: each event commits to its predecessor, so any
edit or deletion of history is detectable."""
from __future__ import annotations

import threading
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.util import canonical_json, sha256_hex, to_jsonable, utcnow
from app.database.models import Workflow, WorkflowEvent

GENESIS = "0" * 64
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def _event_hash(prev_hash: str, e: WorkflowEvent) -> str:
    body = {"workflow_id": e.workflow_id, "seq": e.seq, "event_type": e.event_type, "agent_id": e.agent_id,
            "state": e.state, "message": e.message, "payload": e.payload, "ts": e.ts}
    return sha256_hex(prev_hash + canonical_json(body))


def emit(db: Session, wf: Workflow, event_type: str, message: str, *, agent_id: str | None = None,
         payload=None) -> WorkflowEvent:
    state = wf.state
    body = to_jsonable(payload) if payload is not None else None
    with _locks[wf.id]:
        for _ in range(8):
            last = db.scalars(select(WorkflowEvent).where(WorkflowEvent.workflow_id == wf.id)
                              .order_by(WorkflowEvent.seq.desc()).limit(1)).first()
            e = WorkflowEvent(workflow_id=wf.id, seq=(last.seq + 1) if last else 1, event_type=event_type,
                              agent_id=agent_id, state=state, message=message, payload=body,
                              ts=utcnow().isoformat())
            e.prev_hash = last.hash if last else GENESIS
            e.hash = _event_hash(e.prev_hash, e)
            db.add(e)
            try:
                db.commit()
                return e
            except IntegrityError:  # another process appended concurrently: rebuild on the new head
                db.rollback()
        raise RuntimeError(f"Could not append event to workflow {wf.id}")


def list_events(db: Session, workflow_id: str, after_seq: int = 0) -> list[WorkflowEvent]:
    return list(db.scalars(select(WorkflowEvent).where(WorkflowEvent.workflow_id == workflow_id,
                                                       WorkflowEvent.seq > after_seq).order_by(WorkflowEvent.seq)))


def verify_chain(events: list[WorkflowEvent]) -> tuple[bool, str | None]:
    prev = GENESIS
    for e in events:
        if e.prev_hash != prev or _event_hash(prev, e) != e.hash:
            return False, f"Chain broken at seq {e.seq}"
        prev = e.hash
    return True, None
