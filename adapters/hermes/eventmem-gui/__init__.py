"""eventmem-gui — proxy only.

See ``dashboard/plugin_api.py``: the host mounts that router at
``/api/plugins/eventmem-gui/``.  This module exists because every plugin directory needs a
``register`` (the graph plugin's is a no-op too), and so the agent-side loader has something
to import that does not change agent behaviour.
"""

from __future__ import annotations

__all__ = ["register"]


def register(ctx) -> None:  # noqa: ARG001 - the host passes its context
    return None
