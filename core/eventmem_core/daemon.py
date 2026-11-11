"""eventmem_core.daemon — one resident process: passes, backend supervision, HTTP API.

The worker used to be a loop that ran a pass every ``poll_seconds``.  The daemon is that
loop plus three things a worker could not have:

* **supervision** — for every ``self`` pack listed in ``decision.backends`` it starts the
  process before a pass that has work to do (and never while idle, so an unused model
  costs nothing);
* **a management API** — ``127.0.0.1:9123`` by default, JSON, no auth, because it is bound
  to loopback and the alternative (a socket protocol) is more moving parts for the same
  result;
* **the GUI** — a small static page served from the same port and nothing else.  It is a
  thin client of exactly these routes, so anything the page can do, ``curl`` can do.

The data plane does **not** go through here: recall and ``evt_add`` still run inside the
host process, importing the core directly.  This process only answers "which backend,
when, and how much".

Routes::

    GET  /                           the GUI (static; /ui/* for its assets)
    GET  /api/health                 liveness + db + backend names
    GET  /api/status                 health + cursor lag + last passes
    GET  /api/stats                  event/edge/pair counts, kinds, projects
    GET  /api/config                 effective settings + the file they came from
    POST /api/config                 write overrides, re-read, apply live
    GET  /api/backends               packs with their process state
    POST /api/backends/<id>/probe    one HTTP probe now
    POST /api/backends/<id>/start    launch a self pack
    POST /api/backends/<id>/stop     stop it
    GET  /api/packs                  packs incl. their config.yaml text
    POST /api/packs                  create a pack from text (validated first)
    POST /api/packs/<id>/config      save new text for an existing pack
    POST /api/packs/<id>/delete      remove the folder
    GET  /api/models                 installed runtimes / GGUFs / packs
    POST /api/install                download runtime + model into a new pack (a job)
    GET  /api/job                    the install job's state
    GET  /api/logs                   this process's recent log lines
    POST /api/run-once               run one pass now (409 if one is already running)
    GET  /api/runs                   the last few pass results
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import mimetypes
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from . import install as install_mod
from . import packs as packs_mod
from . import runtime
from . import ui_strings
from . import worker as W
from .decision import from_pack
from .packs import Pack, PackError
from .schema import SCHEMA_VERSION
from .settings import CoreSettings, describe
from .store import Store

_log = logging.getLogger(__name__)

#: How long a backend that probed fine but failed every decision is skipped for.
COOLDOWN = 300

__all__ = ["Daemon", "make_server", "acquire_lock", "serve", "main", "API_PORT", "UI_DIR",
           "NO_GUARD"]

#: Loopback only.  The GUI and the CLI are local by construction.
API_PORT = 9123

#: Static page served at ``/``; lives next to this module so a pip install ships it.
UI_DIR = Path(__file__).resolve().parent / "ui"


class Daemon:
    """The resident object.  Holds no database handle: every read opens/closes."""

    def __init__(self, settings: CoreSettings, *, packs_root: str = "",
                 settings_store: Optional[Any] = None):
        #: *settings_store* is the adapter's ``~/.hermes/eventmem/settings.yaml`` handle: an
        #: object with ``path``, ``read() -> dict`` and ``write(mapping) -> CoreSettings``.
        #: ``None`` (tests, ``--once``) makes ``POST /api/config`` answer 501 rather than
        #: pretend it saved something.
        self.settings_store = settings_store
        self.settings = settings
        self.packs_root = str(packs_root or getattr(settings.decision, "packs_dir", "") or "")
        self.started = time.time()
        self._pass_lock = threading.Lock()
        self._runs: collections.deque = collections.deque(maxlen=20)
        self._stop = threading.Event()
        #: Cooperative cancellation of the *current* pass (``POST /api/stop``).  Distinct from
        #: ``_stop``, which shuts the daemon down.
        self._abort = threading.Event()
        #: Set by ``POST /api/switch`` to cut the poll short: the next pass probes the chain
        #: again, so a backend that came up mid-round gets picked immediately.
        self._wake = threading.Event()
        #: What the current pass is doing right now — the console's live counter.
        self._live: Optional[dict[str, Any]] = None
        #: Backends that answered the probe but failed every decision, and when.  A pass
        #: skips them for COOLDOWN seconds so the chain degrades instead of stalling on a
        #: server that is up but will not serve the model.
        self._down: dict[str, float] = {}
        self.logs: collections.deque = collections.deque(maxlen=400)
        _log_target = logging.getLogger("eventmem_core")
        _log_target.addHandler(_RingHandler(self.logs))
        self._job_lock = threading.Lock()
        self._job: dict[str, Any] = {"state": "idle", "phase": "", "done": 0, "total": 0,
                                     "error": "", "result": None, "started": 0, "finished": 0}
        self._job_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ packs

    def packs(self) -> list[Pack]:
        found, problems = packs_mod.discover(self.packs_root)
        for problem in problems:
            _log.warning("eventmem daemon: %s", problem)
        return found

    def supervised(self) -> list[Pack]:
        """Only packs named in ``decision.backends`` are ours to start.

        Dropping a folder into ``backends/`` must not launch a model; listing it in the
        chain is the explicit opt-in.
        """
        names = tuple(getattr(self.settings.decision, "backends", ()) or ())
        if not names:
            return []
        return [p for p in self.packs() if p.kind == "self" and p.name in names]

    def prepare(self, _settings: CoreSettings) -> None:
        """Called by the pass, only when there is work: start what is down."""
        for pack in self.supervised():
            state = runtime.status(pack)
            if state["running"]:
                continue
            try:
                runtime.start(pack)
            except Exception as exc:  # noqa: BLE001 - a backend that will not start is skipped
                _log.warning("eventmem daemon: %s", exc)

    # ------------------------------------------------------------------ passes

    def run_pass(self) -> dict[str, Any]:
        if not self._pass_lock.acquire(blocking=False):
            return {"skipped": "busy"}
        self._abort.clear()
        started = time.time()
        self._live = {"backend": "", "pairs": 0, "edges": 0, "since": started, "updated": started}

        def progress(state: dict[str, Any]) -> None:
            # Runs on the pass thread, between pairs; the UI reads it through /api/status.
            self._live = {
                "backend": str(state.get("backend") or ""),
                "pairs": int(state.get("pairs") or 0),
                "edges": int(state.get("edges") or 0),
                "since": started,
                "updated": time.time(),
            }

        now = time.time()
        avoid = tuple(name for name, when in self._down.items() if now - when < COOLDOWN)
        try:
            result = W.run_once(self.settings, prepare=self.prepare, on_progress=progress,
                                should_stop=self._abort.is_set, avoid=avoid)
        except Exception as exc:  # noqa: BLE001 - a broken pass must not kill the daemon
            _log.warning("eventmem daemon: pass failed (%s)", exc)
            result = {"skipped": "error", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._live = None
            self._pass_lock.release()
        backend = str(result.get("backend") or "")
        if backend:
            if result.get("skipped") == "jev-error":
                self._down[backend] = time.time()
                _log.warning("eventmem daemon: %s 探活通过但判定全失败，冷却 %d 秒后重试",
                             backend, COOLDOWN)
            else:
                self._down.pop(backend, None)
        entry = {"ts": time.time(), **result}
        self._runs.append(entry)
        return result

    def loop(self, poll: Optional[float] = None) -> None:
        while not self._stop.is_set():
            # Re-read every round: the GUI can change poll_seconds while this loop runs.
            interval = float(poll) if poll and poll > 0 else float(self.settings.edges.poll_seconds)
            self._wake.clear()
            try:
                result = self.run_pass()
                if result.get("skipped") not in ("idle", "busy"):
                    _log.info("eventmem daemon: %s", result)
            except Exception as exc:  # noqa: BLE001
                _log.warning("eventmem daemon: pass raised (%s)", exc)
            # Wake on a switch request or on shutdown; otherwise sit out the interval.
            end = time.time() + max(5.0, interval)
            while not self._stop.is_set() and not self._wake.is_set() and time.time() < end:
                self._stop.wait(min(0.5, max(0.05, end - time.time())))
            self._wake.clear()

    # ------------------------------------------------------------------ views

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "pid": os.getpid(),
            "db": self.settings.db_path,
            "schema": SCHEMA_VERSION,
            "uptime": round(time.time() - self.started, 1),
            "packs_dir": self.packs_root,
            "backends": [p.name for p in self.packs()],
        }

    def backend_status(self, pack: Pack) -> dict[str, Any]:
        base: dict[str, Any] = {
            "name": pack.name,
            "kind": pack.kind,
            "protocol": pack.protocol,
            "model": pack.model,
            "license": pack.license,
            "url": pack.base_url,
            "weights": pack.weights,
            "note": pack.note,
            "has_key": bool(pack.key()),
            "needs_key": pack.needs_key,
            "check_model": pack.check_model,
        }
        if pack.kind == "self":
            base.update(runtime.status(pack))
        else:
            base.update({"configured": bool(pack.base_url), "running": None, "pid": None,
                         "port": pack.port})
        base["in_chain"] = pack.name in tuple(getattr(self.settings.decision, "backends", ()) or ())
        return base

    def status(self) -> dict[str, Any]:
        body = self.health()
        body["backends"] = [self.backend_status(p) for p in self.packs()]
        body["runs"] = list(self._runs)[-5:]
        body["cursor"] = self._cursor_view()
        # Live pass state: the console's "using <backend>, +1 per pair" strip polls this.
        body["live"] = self._live
        body["busy"] = self._pass_lock.locked()
        body["stopping"] = self._abort.is_set()
        body["down"] = {name: round(max(0.0, COOLDOWN - (time.time() - when)), 1)
                        for name, when in self._down.items()
                        if time.time() - when < COOLDOWN}
        return body

    def _cursor_view(self) -> dict[str, Any]:
        path = self.settings.db_path
        if not path or not os.path.exists(path):
            return {"behind": None, "events": None}
        try:
            store = Store(path)
            try:
                raw = store.get_meta("nightly_since_ts")
                cursor = float(raw) if raw else 0.0
                behind = max(0.0, time.time() - cursor) if cursor else None
                return {"behind": round(behind, 1) if behind is not None else None,
                        "events": store.count_events_since(cursor)}
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - status must not fail because of the db
            return {"behind": None, "events": None, "error": str(exc)}

    def stats(self) -> dict[str, Any]:
        """Counts for the overview page.  A read-only connection, so a running pass is safe."""
        path = self.settings.db_path
        empty: dict[str, Any] = {"events": 0, "edges": 0, "pairs": 0, "kinds": {},
                                 "projects": [], "newest": 0}
        if not path or not os.path.exists(path):
            return empty
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            try:
                count = lambda sql: int(conn.execute(sql).fetchone()[0])  # noqa: E731
                kinds = dict(conn.execute(
                    "select kind, count(*) from events group by 1 order by 2 desc").fetchall())
                projects = [{"name": name or "未归类", "events": n, "last": last}
                            for name, n, last in conn.execute(
                                "select p.name, count(*), max(e.ts) from events e "
                                "left join projects p on p.id = e.project_id "
                                "group by 1 order by 2 desc limit 8").fetchall()]
                newest = conn.execute("select max(ts) from events").fetchone()[0] or 0
                return {
                    "events": count("select count(*) from events"),
                    "edges": count("select count(*) from edges"),
                    "pairs": count("select count(*) from pair_evals"),
                    "kinds": kinds,
                    "projects": projects,
                    "newest": float(newest),
                }
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - the page must render even if the db is busy
            return {**empty, "error": str(exc)}

    def config_view(self) -> dict[str, Any]:
        """Effective settings, the file that produced them, and whether we may write it."""
        out: dict[str, Any] = {
            "effective": describe(self.settings),
            "packs_dir": self.packs_root,
            "file": "",
            "overrides": {},
            "writable": self.settings_store is not None,
        }
        if self.settings_store is not None:
            out["file"] = str(getattr(self.settings_store, "path", "") or "")
            try:
                out["overrides"] = self.settings_store.read() or {}
            except Exception as exc:  # noqa: BLE001
                out["error"] = str(exc)
        return out

    # ------------------------------------------------------------------ actions

    def save_config(self, body: Any) -> tuple[int, dict[str, Any]]:
        if self.settings_store is None:
            return 501, {"error": "这个 daemon 没有配置存储（--once 或测试模式）"}
        if not isinstance(body, dict) or not body:
            return 400, {"error": '要一个非空 JSON 对象，例如 {"edges": {"heads_vec_top_k": 8}}'}
        try:
            self.settings = self.settings_store.write(body)
        except (PackError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
        _log.info("eventmem daemon: settings updated from the GUI: %s", sorted(body))
        return 200, {"ok": True, **self.config_view()}

    def packs_view(self) -> dict[str, Any]:
        found, problems = packs_mod.discover(self.packs_root)
        items = []
        for pack in found:
            item = self.backend_status(pack)
            try:
                item["config"] = (Path(pack.path) / packs_mod.PACK_FILE).read_text(encoding="utf-8")
            except OSError as exc:
                item["config"] = ""
                item["config_error"] = str(exc)
            items.append(item)
        return {"packs": items, "problems": problems, "dir": self.packs_root}

    def _pack_text(self, body: Any) -> tuple[str, str]:
        if not isinstance(body, dict):
            raise PackError("要一个 JSON 对象")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PackError("config 文本是空的")
        return str(body.get("name") or ""), text

    def create_pack(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not self.packs_root:
            return 400, {"error": "packs_dir 未设置"}
        try:
            name, text = self._pack_text(body)
        except PackError as exc:
            return 400, {"error": str(exc)}
        name = name.strip()
        if not name or "/" in name or "\\" in name or name.startswith((".", "_")):
            return 400, {"error": "名字只能是一层目录名（不能含 /，不能以 . 或 _ 开头）"}
        folder = Path(self.packs_root) / name
        warnings: list[str] = []
        try:
            pack = packs_mod.parse_text(name, folder, text, warn=warnings.append)
            packs_mod.save_text(folder, text, create_only=True)
        except PackError as exc:
            return 400, {"error": str(exc)}
        _log.info("eventmem daemon: pack %s created (%s)", name, pack.kind)
        return 200, {"ok": True, "pack": self.backend_status(pack), "warnings": warnings}

    def save_pack(self, name: str, body: Any) -> tuple[int, dict[str, Any]]:
        folder = Path(self.packs_root) / name if self.packs_root else None
        if folder is None or not (folder / packs_mod.PACK_FILE).exists():
            return 404, {"error": f"没有后端 {name}"}
        try:
            _, text = self._pack_text(body)
        except PackError as exc:
            return 400, {"error": str(exc)}
        warnings: list[str] = []
        try:
            pack = packs_mod.parse_text(name, folder, text, warn=warnings.append)
            packs_mod.save_text(folder, text)
        except PackError as exc:
            return 400, {"error": str(exc)}
        _log.info("eventmem daemon: pack %s saved", name)
        return 200, {"ok": True, "pack": self.backend_status(pack), "warnings": warnings}

    def delete_pack(self, name: str, body: Any) -> tuple[int, dict[str, Any]]:
        import shutil

        folder = Path(self.packs_root) / name if self.packs_root else None
        if folder is None or not (folder / packs_mod.PACK_FILE).exists():
            return 404, {"error": f"没有后端 {name}"}
        chain = tuple(getattr(self.settings.decision, "backends", ()) or ())
        force = isinstance(body, dict) and bool(body.get("force"))
        if name in chain and not force:
            return 409, {"error": f"{name} 还在 decision.backends 里；先从链路里移除（或 force）"}
        try:
            pack = packs_mod.load_pack(folder)
        except PackError:
            pack = None
        if pack is not None and pack.kind == "self" and runtime.status(pack).get("running"):
            runtime.stop(pack)
        shutil.rmtree(folder)
        _log.info("eventmem daemon: pack %s deleted", name)
        return 200, {"ok": True, "deleted": name}

    def models_view(self) -> dict[str, Any]:
        root = self._root()
        if root is None:
            return {"root": "", "llama": {}, "models": [], "backends": [],
                    "error": "packs_dir 未设置"}
        return install_mod.state(install_mod.Paths(str(root)))

    def job_view(self) -> dict[str, Any]:
        job = dict(self._job)
        job["elapsed"] = round((job.get("finished") or time.time()) - job["started"], 1) \
            if job.get("started") else 0.0
        return {"job": job}

    def start_install(self, body: Any) -> tuple[int, dict[str, Any]]:
        root = self._root()
        if root is None:
            return 400, {"error": "packs_dir 未设置"}
        if not isinstance(body, dict):
            body = {}
        with self._job_lock:
            if self._job["state"] == "running":
                return 409, {"error": "已经有一个安装任务在跑", **self.job_view()}
            self._job = {"state": "running", "phase": "准备中", "done": 0, "total": 0,
                         "error": "", "result": None, "started": time.time(), "finished": 0}
        thread = threading.Thread(target=self._install_worker, args=(body, root),
                                  name="eventmem-install", daemon=True)
        self._job_thread = thread
        thread.start()
        return 202, self.job_view()

    def _on_progress(self, label: str, done: int, total: int, done_flag: bool) -> None:
        self._job.update(phase=str(label), done=int(done), total=int(total))

    def _install_worker(self, body: dict[str, Any], root: Path) -> None:
        paths = install_mod.Paths(str(root))
        install_mod.set_progress(self._on_progress)
        try:
            if body.get("runtime_only"):
                result = {"hardware": install_mod.probe_hardware(),
                          "llama": install_mod.install_llama(
                              paths, asset_url=str(body.get("llama_url") or ""))}
            else:
                result = install_mod.install(
                    paths,
                    str(body.get("name") or "lev"),
                    repo=str(body.get("repo") or install_mod.DEFAULT_REPO),
                    quant=str(body.get("quant") or install_mod.DEFAULT_QUANT),
                    skip_llama=bool(body.get("skip_llama")),
                    llama_url=str(body.get("llama_url") or ""),
                    force=bool(body.get("force")),
                )
            self._job.update(state="done", result=result, phase="完成")
        except Exception as exc:  # noqa: BLE001 - the job reports, the daemon lives on
            self._job.update(state="error", error=f"{type(exc).__name__}: {exc}")
            _log.warning("eventmem daemon: install failed (%s)", exc)
        finally:
            install_mod.set_progress(None)
            self._job["finished"] = time.time()

    def _root(self) -> Optional[Path]:
        return Path(self.packs_root).parent if self.packs_root else None

    def find(self, name: str) -> Optional[Pack]:
        for pack in self.packs():
            if pack.name == name:
                return pack
        return None

    def probe(self, name: str) -> tuple[int, dict[str, Any]]:
        pack = self.find(name)
        if pack is None:
            return 404, {"error": f"没有后端 {name}"}
        provider = from_pack(pack)
        if provider is None:
            state = runtime.status(pack)
            reason = "not-running" if pack.kind == "self" else "no-address"
            return 200, {"ok": False, "reason": reason, **state}
        ok = provider.probe()
        return 200, {"ok": ok, "reason": provider.last_error or "",
                     "url": provider.endpoint.base_url, "protocol": provider.protocol}

    def start_backend(self, name: str) -> tuple[int, dict[str, Any]]:
        pack = self.find(name)
        if pack is None:
            return 404, {"error": f"没有后端 {name}"}
        if pack.kind != "self":
            return 400, {"error": f"{name} 是 {pack.kind} 后端，不归我们启动"}
        try:
            state = runtime.start(pack, probe=_probe_of(pack))
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": str(exc)}
        return 200, {"ok": True, **state}

    def stop_backend(self, name: str) -> tuple[int, dict[str, Any]]:
        pack = self.find(name)
        if pack is None:
            return 404, {"error": f"没有后端 {name}"}
        if pack.kind != "self":
            return 400, {"error": f"{name} 是 {pack.kind} 后端，不归我们停止"}
        stopped = runtime.stop(pack)
        return 200, {"ok": True, "stopped": stopped}

    # ------------------------------------------------------------------ routing

    def route(self, method: str, parts: list[str],
              body: Any = None) -> tuple[int, dict[str, Any]]:
        if not parts or parts[0] != "api":
            return 404, {"error": "not found"}
        tail = parts[1:]

        if method == "GET" and tail == ["health"]:
            return 200, self.health()
        if method == "GET" and tail == ["status"]:
            return 200, self.status()
        if method == "GET" and tail == ["stats"]:
            return 200, self.stats()
        if method == "GET" and tail == ["logs"]:
            return 200, {"logs": list(self.logs)}
        if method == "GET" and tail == ["config"]:
            return 200, self.config_view()
        if method == "GET" and tail == ["i18n"]:
            # One dictionary for both UIs (the desktop plugin and the page served here), keyed
            # by the Chinese source string, so neither has to carry a translation table.
            lang = str(getattr(self.settings, "language", "") or "zh")
            if lang == "auto":
                lang = "zh"
            return 200, {"lang": lang, "map": ui_strings.strings_for(lang)}
        if method == "POST" and tail == ["config"]:
            return self.save_config(body)
        if method == "GET" and tail == ["backends"]:
            return 200, {"backends": [self.backend_status(p) for p in self.packs()]}
        if method == "GET" and tail == ["runs"]:
            return 200, {"runs": list(self._runs)}
        if method == "GET" and tail == ["packs"]:
            return 200, self.packs_view()
        if method == "POST" and tail == ["packs"]:
            return self.create_pack(body)
        if method == "GET" and tail == ["models"]:
            return 200, self.models_view()
        if method == "GET" and tail == ["job"]:
            return 200, self.job_view()
        if method == "POST" and tail == ["install"]:
            return self.start_install(body)
        if method == "POST" and tail == ["run-once"]:
            result = self.run_pass()
            return (409 if result.get("skipped") == "busy" else 200), result
        if method == "POST" and tail == ["stop"]:
            # Graceful: the pair in flight finishes, the cursor stays put.
            running = self._pass_lock.locked()
            self._abort.set()
            return 200, {"ok": True, "stopping": running}
        if method == "POST" and tail == ["switch"]:
            # Stop the current pass *and* cut the poll short: the next pass probes the chain
            # from the top, so a backend that just came up (LM Studio finished loading) wins
            # without anyone restarting the daemon.
            #
            # The cooldown is cleared on purpose: pressing this button is the operator saying
            # "I changed something" (a new model, a fixed key). Without the reset a backend
            # marked down up to five minutes ago would still be skipped and the button would
            # look broken.
            running = self._pass_lock.locked()
            self._abort.set()
            self._down.clear()
            self._wake.set()
            return 200, {"ok": True, "stopping": running}
        if len(tail) == 3 and tail[0] == "packs" and method == "POST":
            name, action = tail[1], tail[2]
            if action == "config":
                return self.save_pack(name, body)
            if action == "delete":
                return self.delete_pack(name, body)
        if len(tail) == 3 and tail[0] == "backends" and method == "POST":
            name, action = tail[1], tail[2]
            if action == "probe":
                return self.probe(name)
            if action == "start":
                return self.start_backend(name)
            if action == "stop":
                return self.stop_backend(name)
        return 404, {"error": "not found"}


def _probe_of(pack: Pack):
    """A zero-argument liveness check for ``runtime.start``; ``None`` before it listens."""
    def check() -> bool:
        provider = from_pack(pack)
        return bool(provider and provider.probe())
    return check


# --------------------------------------------------------------------------- http

class _Handler(BaseHTTPRequestHandler):
    server_version = "eventmem-daemon/1"
    daemon: Daemon  # set by make_server

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        _log.debug("http: " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _send(self, status: int, raw: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, status: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, raw, "application/json; charset=utf-8")

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        if not raw.strip():
            return None
        return json.loads(raw.decode("utf-8"))

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        if method == "GET" and not path.startswith("/api/"):
            return self._serve_static(path)

        try:
            body = self._read_body() if method in ("POST", "PUT") else None
        except (ValueError, UnicodeDecodeError):
            return self._send_json(400, {"error": "请求体不是合法 JSON"})

        parts = [part for part in path.split("/") if part]
        try:
            status, payload = self.daemon.route(method, parts, body)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
        self._send_json(status, payload)

    def _serve_static(self, path: str) -> None:
        """``/`` and ``/ui/…`` out of :data:`UI_DIR`; nothing else is reachable."""
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        if rel.startswith("ui/"):
            rel = rel[3:]
        if not rel or ".." in Path(rel).parts:
            return self._send_json(404, {"error": "not found"})
        root = UI_DIR.resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            return self._send_json(404, {"error": "no such file",
                                         "hint": f"GUI 文件应在 {root}"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def make_server(daemon: Daemon, host: str = "127.0.0.1", port: int = API_PORT):
    handler = type("BoundHandler", (_Handler,), {"daemon": daemon})
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------- process

class _NoGuard:
    """Returned when the platform cannot offer ``flock``: the daemon may run, unguarded.

    Distinct from ``None`` (someone else holds the lock) on purpose — Windows has no
    ``fcntl``, and conflating the two made the daemon refuse to start there.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NO_GUARD"


NO_GUARD = _NoGuard()


def acquire_lock(path: str):
    """One daemon per database.  Returns the open fd, ``None`` when already held, or
    :data:`NO_GUARD` when this platform has no ``flock`` at all.

    ``flock`` is released when the process dies (including SIGKILL), so a crashed daemon
    never leaves a stale lock behind — the failure mode a pid file has.  On Windows the
    guard is simply unavailable; the scheduled task is what keeps it single.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        _log.warning("eventmem daemon: no fcntl; single-instance guard disabled "
                     "(let the scheduled task own the port)")
        return NO_GUARD
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.write(fd, str(os.getpid()).encode())
    return fd


class _RingHandler(logging.Handler):
    """Keeps the last N log records for ``GET /api/logs`` (the GUI's log pane)."""

    def __init__(self, sink: collections.deque):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append({"ts": record.created, "level": record.levelname,
                              "name": record.name, "message": record.getMessage()})
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def serve(daemon: Daemon, *, host: str = "127.0.0.1", port: int = API_PORT,
          poll: Optional[float] = None) -> None:
    server = make_server(daemon, host, port)
    thread = threading.Thread(target=server.serve_forever, name="eventmem-api", daemon=True)
    thread.start()
    _log.info("eventmem daemon: api on http://%s:%s  (GUI at /)", host, server.server_address[1])
    try:
        daemon.loop(poll=poll)
    finally:
        daemon._stop.set()
        server.shutdown()
        server.server_close()


def main(argv: Optional[list[str]] = None, *, settings: Optional[CoreSettings] = None,
         settings_store: Optional[Any] = None) -> int:
    st = settings or CoreSettings()
    parser = argparse.ArgumentParser(description="eventmem daemon (passes + backend supervision)")
    parser.add_argument("--db", default=st.db_path or os.environ.get("EVENTMEM_DB", ""))
    parser.add_argument("--host", default=os.environ.get("EVENTMEM_DAEMON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("EVENTMEM_DAEMON_PORT", API_PORT)))
    parser.add_argument("--poll", type=float, default=st.edges.poll_seconds)
    parser.add_argument("--once", action="store_true", help="跑一轮就退出（调试用）")
    parser.add_argument("--print-settings", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from dataclasses import replace

    if args.db:
        st = replace(st, db_path=os.path.expanduser(args.db))
    if args.print_settings:
        print(json.dumps({"db": st.db_path, "decision": st.decision.__dict__},
                         ensure_ascii=False, sort_keys=True, default=str))
        return 0
    if not st.db_path:
        parser.error("--db is required (db_path is empty and $EVENTMEM_DB is unset)")

    daemon = Daemon(st, settings_store=settings_store)
    if args.once:
        print(json.dumps(daemon.run_pass(), ensure_ascii=False, default=str))
        return 0

    lock = acquire_lock(st.db_path + ".daemon.lock")
    if lock is None:
        _log.error("eventmem daemon: another daemon already owns %s", st.db_path)
        return 1
    _log.info("eventmem daemon: db=%s packs=%s", st.db_path, daemon.packs_root)
    serve(daemon, host=args.host, port=args.port, poll=args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
