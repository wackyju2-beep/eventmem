"""eventmem 连边 worker：一个独立常驻进程，插件不再在宿主里起线程。

为什么是独立进程：Hermes 没有统一后端——``hermes dashboard``、``hermes gateway``、
桌面 App 每次会话拉起的 ``hermes serve --isolated`` 各自都是完整后端，都会加载插件。
以前把轮询放在 ``MemoryProvider.initialize()`` 里，于是每个宿主各起一份 worker，
几份同时算同一批事件、抢同一个单槽模型，还会互相改游标。连边是全局后台任务，
不跟任何会话绑定，所以只该有一个进程在跑：``eventmem-worker.service``。

它只做两件事：看库里有没有游标之后的新事件；有就算边并把游标推到本次开始前。
模型服务由 ``startlux.service`` 常驻，这里只负责在它没起来时补一下。
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
from typing import Any, Optional

logger = logging.getLogger("eventmem.worker")

#: 多久看一次有没有新事件。1 分钟足够「随时开算」，空转成本约等于一次 count(*)。
POLL_SECONDS = float(os.environ.get("EVENTMEM_WORKER_POLL", "60"))

#: 决策服务（常驻，2 线程）。地址与 JevClient 的默认一致。
URL = os.environ.get("EVENTMEM_JEV_URL", "http://127.0.0.1:18090")
#: 决策服务没起来时调用的启动脚本（可选）。默认路径只是作者本机的约定，
#: 别人用 EVENTMEM_START_SCRIPT 指到自己的脚本；脚本不存在时 worker 只打一条 warning。
START_SCRIPT = Path(os.environ.get("EVENTMEM_START_SCRIPT",
                                  str(Path.home() / "models" / "startlux" / "start.sh")))

#: 首次全量回看的窗口（库里连游标都没有时）。
FIRST_RUN_HOURS = 24.0

DEFAULT_DB = Path.home() / ".hermes" / "eventmem" / "events.db"


def _modules():
    """宿主里走相对导入，systemd 里按脚本跑走平铺导入。"""
    if __package__:
        from . import nightly as N
        from .jev import JevClient
        from .store import Store
    else:
        import nightly as N            # type: ignore[no-redef]
        from jev import JevClient      # type: ignore[no-redef]
        from store import Store        # type: ignore[no-redef]
    return N, Store, JevClient


def healthy(url: str = URL) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as resp:
            return json.loads(resp.read()).get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


def _ensure_service() -> bool:
    """Start the resident decision service if it is not up; True when healthy."""
    if healthy():
        return True
    try:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        subprocess.run([str(START_SCRIPT)], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=300)
    except Exception as exc:  # noqa: BLE001 - next poll retries
        logger.warning("eventmem worker: start.sh failed (%s)", exc)
        return False
    return healthy()


def run_once(db_path: str, *, url: str = URL, ensure_service: bool = True) -> dict[str, Any]:
    """跑一轮：有新事件才算边。没有活时**不碰模型**，直接返回 ``{"skipped": "idle"}``。"""
    N, Store, JevClient = _modules()
    store = Store(str(db_path))
    try:
        raw = store.get_meta(N.CURSOR_KEY)
        try:
            cursor = float(raw) if raw else time.time() - FIRST_RUN_HOURS * 3600.0
        except (TypeError, ValueError):
            cursor = time.time() - FIRST_RUN_HOURS * 3600.0
        if store.count_events_since(cursor) <= 0:
            return {"skipped": "idle", "events": 0}
        if ensure_service and not _ensure_service():
            logger.warning("eventmem worker: decision service unavailable")
            return {"skipped": "jev-unavailable"}
        run_start = time.time()
        result = N.build_edges(store, JevClient(base_url=url, timeout=900.0), cursor)
        if result.get("skipped") not in ("jev-unavailable", "jev-error"):
            # 高水位：手工 nightly 或第二个实例同时跑时，先开始的 pass 后结束也不能把游标改回去。
            store.advance_meta_ts(N.CURSOR_KEY, run_start - 120.0)
        return result
    finally:
        store.close()


def _loop(db_path: str, *, poll: float = POLL_SECONDS, url: str = URL) -> None:
    while True:
        try:
            result = run_once(db_path, url=url)
            if result.get("skipped") != "idle":
                logger.info("eventmem worker: %s", result)
        except Exception as exc:  # noqa: BLE001 - a broken pass must not kill the worker
            logger.warning("eventmem worker: pass failed (%s)", exc)
        time.sleep(max(5.0, float(poll)))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="eventmem link worker (one resident process)")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--url", default=URL)
    parser.add_argument("--poll", type=float, default=POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出（调试用）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.once:
        print(json.dumps(run_once(args.db, url=args.url), ensure_ascii=False, default=str))
        return 0
    logger.info("eventmem worker: db=%s url=%s poll=%ss", args.db, args.url, args.poll)
    _loop(args.db, poll=args.poll, url=args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
