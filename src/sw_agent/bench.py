"""Score connected models on real SolidWorks tasks, without SolidWorks.

Each model gets the same short tasks. Its tool calls run against the fake SolidWorks
(sw_mcp.fakes), and the resulting part is checked: size, volume, number of holes. The
score (0-100) is saved in the config, so the router tries the best models first.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Callable

from sw_mcp.core import connection
from sw_mcp.fakes.fake_modeler import FakePart, make_modeling_app

from .assistant import Assistant, Toolbox
from .config import UserConfig
from .router import Candidate, NoModelAvailable, Router

REQUESTS_PER_TASK = 3  # typical: plan, (fix), final answer


def _size(part: FakePart) -> list[float] | None:
    if not part.solids:
        return None
    lo, hi = part.union_box()
    return [round(hi[i] - lo[i], 1) for i in range(3)]


def _cuts(part: FakePart) -> int:
    return sum(1 for f in part.features if getattr(f, "volume_m3", 0) < 0)


def _near(a: float, b: float, rel: float = 0.01) -> bool:
    return abs(a - b) <= rel * abs(b)


@dataclass
class Task:
    id: str
    prompt: str
    check: Callable[[FakePart], tuple[bool, str]]


def _check_plate(part: FakePart) -> tuple[bool, str]:
    size, vol = _size(part), part.volume_m3 * 1e9
    expected = 60 * 40 * 10 - 4 * math.pi * 9 * 10
    ok = size == [60.0, 10.0, 40.0] and _near(vol, expected) and _cuts(part) == 4
    return ok, f"size {size}, volume {vol:.0f} (want {expected:.0f}), {_cuts(part)} holes (want 4)"


def _check_flange(part: FakePart) -> tuple[bool, str]:
    size, vol = _size(part), part.volume_m3 * 1e9
    expected = math.pi * 45**2 * 10 - math.pi * 15**2 * 10 - 6 * math.pi * 16 * 10
    ok = size is not None and _near(size[0], 90) and _near(size[2], 90) and _near(size[1], 10) \
        and _near(vol, expected, 0.02) and _cuts(part) == 7
    return ok, f"size {size}, volume {vol:.0f} (want {expected:.0f}), {_cuts(part)} cuts (want 7)"


def _check_bracket(part: FakePart) -> tuple[bool, str]:
    size = _size(part)
    ok = size is not None and sorted([size[0], size[2]]) == [40.0, 80.0] and size[1] in (50.0, 56.0) \
        and _cuts(part) == 2
    return ok, f"size {size} (want 80 x 50 or 56 x 40), {_cuts(part)} holes (want 2)"


TASKS = [
    Task("plate", "Make a 60 x 40 x 10 mm plate with a 6 mm through hole in each corner, "
                  "8 mm in from both edges.", _check_plate),
    Task("flange", "Make a round flange: 90 mm diameter, 10 mm thick, a 30 mm center bore, and 6 holes "
                   "of 8 mm evenly spaced on a 70 mm bolt circle.", _check_flange),
    Task("bracket", "Make an L-bracket: a base plate 80 x 40 x 6 mm lying flat, and a 6 mm thick wall "
                    "standing up along one 80 mm edge, 50 mm tall in total. Put two 6 mm holes through "
                    "the base.", _check_bracket),
]


@dataclass
class TaskResult:
    task: str
    passed: bool
    detail: str
    requests: int
    seconds: float


@dataclass
class ModelScore:
    candidate: Candidate
    results: list[TaskResult] = field(default_factory=list)
    skipped: str = ""

    @property
    def score(self) -> float | None:
        if not self.results:
            return None
        return round(100.0 * sum(r.passed for r in self.results) / len(self.results), 1)


class _CountingRouter(Router):
    """A router restricted to one model that counts the requests it makes."""

    def __init__(self, base: Router, cand: Candidate) -> None:
        super().__init__(base.config, providers=list(base.providers.values()), key_for=base.key_for,
                         client_factory=base.client_factory, clock=base.clock)
        self._only = cand
        self.requests = 0

    def candidates(self, role: str = "main") -> list[Candidate]:
        return [self._only] if self._only.provider.id not in self._disabled else []

    def chat(self, messages, tools=None, role="main", max_tokens=None, tools_for=None):
        self.requests += 1
        return super().chat(messages, tools, role, max_tokens)  # scoring never offers the OpenAI extras


async def score_model(base: Router, cand: Candidate, tasks: list[Task] = TASKS, max_steps: int = 6,
                      on_progress: Callable[[str], None] = lambda s: None) -> ModelScore:
    result = ModelScore(cand)
    for task in tasks:
        app = make_modeling_app()
        connection.use_app_factory(lambda app=app: app)
        router = _CountingRouter(base, cand)
        started = time.monotonic()
        try:
            async with Toolbox() as toolbox:
                assistant = Assistant(router, toolbox, max_steps=max_steps, record=False)
                reply = await assistant.send(task.prompt)
        finally:
            connection.use_app_factory(None)
        if reply.startswith(("All connected models", "Every connected model", "No working AI model")):
            result.skipped = "rate-limited or unavailable"
            on_progress(f"  {task.id}: skipped ({result.skipped})")
            break
        part = app.created[-1] if app.created else None
        passed, detail = task.check(part) if part is not None else (False, "no part was built")
        result.results.append(TaskResult(task.id, passed, detail, router.requests,
                                         round(time.monotonic() - started, 1)))
        on_progress(f"  {task.id}: {'PASS' if passed else 'fail'} - {detail} ({router.requests} requests)")
    return result


async def run_bench(config: UserConfig, router: Router, max_models: int = 3,
                    on_progress: Callable[[str], None] = print) -> list[ModelScore]:
    scores = []
    for cand in router.candidates()[:max_models]:
        on_progress(f"{cand.label}")
        try:
            scored = await score_model(router, cand, on_progress=on_progress)
        except NoModelAvailable as exc:
            scored = ModelScore(cand, skipped=str(exc))
        scores.append(scored)
        if scored.score is not None:
            entry = config.providers.get(cand.provider.id)
            for model in entry.models if entry else []:
                if model.id == cand.model:
                    model.score = scored.score
    config.save()
    return scores


def estimate_requests(models: int) -> int:
    return models * len(TASKS) * REQUESTS_PER_TASK
