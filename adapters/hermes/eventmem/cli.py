"""eventmem / Hermes — command line: one nightly pass, or the resident worker.

Both run *outside* a host session, so they read the same ``config.yaml`` section the
plugin reads (``plugins.entries.eventmem.settings``) and hand the core a
:class:`CoreSettings`.  That is the point of the split: the thresholds the worker uses
while building edges and the ones the provider uses while recalling are the same values,
from the same file, with the same defaults.

    python -m eventmem.cli nightly --since-hours 24
    python -m eventmem.cli worker --db ~/.hermes/eventmem/events.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

__all__ = ["nightly_main", "worker_main", "daemon_main", "main"]

_EPILOG = """any other flag is passed through to the core:
  nightly: --db --since-hours --since-ts --no-cursor --url --timeout --print-settings
  worker:  --db --url --poll --once --no-service --print-settings
  daemon:  --db --host --port --poll --once --print-settings
  models:  install <名字> [--repo --quant --no-llama --llama-url --force] | list | probe
"""


def _peel(argv: Optional[list[str]]) -> tuple[str, list[str]]:
    """Take ``--hermes-home`` out of *argv*; everything else belongs to the core."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--hermes-home", default="")
    known, rest = parser.parse_known_args(argv or [])
    return known.hermes_home, rest


def nightly_main(argv: Optional[list[str]] = None) -> int:
    from eventmem_core import nightly as N

    from . import settings as S

    home, rest = _peel(argv)
    S.install_language_provider(home or None)
    return N.main(rest, settings=S.build(home or None))


def worker_main(argv: Optional[list[str]] = None) -> int:
    from eventmem_core import worker as W

    from . import settings as S

    home, rest = _peel(argv)
    S.install_language_provider(home or None)
    return W.main(rest, settings=S.build(home or None))


def daemon_main(argv: Optional[list[str]] = None) -> int:
    """The resident process: passes every ``poll_seconds`` plus the management API + GUI."""
    from eventmem_core import daemon as D

    from . import settings as S

    home, rest = _peel(argv)
    S.install_language_provider(home or None)
    store = S.FileStore(home or None)
    return D.main(rest, settings=S.build(home or None), settings_store=store)


def models_main(argv: Optional[list[str]] = None) -> int:
    """``eventmem models install lev`` — runtime + GGUF + pack."""
    from eventmem_core import install as I

    from . import settings as S

    home, rest = _peel(argv)
    root = S.hermes_home(home or None) / "eventmem"
    return I.main(rest, root=str(root))


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="eventmem", description="eventmem Hermes adapter command line",
        epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("nightly", "worker", "daemon", "models"))
    args, rest = parser.parse_known_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "nightly":
        return nightly_main(rest)
    if args.command == "daemon":
        return daemon_main(rest)
    if args.command == "models":
        return models_main(rest)
    return worker_main(rest)


if __name__ == "__main__":
    sys.exit(main())
