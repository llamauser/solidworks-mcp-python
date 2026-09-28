"""The provider list (providers.toml), loaded into simple objects."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources

PRIVACY_LABELS = {
    "no-training": "does not train on your data",
    "may-train": "MAY TRAIN on your prompts",
    "depends": "depends on the model",
    "local": "private, runs on this PC",
}


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    key_url: str
    offer: str
    requires: str
    privacy: str
    privacy_note: str
    model_prefs: tuple[str, ...] = field(default_factory=tuple)
    model_filter: str = ""
    local: bool = False
    paid: bool = False                               # costs money per use: tried after the free ones
    model_exclude: tuple[str, ...] = field(default_factory=tuple)  # ids containing these are not chat models
    max_tokens_param: str = "max_tokens"             # OpenAI's newer models want max_completion_tokens
    reasoning_models: tuple[str, ...] = field(default_factory=tuple)  # id prefixes: no temperature, more tokens

    @property
    def privacy_label(self) -> str:
        return PRIVACY_LABELS.get(self.privacy, self.privacy)


def load_providers(text: str | None = None) -> list[Provider]:
    if text is None:
        text = resources.files("sw_agent").joinpath("providers.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    out = []
    for raw in data.get("provider", []):
        raw = dict(raw)
        for key in ("model_prefs", "model_exclude", "reasoning_models"):
            raw[key] = tuple(raw.get(key, ()))
        raw["base_url"] = raw["base_url"].rstrip("/")
        out.append(Provider(**raw))
    ids = [p.id for p in out]
    if len(ids) != len(set(ids)):
        raise ValueError("providers.toml has duplicate ids")
    return out


def get_provider(provider_id: str) -> Provider:
    for p in load_providers():
        if p.id == provider_id:
            return p
    raise KeyError(provider_id)
