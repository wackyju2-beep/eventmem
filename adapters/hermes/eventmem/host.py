"""eventmem / Hermes — every touchpoint with the Hermes runtime, in one file.

The adapter is allowed to know Hermes exists; the core is not.  Everything that imports
``agent.*`` is funnelled through here so the boundary stays greppable: if it is not in
this file, the core could have done it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["redact", "is_trivial", "spawn_context_thread", "call_llm"]


def redact(text: str) -> str:
    """Best-effort secret scrub before anything is persisted (design §11.8).

    The host redactor is optional: without it we store what we were given rather than
    failing the write.
    """
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:  # noqa: BLE001
        return text


def is_trivial(text: str) -> bool:
    """Host's "nothing to remember here" gate (greetings, slash commands, bare acks)."""
    try:
        from agent.memory_provider import is_trivial_prompt
    except Exception:  # noqa: BLE001 - an older host: record rather than skip
        return False
    try:
        return bool(is_trivial_prompt(text or ""))
    except Exception:  # noqa: BLE001
        return False


def spawn_context_thread(target: Any, *, name: str) -> Any:
    """An unstarted thread that inherits the caller's contextvars (profile scope)."""
    from agent.memory_provider import spawn_context_thread as _spawn

    return _spawn(target, name=name)


def call_llm(messages: list[dict[str, Any]], *, task: str, max_tokens: int = 500,
             timeout: float = 60.0) -> Any:
    """One auxiliary-model completion (compression, goal judge and this share the route)."""
    from agent.auxiliary_client import call_llm as _call

    return _call(task=task, messages=messages, temperature=0, max_tokens=max_tokens,
                 timeout=timeout)
