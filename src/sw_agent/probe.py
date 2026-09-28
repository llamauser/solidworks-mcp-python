"""Check a key and find which models can really call tools (one tiny request per model)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from .llm import ChatClient, LLMError
from .registry import Provider

# Model ids that are never chat models with tool calling.
NOT_CHAT = ("embed", "whisper", "tts", "guard", "rerank", "moderation", "image", "vision-only", "audio",
            "transcribe", "speech", "ocr", "bge", "clip", "flux", "stable-diffusion", "sdxl")

PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "add_numbers",
        "description": "Add two numbers and return the sum.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
}
PROBE_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant that uses tools."},
    {"role": "user", "content": "Use the add_numbers tool to add 2 and 3."},
]


@dataclass
class ModelCheck:
    model: str
    tools_ok: bool
    latency_ms: int = 0
    detail: str = ""


@dataclass
class Discovery:
    provider: Provider
    key_ok: bool
    error: str = ""
    error_kind: str = ""
    model_count: int = 0
    checks: list[ModelCheck] = field(default_factory=list)

    @property
    def working(self) -> list[ModelCheck]:
        return [c for c in self.checks if c.tools_ok]


def rank_models(provider: Provider, models: list[dict]) -> list[str]:
    """Candidate model ids, best first, following the provider's preferences."""
    ids = []
    for m in models:
        mid = str(m["id"])
        low = mid.lower()
        if provider.model_filter and provider.model_filter not in mid:
            continue
        if any(bad in low for bad in NOT_CHAT) or any(bad in low for bad in provider.model_exclude):
            continue
        params = m.get("supported_parameters")
        if isinstance(params, list) and "tools" not in params:
            continue  # OpenRouter tells us up front
        ids.append(mid)

    def score(mid: str) -> tuple[int, int, str]:
        low = mid.lower()
        for i, pref in enumerate(provider.model_prefs):
            if pref.lower() in low:
                return (0, i, mid)
        return (1, 0, mid)

    return sorted(dict.fromkeys(ids), key=score)


def check_tools(client: ChatClient, model: str, sleep=time.sleep) -> ModelCheck:
    for attempt in (1, 2):
        try:
            res = client.chat(model, PROBE_MESSAGES, tools=[PROBE_TOOL], tool_choice="auto", max_tokens=300,
                              temperature=0)
            break
        except LLMError as exc:
            # Some free tiers allow ~1 request a second (Mistral): wait briefly and try once more.
            if attempt == 1 and exc.kind == "rate_limit" and (exc.retry_after or 2) <= 10:
                sleep(exc.retry_after or 2)
                continue
            return ModelCheck(model, False, detail=f"{exc.kind}: {exc}"[:160])
    for call in res.tool_calls:
        fn = call.get("function") or {}
        if fn.get("name") != "add_numbers":
            continue
        try:
            args = fn.get("arguments")
            args = json.loads(args) if isinstance(args, str) else (args or {})
            if float(args.get("a")) == 2 and float(args.get("b")) == 3:
                return ModelCheck(model, True, res.latency_ms, "called the tool correctly")
        except (TypeError, ValueError):
            pass
        return ModelCheck(model, False, res.latency_ms, "called the tool with wrong arguments")
    return ModelCheck(model, False, res.latency_ms, "answered in text instead of calling the tool")


def discover(provider: Provider, key: str | None, http: httpx.Client | None = None,
             max_checks: int = 3) -> Discovery:
    """Validate the key by listing models, then tool-check the best few candidates."""
    client = ChatClient(provider, key, http=http, timeout=45.0)
    try:
        models = client.list_models()
    except LLMError as exc:
        return Discovery(provider, False, str(exc), exc.kind)
    found = Discovery(provider, True, model_count=len(models))
    candidates = rank_models(provider, models)
    if not candidates:
        found.error = "No chat models with tool support were listed."
        return found
    for model in candidates:
        if len(found.checks) >= max_checks:
            break
        check = check_tools(client, model)
        found.checks.append(check)
        if check.detail.startswith("auth"):
            found.key_ok = False
            found.error = check.detail
            break
        if len(found.working) >= 2:  # two working models per provider is plenty
            break
    return found
