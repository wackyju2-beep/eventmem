"""eventmem_core.core — the one object an adapter talks to.

Everything below this line is memory logic: append-only events, project membership,
hybrid recall, the graph around one event, and the nightly edge pass.  Everything an
adapter does — turning a tool call into a dict, prompting a model, rendering a file,
reading a host's config — happens above it.

The facade exists so an adapter never has to import :mod:`eventmem_core.store` or know
which corner of the schema a column lives in.  It is intentionally small and stable;
new capabilities get a method here rather than a public module.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable, Optional

from . import recall as R
from .embed import Encoder
from .jev import JevClient
from .settings import CoreSettings
from .store import UNFILED, Store

__all__ = ["EventMem", "UNFILED"]


def _vector_bytes(vec: Any) -> Optional[bytes]:
    """float32 bytes for ``events.vec``; ``None`` when the value is unusable."""
    if vec is None:
        return None
    try:
        import numpy as np

        return np.asarray(vec, dtype=np.float32).tobytes()
    except Exception:  # noqa: BLE001 - numpy may be absent on a launcher interpreter
        pass
    try:
        import array

        return array.array("f", [float(x) for x in vec]).tobytes()
    except Exception:  # noqa: BLE001
        return None


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


class EventMem:
    """A memory database plus the policies that govern it."""

    def __init__(self, settings: CoreSettings, *, store: Any = None,
                 encoder: Any = None):
        if store is None and not settings.db_path:
            raise ValueError("CoreSettings.db_path is empty")
        self.settings = settings
        self.store = store if store is not None else Store(str(Path(settings.db_path)))
        self._encoder = encoder
        self._encoder_dead = False

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing is best-effort
                pass

    def __enter__(self) -> "EventMem":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def db_path(self) -> str:
        return str(getattr(self.store, "db_path", self.settings.db_path))

    # -- encoder -------------------------------------------------------------

    def encoder(self) -> Any:
        """The ONNX encoder, or ``None`` when the vector arm is unavailable."""
        if self._encoder is not None or self._encoder_dead:
            return self._encoder
        try:
            enc = Encoder(self.settings.models_root or None)
            check = getattr(enc, "available", None)
            if callable(check) and not check():
                self._encoder_dead = True
                return None
            self._encoder = enc
        except Exception:  # noqa: BLE001 - vectors are optional; FTS still works
            self._encoder_dead = True
            self._encoder = None
        return self._encoder

    def load_encoder(self) -> Any:
        """Force the (slow) model load; safe to call from a background thread."""
        enc = self.encoder()
        load = getattr(enc, "load", None) if enc is not None else None
        if callable(load):
            try:
                load()
            except Exception:  # noqa: BLE001
                pass
        return enc

    def encode(self, text: str, *, is_query: bool = False) -> Optional[bytes]:
        enc = self.encoder()
        if enc is None or not text:
            return None
        try:
            vec = enc.encode(text, is_query=is_query)
        except TypeError:
            try:
                vec = enc.encode(text)
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None
        return _vector_bytes(vec)

    def query_vector(self, query: str) -> Any:
        """The query side of the vector arm, for callers that reuse it twice."""
        if not query:
            return None
        return R.query_vector(self.encoder(), query)

    # -- projects ------------------------------------------------------------

    def list_projects(self, *, include_archived: bool = False) -> list[dict]:
        return list(self.store.list_projects(include_archived=include_archived) or [])

    def project_names(self) -> list[str]:
        return [str(p.get("name")) for p in self.list_projects() if p.get("name")]

    def get_project(self, name: str) -> Optional[dict]:
        return self.store.get_project(name)

    def add_project(self, name: str) -> dict:
        """Create or return a project.  The only path that may invent a name."""
        existing = self.store.get_project(name)
        if existing:
            return {
                "id": existing.get("id"),
                "name": str(existing.get("name") or name),
                "existed": True,
                "archived": str(existing.get("status") or "active") == "archived",
            }
        return {"id": self.store.add_project(name), "name": name, "existed": False,
                "archived": False}

    def session_project(self, session_id: str) -> str:
        """The project this session last wrote into (sticky default), or ``""``."""
        getter = getattr(self.store, "get_session_project", None)
        if not callable(getter) or not session_id:
            return ""
        try:
            row = getter(session_id)
        except Exception:  # noqa: BLE001
            return ""
        name = row.get("project") if isinstance(row, dict) else row
        return str(name or "")

    def set_session_project(self, session_id: str, name: Optional[str]) -> None:
        setter = getattr(self.store, "set_session_project", None)
        if not callable(setter) or not session_id:
            return
        try:
            setter(session_id, name or None)
        except Exception:  # noqa: BLE001 - stickiness is a convenience, not a guarantee
            pass

    # -- writes --------------------------------------------------------------

    def add_artifact(self, kind: str, text: str) -> Optional[int]:
        if not text:
            return None
        try:
            return self.store.add_artifact(kind or "其他", text)
        except Exception:  # noqa: BLE001 - the event still matters without the blob
            return None

    def add_event(self, kind: str, text: str, *, ts: Optional[float] = None,
                  project: Optional[str] = None, session: Optional[str] = None,
                  art_id: Optional[int] = None, art_path: Optional[str] = None,
                  extra_projects: Optional[list[str]] = None,
                  vec: Any = None) -> int:
        """Append one event; encodes the vector unless the caller already did."""
        if vec is None:
            label = project or UNFILED
            vec = self.encode("[%s] %s %s" % (label, kind, text))
        return self.store.add_event(kind, text, ts=ts, project=project, session=session,
                                    art_id=art_id, art_path=art_path,
                                    extra_projects=extra_projects, vec=vec)

    def get_event(self, event_id: int) -> Optional[dict]:
        return self.store.get_event(int(event_id))

    def get_artifact(self, art_id: int) -> Optional[dict]:
        getter = getattr(self.store, "get_artifact", None)
        if not callable(getter):
            return None
        try:
            return getter(int(art_id))
        except Exception:  # noqa: BLE001
            return None

    def forget(self, event_id: int) -> bool:
        forget = getattr(self.store, "forget", None)
        if not callable(forget):
            return False
        forget(int(event_id))
        return True

    def add_link(self, src: int, dst: int, rel: str, conf: float = 1.0,
                 decided_by: str = "llm_batch") -> dict:
        """Write one directed edge, with the checks that keep the graph sane."""
        from .settings import RELS

        if rel not in RELS:
            return {"ok": False, "error": f"invalid rel {rel!r}"}
        if int(src) == int(dst):
            return {"ok": False, "error": "an event cannot link to itself"}
        target = self.get_event(int(dst))
        if not target:
            return {"ok": False, "error": f"no event #{dst}"}
        source = self.get_event(int(src))
        if source:
            if (float(source.get("ts") or 0.0), int(source.get("id") or 0)) <= (
                    float(target.get("ts") or 0.0), int(target.get("id") or 0)):
                return {"ok": False, "error": f"event {dst} is not older than {src}"}
        self.store.add_edge(int(src), int(dst), rel, float(conf), decided_by)
        return {"ok": True}

    # -- reads ---------------------------------------------------------------

    def expand(self, event_id: int) -> Optional[dict]:
        """One event with its artifact text, in the shape the model expects."""
        event = self.get_event(event_id)
        if not event:
            return None
        out: dict[str, Any] = {
            "id": event.get("id", event_id),
            "date": _fmt_ts(event.get("ts")),
            "kind": event.get("kind"),
            "text": event.get("text"),
            "project": event.get("project") or UNFILED,
            "artifact": None,
        }
        art_id = event.get("art_id")
        if art_id:
            artifact = self.get_artifact(int(art_id))
            if artifact:
                out["artifact"] = {"kind": artifact.get("kind"), "text": artifact.get("text")}
            else:
                out["note"] = f"artifact #{art_id} is gone"
        else:
            out["note"] = "this event has no artifact"
        if event.get("art_path"):
            out["path"] = event["art_path"]
        return out

    def new_recall_state(self) -> Any:
        factory = getattr(R, "RecallState", None)
        return factory() if callable(factory) else None

    def recall(self, query: str, *, state: Any = None, budget_tokens: Optional[int] = None,
               qvec: Any = None) -> str:
        try:
            return R.recall(self.store, self.encoder(), query, policy=self.settings.recall,
                            budget_tokens=budget_tokens, qvec=qvec, state=state) or ""
        except Exception:  # noqa: BLE001 - recall must never fail a turn
            return ""

    def project_hint(self, query: str, *, qvec: Any = None) -> str:
        try:
            return R.project_hint(self.store, self.encoder(), query,
                                  policy=self.settings.recall, qvec=qvec) or ""
        except Exception:  # noqa: BLE001
            return ""

    def neighbours(self, event_id: Any, *, rels: Optional[Iterable[str]] = None,
                   depth: int = 1) -> str:
        try:
            return R.neighbours(self.store, event_id, rels=rels, depth=depth,
                                policy=self.settings.recall) or ""
        except Exception:  # noqa: BLE001
            return ""

    # -- edges ---------------------------------------------------------------

    def build_edges(self, since_ts: float, *, client: Any = None) -> dict:
        """One nightly pass.  ``client`` defaults to a client built from the settings."""
        from .nightly import build_edges

        jev = client
        if jev is None:
            jev = JevClient(base_url=self.settings.decision.url or None,
                            timeout=self.settings.decision.timeout,
                            path=self.settings.decision.path,
                            model=self.settings.decision.model,
                            think_mode=self.settings.decision.think_mode or None)
        return build_edges(self.store, jev, since_ts, self.settings.edges)

    def cursor(self) -> Optional[float]:
        from .nightly import CURSOR_KEY

        raw = self.store.get_meta(CURSOR_KEY)
        try:
            return float(raw) if raw else None
        except (TypeError, ValueError):
            return None

    def advance_cursor(self, ts: float) -> bool:
        from .nightly import CURSOR_KEY

        return bool(self.store.advance_meta_ts(CURSOR_KEY, float(ts)))
