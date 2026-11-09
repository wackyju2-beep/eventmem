#!/usr/bin/env python3
"""Minimal stand-in for Hermes' ``agent.memory_provider``.

``eventmem/__init__.py`` imports three names from the host package because the plugin is a
host plugin — that import is correct at runtime and must stay strict. The test suite, however,
has to run on a bare interpreter too (CI has no Hermes install), so it installs this stub
**only when the real module is not importable**:

    import _host_stub
    _host_stub.install()
    import eventmem

The stub mirrors the real signatures (``spawn_context_thread`` returns an *unstarted* thread,
``RecallStatus`` is a frozen dataclass with ``provider_label``/``count``/``glyph``) so the tests
exercise the plugin's own logic, not a different contract.
"""

from __future__ import annotations

import re
import sys
import threading
import types
from dataclasses import dataclass
from typing import Any, Callable, Optional

INDICATOR_GLYPH = "🧠"

# Same regex the host uses (``agent.memory_provider.TRIVIAL_PROMPT_RE``): bare
# acknowledgements and slash commands carry no event worth extracting.
TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|sup|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)"
    r"[\s!?.:;,\"'~‘’“”—–…()\[\]{}<>*&^%$#@!+=`\u00a0]*$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: Optional[str]) -> bool:
    """True for empty input, slash commands and bare greetings/acknowledgements."""
    stripped = (text or "").strip()
    if not stripped or stripped.startswith("/"):
        return True
    return bool(TRIVIAL_PROMPT_RE.match(stripped))


@dataclass(frozen=True)
class RecallStatus:
    """What the last prefetch injected (mirrors ``agent.memory_provider.RecallStatus``)."""

    provider_label: str
    count: int
    glyph: str = INDICATOR_GLYPH


class MemoryProvider:
    """Not an ABC here on purpose: the plugin only subclasses it, it never instantiates it."""

    name = ""

    def is_available(self) -> bool:  # pragma: no cover - overridden by the plugin
        return True

    def unavailable_reason(self) -> str:  # pragma: no cover
        return ""


def spawn_context_thread(target: Callable[..., Any], *, name: str, daemon: bool = True,
                         args: tuple = (), kwargs: Optional[dict] = None) -> threading.Thread:
    """Unstarted thread, same contract as the host helper (contextvars copying is a no-op here)."""
    return threading.Thread(target=target, args=args, kwargs=kwargs or {}, name=name, daemon=daemon)


def install() -> bool:
    """Install the stub if the real host module is missing. Returns True when stubbed."""
    try:
        import agent.memory_provider  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "no host here"
        pass
    else:
        return False

    pkg = types.ModuleType("agent")
    pkg.__path__ = []  # keep `import agent.x` resolvable through sys.modules
    mod = types.ModuleType("agent.memory_provider")
    for _name, _value in (
        ("INDICATOR_GLYPH", INDICATOR_GLYPH),
        ("MemoryProvider", MemoryProvider),
        ("RecallStatus", RecallStatus),
        ("spawn_context_thread", spawn_context_thread),
        ("is_trivial_prompt", is_trivial_prompt),
        ("TRIVIAL_PROMPT_RE", TRIVIAL_PROMPT_RE),
    ):
        setattr(mod, _name, _value)
    setattr(pkg, "memory_provider", mod)
    sys.modules["agent"] = pkg
    sys.modules["agent.memory_provider"] = mod
    return True
