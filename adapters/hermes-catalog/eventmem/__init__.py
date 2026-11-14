"""eventmem — Hermes adapter for ``eventmem-core``.

The plugin package Hermes loads.  It owns exactly the host-shaped half of the project:

* ``register(ctx)`` — installs the memory provider;
* ``provider`` — the ``MemoryProvider`` lifecycle and the recall injection;
* ``tools`` — the seven tools the model sees;
* ``extract`` — the turn-end auxiliary completion;
* ``settings`` — Hermes ``config.yaml`` → :class:`eventmem_core.settings.CoreSettings`;

This catalog copy includes the provider-only canonical core in a private namespace.
The worker, daemon, model installer and GUI are not included in this copy.
"""

from __future__ import annotations

import logging

from .eventmem_core.i18n import tr  # noqa: E402

from .extract import AUX_TASK  # noqa: E402


# ``EventMemProvider`` is imported lazily because it subclasses a Hermes host class.

__all__ = [
    "PROVIDER_NAME",
    "PLUGIN_VERSION",
    "AUX_TASK",
    "EventMemProvider",
    "register",
]

logger = logging.getLogger(__name__)

PROVIDER_NAME = "eventmem"
PLUGIN_VERSION = "0.2.1"


def __getattr__(name: str):
    if name == "EventMemProvider":
        from .provider import EventMemProvider

        return EventMemProvider
    raise AttributeError(name)


def register(ctx) -> None:
    """Install the memory provider without process-wide registrations."""
    from .provider import EventMemProvider

    ctx.register_memory_provider(EventMemProvider())

    # Memory-provider discovery also calls register(), even when the provider is
    # not active. Avoid registering process-wide middleware or auxiliary tasks
    # here; repeated discovery would leak and duplicate those registrations.
