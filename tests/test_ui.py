#!/usr/bin/env python3
"""eventmem P4 tests — the GUI: static page, settings round-trip, packs over HTTP.

Run directly:  <python> tests/test_ui.py

The daemon runs for real (a ``ThreadingHTTPServer`` on port 0) and the tests talk HTTP, not
Python internals — the page has no other client, so the routes are the contract.  The one
exception is ``install``: downloading 2 GB in a unit test would be silly, so the job test
replaces ``install_mod.install`` with a fake that reports progress and waits to be released.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for entry in (REPO / "core", REPO / "adapters" / "hermes"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

os.environ.setdefault("EVENTMEM_LANG", "zh")

import eventmem_core.daemon as daemon_mod  # noqa: E402
import eventmem_core.decision as decision  # noqa: E402
import eventmem_core.install as install_mod  # noqa: E402
import eventmem_core.packs as packs  # noqa: E402
import eventmem_core.ui_strings as ui_strings  # noqa: E402
from eventmem_core.settings import CoreSettings, DecisionPolicy  # noqa: E402
from eventmem_core.store import Store  # noqa: E402
from eventmem.settings import FileStore, build as build_settings  # noqa: E402

CASES: list = []

CLOUD = """\
# 云端，注释要活下来
protocol: systemone
model: jev-latest
base_url: https://api.typesafe.ai
api_key_env: TYPESAFE_API_KEY
license: proprietary
"""


def case(fn):
    CASES.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_eq(got, want, msg):
    if got != want:
        raise AssertionError(f"{msg}: got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- helpers

def fresh_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


def settings_for(db: str, packs_dir: str, backends: tuple = ()) -> CoreSettings:
    from dataclasses import replace

    return replace(CoreSettings(db_path=db),
                   decision=replace(DecisionPolicy(), url="", packs_dir=packs_dir,
                                    backends=backends))


class Live:
    """A daemon's HTTP server on an ephemeral port, with a JSON-first client."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.server = None
        self.thread = None

    def __enter__(self):
        self.server = daemon_mod.make_server(self.daemon, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.05)
        return self

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def _open(self, path, method="GET", body=None, raw_body=None):
        data = None
        headers = {}
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    def get_json(self, path):
        status, _ctype, raw = self._open(path)
        return status, json.loads(raw.decode("utf-8"))

    def post(self, path, body=None, *, raw_body=None):
        status, _ctype, raw = self._open(path, "POST", body, raw_body)
        return status, json.loads(raw.decode("utf-8"))

    def get_raw(self, path):
        return self._open(path)

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        return False


def home_with_packs(tmp: str) -> tuple[str, str]:
    """A fake HERMES_HOME: ``<tmp>/eventmem/{settings.yaml,backends}``."""
    hermes = Path(tmp)
    packs_dir = hermes / "eventmem" / "backends"
    packs_dir.mkdir(parents=True, exist_ok=True)
    return str(hermes), str(packs_dir)


# --------------------------------------------------------------------------- static page

@case
def test_index_is_the_gui():
    tmp = tempfile.mkdtemp(prefix="eventmem-ui-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir))) as live:
            status, ctype, raw = live.get_raw("/")
            check_eq(status, 200, "index")
            check("text/html" in ctype, f"html content type, got {ctype}")
            body = raw.decode("utf-8")
            check("eventmem" in body and "调度" in body, "the page itself")

            status, ctype, raw = live.get_raw("/ui/app.js")
            check_eq(status, 200, "app.js")
            check("javascript" in ctype, f"js content type, got {ctype}")
            check(b"refreshOverview" in raw, "the real script, not a stub")

            status, ctype, _ = live.get_raw("/style.css")
            check_eq(status, 200, "style.css")
            check("css" in ctype, "css content type")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_static_stays_inside_the_ui_dir():
    tmp = tempfile.mkdtemp(prefix="eventmem-ui2-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir))) as live:
            for path in ("/../core/eventmem_core/settings.py", "/ui/../../settings.py",
                         "/etc/passwd", "/nope.html"):
                status, _ctype, _raw = live.get_raw(path)
                check_eq(status, 404, f"{path} must not be served")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


# --------------------------------------------------------------------------- settings

@case
def test_config_view_without_a_store_is_honest():
    tmp = tempfile.mkdtemp(prefix="eventmem-cfg-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir))) as live:
            status, body = live.get_json("/api/config")
            check_eq(status, 200, "GET config")
            check_eq(body["writable"], False, "no store -> not writable")
            check_eq(body["file"], "", "no file to name")
            check_eq(body["effective"]["edges"]["margin"], 0.15, "defaults are visible")

            status, body = live.post("/api/config", {"edges": {"margin": 0.2}})
            check_eq(status, 501, "writing without a store is refused, not faked")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_file_store_round_trip_and_merge():
    tmp = tempfile.mkdtemp(prefix="eventmem-store-")
    try:
        store = FileStore(tmp)
        settings = store.write({"edges": {"heads_vec_top_k": 7}})
        check_eq(settings.edges.heads_vec_top_k, 7, "applied in memory")
        saved = Path(store.path)
        check(saved.exists(), f"settings.yaml written to {saved}")
        check("heads_vec_top_k: 7" in saved.read_text(encoding="utf-8"), "yaml on disk")

        store.write({"decision": {"backends": ["cloud", "local"]}})
        reread = build_settings(tmp)
        check_eq(reread.edges.heads_vec_top_k, 7, "second write merged, not replaced")
        check_eq(tuple(reread.decision.backends), ("cloud", "local"), "chain saved")

        store.write({"edges": {"heads_vec_top_k": None}})
        check_eq(build_settings(tmp).edges.heads_vec_top_k, 10, "null deletes the override")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_file_store_refuses_a_bad_value_and_leaves_the_file_alone():
    tmp = tempfile.mkdtemp(prefix="eventmem-store2-")
    try:
        store = FileStore(tmp)
        store.write({"edges": {"margin": 0.2}})
        before = Path(store.path).read_bytes()

        seen = ""
        try:
            store.write({"edges": {"margin": "很松"}})
        except ValueError as exc:
            seen = str(exc)
        check("margin" in seen, f"the message names the key: {seen!r}")

        check_eq(Path(store.path).read_bytes(), before, "the file did not change")
        check_eq(build_settings(tmp).edges.margin, 0.2, "and the old value still wins")

        seen = ""
        try:
            store.write({"edges": {"no_such_knob": 1}})
        except ValueError as exc:
            seen = str(exc)
        check("no_such_knob" in seen, f"unknown keys are refused too: {seen!r}")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_gui_config_change_applies_live():
    tmp = tempfile.mkdtemp(prefix="eventmem-live-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        store = FileStore(home)
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir), settings_store=store)
        with Live(daemon) as live:
            status, body = live.post("/api/config", {"edges": {"heads_vec_top_k": 4}})
            check_eq(status, 200, "POST config")
            check_eq(body["effective"]["edges"]["heads_vec_top_k"], 4, "response shows the new value")
            check_eq(daemon.settings.edges.heads_vec_top_k, 4, "no restart needed")

            status, body = live.post("/api/config", {"edges": {"heads_vec_top_k": 0}})
            check_eq(status, 400, "a value the validator rejects is a 400")
            check_eq(daemon.settings.edges.heads_vec_top_k, 4, "and changes nothing")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


# --------------------------------------------------------------------------- packs

@case
def test_create_pack_from_text():
    tmp = tempfile.mkdtemp(prefix="eventmem-pack-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir),
                                    settings_store=FileStore(home))) as live:
            status, body = live.post("/api/packs", {"name": "typesafe", "text": CLOUD})
            check_eq(status, 200, f"create: {body}")
            check_eq(body["pack"]["kind"], "remote", "a base_url without external is remote")
            on_disk = Path(packs_dir) / "typesafe" / "config.yaml"
            check_eq(on_disk.read_text(encoding="utf-8"), CLOUD.rstrip() + "\n",
                     "the text is the file")

            status, body = live.get_json("/api/packs")
            check_eq(status, 200, "GET packs")
            check_eq([p["name"] for p in body["packs"]], ["typesafe"], "listed")
            check("注释要活下来" in body["packs"][0]["config"], "comments travel to the GUI")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_create_pack_rejects_traversal_duplicates_and_bad_yaml():
    tmp = tempfile.mkdtemp(prefix="eventmem-pack2-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir),
                                    settings_store=FileStore(home))) as live:
            for bad_name in ("../escape", "a/b", ".hidden", ""):
                status, body = live.post("/api/packs", {"name": bad_name, "text": CLOUD})
                check_eq(status, 400, f"name {bad_name!r} refused: {body}")
            check_eq(Path(tmp, "escape").exists(), False, "nothing escaped the pack dir")

            status, body = live.post("/api/packs", {"name": "x", "text": "protocol: nope\n"})
            check_eq(status, 400, "unknown protocol refused")
            check("systemone" in body["error"], f"and says what is allowed: {body}")

            status, _ = live.post("/api/packs", {"name": "ok", "text": CLOUD})
            check_eq(status, 200, "first create works")
            status, body = live.post("/api/packs", {"name": "ok", "text": CLOUD})
            check_eq(status, 400, "the second one does not")
            check("已经" in body["error"], f"and explains why: {body}")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_save_pack_keeps_comments_and_updates_the_pack():
    tmp = tempfile.mkdtemp(prefix="eventmem-pack3-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir),
                                    settings_store=FileStore(home))) as live:
            live.post("/api/packs", {"name": "typesafe", "text": CLOUD})
            edited = CLOUD.replace("model: jev-latest", "model: jev-1.14") + "note: 涨价了\n"
            status, body = live.post("/api/packs/typesafe/config", {"text": edited})
            check_eq(status, 200, f"save: {body}")
            check_eq(body["pack"]["model"], "jev-1.14", "the new model is live")
            text = (Path(packs_dir) / "typesafe" / "config.yaml").read_text(encoding="utf-8")
            check("# 云端，注释要活下来" in text, "comments survived the round-trip")
            check("note: 涨价了" in text, "and so did the new line")

            status, _ = live.post("/api/packs/nope/config", {"text": CLOUD})
            check_eq(status, 404, "saving a pack that does not exist")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_delete_pack_refuses_while_in_chain_then_forces():
    tmp = tempfile.mkdtemp(prefix="eventmem-pack4-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir, backends=("typesafe",)),
                                    settings_store=FileStore(home))) as live:
            live.post("/api/packs", {"name": "typesafe", "text": CLOUD})
            status, body = live.post("/api/packs/typesafe/delete", {})
            check_eq(status, 409, f"in the chain: {body}")
            check((Path(packs_dir) / "typesafe").is_dir(), "still there")

            status, body = live.post("/api/packs/typesafe/delete", {"force": True})
            check_eq(status, 200, f"forced delete: {body}")
            check_eq((Path(packs_dir) / "typesafe").exists(), False, "gone")

            status, _ = live.post("/api/packs/typesafe/delete", {})
            check_eq(status, 404, "deleting twice")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_acceptance_add_a_cloud_backend_and_make_it_default():
    """P4's acceptance criterion: nothing but HTTP — no file editing, no restart."""
    tmp = tempfile.mkdtemp(prefix="eventmem-accept-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir), settings_store=FileStore(home))
        with Live(daemon) as live:
            status, _ = live.post("/api/packs", {"name": "typesafe", "text": CLOUD})
            check_eq(status, 200, "1. add the backend")

            status, body = live.post("/api/config",
                                     {"decision": {"backends": ["typesafe", "startlux"]}})
            check_eq(status, 200, f"2. make it default: {body}")
            check_eq(body["effective"]["decision"]["backends"], ["typesafe", "startlux"],
                     "chain as saved")

        chain = decision.from_settings(build_settings(home))
        check_eq(chain.providers[0].name, "typesafe", "the new backend answers first")
        check_eq(Path(home, "eventmem", "settings.yaml").exists(), True, "persisted")
        check_eq(Path(packs_dir, "typesafe", "config.yaml").exists(), True, "pack persisted")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_saving_the_acceptance_rules_governs_the_next_pass():
    """The 关系接受条件 card has its own save, and it has to reach the running pass.

    ``margin`` and the five minimum confidences are what turn a judged pair into an edge.
    A save that only landed in settings.yaml would not change anything until a restart, so
    the daemon swaps its live settings inside the same call — that object is what
    ``run_once`` reads.
    """
    tmp = tempfile.mkdtemp(prefix="eventmem-acceptrules-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir), settings_store=FileStore(home))
        with Live(daemon) as live:
            status, body = live.post("/api/config", {"edges": {
                "margin": 0.42, "min_conf_change": 0.31, "min_conf_negate": 0.32,
                "min_conf_repeat": 0.33, "min_conf_because": 0.34, "min_conf_refine": 0.35}})
            check_eq(status, 200, f"save: {body}")
            saved = body["effective"]["edges"]
            check_eq(saved["margin"], 0.42, "margin saved")
            check_eq(saved["min_conf_refine"], 0.35, "and the per-relation floors")
            check_eq(daemon.settings.edges.margin, 0.42, "the next pass reads the live object")
            check_eq(daemon.settings.edges.min_conf_negate, 0.32, "every floor, not just the first")
            check_eq(saved["heads_vec_top_k"], 10, "an acceptance save leaves candidates alone")

            status, body = live.post("/api/config", {"edges": {"margin": 0.5}})
            check_eq(status, 200, "a partial save merges")
            check_eq(body["effective"]["edges"]["min_conf_change"], 0.31, "other floors kept")
            check_eq(body["effective"]["edges"]["margin"], 0.5, "and the new margin applied")

        check_eq(build_settings(home).edges.margin, 0.5, "persisted for the next process")
        check_eq(build_settings(home).edges.min_conf_repeat, 0.33, "whole block persisted")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


# --------------------------------------------------------------------------- models + job

@case
def test_models_view_reports_what_is_installed():
    tmp = tempfile.mkdtemp(prefix="eventmem-models-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        root = Path(home) / "eventmem"
        (root / "models" / "lev").mkdir(parents=True)
        (root / "models" / "lev" / "lev-Q4_K_M.gguf").write_bytes(b"x" * 1000)
        (root / "runtimes" / "llama.cpp").mkdir(parents=True)
        (root / "runtimes" / "llama.cpp" / "current.json").write_text(json.dumps(
            {"build": "b12284", "binary": "/opt/llama/llama-server", "asset": "x.tar.gz"}),
            encoding="utf-8")
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir))) as live:
            status, body = live.get_json("/api/models")
            check_eq(status, 200, "GET models")
            check_eq(body["llama"]["build"], "b12284", "runtime build")
            check_eq([m["file"] for m in body["models"]], ["lev-Q4_K_M.gguf"], "the model")
            check_eq(body["models"][0]["size"], 1000, "size in bytes")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_install_job_reports_progress_and_blocks_a_second_one():
    tmp = tempfile.mkdtemp(prefix="eventmem-job-")
    db = fresh_db()
    release = threading.Event()
    calls = []
    original = install_mod.install

    def fake_install(paths, name, **kwargs):
        calls.append((name, kwargs))
        install_mod._progress("download", 100, 400)
        release.wait(timeout=5)
        install_mod._progress("download", 400, 400, done_flag=True)
        return {"name": name, "model": {"file": "lev.gguf"}}

    install_mod.install = fake_install
    try:
        home, packs_dir = home_with_packs(tmp)
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir),
                                    settings_store=FileStore(home))) as live:
            status, body = live.post("/api/install", {"name": "lev", "quant": "Q4_K_M"})
            check_eq(status, 202, f"accepted: {body}")
            check_eq(body["job"]["state"], "running", "it is a job, not a blocking call")

            status, body = live.post("/api/install", {"name": "lev"})
            check_eq(status, 409, "one install at a time")

            deadline = time.time() + 5
            job = None
            while time.time() < deadline:
                _status, body = live.get_json("/api/job")
                job = body["job"]
                if job["state"] != "running":
                    break
                time.sleep(0.02)
            release.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                _status, body = live.get_json("/api/job")
                job = body["job"]
                if job["state"] != "running":
                    break
                time.sleep(0.02)
            check_eq(job["state"], "done", f"job finished: {job}")
            check_eq(job["result"]["model"]["file"], "lev.gguf", "result carried through")
            check_eq(job["done"], 400, "the progress bar saw the last byte")
            check_eq(calls[0][0], "lev", "installed under the requested name")
    finally:
        install_mod.install = original
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


# --------------------------------------------------------------------------- stats + logs

@case
def test_stats_counts_events_and_edges():
    tmp = tempfile.mkdtemp(prefix="eventmem-stats-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        store = Store(db)
        try:
            store.add_project("eventmem")
            a = store.add_event("决定", "我先看看别人怎么做的", project="eventmem")
            b = store.add_event("结果", "实测比推断靠谱", project="eventmem")
            store.add_event("教训", "没有项目也算", project=None)
            store.add_edge(a, b, "因为", 0.9, "jev", probs={"因为": 0.9})
        finally:
            store.close()
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir))) as live:
            status, body = live.get_json("/api/stats")
            check_eq(status, 200, "GET stats")
            check_eq(body["events"], 3, "three events")
            check_eq(body["edges"], 1, "one edge")
            check_eq(body["kinds"]["决定"], 1, "kinds histogram")
            names = [p["name"] for p in body["projects"]]
            check("eventmem" in names, f"projects: {names}")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_extraction_failures_surface_on_the_overview_and_can_be_cleared():
    """抽取失败要出现在概览上（不是只进日志），并且能一键清除。"""
    tmp = tempfile.mkdtemp(prefix="eventmem-notice-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        store = Store(db)
        try:
            store.add_extract_failure("call_failed", "APITimeoutError: 60s",
                                      session="sess-9", ts=1000.0)
            store.add_extract_failure("unparsable", "这不是 JSON", session="sess-9", ts=2000.0)
        finally:
            store.close()
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir))) as live:
            status, body = live.get_json("/api/stats")
            check_eq(status, 200, "GET stats")
            check_eq(body["failures"]["count"], 2, "两次失败")
            check_eq([r["reason"] for r in body["failures"]["last"]],
                     ["unparsable", "call_failed"], "新→旧")
            check_eq(body["events"], 0, "失败没有写成事件")

            status, body = live.post("/api/notices/clear")
            check_eq(status, 200, "POST notices/clear")
            check_eq(body["cleared"], 2, "清掉两条")

            status, body = live.get_json("/api/stats")
            check_eq(body["failures"]["count"], 0, "清完计数归零")
            check_eq(body["failures"]["last"], [], "列表也空")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_clear_edges_wipes_edges_pairs_and_cursor():
    """批量删边：边 + 判别账本一起删、游标归零，事件不动。"""
    tmp = tempfile.mkdtemp(prefix="eventmem-clear-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        store = Store(db)
        try:
            a = store.add_event("决定", "一")
            b = store.add_event("结果", "二", ts=time.time() + 60)
            store.add_edge(b, a, "因为", 0.9, "jev", probs={"因为": 0.9})
            store.add_pair_eval(b, a,
                                axis_probs={"重复": 0.0, "改变": 0.1, "否定": 0.1, "都不是": 0.8},
                                detail_probs={"因为": 0.9, "细化": 0.1}, detail_label="因为")
            store.set_meta("nightly_since_ts", "123.0")
        finally:
            store.close()
        with Live(daemon_mod.Daemon(settings_for(db, packs_dir))) as live:
            status, body = live.post("/api/edges/clear")
            check_eq(status, 200, "POST edges/clear")
            check_eq(body["edges"], 1, "one edge deleted")
            check_eq(body["pairs"], 1, "one pair record deleted")
        store = Store(db)
        try:
            check_eq(store._db.execute("select count(*) from edges").fetchone()[0], 0,
                     "no edges left")
            check_eq(store._db.execute("select count(*) from pair_evals").fetchone()[0], 0,
                     "the evaluated-pairs ledger is what makes a replay possible")
            check_eq(store.get_meta("nightly_since_ts"), "0", "cursor reset to the beginning")
            check_eq(store._db.execute("select count(*) from events").fetchone()[0], 2,
                     "events are untouched")
        finally:
            store.close()
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_logs_ring_and_bad_json():
    tmp = tempfile.mkdtemp(prefix="eventmem-logs-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir))
        import logging

        logging.getLogger("eventmem_core.daemon").warning("eventmem test: 一句话")
        with Live(daemon) as live:
            status, body = live.get_json("/api/logs")
            check_eq(status, 200, "GET logs")
            check(any("一句话" in line["message"] for line in body["logs"]),
                  f"the daemon's own log lands in the ring: {body['logs'][-3:]}")

            status, body = live.post("/api/config", raw_body=b"{not json")
            check_eq(status, 400, "bad JSON is a 400, not a traceback")
            check("JSON" in body["error"], f"and says so: {body}")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_i18n_serves_the_dictionary_only_in_english():
    """One map, keyed by the Chinese source string: Chinese needs no map at all.

    The same dictionary feeds the desktop plugin, so there is exactly one place to add a
    language — and the desktop smoke test renders the console in English against it.
    """
    tmp = tempfile.mkdtemp(prefix="eventmem-i18n-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        settings = settings_for(db, packs_dir)
        daemon = daemon_mod.Daemon(settings)
        with Live(daemon) as live:
            status, body = live.get_json("/api/i18n")
            check_eq(status, 200, "route exists")
            check_eq(body["lang"], "zh", "language comes from the settings")
            check_eq(body["map"], {}, "Chinese is the source text: no map needed")

        settings = dataclasses.replace(settings, language="en")
        with Live(daemon_mod.Daemon(settings)) as live:
            status, body = live.get_json("/api/i18n")
            check_eq(body["lang"], "en", "english")
            check_eq(body["map"].get("跑一轮"), "Run a pass", "and the map is the real dictionary")
            check(len(body["map"]) > 100, "the whole console, not a sample")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_every_translated_literal_has_a_dictionary_entry():
    """A new ``t('…')`` without an entry would ship Chinese to an English user, silently.

    This is the guard that makes the migration stick: the console has no build step, so
    nothing else would notice.
    """
    path = REPO / "adapters" / "hermes" / "desktop-plugins" / "eventmem-gui" / "plugin.js"
    text = path.read_text(encoding="utf-8")
    cjk = re.compile(r"[\u4e00-\u9fff]")
    missing = []
    for call in ("t(", "fmt("):
        for literal in re.findall(re.escape(call) + r"'((?:[^'\\]|\\.)*)'", text):
            if not cjk.search(literal):
                continue
            key = literal.replace("\\n", "\n")
            if key not in ui_strings.EN:
                missing.append(f"{call}… {key[:40]}")
    check_eq(missing, [], "every t('…')/fmt('…') key exists in ui_strings.EN")
    check(len(ui_strings.EN) > 100, "and the dictionary is not a stub")


@case
def test_stop_and_switch_are_cooperative():
    """Stop cancels *this* pass; switch also wakes the poll so the chain is probed again.

    That pair of buttons is why a backend that comes up mid-round (LM Studio finishing its
    load) does not have to wait out a 30-minute pass on the slow one.
    """
    tmp = tempfile.mkdtemp(prefix="eventmem-stop-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir))
        with Live(daemon) as live:
            status, body = live.get_json("/api/status")
            check_eq(status, 200, "status")
            check_eq(body["busy"], False, "idle at rest")
            check_eq(body["live"], None, "no live counter when nothing runs")
            check_eq(body["stopping"], False, "not stopping")

            status, body = live.post("/api/stop", {})
            check_eq(status, 200, "stop answers")
            check_eq(body, {"ok": True, "stopping": False}, "nothing was running, but the flag is set")
            check_eq(daemon._abort.is_set(), True, "so the next pass notices immediately")

            daemon._abort.clear()
            daemon._down["win-autothink"] = time.time()
            status, body = live.post("/api/switch", {})
            check_eq(status, 200, "switch answers")
            check_eq(daemon._abort.is_set(), True, "switch also stops the pass")
            check_eq(daemon._wake.is_set(), True, "and wakes the poll for a fresh probe")
            check_eq(daemon._down, {}, "and clears the cooldown: the operator changed something")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_status_carries_the_live_counter():
    """``/api/status.live`` is what the console animates; the daemon owns the numbers."""
    tmp = tempfile.mkdtemp(prefix="eventmem-live-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir))
        seen = {}

        def fake_run_once(settings, **kwargs):
            kwargs["on_progress"]({"pairs": 4, "edges": 3, "backend": "win-autothink"})
            seen["live"] = dict(daemon._live or {})
            seen["stop"] = kwargs["should_stop"]()
            return {"pairs": 4, "edges": 3, "backend": "win-autothink"}

        original = daemon_mod.W.run_once
        daemon_mod.W.run_once = fake_run_once
        try:
            result = daemon.run_pass()
        finally:
            daemon_mod.W.run_once = original

        check_eq(result["backend"], "win-autothink", "the pass result")
        check_eq(seen["live"].get("pairs"), 4, "the counter was readable mid-pass")
        check_eq(seen["live"].get("backend"), "win-autothink", "with the backend in use")
        check_eq(seen["stop"], False, "and the stop flag was clear")
        check_eq(daemon._live, None, "cleared when the pass ends")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_a_backend_that_probes_but_fails_decisions_is_skipped():
    """A probe only proves the port answers.

    LM Studio will list a model it refuses to serve (a ``vlm`` on ``/v1/systemone``), and a
    keyed endpoint can answer 200 and then reject every question.  Without a cooldown the
    chain picks that backend every round and never advances the cursor.
    """
    tmp = tempfile.mkdtemp(prefix="eventmem-down-")
    db = fresh_db()
    try:
        _home, packs_dir = home_with_packs(tmp)
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir))
        calls = []

        def failing(settings, **kwargs):
            calls.append(kwargs)
            return {"pairs": 3, "edges": 0, "backend": "win-autothink", "skipped": "jev-error"}

        original = daemon_mod.W.run_once
        daemon_mod.W.run_once = failing
        try:
            daemon.run_pass()
            check("win-autothink" in daemon._down, f"down: remembered {daemon._down!r}")
            check_eq(calls[-1].get("avoid"), (), "the first pass tried it")
            daemon.run_pass()
            check_eq(calls[-1].get("avoid"), ("win-autothink",), "the next pass avoids it")

            # A backend that starts working again is trusted immediately.
            daemon_mod.W.run_once = lambda settings, **kw: {
                "pairs": 1, "edges": 1, "backend": "win-autothink", "skipped": None}
            daemon.run_pass()
            check_eq(daemon._down, {}, "success clears the mark")
            check_eq(calls[-1].get("avoid"), ("win-autothink",),
                     "the recovering pass still skipped it once")
        finally:
            daemon_mod.W.run_once = original
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


@case
def test_from_settings_honours_avoid():
    tmp = tempfile.mkdtemp(prefix="eventmem-avoid-")
    db = fresh_db()
    try:
        home, packs_dir = home_with_packs(tmp)
        for name in ("one", "two"):
            folder = Path(packs_dir) / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "config.yaml").write_text(
                "protocol: systemone\nmodel: jev\nexternal: true\n"
                "base_url: http://127.0.0.1:9\n", encoding="utf-8")
        settings = dataclasses.replace(settings_for(db, packs_dir), decision=DecisionPolicy(
            backends=("one", "two"), packs_dir=packs_dir, url=""))
        chain = decision.from_settings(settings)
        check_eq([p.name for p in chain.providers], ["one", "two"], "both, in order")
        chain = decision.from_settings(settings, avoid=("one",))
        check_eq([p.name for p in chain.providers], ["two"], "avoid drops the first")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        Path(db).unlink(missing_ok=True)


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
