from __future__ import annotations

from app.core.util import hash_obj
from app.schemas.domain import ContractTerms

HASH_ALGORITHM = "sha256(canonical-json(contract terms))"


def contract_hash(terms: ContractTerms) -> str:
    return hash_obj({"schema": "checkmate.purchase_contract/v1", "terms": terms.model_dump(mode="json")})


def material_changes(old: ContractTerms, new: ContractTerms) -> list[str]:
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    return sorted(k for k in a if a[k] != b.get(k))
