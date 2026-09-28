"""A thin client for OpenAI-compatible chat APIs (every provider in providers.toml speaks it).

Errors are sorted into kinds the router can act on: a bad key, a rate limit (try another
provider, come back later), an unavailable model, or a request the model cannot handle.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .registry import Provider

APP_NAME = "SolidWorks Assistant"
APP_URL = "https://github.com/llamauser/solidworks-mcp-python"


DEFAULT_MAX_TOKENS = 4096
REASONING_MIN_TOKENS = 8000
# Gemini 3 wants each earlier tool call to carry its "thought signature"; calls made by another
# model have none, and Google documents this value for that case.
GEMINI_DUMMY_SIGNATURE = "skip_thought_signature_validator"
_LIMIT = re.compile(r"\bLimit:?\s*(\d{3,})", re.I)
_TRY_AGAIN = re.compile(r"try again in\s*(?:(\d+)m)?\s*([\d.]+)s", re.I)


class LLMError(Exception):
    """kind: auth | no_credit | rate_limit | too_large | bad_output | unavailable | bad_request | network

    token_limit: the per-minute token cap the provider named (for rate_limit / too_large)."""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None,
                 token_limit: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.token_limit = token_limit


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict]
    raw_message: dict
    usage: dict
    latency_ms: int


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return str(body[0].get("error", {}).get("message") or body[0])[:300]
    return str(err or body)[:300]


def _classify(resp: httpx.Response) -> LLMError:
    status = resp.status_code
    text = _error_text(resp)
    retry = resp.headers.get("retry-after")
    retry_after = float(retry) if retry and retry.replace(".", "", 1).isdigit() else None
    if retry_after is None and (m := _TRY_AGAIN.search(text)):
        retry_after = int(m.group(1) or 0) * 60 + float(m.group(2))
    limit = int(m.group(1)) if (m := _LIMIT.search(text)) and "token" in text.lower() else None
    low = text.lower()
    if status in (401, 403):
        return LLMError("auth", f"The key was refused ({status}): {text}", status)
    if any(w in low for w in ("no credits remaining", "insufficient_quota", "exceeded your current quota",
                              "credit balance is too low")):
        return LLMError("no_credit", f"No credit left on this account: {text}", status)
    if status == 413 or "request too large" in low or "reduce your message size" in low:
        return LLMError("too_large", f"Request too large: {text}", status, retry_after, limit)
    if status == 429 or "rate" in low and "limit" in low:
        return LLMError("rate_limit", f"Rate limited: {text}", status, retry_after, limit)
    if "failed to call a function" in low or "failed_generation" in low or "tool_use_failed" in low:
        return LLMError("bad_output", f"The model wrote a broken tool call: {text}", status)
    if status == 402:
        return LLMError("rate_limit", f"Out of free credit or quota: {text}", status, retry_after)
    if status in (404, 410):
        return LLMError("unavailable", f"Model not available: {text}", status)
    if status >= 500:
        return LLMError("unavailable", f"Provider error {status}: {text}", status, retry_after)
    return LLMError("bad_request", f"Request refused ({status}): {text}", status)


def to_responses_input(messages: list[dict]) -> list[dict]:
    """Chat-completions messages -> Responses API input items (system messages become instructions)."""
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""),
                          "output": m.get("content") or ""})
        elif role == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                args = fn.get("arguments")
                items.append({"type": "function_call", "call_id": c.get("id", ""), "name": fn.get("name", ""),
                              "arguments": args if isinstance(args, str) else json.dumps(args or {})})
        else:
            items.append({"role": "user", "content": m.get("content") or ""})
    return items


def from_responses_output(body: dict) -> tuple[str, list[dict]]:
    """Responses API output -> (text, chat-completions style tool_calls)."""
    texts, calls = [], []
    for item in body.get("output") or []:
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
        elif kind == "function_call":
            calls.append({"id": item.get("call_id") or item.get("id", ""), "type": "function",
                          "function": {"name": item.get("name", ""), "arguments": item.get("arguments") or "{}"}})
    return "\n".join(t for t in texts if t).strip(), calls


def with_thought_signatures(messages: list[dict]) -> list[dict]:
    """Give every earlier tool call a thought signature (Gemini refuses the request otherwise)."""
    out = []
    for msg in messages:
        calls = msg.get("tool_calls")
        if msg.get("role") == "assistant" and calls:
            fixed = []
            for c in calls:
                google = ((c.get("extra_content") or {}).get("google") or {})
                if not google.get("thought_signature"):
                    c = {**c, "extra_content": {**(c.get("extra_content") or {}),
                                                "google": {**google, "thought_signature": GEMINI_DUMMY_SIGNATURE}}}
                fixed.append(c)
            msg = {**msg, "tool_calls": fixed}
        out.append(msg)
    return out


class ChatClient:
    def __init__(self, provider: Provider, key: str | None, http: httpx.Client | None = None,
                 timeout: float = 90.0) -> None:
        self.provider = provider
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if provider.id == "openrouter":
            headers["HTTP-Referer"] = APP_URL
            headers["X-Title"] = APP_NAME
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.provider.base_url}{path}"
        try:
            resp = self._http.request(method, url, headers=self._headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise LLMError("network", f"{self.provider.name} did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise LLMError("network", f"Could not reach {self.provider.name}: {exc}") from exc
        if resp.status_code >= 400:
            raise _classify(resp)
        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMError("unavailable", f"{self.provider.name} sent an unreadable answer.") from exc
        if isinstance(body, dict) and body.get("error"):  # some providers answer 200 with an error
            err = body["error"]
            code = err.get("code") if isinstance(err, dict) else None
            status = int(code) if str(code).isdigit() else 500
            raise _classify(httpx.Response(status, json=body))
        return body

    # ---------------------------------------------------------------- OpenAI Responses API
    def _chat_responses(self, model: str, messages: list[dict], tools: list[dict] | None,
                        max_tokens: int | None, temperature: float) -> ChatResult:
        """The same conversation through OpenAI's /responses. Newer models (GPT-5.x) only allow
        function tools there: /chat/completions refuses tools together with reasoning."""
        payload: dict[str, Any] = {"model": model, "input": to_responses_input(messages), "store": False}
        instructions = "\n\n".join(m["content"] for m in messages if m.get("role") == "system" and m.get("content"))
        if instructions:
            payload["instructions"] = instructions
        limit = max_tokens or DEFAULT_MAX_TOKENS
        if self.is_reasoning(model):
            limit = max(limit, REASONING_MIN_TOKENS)
        else:
            payload["temperature"] = temperature
        payload["max_output_tokens"] = limit
        if tools:
            payload["tools"] = [{"type": "function", "name": t["function"]["name"],
                                 "description": t["function"].get("description", ""),
                                 "parameters": t["function"].get("parameters") or {"type": "object", "properties": {}},
                                 "strict": False} for t in tools]
            payload["tool_choice"] = "auto"
        started = time.monotonic()
        body = self._request("POST", "/responses", json=payload)
        latency = int((time.monotonic() - started) * 1000)
        content, calls = from_responses_output(body)
        if not content and not calls:
            reason = (body.get("incomplete_details") or {}).get("reason") or body.get("status") or "no output"
            raise LLMError("bad_output", f"{self.provider.name} returned no answer ({reason}).")
        usage = body.get("usage") or {}
        return ChatResult(content=content, tool_calls=calls, raw_message=body,
                          usage={"prompt_tokens": usage.get("input_tokens"),
                                 "completion_tokens": usage.get("output_tokens")},
                          latency_ms=latency)

    def is_reasoning(self, model: str) -> bool:
        low = model.lower()
        return any(low.startswith(p.lower()) for p in self.provider.reasoning_models)

    def list_models(self) -> list[dict]:
        body = self._request("GET", "/models")
        data = body.get("data", body) if isinstance(body, dict) else body
        return [m for m in data if isinstance(m, dict) and m.get("id")]

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | None = None, max_tokens: int | None = None,
             temperature: float = 0.1) -> ChatResult:
        if self.provider.api == "responses":
            return self._chat_responses(model, messages, tools, max_tokens, temperature)
        if self.provider.id == "gemini":
            messages = with_thought_signatures(messages)
        payload: dict[str, Any] = {"model": model, "messages": messages}
        limit = max_tokens or DEFAULT_MAX_TOKENS
        if self.is_reasoning(model):
            # Reasoning models accept only the default temperature, and their thinking counts
            # against the output limit, so a small limit would leave no room for the answer.
            limit = max(limit, REASONING_MIN_TOKENS)
        else:
            payload["temperature"] = temperature
        payload[self.provider.max_tokens_param or "max_tokens"] = limit
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        started = time.monotonic()
        body = self._request("POST", "/chat/completions", json=payload)
        latency = int((time.monotonic() - started) * 1000)
        choices = body.get("choices") or []
        if not choices:
            raise LLMError("unavailable", f"{self.provider.name} returned no answer.")
        msg = choices[0].get("message") or {}
        return ChatResult(
            content=msg.get("content") or "",
            tool_calls=msg.get("tool_calls") or [],
            raw_message=msg,
            usage=body.get("usage") or {},
            latency_ms=latency,
        )
