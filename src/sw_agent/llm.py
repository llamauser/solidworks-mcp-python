"""A thin client for OpenAI-compatible chat APIs (every provider in providers.toml speaks it).

Errors are sorted into kinds the router can act on: a bad key, a rate limit (try another
provider, come back later), an unavailable model, or a request the model cannot handle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .registry import Provider

APP_NAME = "SolidWorks Assistant"
APP_URL = "https://github.com/llamauser/solidworks-mcp-python"


class LLMError(Exception):
    """kind: auth | rate_limit | unavailable | bad_request | network"""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


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
    if status in (401, 403):
        return LLMError("auth", f"The key was refused ({status}): {text}", status)
    if status == 429 or "rate" in text.lower() and "limit" in text.lower():
        return LLMError("rate_limit", f"Rate limited: {text}", status, retry_after)
    if status == 402:
        return LLMError("rate_limit", f"Out of free credit or quota: {text}", status, retry_after)
    if status in (404, 410):
        return LLMError("unavailable", f"Model not available: {text}", status)
    if status >= 500:
        return LLMError("unavailable", f"Provider error {status}: {text}", status, retry_after)
    return LLMError("bad_request", f"Request refused ({status}): {text}", status)


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

    def list_models(self) -> list[dict]:
        body = self._request("GET", "/models")
        data = body.get("data", body) if isinstance(body, dict) else body
        return [m for m in data if isinstance(m, dict) and m.get("id")]

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | None = None, max_tokens: int | None = None,
             temperature: float = 0.1) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        if max_tokens:
            payload["max_tokens"] = max_tokens
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
