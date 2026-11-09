#!/usr/bin/env python3
"""Standalone entry point for the two out-of-session jobs.

Hermes' plugin loader is not involved: systemd and cron start a plain interpreter, and
this script puts ``core/`` and ``adapters/hermes/`` on ``sys.path`` before handing over
to the adapter CLI (which reads ``config.yaml`` and calls the core).

    python <repo>/adapters/hermes/run.py worker --db ~/.hermes/eventmem/events.db
    python <repo>/adapters/hermes/run.py nightly --since-hours 24
"""

from __future__ import annotations

import sys
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).resolve().parent          # adapters/hermes
_REPO_ROOT = _ADAPTER_ROOT.parents[1]                    # repo root
for entry in (str(_REPO_ROOT / "core"), str(_ADAPTER_ROOT), str(_REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from eventmem.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
