#!/usr/bin/env python3
"""eventmem worker tests — 独立连边进程的语义：没活不碰模型，命令行走得通。

Run directly:  <python> tests/test_worker.py
"""

from __future__ import annotations

import io
import json
import os
import sys

os.environ["EVENTMEM_LANG"] = "zh"
import tempfile
import time
import traceback
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import eventmem_core.worker as worker  # noqa: E402
from eventmem_core.settings import CoreSettings, DecisionPolicy  # noqa: E402
from eventmem_core.store import Store  # noqa: E402

DEAD_URL = "http://127.0.0.1:9"


def cfg(db: str, url: str = DEAD_URL, *, ensure: str = "") -> CoreSettings:
    """Core settings for a temp database.  ``ensure`` is the (optional) start script."""
    from dataclasses import replace

    return replace(CoreSettings(db_path=db),
                   decision=replace(DecisionPolicy(), url=url, start_script=ensure))

CASES: list = []


def case(fn):
    CASES.append(fn)
    return fn


def fresh_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)          # Store() 自己建库建表
    return path


@case
def test_idle_pass_never_calls_the_model() -> None:
    """没有新事件时直接返回 idle —— 用死端口证明它真的没去请求模型。"""
    db = fresh_db()
    try:
        store = Store(db)
        store.set_meta("nightly_since_ts", "%.3f" % (time.time() + 60))
        store.close()
        got = worker.run_once(cfg(db))
        assert got == {"skipped": "idle", "events": 0}, got
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db + suffix)
            except FileNotFoundError:
                pass


@case
def test_cli_once_on_fresh_db() -> None:
    """``--once`` 是运维用的：空库也要 exit 0 并打印一行 JSON。"""
    db = fresh_db()
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = worker.main(["--db", db, "--once", "--url", DEAD_URL], settings=cfg(db))
        assert rc == 0, rc
        payload = json.loads(buf.getvalue())
        assert payload.get("skipped") == "idle", payload
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db + suffix)
            except FileNotFoundError:
                pass


@case
def test_unhealthy_probe_is_false_not_an_exception() -> None:
    assert worker.healthy("http://127.0.0.1:9") is False


def main() -> int:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {fn.__name__}")
    print(f"PASS {passed} / FAIL {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
