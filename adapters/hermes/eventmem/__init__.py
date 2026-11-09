"""eventmem — Hermes adapter for ``eventmem-core``.

The plugin package Hermes loads.  It owns exactly the host-shaped half of the project:

* ``register(ctx)`` — installs the memory provider, the tool-result middleware and the
  auxiliary extraction task;
* ``provider`` — the ``MemoryProvider`` lifecycle and the recall injection;
* ``tools`` — the seven tools the model sees;
* ``middleware`` — "this changed state, write it down" after Hermes tool calls;
* ``extract`` — the turn-end auxiliary completion;
* ``settings`` — Hermes ``config.yaml`` → :class:`eventmem_core.settings.CoreSettings`;
* ``cli`` — the nightly pass and the resident worker, with the same settings.

The memory logic itself (schema, store, recall, encoder, edges) lives in ``core/`` at the
repository root and is imported as ``eventmem_core``; nothing here re-implements it.
A second adapter (another agent harness) imports the same core and brings its own copy
of the six modules above.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

#: When this package is a symlink into the repository (the normal install), ``core/``
#: is three levels up.  A copied-in package must have ``eventmem-core`` installed.
_CORE_ROOT = Path(__file__).resolve().parents[3] / "core"
if (_CORE_ROOT / "eventmem_core").is_dir() and str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

from eventmem_core.i18n import tr  # noqa: E402

from .extract import AUX_TASK  # noqa: E402
from .middleware import (  # noqa: E402
    MEMORY_HINT_EN,
    MEMORY_HINT_ZH,
    is_state_changing,
    memory_hint,
    on_tool_result,
)

# ``EventMemProvider`` is imported lazily: it subclasses ``agent.memory_provider``, and
# the worker/nightly CLI (no host around) must be able to ``import eventmem.cli``.

__all__ = [
    "PROVIDER_NAME",
    "PLUGIN_VERSION",
    "AUX_TASK",
    "EventMemProvider",
    "MEMORY_HINT_EN",
    "MEMORY_HINT_ZH",
    "is_state_changing",
    "memory_hint",
    "on_tool_result",
    "register",
]

logger = logging.getLogger(__name__)

PROVIDER_NAME = "eventmem"
PLUGIN_VERSION = "0.2.0"


def __getattr__(name: str):
    if name == "EventMemProvider":
        from .provider import EventMemProvider

        return EventMemProvider
    raise AttributeError(name)


def register(ctx) -> None:
    """Install the provider, the tool-execution middleware and the auxiliary task."""
    from .provider import EventMemProvider

    ctx.register_memory_provider(EventMemProvider())

    register_middleware = getattr(ctx, "register_middleware", None)
    if callable(register_middleware):
        register_middleware("tool_execution", on_tool_result)
    else:  # pragma: no cover - every real PluginContext has it
        logger.warning("eventmem: plugin context has no register_middleware; "
                       "the state-change reminder is disabled")

    register_aux = getattr(ctx, "register_auxiliary_task", None)
    if callable(register_aux):
        try:
            register_aux(
                AUX_TASK,
                display_name=tr("eventmem 回合记忆提取", "eventmem turn extraction"),
                description=tr("回合结束后把这一轮对话抽成 0–3 条事件（插件内部使用）",
                               "Extract 0–3 events from one conversation turn (plugin-internal)"),
                defaults={},
            )
        except Exception as exc:  # noqa: BLE001 - unregistered task still uses the auto route
            logger.debug("eventmem: auxiliary task registration failed: %s", exc)
