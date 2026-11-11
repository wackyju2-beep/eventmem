#!/usr/bin/env python3
"""eventmem — the GUI proxy: the daemon's console behind the dashboard's origin.

Run directly:  <python> tests/test_gui_proxy.py

Why this exists: the daemon binds ``127.0.0.1`` on purpose, so when the desktop app is
connected to a *remote* backend, a browser on the laptop cannot open the GUI at all. The
``eventmem-gui`` dashboard plugin re-exports the daemon's HTTP surface under the dashboard's
own origin, which the app already reaches — so the page works with no tunnel.

Two layers are tested: ``forward()`` against a plain HTTP server (stdlib only, always), and
the FastAPI router against the same server (only where fastapi+httpx exist — the dashboard's
interpreter, not a bare CI python). The plugin's UI is a static page, so the contract that
matters is the paths: this file also pins the iframe src and the relative-API invariant in
``ui/app.js`` that make the same page work under both roots.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MODULE = REPO / "adapters" / "hermes" / "eventmem-gui" / "dashboard" / "plugin_api.py"
APP_JS = REPO / "core" / "eventmem_core" / "ui" / "app.js"
DESKTOP_JS = REPO / "adapters" / "hermes" / "desktop-plugins" / "eventmem-gui" / "plugin.js"

os.environ.setdefault("EVENTMEM_LANG", "zh")

CASES: list = []


def case(fn):
    CASES.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_eq(got, want, msg):
    if got != want:
        raise AssertionError(f"{msg}: got {got!r}, want {want!r}")


def load_proxy():
    spec = importlib.util.spec_from_file_location("eventmem_gui_proxy", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


proxy = load_proxy()


# --------------------------------------------------------------------------- echo upstream

class _Echo(BaseHTTPRequestHandler):
    """Stands in for the daemon: answers with what it was asked."""

    def log_message(self, *args):  # noqa: D401 - quiet
        pass

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urlsplit(self.path)
        payload = json.dumps({
            "method": self.command,
            "path": parts.path,
            "query": parts.query,
            "body": body.decode("utf-8", "replace"),
            "ctype": self.headers.get("Content-Type", ""),
        }).encode("utf-8")
        status = 404 if parts.path.endswith("/missing") else 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _reply
    do_POST = _reply
    do_PUT = _reply
    do_PATCH = _reply
    do_DELETE = _reply


class upstream:
    def __init__(self):
        self.httpd = None
        self.thread = None

    def __enter__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{int(self.httpd.server_address[1])}"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        return False


# --------------------------------------------------------------------------- forward()

@case
def test_forward_carries_method_path_query_and_body():
    with upstream() as base:
        status, ctype, raw = proxy.forward(
            "POST", "api/config", query="a=1&b=2", body=b'{"x":1}',
            content_type="application/json", base=base)
        seen = json.loads(raw)
        check_eq(status, 200, "status")
        check("application/json" in ctype, f"content type passed through: {ctype}")
        check_eq(seen["method"], "POST", "method")
        check_eq(seen["path"], "/api/config", "leading slash restored")
        check_eq(seen["query"], "a=1&b=2", "query kept")
        check_eq(seen["body"], '{"x":1}', "body kept")
        check_eq(seen["ctype"], "application/json", "content type kept")


@case
def test_forward_passes_errors_through_as_answers():
    with upstream() as base:
        status, ctype, raw = proxy.forward("GET", "api/missing", base=base)
        check_eq(status, 404, "a 404 from the daemon is a 404 from the proxy")
        check("json" in ctype, f"with its content type: {ctype}")


@case
def test_forward_explains_an_unreachable_daemon():
    status, ctype, raw = proxy.forward("GET", "api/health", base="http://127.0.0.1:1",
                                       timeout=1.0)
    body = json.loads(raw)
    check_eq(status, 502, "unreachable is a 502, not a hang or a 500")
    check("不可达" in body["error"], f"the message says what happened: {body}")
    check("EVENTMEM_DAEMON_URL" in body["hint"], f"and what to do: {body}")


@case
def test_daemon_url_reads_the_environment():
    old = os.environ.get("EVENTMEM_DAEMON_URL")
    try:
        os.environ["EVENTMEM_DAEMON_URL"] = "http://10.0.0.9:9123/"
        check_eq(proxy.daemon_url(), "http://10.0.0.9:9123", "env wins, trailing slash dropped")
        os.environ.pop("EVENTMEM_DAEMON_URL")
        check_eq(proxy.daemon_url(), proxy.DEFAULT_DAEMON, "loopback default")
    finally:
        if old is None:
            os.environ.pop("EVENTMEM_DAEMON_URL", None)
        else:
            os.environ["EVENTMEM_DAEMON_URL"] = old


# --------------------------------------------------------------------------- router

@case
def test_router_serves_the_page_and_the_api():
    """The two URLs the desktop page and the GUI's own fetches actually use."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001 - a bare interpreter has no fastapi
        print(f"  (skipped: {type(exc).__name__}: {exc})")
        return
    if proxy.router is None:
        print("  (skipped: the module loaded without fastapi)")
        return

    old = os.environ.get("EVENTMEM_DAEMON_URL")
    with upstream() as base:
        os.environ["EVENTMEM_DAEMON_URL"] = base
        try:
            app = FastAPI()
            app.include_router(proxy.router, prefix=proxy.PREFIX)
            client = TestClient(app)

            response = client.get(proxy.PREFIX + "/")
            check_eq(response.status_code, 200, "the index")
            seen = response.json()
            check_eq(seen["path"], "/", "proxied to the daemon root")
            check_eq(seen["query"], "", "no query invented")

            response = client.get(proxy.PREFIX + "/api/stats?x=1")
            seen = response.json()
            check_eq(seen["path"], "/api/stats", "the GUI's relative fetch lands on /api/stats")
            check_eq(seen["query"], "x=1", "query forwarded")

            response = client.post(proxy.PREFIX + "/api/config", json={"edges": {"margin": 0.2}})
            seen = response.json()
            check_eq(seen["method"], "POST", "POST kept")
            check_eq(json.loads(seen["body"]), {"edges": {"margin": 0.2}}, "body kept")
        finally:
            if old is None:
                os.environ.pop("EVENTMEM_DAEMON_URL", None)
            else:
                os.environ["EVENTMEM_DAEMON_URL"] = old


# --------------------------------------------------------------------------- the page's contract

@case
def test_the_page_works_under_both_roots():
    """``app.js`` must fetch *relative* URLs, or the proxy prefix eats them."""
    text = APP_JS.read_text(encoding="utf-8")
    check('replace(/^\\//, "")' in text, "app.js strips the leading slash before fetching")
    check("fetch(url, opts)" in text, "and fetches that relative url")
    check('fetch(path, opts)' not in text, "an absolute fetch would break under the prefix")


@case
def test_the_panel_is_the_full_console():
    """The desktop page is a native port of all five web tabs, plus the graph.

    Assertions are on strings that exist only if a tab is really wired: a label alone
    would pass on a comment, so each tab is checked with the endpoint it calls.
    """
    text = DESKTOP_JS.read_text(encoding="utf-8")
    check("jsx('iframe'" not in text, "no iframe: a file:// origin cannot resolve /api/…")
    check("ctx.rest('/api/run-once'" in text, "the header runs a pass")
    for tab in ("概览", "后端", "模型", "调度", "日志", "图谱"):
        check(f"'{tab}'" in text, f"tab {tab} is in the tab bar")
    for endpoint in ("/api/stats", "/api/packs", "/api/models", "/api/install", "/api/config", "/api/logs"):
        check(f"'{endpoint}'" in text, f"the console calls {endpoint}")
    check("TEMPLATES" in text, "new backends come from the same three templates as the web page")
    check("PRESETS" in text, "and the schedule presets are ported too")


@case
def test_the_graph_tab_uses_the_graph_plugin():
    """One graph implementation: the graph plugin hands its page + ctx over a global.

    It is headless on purpose — no route, no chip. If a second entry point comes back,
    the user sees a duplicate 记忆图谱 button (they asked for it gone).
    """
    gui = DESKTOP_JS.read_text(encoding="utf-8")
    graph = DESKTOP_JS.parent.parent / "eventmem-graph" / "plugin.js"
    check(graph.exists(), "the graph plugin is in the repo")
    text = graph.read_text(encoding="utf-8")
    check("globalThis.__eventmemGraph = { GraphPage, ctx" in text, "it publishes GraphPage + its own ctx")
    check("ctx.register(" not in text, "headless: it registers no route and no chip")
    check("ROUTES_AREA" not in text and "STATUSBAR_AREAS" not in text, "and imports neither area")
    check("globalThis.__eventmemGraph" in gui, "the console reads that handshake")
    check("jsx(shared.GraphPage, { ctx: shared.ctx })" in gui, "rendering it with the graph's own ctx keeps its data path")
    check("'/eventmem-graph'" not in gui, "the console never navigates to the deleted route")


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
