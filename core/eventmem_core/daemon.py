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
import datetime
import json
import logging
import math
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
from . import candidate_backfill
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
        self._config_lock = threading.RLock()
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

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "pid": os.getpid(),
            "db": self.settings.db_path,
            "schema": SCHEMA_VERSION,
            "uptime": round(time.time() - self.started, 1),
            "packs_dir": self.packs_root,
            "backends": [p.name for p in self.packs()],
            "paused": self._paused,
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
