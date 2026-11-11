"""eventmem-gui — the Python half is only a dashboard API (a proxy).

The daemon serves its own GUI on ``127.0.0.1:9123``.  That address is useless to a client
that is not on the same machine: the desktop app talks to a remote backend over SSH, and a
browser on the laptop cannot open the remote box's loopback.  This plugin re-exports the
daemon's whole HTTP surface under the dashboard's own origin, so the page works wherever the
dashboard is reachable — with the dashboard's auth in front of it.

Nothing here touches memory: like ``eventmem-graph``, this plugin registers no hooks, no
tools and no provider.

Routes (mounted by the host at ``/api/plugins/eventmem-gui/``)::

    GET  /                  the daemon's index.html (and every asset under it)
    ANY  /api/…             the daemon's JSON routes, unchanged

Point it somewhere else with ``EVENTMEM_DAEMON_URL`` (e.g. when the dashboard and the
daemon are on different hosts).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

__all__ = ["register", "forward", "daemon_url", "DEFAULT_DAEMON", "PREFIX"]

#: Where the daemon listens unless the environment says otherwise.
DEFAULT_DAEMON = "http://127.0.0.1:9123"

#: The path the host mounts this plugin's API under; the desktop page hardcodes the same.
PREFIX = "/api/plugins/eventmem-gui"

#: The daemon binds loopback and has no auth, so the proxy must not be a way to reach it
#: from the internet by accident: the dashboard's own auth is the gate in front of this.
TIMEOUT = 15.0


def daemon_url() -> str:
    """The daemon's root, from ``EVENTMEM_DAEMON_URL`` or the loopback default."""
    return (os.environ.get("EVENTMEM_DAEMON_URL") or DEFAULT_DAEMON).rstrip("/")


def forward(method: str, path: str, *, query: str = "", body: Optional[bytes] = None,
            content_type: str = "", timeout: float = TIMEOUT,
            base: str = "") -> tuple[int, str, bytes]:
    """One request to the daemon: ``(status, content_type, body)``.

    Deliberately free of FastAPI so the proxy can be tested against a plain HTTP server,
    and so a bare interpreter can import this module without the dashboard's dependencies.
    """
    url = f"{base or daemon_url()}/{path.lstrip('/')}"
    if query:
        url += "?" + query.lstrip("?")
    headers = {"User-Agent": "eventmem-dashboard-proxy/1"}
    if body is not None and content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body or None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:      # 4xx/5xx are answers, not failures
        return exc.code, exc.headers.get("Content-Type", "application/json"), exc.read()
    except Exception as exc:  # noqa: BLE001 - unreachable daemon is a 502 with a sentence
        payload = json.dumps({"error": f"eventmem daemon 不可达：{type(exc).__name__}: {exc}",
                              "daemon": base or daemon_url(),
                              "hint": "daemon 没起？`systemctl --user status eventmem-worker`；"
                                      "在别的机器上就用 EVENTMEM_DAEMON_URL 指过去"},
                             ensure_ascii=False).encode("utf-8")
        return 502, "application/json; charset=utf-8", payload


def register(ctx) -> None:  # noqa: ARG001 - the host passes its context
    return None


# --------------------------------------------------------------------------- routes

try:  # the dashboard process has fastapi; tests and a bare CLI import do not need it
    from fastapi import APIRouter, Request
    from fastapi.responses import Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter()

    async def _handle(request: Request, path: str) -> Response:
        body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
        status, ctype, data = await run_in_threadpool(
            forward,
            request.method,
            path,
            query=request.url.query,
            body=body,
            content_type=request.headers.get("content-type", ""),
        )
        return Response(content=data, status_code=status, media_type=ctype or None)

    @router.api_route("/", methods=["GET"])
    async def gui_index(request: Request) -> Response:  # noqa: D401 - one line is enough
        return await _handle(request, "")

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def gui_any(path: str, request: Request) -> Response:
        return await _handle(request, path)

except Exception:  # noqa: BLE001 - no fastapi here; the proxy core above still works
    router = None
