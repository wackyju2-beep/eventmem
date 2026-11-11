"""eventmem_core.worker — the resident process that builds edges as events arrive.

One process per database, not one per host session: a host may run several backends
(dashboard, gateway, one desktop session each), and every one of them loading the
memory provider must not start its own poller — they would all compute the same pairs,
fight over a single-slot model, and race the cursor.

The worker is deliberately dumb: look for events past the cursor; if there are none, do
nothing; otherwise one ``build_edges`` pass and advance the cursor.  The decision
service's lifecycle is not its business — it only pings it, and asks the policy for an
optional start script.

Run it either way::

    python -m eventmem_core.worker --db ~/.hermes/eventmem/events.db
    python core/eventmem_core/worker.py --db ...     # bootstrap adds core/ to sys.path
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):  # executed as a file: make ``eventmem_core`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eventmem_core import nightly as N  # noqa: E402
from eventmem_core.decision import from_settings  # noqa: E402
from eventmem_core.settings import CoreSettings, describe  # noqa: E402
from eventmem_core.store import Store  # noqa: E402

logger = logging.getLogger("eventmem.worker")

__all__ = ["healthy", "ensure_service", "run_once", "loop", "main"]


def healthy(url: str, *, timeout: float = 2.0) -> bool:
    """True when the decision service answers ``/health`` with ``status=ok``."""
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:
            return json.loads(resp.read()).get("status") == "ok"
    except Exception:  # noqa: BLE001 - any failure means "not healthy"
        return False


def ensure_service(script: str, url: str) -> bool:
    """Start the decision service through *script* when it is down; True when healthy.

    An empty or missing script is not an error: the operator may run the service some
    other way (systemd unit, container, remote endpoint).  ``PYTHONPATH`` is stripped
    from the child environment on purpose — a host site-packages ahead of the service's
    own venv shadows ``huggingface_hub`` and breaks its import.
    """
    if healthy(url):
        return True
    if not script:
        return False
    path = Path(script).expanduser()
    if not path.exists():
        logger.warning("eventmem worker: start script %s does not exist", path)
        return False
    try:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        subprocess.run([str(path)], check=False, timeout=300, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001 - a failed start means an unavailable service
        logger.warning("eventmem worker: start script failed (%s)", exc)
        return False
    return healthy(url)


def run_once(settings: CoreSettings, *, ensure: bool = True,
             prepare: Optional[Callable[[CoreSettings], None]] = None,
             on_progress: Optional[Callable[[dict], None]] = None,
             should_stop: Optional[Callable[[], bool]] = None,
             avoid: tuple[str, ...] = ()) -> dict[str, Any]:
    """One pass.  Idle when nothing new; otherwise ask the model and write edges.

    *prepare* runs **after** the idle check and **before** the decision chain is built: the
    daemon uses it to start a self-managed backend, which then has to be visible to
    ``from_settings``.  A manual CLI run passes nothing and never launches anything.
    """
    if not settings.db_path:
        raise ValueError("CoreSettings.db_path is empty")
    url = settings.decision.url
    store = Store(str(Path(settings.db_path).expanduser()))
    try:
        raw = store.get_meta(N.CURSOR_KEY)
        try:
            cursor = float(raw) if raw else time.time() - float(
                settings.edges.first_run_hours) * 3600.0
        except (TypeError, ValueError):
            cursor = time.time() - float(settings.edges.first_run_hours) * 3600.0
        if store.count_events_since(cursor) <= 0:
            return {"skipped": "idle", "events": 0}
        if prepare is not None:
            try:
                prepare(settings)
            except Exception as exc:  # noqa: BLE001 - a backend that will not start is skipped
                logger.warning("eventmem worker: prepare failed (%s)", exc)
        # 只有单端点模式才由 worker 负责拉起服务（v2.1 的 start_script）；pack 链里的后端
        # 不归这个脚本管——外部服务由它自己的宿主启动，自托管的那档由 daemon 的 prepare 管。
        legacy = not tuple(settings.decision.backends or ())
        if ensure and legacy and url and not ensure_service(settings.decision.start_script, url):
            logger.warning("eventmem worker: decision service unavailable")
            return {"skipped": "jev-unavailable"}
        run_start = time.time()
        jev = from_settings(settings, warn=logger.warning, avoid=avoid)
        result = N.build_edges(store, jev, cursor, settings.edges,
                               on_progress=on_progress, should_stop=should_stop)
        # A stopped pass must NOT move the cursor: the untouched events would be skipped
        # forever.  Same for the failure cases.  (Re-running a pair is safe — add_edge
        # dedupes — so a stop costs time, never correctness.)
        if result.get("skipped") not in ("jev-unavailable", "jev-error", "stopped"):
            # 高水位：手工 nightly 或第二个实例同时跑时，先开始的 pass 后结束也不能把游标改回去。
            store.advance_meta_ts(N.CURSOR_KEY, run_start - 120.0)
        return result
    finally:
        store.close()


def loop(settings: CoreSettings, *, poll: Optional[float] = None) -> None:
    """Poll forever.  ``poll=None`` (or <=0) means "use the policy's poll_seconds"."""
    interval = float(poll) if poll and poll > 0 else float(settings.edges.poll_seconds)
    while True:
        try:
            result = run_once(settings)
            if result.get("skipped") != "idle":
                logger.info("eventmem worker: %s", result)
        except Exception as exc:  # noqa: BLE001 - a broken pass must not kill the worker
            logger.warning("eventmem worker: pass failed (%s)", exc)
        time.sleep(max(5.0, interval))


def main(argv: Optional[list[str]] = None, *, settings: Optional[CoreSettings] = None) -> int:
    st = settings or CoreSettings()
    parser = argparse.ArgumentParser(description="eventmem link worker (one resident process)")
    parser.add_argument("--db", default=st.db_path or os.environ.get("EVENTMEM_DB", ""))
    parser.add_argument("--url", default=st.decision.url)
    parser.add_argument("--poll", type=float, default=st.edges.poll_seconds)
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出（调试用）")
    parser.add_argument("--no-service", action="store_true", help="不尝试拉起决策服务")
    parser.add_argument("--print-settings", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from dataclasses import replace

    if args.db:
        st = replace(st, db_path=os.path.expanduser(args.db))
    if args.url != st.decision.url:
        st = replace(st, decision=replace(st.decision, url=args.url))
    if args.print_settings:
        print(json.dumps(describe(st), ensure_ascii=False, sort_keys=True))
        return 0
    if not st.db_path:
        parser.error("--db is required (db_path is empty and $EVENTMEM_DB is unset)")
    if args.once:
        print(json.dumps(run_once(st, ensure=not args.no_service),
                         ensure_ascii=False, default=str))
        return 0
    logger.info("eventmem worker: db=%s url=%s poll=%ss", st.db_path, args.url, args.poll)
    loop(st, poll=args.poll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
