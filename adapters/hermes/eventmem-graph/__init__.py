"""eventmem-graph — the Python half is only a dashboard API.

The desktop UI lives in ``~/.hermes/desktop-plugins/eventmem-graph/plugin.js`` and reaches
``dashboard/plugin_api.py`` through ``ctx.rest('/…')``. This plugin registers no hooks, no
tools and no memory provider, so loading it never changes agent behaviour.
"""

from __future__ import annotations

__all__ = ["register"]


def register(ctx) -> None:  # noqa: ARG001 - the host passes its context
    return None
