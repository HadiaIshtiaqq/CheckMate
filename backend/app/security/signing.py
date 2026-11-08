"""Server signing key for approvals, audit-chain heads and passports.

The key lives OUTSIDE the database (SIGNING_KEY env var, or a key file created on
first run with owner-only permissions). Someone who can edit the database but not
read the key cannot forge an approval or passport without the signature failing.
Someone with access to both still can; anchoring to an external timestamping
service would be the next step."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path

from app.core.config import get_settings
from app.core.util import canonical_json

_lock = threading.Lock()
_key: bytes | None = None


def _load() -> bytes:
    global _key
    with _lock:
        if _key is not None:
            return _key
        s = get_settings()
        if s.signing_key:
            _key = s.signing_key.encode("utf-8")
            return _key
        path = Path(s.signing_key_path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        _key = path.read_text().strip().encode("utf-8")
        return _key


def key_id() -> str:
    return hashlib.sha256(_load()).hexdigest()[:12]


def sign(obj) -> str:
    return hmac.new(_load(), canonical_json(obj).encode("utf-8"), hashlib.sha256).hexdigest()


def verify(obj, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign(obj), signature)
