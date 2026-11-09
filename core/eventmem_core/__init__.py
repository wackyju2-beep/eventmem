"""eventmem core — event-shaped memory, with no host in it.

Public surface, and the whole of it:

* :class:`EventMem` — one database plus the policies that govern it.  Adapters hold one
  of these and call methods; they never reach into ``store`` or the schema.
* :class:`CoreSettings` — every knob, frozen, with the pre-split defaults.
* :func:`apply` — overlay a plain mapping (a host's config section) onto those defaults.

Modules under this package are implementation: ``store`` (SQLite), ``recall``
(fusion + rendering), ``embed`` (ONNX encoder), ``jev`` (decision-model client),
``nightly`` (edge building), ``worker`` (the resident pass), ``i18n`` (render language).
None of them import a host.  If something needs a host, it belongs in an adapter.
"""

from __future__ import annotations

from .core import EventMem, UNFILED
from .settings import (
    KINDS,
    RELS,
    CoreSettings,
    DecisionPolicy,
    EdgePolicy,
    RecallPolicy,
    apply,
    describe,
)
from .store import Store

__version__ = "0.2.1"

__all__ = [
    "EventMem",
    "UNFILED",
    "CoreSettings",
    "DecisionPolicy",
    "EdgePolicy",
    "RecallPolicy",
    "KINDS",
    "RELS",
    "Store",
    "apply",
    "describe",
    "__version__",
]
