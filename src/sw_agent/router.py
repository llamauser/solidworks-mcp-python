"""Pick a working model for every request, and switch automatically when one fails.

Order: benchmark score (best first), then answer speed. Within one user request the router
sticks to the model that answered last (switching back and forth costs rate limit on both).
A rate-limited provider is paused for the time it asks (or a growing back-off); a broken model
is paused for a while; a refused key disables that provider for the session. Free tiers cap the
tokens per minute (Groq: 7000-8000), so the cap a provider names is remembered and requests
larger than it go to another model. Paid providers come after the free ones.

The user can pick any provider and model by hand (even one the setup never tested, e.g. a new
OpenAI model): it is tried first, or used alone with "only" (no switching to other models).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import keys
from .config import UserConfig
from .llm import ChatClient, ChatResult, LLMError
from .registry import Provider, load_providers

log = logging.getLogger("sw_agent.router")

MODEL_PAUSE_S = {"unavailable": 120.0, "network": 60.0, "bad_request": 600.0, "bad_output": 15.0}
CHARS_PER_TOKEN = 3.2  # conservative: measured about 3.4 on our requests
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
        self.sticky: Candidate | None = None  # the model that answered last in this request
        self._token_cap: dict[tuple[str, str], int] = {}
        self.only = False
        manual = getattr(config, "manual", None) or {}
        if manual.get("provider") in self.providers and manual.get("model"):
            self.pinned = (manual["provider"], manual["model"])
            self.only = bool(manual.get("only"))

    # ------------------------------------------------------------ ordering
    def candidates(self, role: str = "main") -> list[Candidate]:
        out = []
        for pid, model in self.config.working_models():
            provider = self.providers.get(pid)
            if provider is None or pid in self._disabled:
                continue
            out.append(Candidate(provider, model.id, model.score, model.latency_ms))
        if self.pinned and self.pinned[0] in self.providers and self.pinned[0] not in self._disabled \
                and all((c.provider.id, c.model) != self.pinned for c in out):
            out.append(Candidate(self.providers[self.pinned[0]], self.pinned[1]))  # picked by hand
        if role == "fast":
            out.sort(key=lambda c: c.latency_ms or 10**9)
        else:  # best first: benchmark score, then speed
            out.sort(key=lambda c: (-(c.score if c.score is not None else 50.0), c.latency_ms or 10**9))
        out.sort(key=lambda c: c.provider.paid)  # free before paid (stable: keeps the order above)
        if self.sticky is not None and not self.sticky.provider.paid:
            out.sort(key=lambda c: c != self.sticky)
        if self.pinned:
            out.sort(key=lambda c: (c.provider.id, c.model) != self.pinned)
            if self.only:
                out = [c for c in out if (c.provider.id, c.model) == self.pinned]
        return out

    def new_turn(self) -> None:
        """A new user request: start again from the best model."""
        self.sticky = None

    def pin(self, provider_id: str, model: str, only: bool = False, remember: bool = False) -> None:
        """Try this model first (only=True: use nothing else). remember=True keeps the choice
        in the user's settings for the next start."""
        if provider_id not in self.providers:
            raise KeyError(provider_id)
        self.pinned = (provider_id, model.strip())
        self.only = only
        if remember:
            self.config.manual = {"provider": provider_id, "model": model.strip(), "only": only}
            self._save_config()

    def unpin(self, remember: bool = False) -> None:
        self.pinned = None
        self.only = False
        if remember:
            self.config.manual = {}
            self._save_config()

    def _save_config(self) -> None:
        try:
            self.config.save()
        except OSError:
            log.warning("could not save the model choice", exc_info=True)

    def _paused_for(self, c: Candidate) -> float:
        now = self.clock()
        until = max(self._provider_pause.get(c.provider.id, 0.0), self._model_pause.get((c.provider.id, c.model), 0.0))
        return max(0.0, until - now)

    # ------------------------------------------------------------ calling
    def client_for(self, provider: Provider) -> ChatClient:
        return self._client(provider)

    def has_key(self, provider_id: str) -> bool:
        provider = self.providers.get(provider_id)
        return provider is not None and (provider.local or bool(self.key_for(provider_id)))

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
            return
        if err.kind == "no_credit":
            self._disabled.add(pid)
            self.on_event(f"{c.provider.name}: your account has no credit left. Add credit on the provider's "
                          "billing page (for OpenAI: platform.openai.com > Settings > Billing), then start again. "
                          "Using the other models meanwhile.")
            return
        if err.token_limit:
            self._token_cap[(pid, c.model)] = err.token_limit
        if err.kind == "too_large":
            self.on_event(f"{c.label} only takes {err.token_limit or 'smaller'} tokens a minute; "
                          "using another model for this request.")
            return
        if err.kind == "rate_limit":
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
             max_tokens: int | None = None,
             tools_for: Callable[[Candidate, list[dict]], list[dict]] | None = None) -> tuple[ChatResult, Candidate]:
        """tools_for(candidate, tools) may add tools only some models get (OpenAI's extras)."""
        candidates = self.candidates(role)
        if not candidates:
            raise NoModelAvailable("No working AI model is set up. Run 'sw-agent setup' first.")
        size = int((len(json.dumps(messages)) + len(json.dumps(tools or []))) / CHARS_PER_TOKEN)
        soonest = None
        too_big = []
        last_error = ""
        for c in candidates:
            cap = self._token_cap.get((c.provider.id, c.model))
            if cap and size > cap * 0.95:
                too_big.append(c.label)
                continue
            wait = self._paused_for(c)
            if wait > 0:
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            offered = tools_for(c, list(tools or [])) if tools_for is not None else tools
            try:
                result = self._client(c.provider).chat(c.model, messages, tools=offered,
                                                       tool_choice="auto" if offered else None,
                                                       max_tokens=max_tokens)
            except LLMError as err:
                log.warning("%s failed: %s (%s)", c.label, err.kind, str(err)[:300])
                self._penalize(c, err)
                last_error = f"{c.label}: {str(err)[:300]}"
                continue
            usage = result.usage or {}
            log.info("%s answered in %d ms (tokens in %s, out %s, tool calls %d)", c.label, result.latency_ms,
                     usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"), len(result.tool_calls))
            self._provider_backoff.pop(c.provider.id, None)
            if self.last_used and self.last_used != c:
                self.on_event(f"Now using {c.label}.")
            self.last_used = c
            self.sticky = c
            return result, c
        log.warning("no model could answer (soonest retry %s s, too big for %s)", soonest, too_big)
        if soonest is None and too_big:
            raise NoModelAvailable("This conversation has grown too long for the connected free models. "
                                   "Start a new conversation (the work in SolidWorks is kept), or connect a "
                                   "provider with a larger allowance (Gemini, Mistral, NVIDIA) with 'sw-agent setup'.")
        if soonest is not None:
            raise NoModelAvailable(f"All connected models are busy or rate-limited. Try again in about "
                                   f"{soonest:.0f} s, or connect more providers with 'sw-agent setup'.")
        detail = f" Last error from {last_error}" if last_error else ""
        if self.pinned and self.only:
            raise NoModelAvailable(f"The model you chose did not answer.{detail} Choose another model under "
                                   "Models, or untick 'Only this model'.")
        raise NoModelAvailable("Every connected model failed just now. Try again in a minute, or run "
                               f"'sw-agent setup' to test your providers.{detail}")

    # ------------------------------------------------------------ reporting
    def status(self) -> list[tuple[str, str, str]]:
        rows = []
        for c in self.candidates():
            wait = self._paused_for(c)
            chosen = self.pinned == (c.provider.id, c.model)
            state = f"paused {wait:.0f} s" if wait else ("only this one" if chosen and self.only
                                                          else "chosen" if chosen else "ready")
            rows.append((c.provider.name, c.model, state))
        return rows
