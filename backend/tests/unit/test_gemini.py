"""Gemini provider tests against a fake HTTP transport (no network, no real keys)."""
import json

import httpx
import pytest

from app.agents.intent_agent import LLMIntent
from app.agents.terms_agent import LLMTerms
from app.integrations.llm.client import GeminiLLM, build_llm, to_gemini_schema

GOOD_TERMS = {"warranty_months": 12, "return_window_days": 30, "free_returns": True, "shipping_cost": 10.0,
              "delivery_days": 5, "subscription_required": False, "embedded_instructions_detected": []}


def ok(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
                                                      "content": {"parts": [{"text": json.dumps(payload)}]}}]})


def err(status: int, msg: str = "", code: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": msg, "status": code}})


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return r(request) if callable(r) else r


def client(rec, keys=("key-A", "key-B"), models=("gemini-x", "gemini-y")) -> GeminiLLM:
    return GeminiLLM(list(keys), list(models), transport=httpx.MockTransport(rec), rate_limit_cooldown=60)


def test_schema_conversion_handles_nullable_arrays_and_refs():
    s = to_gemini_schema(LLMIntent.model_json_schema())
    assert s["type"] == "OBJECT"
    assert s["properties"]["max_budget"] == {"type": "NUMBER", "nullable": True}
    spec = s["properties"]["specifications"]
    assert spec["type"] == "ARRAY" and spec["items"]["type"] == "OBJECT"
    assert set(spec["items"]["properties"]) == {"key", "value"}
    assert "max_budget" in s["required"]
    assert "$ref" not in json.dumps(s) and "title" not in json.dumps(s)


def test_success_sends_key_in_header_never_in_url():
    rec = Recorder(ok(GOOD_TERMS))
    out = client(rec).structured("sys", "policy text", LLMTerms)
    assert out.warranty_months == 12
    req = rec.requests[0]
    assert req.headers["x-goog-api-key"] == "key-A"
    assert "key-A" not in str(req.url) and "key=" not in str(req.url)
    body = json.loads(req.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"]["type"] == "OBJECT"
    assert "untrusted_content" in body["systemInstruction"]["parts"][0]["text"]
    assert str(req.url).endswith("/models/gemini-x:generateContent")


def test_rotates_to_next_key_when_rate_limited():
    rec = Recorder(err(429, "Resource exhausted", "RESOURCE_EXHAUSTED"), ok(GOOD_TERMS))
    g = client(rec)
    assert g.structured("s", "u", LLMTerms) is not None
    assert [r.headers["x-goog-api-key"] for r in rec.requests] == ["key-A", "key-B"]
    assert g.status()["keys_available"] == 1 and g.stats["rate_limited"] == 1


def test_invalid_key_is_disabled_and_skipped():
    rec = Recorder(err(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT"), ok(GOOD_TERMS))
    g = client(rec)
    assert g.structured("s", "u", LLMTerms) is not None
    rec.responses = [ok(GOOD_TERMS)]
    g.structured("s", "different prompt", LLMTerms)
    assert rec.requests[-1].headers["x-goog-api-key"] == "key-B"  # key-A stays disabled


def test_retired_model_falls_back_to_next_model():
    rec = Recorder(err(404, "models/gemini-x is not found", "NOT_FOUND"), ok(GOOD_TERMS))
    g = client(rec)
    assert g.structured("s", "u", LLMTerms) is not None
    assert str(rec.requests[1].url).endswith("/models/gemini-y:generateContent")
    assert g.model == "gemini-y"


def test_schema_rejection_retries_with_schema_in_prompt():
    rec = Recorder(err(400, "Invalid JSON payload: responseSchema", "INVALID_ARGUMENT"), ok(GOOD_TERMS))
    assert client(rec).structured("s", "u", LLMTerms) is not None
    second = json.loads(rec.requests[1].content)
    assert "responseSchema" not in second["generationConfig"]
    assert "JSON Schema" in second["contents"][0]["parts"][0]["text"]


def test_all_keys_exhausted_returns_none_for_deterministic_fallback():
    rec = Recorder(err(429, code="RESOURCE_EXHAUSTED"))
    g = client(rec)
    assert g.structured("s", "u", LLMTerms) is None
    n = len(rec.requests)
    assert g.structured("s", "u2", LLMTerms) is None
    assert len(rec.requests) == n  # cooling down: no further calls spent


@pytest.mark.parametrize("resp", [
    httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "not json"}]}}]}),
    ok({"warranty_months": "twelve"}),  # fails schema validation
    httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
    httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]}),
    err(500, code="INTERNAL"),
])
def test_bad_responses_return_none(resp):
    assert client(Recorder(resp)).structured("s", "u", LLMTerms) is None


def test_fenced_json_is_accepted():
    text = "```json\n" + json.dumps(GOOD_TERMS) + "\n```"
    resp = httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]})
    assert client(Recorder(resp)).structured("s", "u", LLMTerms).delivery_days == 5


def test_identical_prompts_are_cached():
    rec = Recorder(ok(GOOD_TERMS))
    g = client(rec)
    g.structured("s", "u", LLMTerms)
    g.structured("s", "u", LLMTerms)
    assert len(rec.requests) == 1 and g.stats["cache_hits"] == 1


def test_factory_prefers_gemini_when_keys_present():
    from dataclasses import replace

    from app.core.config import get_settings

    s = get_settings()
    assert build_llm(replace(s, gemini_api_keys=("k",))).provider == "gemini"
    assert build_llm(replace(s, gemini_api_keys=(), openai_api_key=None)).provider == "none"
    assert build_llm(replace(s, llm_provider="none", gemini_api_keys=("k",))).provider == "none"


def test_retired_model_hint_is_followed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "old-model" in request.url.path:
            return err(404, "This model models/old-model is no longer available to new users. Please update your "
                            "code to use models/new-model for the latest features.", "NOT_FOUND")
        return ok(GOOD_TERMS)

    g = GeminiLLM(["k"], ["old-model"], transport=httpx.MockTransport(handler))
    assert g.structured("s", "u", LLMTerms) is not None
    assert seen[-1].endswith("/models/new-model:generateContent") and g.model == "new-model"


def test_slow_model_is_skipped_for_the_next():
    def handler(request: httpx.Request) -> httpx.Response:
        if "slow-model" in request.url.path:
            raise httpx.ReadTimeout("timed out", request=request)
        return ok(GOOD_TERMS)

    g = GeminiLLM(["k"], ["slow-model", "fast-model"], transport=httpx.MockTransport(handler))
    assert g.structured("s", "u", LLMTerms) is not None and g.model == "fast-model"
