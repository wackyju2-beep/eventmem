"""eventmem_core.daemon — one resident process: passes, backend supervision, HTTP API.

The worker used to be a loop that ran a pass every ``poll_seconds``.  The daemon is that
loop plus three things a worker could not have:

* **supervision** — for every ``self`` pack listed in ``decision.backends`` it starts the
  process before a pass that has work to do (and never while idle, so an unused model
  costs nothing);
* **a management API** — ``127.0.0.1:9123`` by default, JSON, optionally bearer-token
  protected. Loopback without a token is the local trust boundary; binding a non-loopback
  address requires a token, because the same port exposes event edits and backend control;
* **the GUI** — a small static page served from the same port and nothing else.  It is a
  thin client of exactly these routes, so anything the page can do, ``curl`` can do.

The data plane used to not go through here: recall and ``evt_add`` ran inside the host
process, importing the core directly.  That is still the default (same machine, same file),
but :meth:`Daemon.data_plane` now mirrors ``EventMem`` over HTTP for a host that lives on
another machine — the database file itself never leaves this box.

Routes::

    GET  /                           the GUI (static; /ui/* for its assets)
    GET  /api/health                 identity + schema (minimal when unauthenticated)
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
    POST /api/restart                gracefully restart this standalone daemon (202 accepted)
    POST /api/run-once               run one pass now (409 if one is already running)
    GET  /api/runs                   the last few pass results
    -- data plane (a thin mirror of EventMem, for a host on another machine) --
    POST /api/mem/events             add one event (kind, text, project, session, agent…)
    GET  /api/mem/events/<id>        one event + its artifact text (``EventMem.expand``)
    POST /api/mem/events/<id>/forget remove one event (``EventMem.forget``)
    POST /api/mem/neighbors          edges around one event (``EventMem.neighbours``)
    POST /api/mem/recall             recall block + project candidates for one query
    GET  /api/mem/projects           the project vocabulary
    POST /api/mem/projects           create a project (the only way one is born)
    POST /api/mem/session/project    read the session's sticky project; send one to set it
    -- graph (the 3D view: whole tables in, human edits out) --
    GET  /api/mem/graph              events + edges + pairs + positions + facets
    POST /api/mem/graph/semantic     rank events by embedding similarity to free text
    POST /api/mem/graph/event        edit one event's text / kind / project
    POST /api/mem/graph/events       batch-edit project / kind on a selection
    POST /api/mem/graph/edge         write or re-point one human relation
    POST /api/mem/graph/edge/delete  delete one relation (tombstoned against jev)
    POST /api/mem/graph/positions    persist dragged atom positions
    POST /api/mem/graph/positions/clear  drop every saved position
"""

from __future__ import annotations

import argparse
import collections
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from . import install as install_mod
from . import candidate_backfill
from . import packs as packs_mod
from . import runtime
from . import ui_strings
from . import worker as W
from .core import EventMem
from .decision import from_pack
from .packs import Pack, PackError
from .schema import SCHEMA_VERSION
from .settings import CoreSettings, describe
from .store import Store, KINDS, RELS, _KEEP

_log = logging.getLogger(__name__)

#: How long a backend that probed fine but failed every decision is skipped for.
COOLDOWN = 300

#: Data-plane recall keeps one ``RecallState`` per session id, so a remote host gets the
#: same "already shown this session" collapsing an in-process adapter would. Bounded, LRU.
RECALL_STATES = 64

#: 人工校正的边界：正文上限、一次批量最多几条、一次最多存多少个位置。
MAX_GRAPH_TEXT = 500
MAX_GRAPH_BATCH = 500
MAX_GRAPH_POSITIONS = 500


def _rendered_count(block: str) -> int:
    """Event lines in a rendered recall block (the header has no ``#id``)."""
    return sum(1 for line in block.splitlines() if re.search(r"#\d+", line))

__all__ = ["Daemon", "make_server", "acquire_lock", "serve", "main", "API_PORT", "UI_DIR",
           "NO_GUARD", "API_VERSION", "SERVICE_ID", "db_identity", "is_loopback_host",
           "read_token_file"]

#: Loopback only.  The GUI and the CLI are local by construction.
API_PORT = 9123

#: Static page served at ``/``; lives next to this module so a pip install ships it.
UI_DIR = Path(__file__).resolve().parent / "ui"

#: Data-plane protocol version reported by ``/api/health``. Adapters refuse an endpoint
#: that does not name itself and speak this version — "HTTP 200 with JSON" is not identity.
API_VERSION = 1
SERVICE_ID = "eventmem-daemon"


def db_identity(path: str) -> str:
    """Stable id for one database **location**, not its contents.

    The adapter can require this id, so a daemon on the expected port that happens to own a
    different ``events.db`` is rejected before the first write. Path is normalised and
    resolved; the first 16 hex characters are enough to tell local databases apart.
    """
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        resolved = str(path)
    if os.name == "nt":
        resolved = resolved.lower()
    return hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def is_loopback_host(host: str) -> bool:
    """Whether a bind address is loopback (``localhost`` included)."""
    value = str(host or "").strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def read_token_file(path: str) -> str:
    """Read a token file, stripping trailing whitespace and a UTF-8 BOM."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        return handle.read().strip()


class Daemon:
    """The resident object.  Management reads open and close the database per call; the
    data plane (``/api/mem/*``) keeps one ``EventMem`` alive because loading the encoder
    costs ~1.3 s and a remote host recalls every turn."""

    def __init__(self, settings: CoreSettings, *, packs_root: str = "",
                 settings_store: Optional[Any] = None, api_token: str = ""):
        #: *settings_store* is the adapter's ``~/.hermes/eventmem/settings.yaml`` handle: an
        #: object with ``path``, ``read() -> dict`` and ``write(mapping) -> CoreSettings``.
        #: ``None`` (tests, ``--once``) makes ``POST /api/config`` answer 501 rather than
        #: pretend it saved something.
        self.settings_store = settings_store
        self.settings = settings
        #: When non-empty every API route except the (minimal) health probe requires
        #: ``Authorization: Bearer <token>``. Empty means loopback trust only.
        self.api_token = str(api_token or "").strip()
        self.packs_root = str(packs_root or getattr(settings.decision, "packs_dir", "") or "")
        self.started = time.time()
        self.instance_id = uuid.uuid4().hex
        self._restart_lock = threading.Lock()
        self._restart_requested = threading.Event()
        self._restart_enabled = False
        self._pass_lock = threading.Lock()
        self._config_lock = threading.RLock()
        #: 数据面：长驻 EventMem + 每会话的 RecallState。管理与数据面共用一把锁，
        #: 所以「reset 掉一个会话」和「同一会话同时来的两个 recall」不会打架。
        self._mem_lock = threading.RLock()
        self._mem_obj: Optional[EventMem] = None
        self._recall_states: collections.OrderedDict = collections.OrderedDict()
        self._preview_tokens: set[str] = set()
        self._backfill_plans: dict[str, Any] = {}
        self._backfill: dict[str, Any] = {'state': 'idle', 'pairs': 0, 'edges': 0,
                                         'result': None, 'error': '', 'started': 0, 'finished': 0}
        self._backfill_thread: Optional[threading.Thread] = None
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
        #: Packs this daemon started for the current pass (``release_after_pass`` stops them
        #: when the pass ends, so a GPU backend does not sit on the card between rounds).
        self._auto_started: list[Pack] = []
        self.logs: collections.deque = collections.deque(maxlen=400)
        _log_target = logging.getLogger("eventmem_core")
        _log_target.addHandler(_RingHandler(self.logs))
        self._job_lock = threading.Lock()
        self._job: dict[str, Any] = {"state": "idle", "phase": "", "done": 0, "total": 0,
                                     "error": "", "result": None, "started": 0, "finished": 0}
        self._job_thread: Optional[threading.Thread] = None
        #: When the last pass finished (0 = never).  The interval/daily/weekly schedule below
        #: compares this against the current time instead of sleeping a fixed amount, so a
        #: long pass never stacks a second one on top of itself.
        self._last_pass_end = 0.0
        #: Operator pause.  Distinct from ``_abort`` (stop *this* pass): while paused no pass
        #: starts at all, however the schedule is set, until the button is pressed again.
        #: Persisted in the database so a daemon restart does not restart the work.
        self._paused = self._read_paused()

    # ------------------------------------------------------------------ packs

    def packs(self) -> list[Pack]:
        found, problems = packs_mod.discover(self.packs_root)
        for problem in problems:
            _log.warning("eventmem daemon: %s", problem)
        return found

    def supervised(self) -> list[Pack]:
        """Only packs named in ``decision.backends`` are ours to start, in chain order.

        Dropping a folder into ``backends/`` must not launch a model; listing it in the
        chain is the explicit opt-in.
        """
        names = tuple(getattr(self.settings.decision, "backends", ()) or ())
        if not names:
            return []
        by_name = {p.name: p for p in self.packs()}
        return [by_name[name] for name in map(str, names)
                if name in by_name and by_name[name].kind == "self"]

    def prepare(self, _settings: CoreSettings) -> None:
        """Warm the backend this pass will actually use — the first live one in the chain.

        Called by the pass, only when there is work.  The chain is a *fallback order* and
        one backend serves the whole pass, so warming every pack loads models the pass
        never asks: two GPU packs cannot even sit on the card at the same time.  Walk the
        chain in order and return at the first entry that answers (an external pack) or
        that we manage to start; a refused start (the VRAM gate) falls through to the next.
        """
        self._auto_started = []
        names = tuple(getattr(self.settings.decision, "backends", ()) or ())
        if not names:
            return
        by_name = {p.name: p for p in self.packs()}
        for name in map(str, names):
            pack = by_name.get(name)
            if pack is None:
                continue
            if pack.kind != "self":
                provider = from_pack(pack)
                try:
                    if provider is not None and provider.probe():
                        return          # an external backend is already up; nothing to warm
                except Exception:  # noqa: BLE001 - a broken probe is just "not this one"
                    pass
                continue
            if runtime.status(pack)["running"]:
                return
            try:
                runtime.start(pack, probe=_probe_of(pack))
                self._auto_started.append(pack)
                return
            except Exception as exc:  # noqa: BLE001 - a backend that will not start is skipped
                _log.warning("eventmem daemon: %s", exc)

    def release(self) -> None:
        """Stop the packs this pass started that asked to be released (VRAM back to the user).

        Only what *we* started in this pass: a backend the operator launched by hand stays up.
        """
        for pack in getattr(self, "_auto_started", ()):
            if not getattr(pack, "release_after_pass", False):
                continue
            try:
                stopped = runtime.stop(pack)
                _log.info("eventmem daemon: released %s (stopped=%s)", pack.name, stopped)
            except Exception as exc:  # noqa: BLE001 - a stuck stop must not kill the daemon
                _log.warning("eventmem daemon: release %s failed (%s)", pack.name, exc)
        self._auto_started = []

    # ------------------------------------------------------------------ passes

    def run_pass(self) -> dict[str, Any]:
        if not self._pass_lock.acquire(blocking=False):
            return {"skipped": "busy"}
        self._abort.clear()
        started = time.time()
        self._live = {"backend": "", "pairs": 0, "edges": 0, "since": started, "updated": started,
                      "event_index": 0, "events_total": 0, "pairs_planned": None}

        def progress(state: dict[str, Any]) -> None:
            # Runs on the pass thread, between pairs; the UI reads it through /api/status.
            planned = state.get("pairs_planned")
            self._live = {
                "backend": str(state.get("backend") or ""),
                "pairs": int(state.get("pairs") or 0),
                "edges": int(state.get("edges") or 0),
                # 实时进度：走到第几个事件 / 一共几个；待判 = planned - pairs（planned 是估算）
                "event_index": int(state.get("event_index") or 0),
                "events_total": int(state.get("events_total") or 0),
                "pairs_planned": int(planned) if planned is not None else None,
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
            self._last_pass_end = time.time()
            self.release()          # 一轮跑完就把 GPU 让回去（只停这一轮自己拉起的 pack）
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
        """Run passes on the configured schedule until the daemon stops.

        ``edges.schedule`` picks the trigger — ``interval`` (every ``poll_seconds``),
        ``daily`` / ``weekly`` (once at ``schedule_at``) or ``off`` (button only) — and the
        pause flag vetoes all of them.  Re-read every round so the GUI can change it live.
        """
        while not self._stop.is_set():
            self._wake.clear()
            if self._due(poll):
                try:
                    result = self.run_pass()
                    if result.get("skipped") not in ("idle", "busy"):
                        _log.info("eventmem daemon: %s", result)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("eventmem daemon: pass raised (%s)", exc)
            # Wake on a switch/pause request or on shutdown; otherwise sit out the interval.
            end = time.time() + self._check_every(poll)
            while not self._stop.is_set() and not self._wake.is_set() and time.time() < end:
                self._stop.wait(min(0.5, max(0.05, end - time.time())))
            self._wake.clear()

    # -------------------------------------------------------------- scheduling

    def _schedule_mode(self) -> str:
        mode = str(getattr(self.settings.edges, "schedule", "interval") or "interval")
        return mode if mode in ("interval", "daily", "weekly", "off") else "interval"

    def _today_target(self) -> Optional[float]:
        """Today's ``schedule_at`` as a timestamp; ``None`` on a day the weekly rule skips."""
        raw = str(getattr(self.settings.edges, "schedule_at", "03:30") or "03:30")
        try:
            hh, mm = [int(part) for part in raw.split(":", 1)]
        except (TypeError, ValueError):
            return None
        now = datetime.datetime.now()
        if self._schedule_mode() == "weekly":
            want = int(getattr(self.settings.edges, "schedule_weekday", 0) or 0) % 7
            if now.weekday() != want:
                return None
        try:
            return datetime.datetime(now.year, now.month, now.day, hh, mm).timestamp()
        except ValueError:
            return None

    def _due(self, poll: Optional[float] = None) -> bool:
        """True when a pass should start now."""
        if self._paused:
            return False
        mode = self._schedule_mode()
        if mode == "off":
            return False
        if mode == "interval":
            interval = float(poll) if poll and poll > 0 else float(self.settings.edges.poll_seconds)
            return (time.time() - self._last_pass_end) >= max(1.0, interval)
        target = self._today_target()
        # One pass per slot: the last pass must have finished before the slot opened.
        return target is not None and self._last_pass_end < target <= time.time()

    def _check_every(self, poll: Optional[float] = None) -> float:
        """How long the loop may sleep before it looks at the clock again."""
        if self._schedule_mode() != "interval":
            return 30.0
        interval = float(poll) if poll and poll > 0 else float(self.settings.edges.poll_seconds)
        return min(max(1.0, interval), 60.0)

    def _read_paused(self) -> bool:
        path = self.settings.db_path
        if not path or not os.path.exists(path):
            return False
        try:
            with Store(path) as store:
                return str(store.get_meta("schedule_paused") or "") == "1"
        except Exception:  # noqa: BLE001 - a read failure must not stop the daemon
            return False

    def set_paused(self, paused: bool) -> bool:
        """Turn the operator pause on/off; persisted so a restart keeps the choice."""
        self._paused = bool(paused)
        if self._paused:
            # Pause means pause: stop the pass that is running too, not just future ones.
            self._abort.set()
        path = self.settings.db_path
        if path and os.path.exists(path):
            try:
                with Store(path) as store:
                    store.set_meta("schedule_paused", "1" if self._paused else "0")
            except Exception as exc:  # noqa: BLE001
                _log.warning("eventmem daemon: could not persist the pause flag (%s)", exc)
        if not self._paused:
            self._wake.set()        # resume = look at the schedule now, not a minute later
        _log.info("eventmem daemon: %s", "paused" if self._paused else "resumed")
        return self._paused

    def _schedule_view(self) -> dict[str, Any]:
        mode = self._schedule_mode()
        return {
            "mode": mode,
            "at": str(getattr(self.settings.edges, "schedule_at", "03:30") or "03:30"),
            "weekday": int(getattr(self.settings.edges, "schedule_weekday", 0) or 0),
            "poll_seconds": float(self.settings.edges.poll_seconds),
            "paused": self._paused,
            "last_pass": self._last_pass_end or None,
        }

    # ------------------------------------------------------------------ views

    def _capabilities(self) -> dict[str, Any]:
        """Cheap feature probes for the setup page (no model load, no network)."""
        now = time.time()
        cached = getattr(self, "_caps_cache", None)
        if cached and now - cached[0] < 30:
            return cached[1]
        try:
            from .embed import Encoder

            embedding = bool(Encoder(self.settings.models_root or None).available())
        except Exception:  # noqa: BLE001 - a broken optional runtime is just "off"
            embedding = False
        decision = bool(getattr(self.settings.decision, "backends", ())
                        or getattr(self.settings.decision, "url", ""))
        value = {"embedding": embedding, "decision": decision}
        self._caps_cache = (now, value)
        return value

    def health(self, *, authorized: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "ok",
            "service": SERVICE_ID,
            "api": API_VERSION,
            "schema": SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "db_id": db_identity(self.settings.db_path),
            "uptime": round(time.time() - self.started, 1),
            "auth": "token" if self.api_token else "none",
            "capabilities": self._capabilities(),
        }
        if authorized:
            # Paths, PIDs and pack names are for the local operator. A remote caller that
            # has not presented the token gets identity only.
            body.update({
                "pid": os.getpid(),
                "db": self.settings.db_path,
                "packs_dir": self.packs_root,
                "backends": [p.name for p in self.packs()],
                "paused": self._paused,
            })
        return body

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
        body["backfill"] = dict(self._backfill)
        body["busy"] = self._pass_lock.locked()
        body["stopping"] = self._abort.is_set()
        body["paused"] = self._paused
        body["schedule"] = self._schedule_view()
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
                                 "projects": [], "newest": 0,
                                 "failures": {"count": 0, "last": []}}
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
                # 回合末抽取失败（v10）：一次失败 = 那一轮什么都没记。概览页要能一眼看到，
                # 而不是等用户发现「怎么没记住」再翻日志。老库没有这张表时按 0 处理。
                try:
                    failures = [
                        {"ts": float(r[0]), "session": r[1], "reason": r[2], "detail": r[3]}
                        for r in conn.execute(
                            "select ts, session, reason, detail from extract_failures "
                            "order by ts desc, id desc limit 5").fetchall()
                    ]
                    failed = int(conn.execute("select count(*) from extract_failures")
                                 .fetchone()[0])
                except sqlite3.Error:
                    failures, failed = [], 0
                return {
                    "events": count("select count(*) from events"),
                    "edges": count("select count(*) from edges"),
                    "pairs": count("select count(*) from pair_evals"),
                    "kinds": kinds,
                    "projects": projects,
                    "newest": float(newest),
                    "failures": {"count": failed, "last": failures},
                }
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - the page must render even if the db is busy
            return {**empty, "error": str(exc)}

    def clear_edges(self) -> tuple[int, dict[str, Any]]:
        """Wipe the edge graph **and** the pair ledger, then reset the cursor.

        Deleting ``edges`` alone would rebuild nothing: the nightly pass skips any pair it
        has already evaluated, and that ledger is ``pair_evals``.  Manual deletions
        (``edge_deletions``) stay — those are the operator's decisions, not model output.
        """
        if self._pass_lock.locked():
            return 409, {"error": "本轮还在跑，先 POST /api/stop 再清空"}
        path = self.settings.db_path
        if not path or not os.path.exists(path):
            return 200, {"ok": True, "edges": 0, "pairs": 0, "cursor": 0}
        try:
            conn = sqlite3.connect(path, timeout=10.0)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                edges = int(conn.execute("DELETE FROM edges").rowcount or 0)
                pairs = int(conn.execute("DELETE FROM pair_evals").rowcount or 0)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('nightly_since_ts', '0') "
                    "ON CONFLICT(key) DO UPDATE SET value = '0'")
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return 503, {"error": f"清空失败：{exc}"}
        _log.info("eventmem daemon: cleared %d edges + %d pair evals; cursor reset", edges, pairs)
        return 200, {"ok": True, "edges": edges, "pairs": pairs, "cursor": 0}

    def clear_notices(self) -> tuple[int, dict[str, Any]]:
        """Clear the extraction-failure notices (the operator fixed the model)."""
        path = self.settings.db_path
        if not path or not os.path.exists(path):
            return 200, {"ok": True, "cleared": 0}
        try:
            conn = sqlite3.connect(path, timeout=5.0)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                cur = conn.execute("DELETE FROM extract_failures")
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return 503, {"error": f"清除失败：{exc}"}
        return 200, {"ok": True, "cleared": int(cur.rowcount or 0)}

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

    def _backfill_policy(self, body: Any):
        from dataclasses import replace
        from .settings import KINDS
        if not isinstance(body, dict) or not isinstance(body.get('edges'), dict):
            raise ValueError('expected {edges: {...}}')
        values = body['edges']
        counts = {'heads_vec_top_k', 'session_heads_top_k', 'cross_top_k',
                  'session_recent', 'unclosed_lessons', 'decision_skeleton'}
        cosines = {'heads_cos_min', 'cross_cos_min'}
        ignored = {'margin', 'min_conf_change', 'min_conf_negate', 'min_conf_repeat',
                   'min_conf_because', 'min_conf_refine', 'poll_seconds',
                   'schedule', 'schedule_at', 'schedule_weekday'}
        if set(values) - counts - cosines - ignored - {'cross_kinds'}:
            raise ValueError('unknown backfill edge field')
        for key, value in values.items():
            if key == 'cross_kinds':
                if not isinstance(value, list) or any(not isinstance(v, str) or v not in KINDS for v in value):
                    raise ValueError('cross_kinds must be an array of known event kinds')
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{key} must be a finite number')
            if key in counts and (value < 0 or int(value) != value):
                raise ValueError(f'{key} must be a non-negative integer')
            if key in cosines and not -1 <= value <= 1:
                raise ValueError(f'{key} must be between -1 and 1')
            if key in ignored and (value < 0 or (key != 'poll_seconds' and value > 1)):
                raise ValueError(f'{key} out of range')
        changes = {key: int(values[key]) if key in counts else values[key]
                   for key in counts | cosines if key in values}
        if 'cross_kinds' in values:
            changes['cross_kinds'] = tuple(values['cross_kinds'])
        return replace(self.settings.edges, **changes)

    def backfill_preview(self, body: Any) -> tuple[int, dict[str, Any]]:
        try:
            with self._config_lock:
                policy = self._backfill_policy(body)
        except ValueError as exc:
            return 400, {'error': str(exc)}
        if not self._pass_lock.acquire(blocking=False):
            return 409, {'error': 'daemon pass busy'}
        try:
            if not self.settings.db_path or not os.path.exists(self.settings.db_path):
                return 400, {'error': 'database not found'}
            with Store(self.settings.db_path) as store:
                plan = candidate_backfill.preview(store, policy)
            self._backfill_plans[plan.token] = plan
            if len(self._backfill_plans) > 32:
                self._backfill_plans = {plan.token: plan}
            return 200, {'token': plan.token, 'event_count': plan.event_count,
                         'observed_pairs': len(plan.observed_pairs), 'exact_total': None}
        finally:
            self._pass_lock.release()

    def backfill_start(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not self._pass_lock.acquire(blocking=False):
            return 409, {'error': 'daemon pass busy'}
        handed_off = False
        try:
            with self._config_lock:
                try:
                    policy = self._backfill_policy(body)
                except ValueError as exc:
                    return 400, {'error': str(exc)}
                token = body.get('token')
                plan = self._backfill_plans.get(token) if isinstance(token, str) else None
                if plan is None or plan.policy != policy:
                    return 409, {'error': 'preview required or policy changed'}
                settings = self.settings
                if not settings.db_path or not os.path.exists(settings.db_path):
                    return 400, {'error': 'database not found'}
                with Store(settings.db_path) as store:
                    if candidate_backfill._fingerprint(store, policy) != token:
                        return 409, {'error': 'preview is stale; preview again'}
                self._backfill_plans.clear()
                self._abort.clear()
                started = time.time()
                self._backfill = {'state': 'running', 'pairs': 0, 'edges': 0,
                                  'result': None, 'error': '', 'started': started, 'finished': 0}
                thread = threading.Thread(target=self._backfill_worker, args=(settings, plan),
                                          name='eventmem-backfill', daemon=True)
                self._backfill_thread = thread
                thread.start()
                handed_off = True
                return 202, {'ok': True, 'backfill': dict(self._backfill)}
        finally:
            if not handed_off:
                self._pass_lock.release()

    def _backfill_worker(self, settings, plan) -> None:
        started = self._backfill['started']
        def progress(state):
            self._backfill.update(pairs=state['pairs'], edges=state['edges'])
            self._live = {'backend': str(getattr(client, 'active_name', '') or ''),
                          'pairs': state['pairs'], 'edges': state['edges'],
                          'since': started, 'updated': time.time()}
        try:
            self._live = {'backend': '', 'pairs': 0, 'edges': 0,
                          'since': started, 'updated': started}
            if not self._abort.is_set() and not self._stop.is_set():
                self.prepare(settings)
                if not settings.decision.backends and settings.decision.url:
                    if not W.ensure_service(settings.decision.start_script, settings.decision.url):
                        result = {'status': 'jev-unavailable', 'pairs': 0, 'edges': 0}
                        self._backfill.update(state='done', result=result)
                        return
            now = time.time()
            avoid = tuple(n for n, t in self._down.items() if now - t < COOLDOWN)
            client = W.from_settings(settings, warn=_log.warning, avoid=avoid)
            with Store(settings.db_path) as store:
                result = candidate_backfill.execute(
                    store, client, plan, token=plan.token,
                    should_stop=lambda: self._abort.is_set() or self._stop.is_set(),
                    on_progress=progress)
            backend = str(getattr(client, 'active_name', '') or '')
            if backend and result['status'] == 'jev-error':
                self._down[backend] = time.time()
            elif backend and result['status'] == 'completed':
                self._down.pop(backend, None)
            self._backfill.update(state='done', result=result, pairs=result['pairs'], edges=result['edges'])
        except Exception as exc:  # noqa: BLE001
            _log.warning('eventmem daemon: backfill failed (%s)', exc)
            self._backfill.update(state='error', error=f'{type(exc).__name__}: {exc}')
        finally:
            self._backfill['finished'] = time.time()
            self._live = None
            self._pass_lock.release()

    def save_config(self, body: Any) -> tuple[int, dict[str, Any]]:
        if self.settings_store is None:
            return 501, {"error": "这个 daemon 没有配置存储（--once 或测试模式）"}
        if not isinstance(body, dict) or not body:
            return 400, {"error": '要一个非空 JSON 对象，例如 {"edges": {"heads_vec_top_k": 8}}'}
        try:
            with self._config_lock:
                self.settings = self.settings_store.write(body)
        except (PackError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
        _log.info("eventmem daemon: settings updated from the GUI: %s", sorted(body))
        return 200, {"ok": True, **self.config_view()}

    def recalibrate(self, body: Any, *, apply_changes: bool) -> tuple[int, dict[str, Any]]:
        from .settings import apply as apply_settings
        keys = {'margin', 'min_conf_change', 'min_conf_negate', 'min_conf_repeat',
                'min_conf_because', 'min_conf_refine'}
        if not isinstance(body, dict) or not isinstance(body.get('edges'), dict):
            return 400, {'error': 'expected {edges: {...}}'}
        historical_scale = body.get('historical_scale')
        if historical_scale is not None and historical_scale != 'conditional':
            return 400, {'error': 'historical_scale must be null or conditional'}
        values = body['edges']
        if set(values) - keys or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                     for v in values.values()):
            return 400, {'error': 'edges must contain only numeric threshold fields'}
        token = body.get('token') if apply_changes else None
        if apply_changes and (not isinstance(token, str) or token not in self._preview_tokens):
            return 409, {'error': 'preview required or token expired'}
        try:
            policy = apply_settings(self.settings, {'edges': values}, strict=True).edges
        except ValueError as exc:
            return 400, {'error': str(exc)}
        if not self._pass_lock.acquire(blocking=False):
            return 409, {'error': 'daemon pass busy'}
        try:
            self._config_lock.acquire()
            if apply_changes and token not in self._preview_tokens:
                return 409, {'error': 'preview required or token expired'}
            policy = apply_settings(self.settings, {'edges': values}, strict=True).edges
            if not self.settings.db_path or not os.path.exists(self.settings.db_path):
                return 400, {'error': 'database not found'}
            with Store(self.settings.db_path) as store:
                try:
                    result = store.recalibrate(policy, token=token, historical_scale=historical_scale)
                except ValueError as exc:
                    return 409, {'error': str(exc)}
            if apply_changes:
                self._preview_tokens.clear()
                result['ok'] = True
            else:
                self._preview_tokens.add(result['token'])
                if len(self._preview_tokens) > 32:
                    self._preview_tokens = {result['token']}
            return 200, result
        finally:
            self._config_lock.release()
            self._pass_lock.release()

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

    # -------------------------------------------------------------- data plane
    # 数据面 = EventMem 的 REST 镜像。库文件只在本机：另一台机器上的宿主（另一个 agent）
    # 通过这里读写同一份记忆，SQLite 本身绝不放网络盘。
    # 这里**不是**工具层：批量上限、index 链接、归档提示、脱敏、项目黏性都归调用它的
    # adapter——那才是宿主相关的翻译。

    def mem(self) -> EventMem:
        """数据面用的长驻 ``EventMem``（编码器只加载一次）。"""
        with self._mem_lock:
            if self._mem_obj is None:
                self._mem_obj = EventMem(self.settings)
            return self._mem_obj

    def close_mem(self) -> None:
        with self._mem_lock:
            mem, self._mem_obj = self._mem_obj, None
        if mem is not None:
            try:
                mem.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

    def data_plane(self, method: str, tail: list[str],
                   body: Any) -> tuple[int, dict[str, Any]]:
        try:
            if tail == ["events"] and method == "POST":
                return self.mem_add(body)
            if tail == ["recall"] and method == "POST":
                return self.mem_recall(body)
            if tail == ["neighbors"] and method == "POST":
                return self.mem_neighbors(body)
            if tail == ["graph"] and method == "GET":
                return self.mem_graph()
            if tail == ["graph", "semantic"] and method == "POST":
                return self.mem_graph_semantic(body)
            if tail == ["graph", "event"] and method == "POST":
                return self.mem_graph_event(body)
            if tail == ["graph", "events"] and method == "POST":
                return self.mem_graph_events(body)
            if tail == ["graph", "edge"] and method == "POST":
                return self.mem_graph_edge(body)
            if tail == ["graph", "edge", "delete"] and method == "POST":
                return self.mem_graph_edge_delete(body)
            if tail == ["graph", "positions"] and method == "POST":
                return self.mem_graph_positions(body)
            if tail == ["graph", "positions", "clear"] and method == "POST":
                return self.mem_graph_positions_clear()
            if tail == ["projects"] and method == "GET":
                return 200, {"projects": self.mem().list_projects()}
            if tail == ["projects"] and method == "POST":
                name = str((body or {}).get("name") or "").strip() if isinstance(body, dict) else ""
                if not name:
                    return 400, {"error": "name is required"}
                return 200, {"project": self.mem().add_project(name)}
            if tail == ["session", "project"] and method == "POST":
                return self.mem_session_project(body)
            if len(tail) == 2 and tail[0] == "events" and method == "GET":
                event_id = int(tail[1])
                out = self.mem().expand(event_id)
                return (200, out) if out else (404, {"error": f"no event #{event_id}"})
            if (len(tail) == 3 and tail[0] == "events" and tail[2] == "forget"
                    and method == "POST"):
                event_id = int(tail[1])
                return 200, {"id": event_id, "removed": bool(self.mem().forget(event_id))}
        except ValueError as exc:
            return 400, {"error": f"ValueError: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a bad value must not 500 the daemon
            return 400, {"error": f"{type(exc).__name__}: {exc}"}
        return 404, {"error": "not found"}

    def mem_add(self, body: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object"}
        kind = str(body.get("kind") or "").strip()
        text = str(body.get("text") or "").strip()
        if not kind or not text:
            return 400, {"error": "kind and text are required"}
        mem = self.mem()
        art_id = None
        if body.get("artifact"):
            art_id = mem.add_artifact(str(body.get("artifact_kind") or "其他"),
                                      str(body["artifact"]))
        event_id = mem.add_event(
            kind, text,
            project=body.get("project") or None,
            session=body.get("session") or None,
            # 不传就是 NULL：跨机的写入必须自带 ``agent``，这里不替调用方猜身份。
            agent=body.get("agent") or None,
            art_id=art_id,
            art_path=str(body.get("path") or "").strip() or None,
            extra_projects=body.get("extra_projects") or None,
        )
        return 200, {"ok": True, "id": event_id, "agent": body.get("agent") or None}

    def mem_recall(self, body: Any) -> tuple[int, dict[str, Any]]:
        payload = body if isinstance(body, dict) else {}
        query = str(payload.get("query") or "").strip()
        if not query:
            return 400, {"error": "query is required"}
        mem = self.mem()
        session = str(payload.get("session") or "").strip()
        state = None
        if session:
            with self._mem_lock:
                if payload.get("reset"):
                    self._recall_states.pop(session, None)
                state = self._recall_states.get(session)
                if state is None:
                    state = mem.new_recall_state()
                    self._recall_states[session] = state
                    while len(self._recall_states) > RECALL_STATES:
                        self._recall_states.popitem(last=False)
                else:
                    self._recall_states.move_to_end(session)
        budget = payload.get("budget_tokens")
        qvec = mem.query_vector(query)
        block = mem.recall(query, state=state, qvec=qvec,
                           budget_tokens=int(budget) if budget else None) or ""
        # 项目候选与召回块分开回：宿主自己决定拼成一段还是两段（Hermes 是拼在块尾）。
        hint = mem.project_hint(query, qvec=qvec) or ""
        return 200, {"block": block, "hint": hint, "count": _rendered_count(block),
                     "stateless": not bool(session)}

    def mem_session_project(self, body: Any) -> tuple[int, dict[str, Any]]:
        """会话的项目粘性：``{session}`` 读，``{session, project}`` 写。

        粘性本身是 adapter 的事（每次写事件时自己决定要不要带 project），但「哪个会话
        记在哪个项目」必须跟着数据库走：宿主进程重启后，同一会话的后半段才不会散到
        unfiled 里。Hermes 侧一直用同一张 ``session_project`` 表，这里只是把它开到
        数据面上，语义与 ``EventMem.session_project`` 一一对应。
        """
        payload = body if isinstance(body, dict) else {}
        session = str(payload.get("session") or "").strip()
        if not session:
            return 400, {"error": "session is required"}
        mem = self.mem()
        if "project" not in payload:
            return 200, {"session": session, "project": mem.session_project(session) or ""}
        raw = payload.get("project")
        name = "" if raw is None else str(raw).strip()
        if name.lower() in ("", "unfiled", "未归类"):
            name = ""
        if name and mem.store.get_project(name) is None:
            return 400, {"error": f"unknown project: {name!r}"}
        mem.set_session_project(session, name or None)
        return 200, {"ok": True, "session": session, "project": name}

    def mem_neighbors(self, body: Any) -> tuple[int, dict[str, Any]]:
        payload = body if isinstance(body, dict) else {}
        raw_id = payload.get("event_id")
        if raw_id is None:
            return 400, {"error": "event_id is required"}
        rels = payload.get("rels")
        if isinstance(rels, str):
            rels = [part for part in re.split(r"[,/\s]+", rels) if part]
        if not isinstance(rels, list) or not rels:
            rels = None
        event_id = int(raw_id)
        block = self.mem().neighbours(event_id, rels=rels,
                                      depth=int(payload.get("depth") or 1)) or ""
        return 200, {"event_id": event_id, "context": block, "found": bool(block.strip())}

    # -------------------------------------------------- graph (可视化 / 人工校正)
    # 图谱是**整表**读、**人工**写的另一条路径：召回都是 top-k，只有它要全库；
    # 人工修改是少数几条 UPDATE，但要顺带处理项目归属、跨项目标记和候选重选。
    # 所以它不经过工具层，和 ``/api/mem/events`` 一样是数据面的一部分。

    def mem_graph(self) -> tuple[int, dict[str, Any]]:
        return 200, self.mem().store.graph_view()

    def mem_graph_semantic(self, body: Any) -> tuple[int, dict[str, Any]]:
        payload = body if isinstance(body, dict) else {}
        query = str(payload.get("q") or "").strip()
        if not query:
            return 400, {"error": "q 不能为空"}
        if len(query) > MAX_GRAPH_TEXT:
            return 400, {"error": f"q 最多 {MAX_GRAPH_TEXT} 字"}
        try:
            top = int(payload.get("top") if payload.get("top") is not None else 16)
        except (TypeError, ValueError):
            top = 16
        top = max(1, min(top, 200))
        raw_min = payload.get("min_score")
        try:
            min_score = float(raw_min) if raw_min is not None else None
        except (TypeError, ValueError):
            min_score = None
        mem = self.mem()
        qvec = mem.query_vector(query)
        if qvec is None:
            return 503, {"error": "Embedding 编码器不可用（模型文件或运行时缺失）"}
        try:
            found = mem.store.semantic_hits(qvec, top=top, min_score=min_score)
        except ValueError as exc:
            return 503, {"error": str(exc)}
        return 200, {"ok": True, "query": query, "top": top, "dim": found["dim"],
                     "scanned": found["scanned"], "skipped": found["skipped"],
                     "hits": found["hits"]}

    @staticmethod
    def _graph_project_id(store: Store, name: Any) -> tuple[Optional[int], Optional[str]]:
        """``(project_id, error)``: 未归类 → ``(None, None)``; an unknown name is an error."""
        if name is None:
            return None, None
        name = str(name).strip()
        if not name or name.lower() == "unfiled" or name == "未归类":
            return None, None
        row = store.get_project(name)
        if row is None:
            return None, f"没有项目 {name!r}；先在图谱里建（proj_add）"
        return int(row["id"]), None

    def mem_graph_event(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Edit one event's text / kind / project, then re-encode and re-mark its candidates."""
        payload = body if isinstance(body, dict) else {}
        try:
            event_id = int(payload["id"])
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "id 必须是事件编号"}
        text = payload.get("text")
        kind = payload.get("kind")
        has_project = "project" in payload
        if text is None and kind is None and not has_project:
            return 400, {"error": "text、kind、project 至少要改一个"}
        if text is not None:
            if not isinstance(text, str) or not text.strip():
                return 400, {"error": "正文不能为空"}
            if len(text) > MAX_GRAPH_TEXT:
                return 400, {"error": f"正文最多 {MAX_GRAPH_TEXT} 字"}
        if kind is not None and kind not in KINDS:
            return 400, {"error": f"类型必须是 {'/'.join(KINDS)}"}

        mem = self.mem()
        store = mem.store
        event = store.get_event(event_id)
        if event is None:
            return 404, {"error": f"事件 #{event_id} 不存在"}
        pid, error = self._graph_project_id(store, payload.get("project")) if has_project else (None, None)
        if error:
            return 400, {"error": error}

        stamp = time.time()
        store.update_event(
            event_id,
            text=text,
            kind=kind,
            project_id=(pid if has_project else _KEEP),
            edited_ts=stamp,
        )
        updated = store.get_event(event_id) or event
        state, dim, note = self._reembed_event(mem, updated)
        changed_project = has_project and (updated.get("project") or "") != (event.get("project") or "")
        changed_kind = kind is not None and kind != event.get("kind")
        cross = store.refresh_cross([event_id]) if changed_project else 0
        reseat = store.mark_reseat([event_id]) if (changed_project or changed_kind) else 0.0
        return 200, {"ok": True, "id": event_id, "vector": state, "dim": int(dim), "note": note,
                     "cross": int(cross), "reseat": reseat or None}

    def _reembed_event(self, mem: EventMem, event: dict[str, Any]) -> tuple[str, int, str]:
        """Re-encode one event from its stored text/kind/project.

        Same payload as ``EventMem.add_event`` (``[项目] 类型 正文``) so an edited event keeps
        competing in the vector arm with the text a human just approved.  A missing encoder skips
        the vector only — the text edit always lands.
        """
        vec = mem.encode("[%s] %s %s" % (event.get("project") or "未归类", event["kind"], event["text"]))
        if not vec:
            return "skipped", 0, "Embedding 编码器不可用（模型文件或运行时缺失）"
        mem.store.update_vec(int(event["id"]), vec)
        return "updated", len(vec) // 4, ""

    def mem_graph_events(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Batch-edit ``project`` / ``kind`` on a lasso selection.

        A project is not a cosmetic label: it is part of the re-encoded vector payload
        (``[项目] 类型 正文``), it is a membership row in ``event_projects`` (that is what candidate
        selection reads) and it decides ``edges.cross``.  No edge is deleted: existing relations
        stay, only never-asked pairs can appear.
        """
        if not isinstance(body, dict):
            return 400, {"error": "要一个 JSON 对象"}
        raw = body.get("ids")
        if not isinstance(raw, list) or not raw:
            return 400, {"error": "ids 必须是非空数组"}
        ids: list[int] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, int):
                return 400, {"error": "ids 只能是事件编号"}
            if value not in ids:
                ids.append(value)
            if len(ids) > MAX_GRAPH_BATCH:
                return 400, {"error": f"一次最多改 {MAX_GRAPH_BATCH} 条"}
        has_project = "project" in body
        kind = body.get("kind")
        if kind is not None and kind not in KINDS:
            return 400, {"error": f"类型必须是 {'/'.join(KINDS)}"}
        if not has_project and kind is None:
            return 400, {"error": "project 或 kind 至少要改一个"}

        mem = self.mem()
        store = mem.store
        pid, error = self._graph_project_id(store, body.get("project")) if has_project else (None, None)
        if error:
            return 400, {"error": error}
        found = sorted(int(ev["id"]) for ev in store.get_events(ids))
        if not found:
            return 404, {"error": "这些事件都不存在"}
        missing = sorted(set(ids) - set(found))

        stamp = time.time()
        for event_id in found:
            store.update_event(event_id, kind=kind, project_id=(pid if has_project else _KEEP),
                               edited_ts=stamp)
        # 项目进了向量 payload，不重算等于检索还按旧项目走。
        vector = {"updated": 0, "skipped": 0, "note": ""}
        for event in store.get_events(found):
            state, _dim, note = self._reembed_event(mem, event)
            vector[state] = vector.get(state, 0) + 1
            if state == "skipped" and not vector["note"]:
                vector["note"] = note
        cross = store.refresh_cross(found)
        reseat = store.mark_reseat(found)
        return 200, {"ok": True, "updated": len(found), "missing": missing, "vector": vector,
                     "cross": int(cross), "reseat": reseat or None}

    def mem_graph_edge(self, body: Any) -> tuple[int, dict[str, Any]]:
        """Create or re-point a human relation. ``old_rel`` present = replace it."""
        payload = body if isinstance(body, dict) else {}
        try:
            src, dst = int(payload["src"]), int(payload["dst"])
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "src/dst 必须是事件编号"}
        rel = payload.get("rel")
        old_rel = payload.get("old_rel")
        if rel not in RELS:
            return 400, {"error": f"关系必须是 {'/'.join(RELS)}"}
        if old_rel is not None and old_rel not in RELS:
            return 400, {"error": "old_rel 不合法"}
        try:
            conf = float(payload.get("conf")) if payload.get("conf") is not None else 0.5
        except (TypeError, ValueError):
            conf = 0.5
        self.mem().store.set_edge(src, dst, rel, conf, old_rel=old_rel)
        return 200, {"ok": True, "src": src, "dst": dst, "rel": rel}

    def mem_graph_edge_delete(self, body: Any) -> tuple[int, dict[str, Any]]:
        payload = body if isinstance(body, dict) else {}
        try:
            src, dst = int(payload["src"]), int(payload["dst"])
        except (KeyError, TypeError, ValueError):
            return 400, {"error": "src/dst 必须是事件编号"}
        rel = payload.get("rel")
        if rel not in RELS:
            return 400, {"error": f"关系必须是 {'/'.join(RELS)}"}
        removed = self.mem().store.delete_edge(src, dst, rel)
        return 200, {"ok": True, "deleted": 1 if removed else 0}

    def mem_graph_positions(self, body: Any) -> tuple[int, dict[str, Any]]:
        """摆过的位置存下来：刷新/重启后原子还在被拉到的位置。"""
        payload = body if isinstance(body, dict) else {}
        items = payload.get("positions")
        if not isinstance(items, list):
            return 400, {"error": "positions 必须是数组"}
        rows = []
        for item in items[:MAX_GRAPH_POSITIONS]:
            try:
                event_id = int(item["id"])
                x, y, z = float(item["x"]), float(item["y"]), float(item["z"])
            except (KeyError, TypeError, ValueError):
                return 400, {"error": f"位置条目不合法：{item!r}"}
            if not all(map(math.isfinite, (x, y, z))):
                return 400, {"error": "坐标必须是有限数"}
            rows.append((event_id, x, y, z))
        saved = self.mem().store.save_atom_positions(rows)
        return 200, {"ok": True, "saved": saved}

    def mem_graph_positions_clear(self) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True, "cleared": self.mem().store.clear_atom_positions()}

    # ------------------------------------------------------------------ routing

    def route(self, method: str, parts: list[str],
              body: Any = None, *, authorized: bool = True) -> tuple[int, dict[str, Any]]:
        if not parts or parts[0] != "api":
            return 404, {"error": "not found"}
        tail = parts[1:]

        if tail[:1] == ["mem"]:
            return self.data_plane(method, tail[1:], body)

        if method == "POST" and tail == ["restart"]:
            with self._restart_lock:
                if not self._restart_enabled:
                    return 503, {"error": "restart is unavailable in this hosting mode"}
                if self._restart_requested.is_set():
                    return 409, {"error": "restart already in progress"}
                self._restart_requested.set()
            return 202, {"ok": True, "restarting": True, "instance_id": self.instance_id}
        if method == "GET" and tail == ["health"]:
            return 200, self.health(authorized=authorized)
        if method == "GET" and tail == ["status"]:
            return 200, self.status()
        if method == "GET" and tail == ["stats"]:
            return 200, self.stats()
        if method == "POST" and tail == ["notices", "clear"]:
            return self.clear_notices()
        if method == "POST" and tail == ["edges", "clear"]:
            return self.clear_edges()
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
        if method == 'POST' and tail == ['recalibrate', 'preview']:
            return self.recalibrate(body, apply_changes=False)
        if method == 'POST' and tail == ['recalibrate', 'apply']:
            return self.recalibrate(body, apply_changes=True)
        if method == 'POST' and tail == ['backfill', 'preview']:
            return self.backfill_preview(body)
        if method == 'POST' and tail == ['backfill', 'start']:
            return self.backfill_start(body)
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
            # Graceful: the pair in flight finishes, the cursor stays put.  The schedule is
            # untouched, so the next tick starts a pass again — use /api/pause for that.
            running = self._pass_lock.locked()
            self._abort.set()
            return 200, {"ok": True, "stopping": running}
        if method == "POST" and tail == ["pause"]:
            # The operator switch: pause stops the running pass *and* every future one until
            # resume, whatever the schedule says.  Omitted body = pause.
            want = body.get("paused") if isinstance(body, dict) else None
            paused = self.set_paused(True if want is None else bool(want))
            return 200, {"ok": True, "paused": paused, "schedule": self._schedule_view()}
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
            if self._paused:
                self.set_paused(False)   # "switch and rerun" means the operator wants work
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

class _DrainServer(ThreadingHTTPServer):
    # server_close joins all request threads, so no database writes are abandoned at restart.
    daemon_threads = False
    block_on_close = True


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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
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

    def _authorized(self) -> bool:
        """Constant-time bearer check; an unset token keeps loopback-only behaviour."""
        token = self.daemon.api_token
        if not token:
            return True
        header = str(self.headers.get("Authorization") or "")
        return hmac.compare_digest(header.encode("utf-8", "surrogatepass"),
                                   ("Bearer " + token).encode("utf-8", "surrogatepass"))

    def _same_origin(self) -> bool:
        """Reject a browser POST from another site (CSRF), while allowing non-browser clients.

        The console and the Hermes/DSH proxies talk server-to-server and send no Origin; a
        local browser page sends its own origin; a malicious site sends a different one.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        host = str(self.headers.get("Host") or "").lower()
        return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        if method == "GET" and not path.startswith("/api/"):
            return self._serve_static(path)

        parts = [part for part in path.split("/") if part]
        is_restart = parts == ["api", "restart"]
        # A browser on another site must not be able to drive a loopback service through a
        # simple cross-origin form POST. Applies to every API write, not only restart.
        if method == "POST" and not self._same_origin():
            return self._send_json(403, {"error": "cross-origin request forbidden"})
        authorized = self._authorized()
        # Health is the only unauthenticated route, and an unauthenticated caller gets the
        # identity fields only (no db path, pid, or pack names).
        if (path.startswith("/api/") and not (method == "GET" and path == "/api/health")
                and not authorized):
            return self._send_json(401, {"error": "missing or invalid API token"})
        try:
            body = self._read_body() if method in ("POST", "PUT") else None
        except (ValueError, UnicodeDecodeError):
            return self._send_json(400, {"error": "请求体不是合法 JSON"})

        try:
            if self.daemon._restart_requested.is_set() and not is_restart:
                status, payload = 503, {"error": "restart in progress"}
            else:
                status, payload = self.daemon.route(method, parts, body, authorized=authorized)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            status, payload = 500, {"error": f"{type(exc).__name__}: {exc}"}
        try:
            self._send_json(status, payload)
        finally:
            if is_restart and status == 202:
                # Only wake the lifecycle after the acceptance response has been attempted.
                self.daemon._stop.set()
                self.daemon._wake.set()

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
    server = _DrainServer((host, int(port)), handler)
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
    _log.info("eventmem daemon: api on http://%s:%s  (GUI at /, auth=%s)",
              host, server.server_address[1], "token" if daemon.api_token else "none")
    try:
        daemon.loop(poll=poll)
    finally:
        daemon._stop.set()
        server.shutdown()
        server.server_close()  # drain all HTTP requests before closing their shared resources
        for background in (daemon._job_thread, daemon._backfill_thread):
            if background is not None:
                background.join()
        daemon.close_mem()


def main(argv: Optional[list[str]] = None, *, settings: Optional[CoreSettings] = None,
         settings_store: Optional[Any] = None) -> int:
    st = settings or CoreSettings()
    parser = argparse.ArgumentParser(description="eventmem daemon (passes + backend supervision)")
    parser.add_argument("--db", default=st.db_path or os.environ.get("EVENTMEM_DB", ""))
    parser.add_argument("--host", default=os.environ.get("EVENTMEM_DAEMON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("EVENTMEM_DAEMON_PORT", API_PORT)))
    parser.add_argument("--poll", type=float, default=st.edges.poll_seconds)
    parser.add_argument("--token", default=os.environ.get("EVENTMEM_DAEMON_TOKEN", ""),
                        help="require Bearer auth on every API route except /api/health")
    parser.add_argument("--token-file", default=os.environ.get("EVENTMEM_DAEMON_TOKEN_FILE", ""),
                        help="read the API token from this file (preferred over --token)")
    parser.add_argument("--once", action="store_true", help="跑一轮就退出（调试用）")
    parser.add_argument("--print-settings", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from dataclasses import replace

    token = str(args.token or "").strip()
    if args.token_file:
        try:
            token = read_token_file(args.token_file)
        except OSError as exc:
            parser.error(f"cannot read --token-file {args.token_file}: {exc}")
    # Binding off loopback without a token would expose event edits and backend control to
    # the network. Refuse instead of printing a warning nobody reads.
    if not is_loopback_host(args.host) and not token:
        parser.error("binding a non-loopback address requires --token or --token-file")

    if args.db:
        st = replace(st, db_path=os.path.expanduser(args.db))
    if args.print_settings:
        print(json.dumps({"db": st.db_path, "decision": st.decision.__dict__},
                         ensure_ascii=False, sort_keys=True, default=str))
        return 0
    if not st.db_path:
        parser.error("--db is required (db_path is empty and $EVENTMEM_DB is unset)")

    daemon = Daemon(st, settings_store=settings_store, api_token=token)
    if args.once:
        print(json.dumps(daemon.run_pass(), ensure_ascii=False, default=str))
        return 0

    lock = acquire_lock(st.db_path + ".daemon.lock")
    if lock is None:
        _log.error("eventmem daemon: another daemon already owns %s", st.db_path)
        return 1
    _log.info("eventmem daemon: db=%s packs=%s", st.db_path, daemon.packs_root)
    # orig_argv preserves -I, -m and interpreter options that sys.argv drops. Embedded
    # interpreters cannot reliably reconstruct an entry point and must not offer restart.
    original = getattr(sys, "orig_argv", None)
    daemon._restart_enabled = bool(original and len(original) > 1 and sys.executable)
    serve(daemon, host=args.host, port=args.port, poll=args.poll)
    if daemon._restart_requested.is_set():
        if lock is not NO_GUARD:
            os.close(lock)
        try:
            argv = [sys.executable, *original[1:]]
            # Windows CRT spawnv does not quote argv elements itself.
            if os.name == "nt":
                argv = [subprocess.list2cmdline([arg]) for arg in argv]
            os.execv(sys.executable, argv)
        except OSError:
            _log.exception("eventmem daemon: restart failed after shutdown")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
