"""Best-effort local wakeup for the resident edge daemon.

Event writes remain durable in SQLite; this socket is only a latency hint. If a
notification is lost, timer scheduling and the daemon's startup check still find
the event later.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional


def socket_path(db_path: str) -> str:
    return str(Path(db_path).expanduser()) + ".wakeup"


def notify(db_path: str) -> None:
    """Wake the daemon owning *db_path*; never make an event write fail."""
    if not db_path or db_path == ":memory:" or not hasattr(socket, "AF_UNIX"):
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"event", socket_path(db_path))
        finally:
            sock.close()
    except (OSError, ValueError):
        pass


class Listener:
    """A tiny datagram listener owned by one daemon process."""

    def __init__(self, db_path: str, callback: Callable[[], None]):
        self.path = socket_path(db_path)
        self.callback = callback
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if not hasattr(socket, "AF_UNIX"):
            return False
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(self.path)
            sock.settimeout(0.5)
            self._sock = sock
            self._thread = threading.Thread(target=self._run, name="eventmem-wakeup", daemon=True)
            self._thread.start()
            return True
        except OSError:
            self.close()
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sock.recv(64)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.callback()
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            pass