"""Deterministic helpers: money arithmetic, canonical hashing, ids and time."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

CENT = Decimal("0.01")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def money(value: Any) -> Decimal | None:
    """Normalise any numeric input to a 2-dp Decimal. Floats go through str() so
    binary floating point never leaks into financial values."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value))
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value: Any) -> str | None:
    m = money(value)
    return None if m is None else f"{m:.2f}"


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"Not JSON serialisable: {type(o)!r}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def to_jsonable(obj: Any) -> Any:
    return json.loads(canonical_json(obj))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def hmac_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
