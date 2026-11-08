"""Agent runtime: narrow responsibilities, typed outputs, restricted tools."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from app.core.util import to_jsonable, utcnow
from app.schemas.common import AgentEnvelope, EvidenceRef

log = logging.getLogger("checkmate.agents")


class ToolPermissionError(PermissionError):
    pass


class ToolRegistry:
    def __init__(self, tools: dict[str, Callable[..., Any]]):
        self._tools = dict(tools)

    def get(self, name: str) -> Callable[..., Any]:
        if name not in self._tools:
            raise KeyError(f"Unknown tool {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)


class ScopedTools:
    """The only handle an agent has on the outside world. Calls outside the
    agent's allowlist raise ToolPermissionError."""

    def __init__(self, registry: ToolRegistry, allowed: frozenset[str], agent_id: str):
        self._registry, self._allowed, self._agent_id = registry, allowed, agent_id
        self.calls: list[str] = []

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in self._allowed:
            raise ToolPermissionError(f"{self._agent_id} is not permitted to call {name}")
        self.calls.append(name)
        return self._registry.get(name)(*args, **kwargs)

    def can(self, name: str) -> bool:
        return name in self._allowed


@dataclass
class AgentOutput:
    value: Any
    evidence: list[EvidenceRef] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    confidence: float | None = None


@dataclass
class AgentRunResult:
    envelope: AgentEnvelope
    value: Any
    started_at: Any
    finished_at: Any
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.envelope.status == "OK"


class BaseAgent:
    agent_id: ClassVar[str] = "base"
    allowed_tools: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, registry: ToolRegistry):
        self.tools = ScopedTools(registry, self.allowed_tools, self.agent_id)

    def run(self, **inputs: Any) -> AgentOutput:  # pragma: no cover - abstract
        raise NotImplementedError

    def execute(self, workflow_id: str, subject: str | None = None, **inputs: Any) -> AgentRunResult:
        started = utcnow()
        t0 = time.perf_counter()
        try:
            out = self.run(**inputs)
            value = out.value
            payload = to_jsonable(value.model_dump(mode="json") if isinstance(value, BaseModel) else
                                  [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value]
                                  if isinstance(value, list) else value)
            env = AgentEnvelope(workflow_id=workflow_id, agent_id=self.agent_id, timestamp=utcnow(), status="OK",
                                subject=subject, result=payload, evidence_references=out.evidence,
                                confidence=out.confidence, missing_information=out.missing)
        except ToolPermissionError:
            raise  # a permission violation is a programming/security error, never swallowed
        except Exception as e:  # failed agents never produce passing results
            log.exception("agent %s failed", self.agent_id)
            value = None
            env = AgentEnvelope(workflow_id=workflow_id, agent_id=self.agent_id, timestamp=utcnow(),
                                status="ERROR", subject=subject, error=f"{e.__class__.__name__}: {e}",
                                missing_information=["agent output unavailable"])
        return AgentRunResult(envelope=env, value=value, started_at=started, finished_at=utcnow(),
                              duration_ms=int((time.perf_counter() - t0) * 1000))
