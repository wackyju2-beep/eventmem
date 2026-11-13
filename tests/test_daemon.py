#!/usr/bin/env python3
"""eventmem P2 tests — the daemon: single instance, HTTP management, process supervision.

Run directly:  <python> tests/test_daemon.py

The daemon runs for real in these tests (a real ``ThreadingHTTPServer`` on port 0, real
HTTP calls) and so does the supervised process (a tiny python http server written to a temp
dir).  Nothing is mocked except ``runtime.start`` in the one test that is about *which*
packs we are willing to start.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

os.environ.setdefault("EVENTMEM_LANG", "zh")

import eventmem_core.daemon as daemon_mod  # noqa: E402
import eventmem_core.decision as decision  # noqa: E402
import eventmem_core.packs as packs  # noqa: E402
import eventmem_core.runtime as runtime  # noqa: E402
import eventmem_core.worker as worker  # noqa: E402
from eventmem_core.settings import CoreSettings, DecisionPolicy  # noqa: E402
from eventmem_core.store import Store  # noqa: E402

CASES: list = []

FAKE_SERVER = '''\
import http.server, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


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
    os.unlink(path)  # Store() creates it
    return path


def remove(path: str) -> None:
    """Clean-up that tolerates a db that was never opened."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def settings_for(db: str, *, packs_dir: str = "", backends: tuple = (),
                 url: str = "") -> CoreSettings:
    from dataclasses import replace

    return replace(
        CoreSettings(db_path=db),
        decision=replace(DecisionPolicy(), url=url, packs_dir=packs_dir, backends=backends),
    )


def make_pack(root: Path, name: str, body: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(body, encoding="utf-8")
    return folder


class running_server:
    """Start a daemon's HTTP server on an ephemeral port for one test."""

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

    def request(self, path: str, *, method: str = "GET"):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 4xx/5xx come back as an exception
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        return False


# --------------------------------------------------------------------------- lock

@case
def test_one_daemon_per_database():
    tmp = tempfile.mkdtemp(prefix="eventmem-lock-")
    try:
        path = os.path.join(tmp, "events.db.daemon.lock")
        first = daemon_mod.acquire_lock(path)
        check(first is not None, "first lock")
        check_eq(daemon_mod.acquire_lock(path), None, "second attempt must fail")
        os.close(first)
        second = daemon_mod.acquire_lock(path)
        check(second is not None, "the lock is released when the fd closes")
        os.close(second)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


@case
def test_no_flock_is_not_the_same_as_locked():
    """Windows has no ``fcntl``: that means "run unguarded", not "someone else owns it".

    Conflating the two made the daemon exit 1 on Windows before it ever bound a port.
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("no fcntl on this platform")
        return real_import(name, *args, **kwargs)

    tmp = tempfile.mkdtemp(prefix="eventmem-nolock-")
    try:
        builtins.__import__ = fake_import
        try:
            got = daemon_mod.acquire_lock(os.path.join(tmp, "events.db.daemon.lock"))
        finally:
            builtins.__import__ = real_import
        check(got is daemon_mod.NO_GUARD, f"got {got!r}, want NO_GUARD")
        check(got is not None, "and main() must not read it as 'already held'")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- http api

@case
def test_health_and_status_endpoints():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        with running_server(daemon) as api:
            status, body = api.request("/api/health")
            check_eq(status, 200, "health status")
            check_eq(body["status"], "ok", "health body")
            check_eq(body["db"], db, "db path echoed")
            check_eq(body["schema"], 9, "schema version")

            status, body = api.request("/api/status")
            check_eq(status, 200, "status code")
            check("cursor" in body and "runs" in body, f"status shape: {sorted(body)}")
    finally:
        remove(db)


@case
def test_unknown_route_is_404():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        with running_server(daemon) as api:
            check_eq(api.request("/api/nope")[0], 404, "unknown path")
            check_eq(api.request("/api/status", method="POST")[0], 404, "wrong method")
    finally:
        remove(db)


@case
def test_run_once_is_409_while_a_pass_is_running():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        check(daemon._pass_lock.acquire(blocking=False), "hold the pass lock")
        try:
            with running_server(daemon) as api:
                status, body = api.request("/api/run-once", method="POST")
            check_eq(status, 409, "busy")
            check_eq(body["skipped"], "busy", "reason")
        finally:
            daemon._pass_lock.release()
    finally:
        remove(db)


@case
def test_run_once_on_an_idle_db_does_nothing():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        with running_server(daemon) as api:
            status, body = api.request("/api/run-once", method="POST")
        check_eq(status, 200, "ok")
        check_eq(body["skipped"], "idle", "no events -> idle")
        check_eq([r["skipped"] for r in daemon._runs], ["idle"], "the run is remembered")
    finally:
        remove(db)


@case
def test_backends_listing_shows_process_state():
    root = Path(tempfile.mkdtemp(prefix="eventmem-daemon-packs-"))
    db = fresh_db()
    try:
        make_pack(root, "startlux", "protocol: systemone\nweights: model.gguf\nport: 0\n")
        make_pack(root, "lmstudio",
                  "protocol: systemone\nmodel: lev\nbase_url: http://127.0.0.1:1234\n"
                  "external: true\n")
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir=str(root), backends=("startlux",)))
        with running_server(daemon) as api:
            status, body = api.request("/api/backends")
        check_eq(status, 200, "ok")
        by_name = {entry["name"]: entry for entry in body["backends"]}
        check_eq(sorted(by_name), ["lmstudio", "startlux"], "both packs listed")
        check_eq(by_name["startlux"]["running"], False, "nothing started yet")
        check_eq(by_name["startlux"]["in_chain"], True, "listed in decision.backends")
        check_eq(by_name["lmstudio"]["in_chain"], False, "not in the chain")
        check_eq(by_name["lmstudio"]["running"], None, "external is not ours to run")
    finally:
        remove(db)
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_probe_says_not_running_instead_of_failing():
    root = Path(tempfile.mkdtemp(prefix="eventmem-probe-"))
    db = fresh_db()
    try:
        make_pack(root, "startlux", "protocol: systemone\nweights: model.gguf\n")
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir=str(root), backends=("startlux",)))
        with running_server(daemon) as api:
            status, body = api.request("/api/backends/startlux/probe", method="POST")
            check_eq(status, 200, "probe is not an error")
            check_eq(body["ok"], False, "not up")
            check_eq(body["reason"], "not-running", "and it says why")
            check_eq(api.request("/api/backends/ghost/probe", method="POST")[0], 404, "unknown")
    finally:
        remove(db)
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_only_self_packs_can_be_started_or_stopped():
    root = Path(tempfile.mkdtemp(prefix="eventmem-start-"))
    db = fresh_db()
    try:
        make_pack(root, "lmstudio",
                  "protocol: systemone\nmodel: lev\nbase_url: http://127.0.0.1:1234\n"
                  "external: true\n")
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir=str(root)))
        with running_server(daemon) as api:
            check_eq(api.request("/api/backends/lmstudio/start", method="POST")[0], 400,
                     "someone else's server is not ours to launch")
            check_eq(api.request("/api/backends/lmstudio/stop", method="POST")[0], 400,
                     "nor to stop")
    finally:
        remove(db)
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- runtime

@case
def test_runtime_starts_waits_and_stops_a_real_process():
    root = Path(tempfile.mkdtemp(prefix="eventmem-runtime-"))
    try:
        script = root / "fake_server.py"
        script.write_text(FAKE_SERVER, encoding="utf-8")
        port = runtime.free_port()
        folder = make_pack(root, "selfie",
                           "protocol: systemone\nweights: model.gguf\n"
                           f"port: {port}\n"
                           f"launch: {sys.executable} {script}\n"
                           "args: [--port, '{port}']\n")
        (folder / "model.gguf").write_bytes(b"x")
        pack = packs.load_pack(folder)

        state = runtime.start(pack, probe=lambda: _probe_port(port), timeout=15)
        check(runtime.alive(state["pid"]), "process is alive")
        check_eq(state["port"], port, "the configured port is used")
        check(runtime.state_path(pack).exists(), ".runtime.json written")
        check_eq(runtime.read_state(pack)["pid"], state["pid"], "state round-trips")

        check_eq(runtime.stop(pack), True, "stop reports success")
        time.sleep(0.3)
        check_eq(runtime.alive(state["pid"]), False, "process is gone")
        check_eq(runtime.read_state(pack), None, "state cleared")
        check_eq(runtime.stop(pack), False, "stopping nothing is not an error")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def _probe_port(port: int) -> bool:
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - not up yet
        return False


@case
def test_runtime_auto_without_a_binary_explains_what_to_do():
    root = Path(tempfile.mkdtemp(prefix="eventmem-auto-"))
    try:
        folder = make_pack(root, "startlux", "protocol: systemone\nweights: model.gguf\n")
        (folder / "model.gguf").write_bytes(b"x")
        pack = packs.load_pack(folder)
        seen = ""
        try:
            runtime.resolve_command(pack, 1234)
        except RuntimeError as exc:
            seen = str(exc)
        check("EVENTMEM_LLAMA_SERVER" in seen, f"says what to set: {seen!r}")
        check("P3" in seen, "and that the downloader is not built yet")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_runtime_resolves_port_and_weights_placeholders():
    root = Path(tempfile.mkdtemp(prefix="eventmem-args-"))
    try:
        folder = make_pack(root, "x", "protocol: systemone\nweights: model.gguf\n"
                                     "launch: my-server\n"
                                     "args: [-m, '{weights}', --port, '{port}', --dir, '{dir}']\n")
        pack = packs.load_pack(folder)
        command = runtime.resolve_command(pack, 18090)
        check_eq(command[0], "my-server", "binary")
        check_eq(command[1:3], ["-m", str(folder / "model.gguf")], "weights substituted")
        check("18090" in command, "port substituted")
        check(str(folder) in command, "dir substituted")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_from_pack_finds_the_running_self_pack():
    root = Path(tempfile.mkdtemp(prefix="eventmem-frompack-"))
    try:
        folder = make_pack(root, "selfie", "protocol: systemone\nweights: model.gguf\n")
        (folder / "model.gguf").write_bytes(b"x")
        pack = packs.load_pack(folder)

        check_eq(decision.from_pack(pack), None, "not running -> not in the chain")

        runtime.write_state(pack, {"pid": os.getpid(), "port": 45678, "started": time.time(),
                                   "command": ["x"]})
        provider = decision.from_pack(pack)
        check(provider is not None, "running -> provider")
        check_eq(provider.endpoint.base_url, "http://127.0.0.1:45678", "url from the state file")

        runtime.write_state(pack, {"pid": 999_999_999, "port": 45678, "started": 0,
                                   "command": ["x"]})
        check_eq(decision.from_pack(pack), None, "dead pid -> not in the chain")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_prepare_only_touches_packs_in_the_chain():
    root = Path(tempfile.mkdtemp(prefix="eventmem-prepare-"))
    db = fresh_db()
    try:
        for name in ("listed", "unlisted"):
            folder = make_pack(root, name, "protocol: systemone\nweights: model.gguf\n")
            (folder / "model.gguf").write_bytes(b"x")
        daemon = daemon_mod.Daemon(settings_for(db, packs_dir=str(root), backends=("listed",)))

        started: list[str] = []
        original = runtime.start
        runtime.start = lambda pack, **kw: started.append(pack.name) or {}
        try:
            daemon.prepare(daemon.settings)
        finally:
            runtime.start = original
        check_eq(started, ["listed"], "only the pack named in decision.backends is launched")
    finally:
        remove(db)
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- pass wiring

@case
def test_prepare_runs_before_the_chain_but_only_when_there_is_work():
    db = fresh_db()
    try:
        store = Store(db)
        store.add_event("决定", "一条事件")
        store.close()

        seen: list[str] = []

        def prepare(_settings):
            seen.append("prepare")

        cfg = settings_for(db, url="")  # empty url -> the pass stops at jev-unavailable
        result = worker.run_once(cfg, prepare=prepare)
        check_eq(seen, ["prepare"], "called once when there is work")
        check_eq(result.get("skipped"), "jev-unavailable", "and the pass really ran")

        seen.clear()
        empty = fresh_db()
        try:
            idle = worker.run_once(settings_for(empty, url=""), prepare=prepare)
        finally:
            remove(empty)
        check_eq(idle.get("skipped"), "idle", "nothing new")
        check_eq(seen, [], "no work -> no backend started")
    finally:
        remove(db)


@case
def test_pass_result_names_the_backend():
    import eventmem_core.nightly as nightly

    class Provider:
        active_name = "lev-4b"

        def available(self):
            return True

        def decide_choice(self, state, questions):
            return None

    class Store:
        def recent_events(self, since):
            return [{"id": 1, "ts": 1.0, "kind": "决定", "text": "只有一条",
                     "project_id": None, "session": "s1"}]

    result = nightly.build_edges(Store(), Provider(), 0)
    check_eq(result.get("backend"), "lev-4b", "the pass reports which backend answered")
    check_eq(result.get("skipped"), None, "one lone event is a valid (empty) pass")


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
