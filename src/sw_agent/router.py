"""Pick a working model for every request, and switch automatically when one fails.

Order: benchmark score (best first), then answer speed. A rate-limited provider is paused
for the time it asks (or a growing back-off); a broken model is paused for a while; a
refused key disables that provider for the session. The user can pin one model.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import keys
from .config import UserConfig
from .llm import ChatClient, ChatResult, LLMError
from .registry import Provider, load_providers

log = logging.getLogger("sw_agent.router")

MODEL_PAUSE_S = {"unavailable": 120.0, "network": 60.0, "bad_request": 600.0}
RATE_LIMIT_START_S = 30.0
RATE_LIMIT_MAX_S = 900.0


@dataclass(frozen=True)
class Candidate:
    provider: Provider
    model: str
    score: float | None = None
    latency_ms: int = 0

    @property
    def label(self) -> str:
        return f"{self.provider.name} / {self.model}"


class NoModelAvailable(Exception):
    pass


class Router:
    def __init__(
        self,
        config: UserConfig,
        providers: list[Provider] | None = None,
        key_for: Callable[[str], str | None] = keys.get_key,
        client_factory: Callable[[Provider, str | None], ChatClient] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.providers = {p.id: p for p in (providers if providers is not None else load_providers())}
        self.key_for = key_for
        self.client_factory = client_factory or (lambda p, k: ChatClient(p, k))
        self.clock = clock
        self.on_event = on_event or (lambda msg: None)
        self._clients: dict[str, ChatClient] = {}
        self._provider_pause: dict[str, float] = {}
        self._provider_backoff: dict[str, float] = {}
        self._model_pause: dict[tuple[str, str], float] = {}
        self._disabled: set[str] = set()
        self.pinned: tuple[str, str] | None = None
        self.last_used: Candidate | None = None

    # ------------------------------------------------------------ ordering
    def candidates(self, role: str = "main") -> list[Candidate]:
        out = []
        for pid, model in self.config.working_models():
            provider = self.providers.get(pid)
            if provider is None or pid in self._disabled:
                continue
            out.append(Candidate(provider, model.id, model.score, model.latency_ms))
        if role == "fast":
            out.sort(key=lambda c: c.latency_ms or 10**9)
        else:  # best first: benchmark score, then speed
            out.sort(key=lambda c: (-(c.score if c.score is not None else 50.0), c.latency_ms or 10**9))
        if self.pinned:
            out.sort(key=lambda c: (c.provider.id, c.model) != self.pinned)
        return out

    def pin(self, provider_id: str, model: str) -> None:
        self.pinned = (provider_id, model)

    def unpin(self) -> None:
        self.pinned = None

    def _paused_for(self, c: Candidate) -> float:
        now = self.clock()
        until = max(self._provider_pause.get(c.provider.id, 0.0), self._model_pause.get((c.provider.id, c.model), 0.0))
        return max(0.0, until - now)

    # ------------------------------------------------------------ calling
    def _client(self, provider: Provider) -> ChatClient:
        if provider.id not in self._clients:
            key = None if provider.local else self.key_for(provider.id)
            self._clients[provider.id] = self.client_factory(provider, key)
        return self._clients[provider.id]

    def _penalize(self, c: Candidate, err: LLMError) -> None:
        now = self.clock()
        pid = c.provider.id
        if err.kind == "auth":
            self._disabled.add(pid)
            self.on_event(f"{c.provider.name}: the key was refused, not using it this session "
                          "(run 'sw-agent setup' to fix it).")
        elif err.kind == "rate_limit":
            backoff = min(RATE_LIMIT_MAX_S, self._provider_backoff.get(pid, RATE_LIMIT_START_S / 2) * 2)
            self._provider_backoff[pid] = backoff
            wait = err.retry_after if err.retry_after else backoff
            self._provider_pause[pid] = now + wait
            self.on_event(f"{c.provider.name} is rate-limited; pausing it for {wait:.0f} s.")
        else:
            wait = MODEL_PAUSE_S.get(err.kind, 120.0)
            self._model_pause[(pid, c.model)] = now + wait
            self.on_event(f"{c.label} failed ({err.kind}); trying another model.")

    def chat(self, messages: list[dict], tools: list[dict] | None = None, role: str = "main",
             max_tokens: int | None = None) -> tuple[ChatResult, Candidate]:
        candidates = self.candidates(role)
        if not candidates:
            raise NoModelAvailable("No working AI model is set up. Run 'sw-agent setup' first.")
        soonest = None
        for c in candidates:
            wait = self._paused_for(c)
            if wait > 0:
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            try:
                result = self._client(c.provider).chat(c.model, messages, tools=tools,
                                                       tool_choice="auto" if tools else None,
                                                       max_tokens=max_tokens)
            except LLMError as err:
                log.warning("%s failed: %s (%s)", c.label, err.kind, str(err)[:300])
                self._penalize(c, err)
                continue
            usage = result.usage or {}
            log.info("%s answered in %d ms (tokens in %s, out %s, tool calls %d)", c.label, result.latency_ms,
                     usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"), len(result.tool_calls))
            self._provider_backoff.pop(c.provider.id, None)
            if self.last_used and self.last_used != c:
                self.on_event(f"Now using {c.label}.")
            self.last_used = c
            return result, c
        log.warning("no model could answer (soonest retry %s s)", soonest)
        if soonest is not None:
            raise NoModelAvailable(f"All connected models are busy or rate-limited. Try again in about "
                                   f"{soonest:.0f} s, or connect more providers with 'sw-agent setup'.")
        raise NoModelAvailable("Every connected model failed just now. Try again in a minute, or run "
                               "'sw-agent setup' to test your providers.")

    # ------------------------------------------------------------ reporting
    def status(self) -> list[tuple[str, str, str]]:
        rows = []
        for c in self.candidates():
            wait = self._paused_for(c)
            state = f"paused {wait:.0f} s" if wait else ("pinned" if self.pinned == (c.provider.id, c.model) else "ready")
            rows.append((c.provider.name, c.model, state))
        return rows
