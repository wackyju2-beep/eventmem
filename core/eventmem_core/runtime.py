"""eventmem_core.runtime — supervising a ``self`` pack's process.

A ``self`` pack is one *we* launch: weights on disk, a command line, a port.  This module
owns that process's lifecycle and nothing else — which backend to ask is
:mod:`eventmem_core.decision`, *when* to ask is :mod:`eventmem_core.daemon`.

The port rule
-------------
``port: 0`` means "pick a free one at start and remember it".  The choice is written to
``<pack>/.runtime.json`` next to ``config.yaml``, so another process (the plugin, a CLI
pass, the GUI) can find the address without talking to the daemon.  An explicit port is
written there too — the file always answers "where is this backend right now", and its
absence answers "not running".

Placeholders in ``launch``/``args``: ``{port}``, ``{weights}``, ``{dir}``.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .packs import Pack

_log = logging.getLogger(__name__)

__all__ = [
    "STATE_FILE", "LOG_FILE", "state_path", "read_state", "write_state", "clear_state",
    "alive", "free_port", "resolve_command", "start", "stop", "status",
]

STATE_FILE = ".runtime.json"
LOG_FILE = ".runtime.log"

#: pid -> Popen for every child this process launched.  Needed to reap them: a killed but
#: unreaped child is a zombie, and ``os.kill(pid, 0)`` still succeeds for a zombie.
_CHILDREN: dict[int, subprocess.Popen] = {}

#: How long a freshly started server gets to answer, and how long SIGTERM gets before SIGKILL.
START_TIMEOUT = 120.0
STOP_TIMEOUT = 10.0


# --------------------------------------------------------------------------- state file

def state_path(pack: Pack) -> Path:
    return Path(pack.path) / STATE_FILE


def read_state(pack: Pack) -> Optional[dict[str, Any]]:
    """Whatever the last successful start wrote; ``None`` when the file is absent/garbage."""
    try:
        data = json.loads(state_path(pack).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_state(pack: Pack, state: dict[str, Any]) -> dict[str, Any]:
    target = state_path(pack)
    tmp = target.with_suffix(".json.tmp")
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return state


def clear_state(pack: Pack) -> None:
    try:
        state_path(pack).unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- process

def alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, just not ours
        return True
    except OSError:
        return False
    return True


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _default_llama_server(pack: Pack) -> str:
    """Where llama-server lives: env var, then the installer's marker, then the legacy path."""
    explicit = os.environ.get("EVENTMEM_LLAMA_SERVER", "").strip()
    if explicit:
        return explicit
    base = Path(pack.path).parent.parent          # <root>/backends/<name> -> <root>
    marker = base / "runtimes" / "llama.cpp" / "current.json"
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        binary = str(state.get("binary") or "")
        if binary and Path(binary).exists():
            return binary
    except (OSError, ValueError):
        pass
    return str(base / "runtimes" / "llama.cpp" / "llama-server")


def _substitute(text: str, pack: Pack, port: int) -> str:
    return (text.replace("{port}", str(port))
                .replace("{weights}", pack.weights or "")
                .replace("{dir}", pack.path))


def resolve_command(pack: Pack, port: int) -> list[str]:
    """The argv to launch *pack*.  Raises ``RuntimeError`` with something actionable."""
    launch = (pack.launch or "").strip()
    auto = launch in ("", "auto")
    if auto:
        if not pack.weights:
            raise RuntimeError(f"{pack.name}: 自托管 pack 需要 weights")
        binary = _default_llama_server(pack)
        if not Path(binary).exists():
            raise RuntimeError(
                f"{pack.name}: 找不到 llama.cpp（{binary}）。P3 还没装运行时；"
                "现在可以设 EVENTMEM_LLAMA_SERVER，或在 config.yaml 里写 launch: <命令>")
        command = [binary, "-m", pack.weights]
        if not any("{port}" in arg for arg in pack.args) and "--port" not in pack.args:
            command += ["--port", str(port)]
    else:
        command = shlex.split(_substitute(launch, pack, port))
    command += [_substitute(arg, pack, port) for arg in pack.args]
    return command


def _popen(command: list[str], cwd: str, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
    handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(command)}\n")
    handle.flush()
    kwargs: dict[str, Any] = {"cwd": cwd, "stdout": handle, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)  # noqa: S603 - the user's own command
    finally:
        handle.close()  # the child keeps its own descriptor
    # 记住句柄，否则被杀掉的子进程会变成僵尸（os.kill(pid,0) 对僵尸仍然成功）。
    _CHILDREN[process.pid] = process
    return process


def _reap(pid: Any) -> None:
    """Wait on a child we launched, so it does not linger as a zombie."""
    try:
        process = _CHILDREN.pop(int(pid), None)
    except (TypeError, ValueError):
        return
    if process is None:
        return
    try:
        process.wait(timeout=1.0)
    except Exception:  # noqa: BLE001 - still running; the caller decides
        pass


def start(
    pack: Pack,
    *,
    probe: Optional[Callable[[], bool]] = None,
    timeout: float = START_TIMEOUT,
) -> dict[str, Any]:
    """Launch the pack and wait until *probe* says it is up.

    Returns the stored state (``{"pid", "port", "started", "command"}``).  On failure the
    half-started process is stopped again, so a broken launch cannot leave a zombie that
    the next attempt will mistake for a healthy backend.
    """
    current = read_state(pack)
    if current:
        if alive(current.get("pid")):
            return current
        _reap(current.get("pid"))  # a dead child of ours: collect it before starting another

    port = int(pack.port) or free_port()
    command = resolve_command(pack, port)
    log_path = Path(pack.path) / LOG_FILE
    process = _popen(command, pack.path, log_path)
    state = write_state(pack, {
        "pid": process.pid, "port": port, "started": time.time(), "command": command,
    })
    _log.info("eventmem runtime: started %s pid=%s port=%s", pack.name, process.pid, port)

    if probe is None:
        time.sleep(0.5)
        return state

    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        if process.poll() is not None:
            # A launcher that exits can never become healthy — say why instead of waiting
            # out the whole timeout (a VRAM gate that refused to start lands here).
            code = process.returncode
            stop(pack)
            raise RuntimeError(f"{pack.name}: 启动脚本退出（exit {code}），日志 {log_path}")
        if probe():
            return state
        time.sleep(0.5)
    stop(pack)
    raise RuntimeError(f"{pack.name}: 启动后没有响应（日志 {log_path}）")


def stop(pack: Pack, *, timeout: float = STOP_TIMEOUT) -> bool:
    """Terminate the process this pack's state points at.  ``False`` = nothing was running."""
    state = read_state(pack)
    clear_state(pack)
    if not state:
        return False
    pid = state.get("pid")
    if not alive(pid):
        return False
    _terminate(int(pid), timeout)
    _log.info("eventmem runtime: stopped %s pid=%s", pack.name, pid)
    return True


def _terminate(pid: int, timeout: float) -> None:
    if os.name == "nt":  # pragma: no cover - the daemon runs on Linux for now
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        _reap(pid)
        return
    try:
        group = os.getpgid(pid)
    except OSError:
        group = None
    child = _CHILDREN.get(pid)

    def send(sig) -> None:
        try:
            if group is not None and group != os.getpgid(0):
                os.killpg(group, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, OSError):
            pass

    send(signal.SIGTERM)
    if child is not None:
        # wait() reaps the child; polling alive() would never see a zombie go away.
        try:
            child.wait(timeout=max(1.0, timeout))
            _CHILDREN.pop(pid, None)
            return
        except subprocess.TimeoutExpired:
            pass
    else:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not alive(pid):
                return
            time.sleep(0.2)
    send(signal.SIGKILL)
    if child is not None:
        try:
            child.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        _CHILDREN.pop(pid, None)


def status(pack: Pack, *, probe: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """What we know about the process without starting anything."""
    state = read_state(pack) or {}
    pid = state.get("pid")
    running = alive(pid)
    out: dict[str, Any] = {
        "name": pack.name,
        "kind": pack.kind,
        "configured": bool(pack.base_url or pack.weights),
        "running": running,
        "pid": int(pid) if running and pid else None,
        "port": int(state.get("port") or pack.port or 0),
        "started": state.get("started"),
        "log": str(Path(pack.path) / LOG_FILE),
    }
    if probe is not None:
        try:
            out["healthy"] = bool(probe())
        except Exception:  # noqa: BLE001 - a broken probe is an unhealthy backend
            out["healthy"] = False
    return out
