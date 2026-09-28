"""API keys live in the Windows Credential Manager (via keyring), never in files or logs.

An environment variable SW_AGENT_KEY_<PROVIDER> (e.g. SW_AGENT_KEY_GROQ) overrides the stored
key, which is handy for testing on another machine.
"""

from __future__ import annotations

import os

import keyring
import keyring.errors

SERVICE = "sw_agent"


def _env_name(provider_id: str) -> str:
    return "SW_AGENT_KEY_" + provider_id.upper().replace("-", "_")


def get_key(provider_id: str) -> str | None:
    env = os.environ.get(_env_name(provider_id))
    if env:
        return env.strip()
    try:
        return keyring.get_password(SERVICE, provider_id)
    except keyring.errors.KeyringError:
        return None


def set_key(provider_id: str, key: str) -> None:
    keyring.set_password(SERVICE, provider_id, key.strip())


def delete_key(provider_id: str) -> None:
    try:
        keyring.delete_password(SERVICE, provider_id)
    except keyring.errors.PasswordDeleteError:
        pass


def mask(key: str | None) -> str:
    if not key:
        return "(none)"
    return f"...{key[-4:]}" if len(key) > 8 else "****"
