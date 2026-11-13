"""All SQL for eventmem: events, projects, artifacts, edges, FTS5, forget.


Caller-visible guarantees that are easy to get wrong:

* **Event dicts** always have the contract shape
  ``{id, ts, kind, text, project, project_id, session, art_id, src_msg, vec}``.
* **``add_event(project=...)``** refuses names that are not in ``projects`` (``UnknownProject``);
  ``None`` (or ``"unfiled"``, the vocabulary word from §4.3) means unclassified.
* **``add_edge``** is idempotent on ``(src, dst, rel)``: a second insert is ignored, the first
  ``conf``/``decided_by`` win. Nothing is ever duplicated.
* **``chain``** walks 改变/否定 edges in *both* directions (back to predecessors, forward to the
  replacement), with a visited set and a depth cap, so a cyclic graph can never hang recall.
  Events come back sorted by ``(ts, id)``; each carries ``superseded_by`` (later events that
  replaced it, with their ts) and ``invalidated``.
* **``fts_search``** returns SQLite ``bm25()`` scores as-is: **more negative = more relevant**
  (SQLite's own convention). It ORs extracted terms — never the whole query as one phrase, which
  would demand a verbatim match. The trigram tokenizer has a 3-character floor, so a query that
  yields no usable term (e.g. two Chinese characters) falls back to a LIKE scan scored ``-1e-12``,
  same trick localmem's lexical arm uses.
* **``forget``** is physical: event, its edges both ways, its event_projects and its exclusive
  artifact are deleted, the WAL is truncated, and a text-free tombstone (id + sha256) is left.

The connection is WAL, ``foreign_keys=ON``, ``check_same_thread=False`` and guarded by a
re-entrant lock: Hermes prefetches on a worker thread while the agent thread writes events.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Optional, Sequence

try:  # imported as ``eventmem.store`` (package) …
    from .schema import ensure_schema
except ImportError:  # … or as a top-level module by the test scripts / direct import
    from schema import ensure_schema  # type: ignore[no-redef]

KINDS = ("决定", "尝试", "结果", "变更", "教训")
RELS = ("改变", "否定", "重复", "因为", "细化")

REL_CHANGE = "改变"
REL_DENY = "否定"
REL_REPEAT = "重复"
#: 入边里有这几种就不再是「活跃链头」（设计 §5，v5）：被取代、被否决、被重复过。
HEAD_KILL_RELS = (REL_CHANGE, REL_DENY, REL_REPEAT)
UNFILED = "unfiled"  # §4.3: the one name that means "no project"

_EVENT_COLS = ("e.id, e.ts, e.kind, e.text, e.project_id, e.art_id, e.art_path, "
               "e.src_msg, e.session, e.vec")
_EVENT_SELECT = (
    f"SELECT {_EVENT_COLS}, p.name AS project_name "
    "FROM events e LEFT JOIN projects p ON p.id = e.project_id"
)

# Latin/number runs (keep ``.``/``+``/``#``/``-`` inside, e.g. ``2T``, ``qwen3.5``, ``C++``) and CJK runs.
_TERM_RE = re.compile(r"[A-Za-z0-9_]+(?:[.+#\-][A-Za-z0-9_]+)*|[\u3400-\u9fff]+")


class UnknownProject(Exception):
    """Raised when a project name was not registered. The write is rejected, not silently dropped."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _ranked(scores: dict[int, float], limit: int) -> list[tuple[int, float]]:
    """``{id: bm25}`` → ``[(id, score)]`` best first (bm25 is negative, so ascending)."""
    return sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))[:limit]


def _event_row(row: sqlite3.Row) -> dict:
    """Map one joined events row to the contract's event dict."""
    vec = row["vec"]
    return {
        "id": int(row["id"]),
        "ts": float(row["ts"]),
        "kind": row["kind"],
        "text": row["text"],
        "project": row["project_name"],
        "project_id": row["project_id"],
        "session": row["session"],
        "art_id": row["art_id"],
        "art_path": row["art_path"],
        "src_msg": row["src_msg"],
        "vec": bytes(vec) if vec is not None else None,
    }


def _is_newer(a: dict, b: dict) -> bool:
    """True when event ``a`` is strictly later than event ``b`` (ts, then id)."""
    return (float(a.get("ts") or 0.0), int(a.get("id") or 0)) > (
        float(b.get("ts") or 0.0), int(b.get("id") or 0))


def _unit_vector(raw: Any) -> Optional[list[float]]:
    """``events.vec`` (float32 blob) as a unit vector; ``None`` when absent/unusable."""
    if not raw:
        return None
    try:
        values = array.array("f")
        values.frombytes(bytes(raw))
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(sum(v * v for v in values))
    if not norm or not math.isfinite(norm):
        return None
    return [v / norm for v in values]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine of two unit vectors (0.0 when the dimensions do not match)."""
    if len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


def _as_bytes(vec: Any) -> Optional[bytes]:
    if vec is None:
        return None
    if isinstance(vec, (bytes, bytearray, memoryview)):
        return bytes(vec)
    tobytes = getattr(vec, "tobytes", None)  # numpy arrays are accepted without importing numpy
    if callable(tobytes):
        return tobytes()
    raise TypeError(f"vec must be bytes-like, got {type(vec).__name__}")


def _fts_terms(q: str, max_terms: int = 24) -> list[str]:
    """FTS5 trigram terms: latin tokens as-is, CJK runs as sliding 3-grams.

    A CJK run shorter than 3 characters is dropped — the trigram index has nothing to match it
    against; ``Store.fts_search`` covers that case with a LIKE fallback.
    """
    out: list[str] = []
    for tok in _TERM_RE.findall(q or ""):
        if tok[0].isascii():
            if len(tok) >= 2:
                out.append(tok)
        elif len(tok) < 3:
            continue
        elif len(tok) == 3:
            out.append(tok)
        else:
            out.extend(tok[i:i + 3] for i in range(len(tok) - 2))
    seen: list[str] = []
    for t in out:
        if t not in seen:
            seen.append(t)
    return seen[:max_terms]


class Store:
    """One SQLite connection + every query eventmem needs."""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            self.db_path, timeout=10.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        ensure_schema(self._conn)

    # ------------------------------------------------------------------ plumbing

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store is closed")
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _select_events(
        self,
        where: str = "",
        params: Sequence[Any] = (),
        order: str = "e.ts ASC, e.id ASC",
        limit: Optional[int] = None,
    ) -> list[dict]:
        sql = _EVENT_SELECT
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY " + order
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, int(limit))
        return [_event_row(r) for r in self._db.execute(sql, params).fetchall()]

    def _events_by_ids(self, ids: Iterable[int]) -> list[dict]:
        ids = [int(i) for i in ids]
        out: list[dict] = []
        for chunk in _chunks(ids, 400):
            out.extend(self._select_events(f"e.id IN ({_placeholders(len(chunk))})", tuple(chunk)))
        out.sort(key=lambda e: (e["ts"], e["id"]))
        return out

    # ------------------------------------------------------------------ projects

    def add_project(self, name: str) -> int:
        """Register a project. Idempotent: an existing name returns its id unchanged."""
        name = (name or "").strip()
        if not name:
            raise ValueError("project name must be non-empty")
        with self._lock:
            row = self._db.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
            if row is not None:
                return int(row["id"])
            cur = self._db.execute(
                "INSERT INTO projects(name, status, created_ts) VALUES(?, 'active', ?)",
                (name, time.time()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def get_project(self, name: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT id, name, status, created_ts FROM projects WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        return {"id": int(row["id"]), "name": row["name"], "status": row["status"],
                "created_ts": float(row["created_ts"])}

    def list_projects(self, include_archived: bool = False) -> list[dict]:
        sql = (
            "SELECT p.id, p.name, p.status, p.created_ts, "
            "       (SELECT count(*) FROM event_projects ep WHERE ep.project_id = p.id) AS event_count "
            "FROM projects p"
        )
        params: tuple = ()
        if not include_archived:
            sql += " WHERE p.status != 'archived'"
        sql += " ORDER BY CASE WHEN p.status = 'archived' THEN 1 ELSE 0 END, p.name"
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [
            {"id": int(r["id"]), "name": r["name"], "status": r["status"],
             "created_ts": float(r["created_ts"]), "event_count": int(r["event_count"])}
            for r in rows
        ]

    def archive_project(self, name: str) -> None:
        with self._lock:
            cur = self._db.execute("UPDATE projects SET status = 'archived' WHERE name = ?", (name,))
            self._db.commit()
        if cur.rowcount == 0:
            raise UnknownProject(name)

    def require_project(self, name: str) -> int:
        """Id of an existing project; ``UnknownProject`` otherwise."""
        with self._lock:
            row = self._db.execute("SELECT id FROM projects WHERE name = ?", ((name or "").strip(),)).fetchone()
        if row is None:
            raise UnknownProject(f"unknown project: {name!r}")
        return int(row["id"])

    def _resolve_project(self, name: Optional[str]) -> Optional[int]:
        """``None``/``""``/``unfiled`` → NULL; an existing name → id; anything else → UnknownProject."""
        if name is None:
            return None
        name = str(name).strip()
        if not name:
            return None
        row = self._db.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return int(row["id"])
        if name.lower() == UNFILED:
            return None
        raise UnknownProject(f"unknown project: {name!r}")

    # ------------------------------------------------------- session stickiness
    # Not in the contract's Store list, but §3's session_project table has no other owner.

    def set_session_project(self, session_id: str, project: Optional[str]) -> None:
        with self._lock:
            pid = self._resolve_project(project)
            self._db.execute(
                "INSERT INTO session_project(session_id, project_id, ts) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET project_id = excluded.project_id, ts = excluded.ts",
                (session_id, pid, time.time()),
            )
            self._db.commit()

    def get_session_project(self, session_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT sp.session_id, sp.project_id, p.name AS project_name "
                "FROM session_project sp LEFT JOIN projects p ON p.id = sp.project_id "
                "WHERE sp.session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {"session_id": row["session_id"], "project_id": row["project_id"],
                "project": row["project_name"]}

    # ------------------------------------------------------------------ events

    def add_event(
        self,
        kind: str,
        text: str,
        *,
        ts: Optional[float] = None,
        project: Optional[str] = None,
        session: Optional[str] = None,
        src_msg: Optional[int] = None,
        art_id: Optional[int] = None,
        art_path: Optional[str] = None,
        extra_projects: Optional[list[str]] = None,
        vec: Optional[bytes] = None,
    ) -> int:
        """Append one event. ``project`` must already exist (or be None/``unfiled``)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        text = (text or "").strip()
        if not text:
            raise ValueError("event text must be non-empty")
        ts = float(ts) if ts is not None else time.time()
        art_path = (art_path or "").strip() or None

        # 墓碑只拦「从旧消息重新抽取」这条路（src_msg 非空 = backfill/回填）；模型在当下
        # 重新写下同一句话是合法的新事件，不能因为曾经忘过就永远拒绝。
        if src_msg is not None and self.is_tombstoned_text(text):
            raise ValueError("event text was forgotten (tombstone); refusing to re-add it")

        with self._lock:
            pid = self._resolve_project(project)
            extra_ids: list[int] = []
            for name in extra_projects or ():
                xid = self._resolve_project(name)
                if xid is not None and xid != pid and xid not in extra_ids:
                    extra_ids.append(xid)

            cur = self._db.execute(
                "INSERT INTO events(ts, kind, text, project_id, art_id, art_path, src_msg, session, vec) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ts, kind, text, pid, art_id, art_path, src_msg, session, _as_bytes(vec)),
            )
            eid = int(cur.lastrowid)
            if pid is not None:
                self._db.execute(
                    "INSERT INTO event_projects(event_id, project_id, is_primary) VALUES(?,?,1)",
                    (eid, pid),
                )
            for xid in extra_ids:
                self._db.execute(
                    "INSERT INTO event_projects(event_id, project_id, is_primary) VALUES(?,?,0)",
                    (eid, xid),
                )
            self._db.commit()
            return eid

    def get_event(self, eid: int) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(_EVENT_SELECT + " WHERE e.id = ?", (eid,)).fetchone()
        return _event_row(row) if row is not None else None

    def get_events(self, ids: list[int]) -> list[dict]:
        """Events for ``ids``, in the order given, missing ids skipped."""
        if not ids:
            return []
        found = {e["id"]: e for e in self._events_by_ids(ids)}
        out: list[dict] = []
        for i in ids:
            ev = found.get(int(i))
            if ev is not None:
                out.append(ev)
        return out

    def update_vec(self, eid: int, vec: bytes) -> None:
        """Set/replace the embedding. Unknown ids are a no-op (background passes race forgets)."""
        with self._lock:
            self._db.execute("UPDATE events SET vec = ? WHERE id = ?", (_as_bytes(vec), eid))
            self._db.commit()

    def session_window(
        self, session: str, center_id: int, before: int = 2, after: int = 2
    ) -> list[dict]:
        """``before`` events + ``center_id`` + ``after`` events, same session, ts ascending."""
        if session is None:
            return []
        before = max(0, int(before))
        after = max(0, int(after))
        with self._lock:
            center = self._db.execute(
                _EVENT_SELECT + " WHERE e.id = ? AND e.session = ?", (center_id, session)
            ).fetchone()
            if center is None:
                return []
            cts = float(center["ts"])
            earlier = self._select_events(
                "e.session = ? AND (e.ts < ? OR (e.ts = ? AND e.id < ?))",
                (session, cts, cts, int(center_id)),
                order="e.ts DESC, e.id DESC",
                limit=before,
            )
            later = self._select_events(
                "e.session = ? AND (e.ts > ? OR (e.ts = ? AND e.id > ?))",
                (session, cts, cts, int(center_id)),
                order="e.ts ASC, e.id ASC",
                limit=after,
            )
            out = earlier + [_event_row(center)] + later
            out.sort(key=lambda e: (e["ts"], e["id"]))
            return out

    def project_events(self, project_id: int) -> list[dict]:
        """Every event touching ``project_id`` — primary or secondary — ts ascending."""
        with self._lock:
            return self._select_events(
                "e.project_id = ? OR e.id IN "
                "(SELECT event_id FROM event_projects WHERE project_id = ?)",
                (int(project_id), int(project_id)),
            )

    def recent_events(self, since_ts: float) -> list[dict]:
        with self._lock:
            return self._select_events("e.ts >= ?", (float(since_ts),))

    # ----------------------------------------------------------------- artifacts

    def add_artifact(self, kind: str, text: str, src_msg: Optional[int] = None) -> int:
        """Store raw text once per sha256 — the same command/log gets one id forever."""
        text = text if isinstance(text, str) else str(text)
        sha = _sha256(text)
        with self._lock:
            row = self._db.execute("SELECT id FROM artifacts WHERE sha = ?", (sha,)).fetchone()
            if row is not None:
                return int(row["id"])
            cur = self._db.execute(
                "INSERT INTO artifacts(sha, kind, text, src_msg) VALUES(?,?,?,?)",
                (sha, kind, text, src_msg),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def get_artifact(self, aid: int) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT id, sha, kind, text, src_msg FROM artifacts WHERE id = ?", (aid,)
            ).fetchone()
        if row is None:
            return None
        return {"id": int(row["id"]), "sha": row["sha"], "kind": row["kind"],
                "text": row["text"], "src_msg": row["src_msg"]}

    # --------------------------------------------------------------------- meta

    def count_events_since(self, ts: float) -> int:
        """How many events are strictly newer than ``ts``.

        The link worker's "is there anything to do?" probe: a cheap count that lets it
        return to sleep without starting the decision service.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT count(*) FROM events WHERE ts > ?", (float(ts),)
            ).fetchone()
        return int(row[0]) if row else 0

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read one ``meta`` value (schema version, nightly cursor); ``default`` when absent."""
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
        return default if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        """Insert or replace one ``meta`` value."""
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
            self._db.commit()

    def advance_meta_ts(self, key: str, value: float) -> bool:
        """Move a timestamp cursor forward only; returns True when it moved.

        Several Hermes processes (dashboard, gateway, per-session backend) each run a link
        worker against the same database, so a pass that started earlier can finish later.
        A plain write would let the older pass rewind the cursor and make the next one redo
        work that is already done; the cursor is a high-water mark, never a last-writer.
        """
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
            try:
                current = float(row["value"]) if row is not None else 0.0
            except (TypeError, ValueError):
                current = 0.0
            if float(value) <= current:
                return False
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), "%.3f" % float(value)),
            )
            self._db.commit()
        return True

    # --------------------------------------------------------------------- edges

    def add_edge(
        self,
        src: int,
        dst: int,
        rel: str,
        conf: float,
        decided_by: str,
        cross: Optional[int] = None,
        probs: Optional[dict] = None,
        backend: str = "",
    ) -> None:
        """Insert a relation; a repeated ``(src, dst, rel)`` is ignored, never duplicated.

        ``probs`` is the raw distribution the chosen label came from (axis: 4 entries with the
        server's none option; detail: 3).  It is kept so a later margin/selection change can be
        re-evaluated from the database instead of re-running the model.

        ``backend`` is *which* decision backend produced it (a pack name, or ``default`` for the
        legacy single endpoint).  Two models answer differently, so a threshold recomputed over
        one distribution must not silently absorb the other's edges.
        """
        if rel not in RELS:
            raise ValueError(f"rel must be one of {RELS}, got {rel!r}")
        if cross is None:
            cross = self._infer_cross(int(src), int(dst))
        blob = json.dumps(probs, ensure_ascii=False, sort_keys=True) if probs else None
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO edges(src, dst, rel, conf, decided_by, cross, probs, backend) "
                "SELECT ?,?,?,?,?,?,?,? WHERE ? != 'jev' OR NOT EXISTS "
                "(SELECT 1 FROM edge_deletions WHERE src=? AND dst=?)",
                (int(src), int(dst), rel, float(conf), decided_by, int(cross), blob, str(backend),
                 decided_by, int(src), int(dst)),
            )
            self._db.commit()

    def recalibrate(self, policy, *, token=None, historical_scale=None):
        """Preview or atomically apply a snapshot. No model calls or schema changes.

        A whole pair is skipped on incomplete distributions, missing/edited events, or any
        human/other/manual edge. This prevents contradictory partial updates of either axis.
        ``historical_scale='conditional'`` explicitly opts into interpreting complete
        conditional-form archives; provenance is otherwise unknown. The option is bound
        into the preview token. Original archive and edge distributions remain unchanged.
        """
        import hashlib
        import math
        from dataclasses import asdict
        from .nightly import _accepted, _AXIS_RELS, _DETAIL_RELS, NONE_LABEL
        from .providers import probability_scale
        if historical_scale not in (None, 'conditional'):
            raise ValueError('historical_scale must be None or conditional')

        def distribution(raw, labels):
            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict) or set(obj) != set(labels) | {NONE_LABEL}:
                    return None
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                       not math.isfinite(v) or v < 0 or v > 1 for v in obj.values()):
                    return None
                scale = probability_scale((obj[k] for k in labels), obj[NONE_LABEL])
                if scale is None or (scale == 'conditional' and historical_scale != 'conditional'):
                    return None
                return obj
            except (TypeError, ValueError):
                return None

        def answer(dist):
            probs = {k: v for k, v in dist.items() if k != NONE_LABEL}
            return {'probabilities': probs, 'none': dist[NONE_LABEL],
                    'probability_scale': probability_scale(probs.values(), dist[NONE_LABEL])}

        with self._lock:
            db = self._db
            db.execute('BEGIN IMMEDIATE' if token is not None else 'BEGIN')
            try:
                pairs = [dict(r) for r in db.execute('SELECT * FROM pair_evals ORDER BY src,dst')]
                edges = [dict(r) for r in db.execute('SELECT * FROM edges ORDER BY src,dst,rel')]
                deletions = [dict(r) for r in db.execute('SELECT * FROM edge_deletions ORDER BY src,dst,rel')]
                events = [dict(r) for r in db.execute('SELECT id, project_id, edited_ts FROM events ORDER BY id')]
                snapshot = {'pairs': pairs, 'edges': edges, 'deletions': deletions,
                            'events': events, 'policy': asdict(policy),
                            'historical_scale': historical_scale}
                digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                if token is not None and token != digest:
                    raise ValueError('stale preview')
                by_pair = {}
                for edge in edges:
                    by_pair.setdefault((edge['src'], edge['dst']), []).append(edge)
                by_id = {e['id']: e for e in events}
                added = {r: 0 for r in RELS}
                removed = {r: 0 for r in RELS}
                skipped = 0
                deleted_pairs = {(d['src'], d['dst']) for d in deletions}
                manual_deletion_pairs = 0
                deletes, inserts, labels_update, conf_updates = [], [], [], []
                for pair in pairs:
                    src, dst = pair['src'], pair['dst']
                    old = by_pair.get((src, dst), [])
                    a, b = by_id.get(src), by_id.get(dst)
                    axis = distribution(pair['axis_probs'], _AXIS_RELS)
                    detail = distribution(pair['detail_probs'], _DETAIL_RELS)
                    def matches_eval(edge):
                        labels, expected = ((_AXIS_RELS, axis) if edge['rel'] in _AXIS_RELS
                                            else (_DETAIL_RELS, detail))
                        return distribution(edge['probs'], labels) == expected
                    expected_old = {pair['axis_label']} if pair['axis_label'] else set()
                    if pair['axis_label'] != '重复' and pair['detail_label']:
                        expected_old.add(pair['detail_label'])
                    if (src, dst) in deleted_pairs or (dst, src) in deleted_pairs:
                        manual_deletion_pairs += 1
                        skipped += 1
                        continue
                    if (not a or not b or a['edited_ts'] is not None or b['edited_ts'] is not None
                            or axis is None or detail is None
                            or not expected_old.issubset({e['rel'] for e in old}) or any(
                                e['decided_by'] != 'jev' or e['edited_ts'] is not None
                                or not matches_eval(e) for e in old)):
                        skipped += 1
                        continue
                    axis_hit = _accepted(answer(axis), _AXIS_RELS, policy.margin, policy)
                    detail_hit = None if axis_hit and axis_hit[0] == '重复' else _accepted(
                        answer(detail), _DETAIL_RELS, policy.margin, policy)
                    desired = {h[0]: h for h in (axis_hit, detail_hit) if h is not None}
                    labels_update.append((axis_hit[0] if axis_hit else None,
                                          detail_hit[0] if detail_hit else None, src, dst))
                    existing = {e['rel']: e for e in old}
                    for rel in existing.keys() & desired.keys():
                        dist = axis if rel in _AXIS_RELS else detail
                        if (historical_scale == 'conditional' and
                                probability_scale((dist[k] for k in (_AXIS_RELS if rel in _AXIS_RELS else _DETAIL_RELS)),
                                                  dist[NONE_LABEL]) == 'conditional' and
                                existing[rel]['conf'] != desired[rel][1]):
                            conf_updates.append((desired[rel][1], src, dst, rel))
                    for rel in existing.keys() - desired.keys():
                        removed[rel] += 1
                        deletes.append((src, dst, rel))
                    for rel in desired.keys() - existing.keys():
                        hit = desired[rel]
                        added[rel] += 1
                        cross = int(a['project_id'] is not None and b['project_id'] is not None
                                    and a['project_id'] != b['project_id'])
                        inserts.append((src, dst, rel, hit[1], 'jev', cross,
                                        json.dumps(hit[2], ensure_ascii=False, sort_keys=True), ''))
                if token is not None:
                    db.executemany('DELETE FROM edges WHERE src=? AND dst=? AND rel=?', deletes)
                    db.executemany('INSERT INTO edges(src,dst,rel,conf,decided_by,cross,probs,backend) '
                                   'VALUES(?,?,?,?,?,?,?,?)', inserts)
                    db.executemany('UPDATE pair_evals SET axis_label=?, detail_label=? '
                                   'WHERE src=? AND dst=?', labels_update)
                    db.executemany('UPDATE edges SET conf=? WHERE src=? AND dst=? AND rel=?', conf_updates)
                db.commit()
                archived = {(p['src'], p['dst']) for p in pairs}
                uncovered = sum(1 for e in edges if e['decided_by'] == 'jev'
                                and (e['src'], e['dst']) not in archived)
                return {'token': digest, 'added': added, 'removed': removed, 'skipped': skipped,
                        'confidence_updates': len(conf_updates),
                        'uncovered_jev_edges': uncovered, 'manual_deletion_pairs': manual_deletion_pairs,
                        'legacy_deletions_unknown': True}
            except Exception:
                db.rollback()
                raise

    # ------------------------------------------------ 当前状态解析（v10 之后）

    def resolve_head(self, eid: int, *, max_hops: int = 3, min_cos: float = 0.0,
                     rels: Sequence[str] = (REL_CHANGE,)) -> Optional[dict]:
        """Follow 改变 successors to the current value of the same slot.

        Only 改变 (a 否定 offers no replacement, so it is not a value to walk to), only a
        **single** successor per step (two successors = we cannot tell which branch is the
        value), and only while the two events look like the same thing (``cos >= min_cos``).
        Returns ``{"event", "hops"}`` when the walk moved at least one hop, else ``None`` —
        a refused walk must leave the caller on exactly the behaviour it had before.

        This is deliberately *not* transitive closure: in the production graph 改变/否定 is
        one 356-node blob, and walking it returns "whatever happened last", which is worse
        than the one hop recall already gives.
        """
        rels = tuple(r for r in (rels or ()) if r in RELS) or (REL_CHANGE,)
        with self._lock:
            cur = self.get_event(int(eid))
            if not cur:
                return None
            hops = 0
            seen = {int(cur["id"])}
            cur_vec = _unit_vector(cur.get("vec"))
            cap = max(0, int(max_hops))
            while True:
                rows = self._db.execute(
                    "SELECT g.src FROM edges g JOIN events s ON s.id = g.src "
                    "JOIN events d ON d.id = g.dst "
                    f"WHERE g.dst = ? AND g.rel IN ({_placeholders(len(rels))}) "
                    "AND (s.ts > d.ts OR (s.ts = d.ts AND s.id > d.id))",
                    (int(cur["id"]), *rels),
                ).fetchall()
                nxt = sorted({int(r[0]) for r in rows} - seen)
                if not nxt:
                    break                  # 没有后继：这就是当前值
                if len(nxt) != 1:
                    return None            # 分叉：不知道哪条是当前值，什么都不宣称
                if hops >= cap:
                    return None            # 还没走到尽头：可能是中间态，不宣称
                cand = self.get_event(nxt[0])
                if not cand or not _is_newer(cand, cur):
                    break
                cand_vec = _unit_vector(cand.get("vec"))
                if min_cos and cur_vec and cand_vec and _cosine(cur_vec, cand_vec) < float(min_cos):
                    break                  # 换了别的东西，不是同一个取值 → 停在这里
                cur, cur_vec = cand, cand_vec
                seen.add(int(cand["id"]))
                hops += 1
            if hops == 0:
                return None
            return {"event": cur, "hops": hops}

    def clear_edges(self) -> int:
        """Delete every edge (the graph's bulk "start over").  Tombstones are untouched."""
        with self._lock:
            cur = self._db.execute("DELETE FROM edges")
            self._db.commit()
        return int(cur.rowcount or 0)

    # ------------------------------------------------ 回合抽取失败（v10）

    def add_extract_failure(self, reason: str, detail: str = "", *, session: Optional[str] = None,
                            ts: Optional[float] = None, keep: int = 200) -> int:
        """Record one failed turn-end extraction; no event is written for that turn.

        Kept as a *notice*, not an event: the turn produced nothing worth remembering, and
        what an operator needs to see is the failure (a broken aux model), not a mechanical
        restatement of the question.  Retention is capped so a permanently broken model
        cannot grow the file without bound.
        """
        stamp = float(ts if ts is not None else time.time())
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO extract_failures(ts, session, reason, detail) VALUES(?,?,?,?)",
                (stamp, session or None, str(reason or "unknown"), (detail or "")[:400]),
            )
            cap = max(1, int(keep))
            self._db.execute(
                "DELETE FROM extract_failures WHERE id NOT IN "
                "(SELECT id FROM extract_failures ORDER BY id DESC LIMIT ?)",
                (cap,),
            )
            self._db.commit()
        return int(cur.lastrowid or 0)

    def extract_failures(self, limit: int = 5) -> list[dict]:
        """Newest first: ``{id, ts, session, reason, detail}``."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, session, reason, detail FROM extract_failures "
                "ORDER BY ts DESC, id DESC LIMIT ?", (max(1, int(limit)),),
            ).fetchall()
        return [{"id": int(r["id"]), "ts": float(r["ts"]), "session": r["session"],
                 "reason": r["reason"], "detail": r["detail"]} for r in rows]

    def clear_extract_failures(self) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM extract_failures")
            self._db.commit()
        return int(cur.rowcount or 0)

    # ------------------------------------------------ manual edits (可视化界面)

    def update_event(
        self,
        eid: int,
        *,
        text: Optional[str] = None,
        kind: Optional[str] = None,
        edited_ts: Optional[float] = None,
        vec: Any = None,
    ) -> bool:
        """Edit an event's text/kind and stamp ``edited_ts`` (rainbow highlight in the graph).

        The FTS trigger keeps the lexical index in sync.  ``vec`` is optional on purpose: a
        typo fix does not have to evict the event from the vector arm, so the caller decides
        whether to re-encode (the graph's editor always does — its button says so).
        """
        sets: list[str] = []
        params: list[Any] = []
        if text is not None:
            text = text.strip()
            if not text:
                raise ValueError("text must be non-empty")
            sets.append("text = ?")
            params.append(text)
        if kind is not None:
            if kind not in KINDS:
                raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
            sets.append("kind = ?")
            params.append(kind)
        if not sets:
            return False
        if vec is not None:
            sets.append("vec = ?")
            params.append(_as_bytes(vec))
        sets.append("edited_ts = ?")
        params.append(float(edited_ts if edited_ts is not None else time.time()))
        params.append(int(eid))
        with self._lock:
            cur = self._db.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", params)
            self._db.commit()
        return cur.rowcount > 0

    def set_edge(
        self,
        src: int,
        dst: int,
        rel: str,
        conf: float = 0.5,
        *,
        old_rel: Optional[str] = None,
        edited_ts: Optional[float] = None,
    ) -> None:
        """Write a human relation (``decided_by='human'``), optionally replacing ``old_rel``."""
        if rel not in RELS:
            raise ValueError(f"rel must be one of {RELS}, got {rel!r}")
        stamp = float(edited_ts if edited_ts is not None else time.time())
        cross = self._infer_cross(int(src), int(dst))
        with self._lock:
            if old_rel and old_rel != rel:
                self._db.execute(
                    "INSERT OR REPLACE INTO edge_deletions(src,dst,rel,ts) "
                    "SELECT src,dst,rel,? FROM edges WHERE src=? AND dst=? AND rel=?",
                    (stamp, int(src), int(dst), old_rel),
                )
                self._db.execute(
                    "DELETE FROM edges WHERE src = ? AND dst = ? AND rel = ?",
                    (int(src), int(dst), old_rel),
                )
            self._db.execute(
                "INSERT INTO edges(src, dst, rel, conf, decided_by, cross, probs, edited_ts) "
                "VALUES(?,?,?,?, 'human', ?, NULL, ?) "
                "ON CONFLICT(src, dst, rel) DO UPDATE SET "
                "decided_by = 'human', edited_ts = excluded.edited_ts",
                (int(src), int(dst), rel, float(conf), int(cross), stamp),
            )
            self._db.commit()

    def delete_edge(self, src: int, dst: int, rel: str) -> bool:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO edge_deletions(src,dst,rel,ts) "
                    "SELECT src,dst,rel,? FROM edges WHERE src=? AND dst=? AND rel=?",
                    (time.time(), int(src), int(dst), rel),
                )
                cur = self._db.execute(
                    "DELETE FROM edges WHERE src = ? AND dst = ? AND rel = ?",
                    (int(src), int(dst), rel),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return cur.rowcount > 0

    def add_pair_eval(
        self,
        src: int,
        dst: int,
        *,
        axis_probs: Optional[dict] = None,
        detail_probs: Optional[dict] = None,
        axis_label: Optional[str] = None,
        detail_label: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        """Record one evaluated pair — including the ones that produced no edge (grey lines)."""
        with self._lock:
            self._db.execute(
                "INSERT INTO pair_evals(src, dst, axis_probs, detail_probs, axis_label, "
                "detail_label, ts) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(src, dst) DO UPDATE SET axis_probs = excluded.axis_probs, "
                "detail_probs = excluded.detail_probs, axis_label = excluded.axis_label, "
                "detail_label = excluded.detail_label, ts = excluded.ts",
                (
                    int(src),
                    int(dst),
                    json.dumps(axis_probs, ensure_ascii=False, sort_keys=True) if axis_probs else None,
                    json.dumps(detail_probs, ensure_ascii=False, sort_keys=True) if detail_probs else None,
                    axis_label,
                    detail_label,
                    float(ts if ts is not None else time.time()),
                ),
            )
            self._db.commit()

    def _infer_cross(self, src: int, dst: int) -> int:
        """Cross-project when both events have a primary project and those differ."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, project_id FROM events WHERE id IN (?, ?)", (src, dst)
            ).fetchall()
        by_id = {int(r["id"]): r["project_id"] for r in rows}
        a, b = by_id.get(src), by_id.get(dst)
        return 1 if (a is not None and b is not None and a != b) else 0

    def _neighbors(self, ids: Iterable[int], rels: Sequence[str]) -> set[int]:
        ids = sorted({int(i) for i in ids})
        rel_ph = _placeholders(len(rels))
        out: set[int] = set()
        with self._lock:
            for chunk in _chunks(ids, 400):
                ph = _placeholders(len(chunk))
                rows = self._db.execute(
                    f"SELECT src, dst FROM edges WHERE rel IN ({rel_ph}) AND src IN ({ph})",
                    (*rels, *chunk),
                ).fetchall()
                out.update(int(r["dst"]) for r in rows)
                rows = self._db.execute(
                    f"SELECT src, dst FROM edges WHERE rel IN ({rel_ph}) AND dst IN ({ph})",
                    (*rels, *chunk),
                ).fetchall()
                out.update(int(r["src"]) for r in rows)
        return out

    def chain(
        self, eid: int, rels: Sequence[str] = (REL_CHANGE, REL_DENY), max_depth: int = 20
    ) -> list[dict]:
        """The whole 改变/否定 connected chain containing ``eid``, ts ascending.

        Traversal is bidirectional (a hit on an old event must drag its replacement in, and a hit
        on the new one must drag the dead value in), bounded by ``max_depth`` hops and a visited
        set, so cycles terminate. Each returned event gains:

        * ``superseded_by`` — ``[{"id", "ts", "rel"}]`` of later in-chain events pointing at it
        * ``invalidated``   — True when at least one of those relations is ``改变``
        """
        rels = tuple(r for r in (rels or ()) if r in RELS)
        with self._lock:
            start = self._db.execute(_EVENT_SELECT + " WHERE e.id = ?", (int(eid),)).fetchone()
            if start is None:
                return []
            if not rels:
                return [_event_row(start)]

            seen: set[int] = {int(eid)}
            frontier: set[int] = {int(eid)}
            depth = 0
            cap = max(0, int(max_depth))
            while frontier and depth < cap:
                nxt = self._neighbors(frontier, rels) - seen
                if not nxt:
                    break
                seen |= nxt
                frontier = nxt
                depth += 1

            events = self._events_by_ids(sorted(seen))
            by_id = {e["id"]: e for e in events}
            for e in events:
                e["superseded_by"] = []
                e["invalidated"] = False

            rel_ph = _placeholders(len(rels))
            for chunk in _chunks(sorted(seen), 400):
                ph = _placeholders(len(chunk))
                rows = self._db.execute(
                    f"SELECT src, dst, rel FROM edges WHERE rel IN ({rel_ph}) AND dst IN ({ph})",
                    (*rels, *chunk),
                ).fetchall()
                for r in rows:
                    src, dst = int(r["src"]), int(r["dst"])
                    if src not in by_id or dst not in by_id:
                        continue  # edge leaving the depth-limited component
                    if not _is_newer(by_id[src], by_id[dst]):
                        continue  # 反向边是坏数据：不能把更早的一条标成「被取代」
                    by_id[dst]["superseded_by"].append(
                        {"id": src, "ts": by_id[src]["ts"], "rel": r["rel"]}
                    )

            for e in events:
                e["superseded_by"].sort(key=lambda x: (x["ts"], x["id"]))
                e["invalidated"] = any(x["rel"] == REL_CHANGE for x in e["superseded_by"])
            return events

    def inbound_degree(self, eid: int) -> int:
        with self._lock:
            row = self._db.execute("SELECT count(*) FROM edges WHERE dst = ?", (int(eid),)).fetchone()
        return int(row[0])

    def edges_into(self, eid: int) -> list[dict]:
        """Edges pointing at ``eid``, each joined with the source event's ts/kind/text."""
        with self._lock:
            rows = self._db.execute(
                "SELECT g.src, g.dst, g.rel, g.conf, g.decided_by, g.cross, g.backend, "
                "       e.ts AS src_ts, e.kind AS src_kind, e.text AS src_text "
                "FROM edges g LEFT JOIN events e ON e.id = g.src "
                "WHERE g.dst = ? ORDER BY e.ts ASC, g.src ASC",
                (int(eid),),
            ).fetchall()
        return [
            {"src": int(r["src"]), "dst": int(r["dst"]), "rel": r["rel"], "conf": float(r["conf"]),
             "decided_by": r["decided_by"], "cross": int(r["cross"]), "backend": r["backend"],
             "src_ts": float(r["src_ts"]) if r["src_ts"] is not None else None,
             "src_kind": r["src_kind"], "src_text": r["src_text"]}
            for r in rows
        ]

    def edges_from(self, eid: int) -> list[dict]:
        """Edges starting at ``eid`` (it points at older events), joined with the target.

        Mirror of :meth:`edges_into`: the graph has one direction (new → old), so a
        navigation tool needs both sides to show "what replaced me" and "what I explain".
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT g.src, g.dst, g.rel, g.conf, g.decided_by, g.cross, g.backend, "
                "       e.ts AS dst_ts, e.kind AS dst_kind, e.text AS dst_text "
                "FROM edges g LEFT JOIN events e ON e.id = g.dst "
                "WHERE g.src = ? ORDER BY e.ts ASC, g.dst ASC",
                (int(eid),),
            ).fetchall()
        return [
            {"src": int(r["src"]), "dst": int(r["dst"]), "rel": r["rel"], "conf": float(r["conf"]),
             "decided_by": r["decided_by"], "cross": int(r["cross"]), "backend": r["backend"],
             "dst_ts": float(r["dst_ts"]) if r["dst_ts"] is not None else None,
             "dst_kind": r["dst_kind"], "dst_text": r["dst_text"]}
            for r in rows
        ]

    def head_ids(self) -> set[int]:
        """Events nothing has superseded: no incoming 改变 / 否定 / 重复.

        A retracted value (改变), a rejected approach (否定) and a restatement (重复) all stop
        being candidates for the next comparison.  They stay in the database and in recall —
        only the pairing set shrinks.
        """
        ph = _placeholders(len(HEAD_KILL_RELS))
        with self._lock:
            rows = self._db.execute(
                "SELECT e.id FROM events e WHERE NOT EXISTS ("
                f" SELECT 1 FROM edges g WHERE g.dst = e.id AND g.rel IN ({ph}))",
                HEAD_KILL_RELS,
            ).fetchall()
        return {int(r[0]) for r in rows}

    def session_before(
        self, session: Optional[str], ts: float, eid: int, limit: int,
        kinds: Optional[Sequence[str]] = None,
    ) -> list[dict]:
        """Up to ``limit`` most recent events of ``session`` strictly older than ``(ts, eid)``."""
        if not session or int(limit) <= 0:
            return []
        where = "e.session = ? AND (e.ts < ? OR (e.ts = ? AND e.id < ?))"
        params: list[Any] = [str(session), float(ts), float(ts), int(eid)]
        if kinds:
            where += f" AND e.kind IN ({_placeholders(len(kinds))})"
            params.extend(kinds)
        with self._lock:
            return self._select_events(where, tuple(params), order="e.ts DESC, e.id DESC",
                                       limit=int(limit))

    def unclosed_lessons(
        self, ts: float, eid: int, limit: int = 2, scan: int = 30,
    ) -> list[dict]:
        """The latest ``limit`` 教训 events with no later 结果/变更 in their project.

        Closed = the project (or, for an unfiled lesson, the session) produced a 结果 or 变更
        afterwards: the lesson has been acted on.  Open lessons are the ones worth re-reading
        when a new event shows up.
        """
        if int(limit) <= 0:
            return []
        with self._lock:
            rows = self._select_events(
                "e.kind = ? AND (e.ts < ? OR (e.ts = ? AND e.id < ?))",
                (KINDS[-1], float(ts), float(ts), int(eid)),
                order="e.ts DESC, e.id DESC", limit=int(scan),
            )
            out: list[dict] = []
            for ev in rows:
                closed = self._db.execute(
                    "SELECT 1 FROM events x WHERE x.ts > ? AND x.kind IN ('结果','变更') AND ("
                    " (? IS NOT NULL AND x.project_id = ?) OR (? IS NULL AND x.session = ?)) LIMIT 1",
                    (float(ev["ts"]), ev["project_id"], ev["project_id"],
                     ev["project_id"], ev["session"]),
                ).fetchone()
                if closed is not None:
                    continue
                out.append(ev)
                if len(out) >= int(limit):
                    break
        return out

    def active_decisions(
        self, project_ids: Sequence[int], ts: float, eid: int, limit: int = 5,
    ) -> list[dict]:
        """Latest ``limit`` 决定 of those projects that nothing has 改变/否定-ed."""
        ids = [int(p) for p in project_ids or ()]
        if not ids or int(limit) <= 0:
            return []
        ph = _placeholders(len(ids))
        where = (
            f"e.kind = '决定' AND (e.ts < ? OR (e.ts = ? AND e.id < ?)) "
            f"AND (e.project_id IN ({ph}) OR e.id IN "
            f"(SELECT event_id FROM event_projects WHERE project_id IN ({ph}))) "
            "AND NOT EXISTS (SELECT 1 FROM edges g WHERE g.dst = e.id "
            "AND g.rel IN ('改变','否定'))"
        )
        params: list[Any] = [float(ts), float(ts), int(eid), *ids, *ids]
        with self._lock:
            return self._select_events(where, tuple(params), order="e.ts DESC, e.id DESC",
                                       limit=int(limit))

    # ------------------------------------------------------------------- search

    def fts_search(self, query: str, limit: int = 20) -> list[tuple[int, float]]:
        """Lexical arm: ``[(event_id, bm25_score)]``, best first (score is negative).

        Terms are ORed: a phrase query would require the user's whole sentence to appear verbatim.
        Both lexical tables are searched — ``events.text`` directly and ``artifacts.text`` mapped
        back through ``events.art_id`` — because a command or an error code lives in the artifact
        while the event sentence only says "跑了一次训练" (design §6).

        When the query has no trigram-matchable term (two Chinese characters, say), a LIKE scan
        returns substring hits scored ``-1e-12`` so they sort after real BM25 hits.
        """
        q = (query or "").strip()
        limit = max(1, int(limit))
        if not q:
            return []

        scores: dict[int, float] = {}

        def keep(eid: int, score: float) -> None:
            prev = scores.get(eid)
            scores[eid] = score if prev is None else min(prev, score)  # more negative = better

        terms = _fts_terms(q)
        if terms:
            expr = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            try:
                with self._lock:
                    rows = self._db.execute(
                        "SELECT rowid AS eid, bm25(events_fts) AS score FROM events_fts "
                        "WHERE events_fts MATCH ? ORDER BY score LIMIT ?",
                        (expr, limit),
                    ).fetchall()
                    for r in rows:
                        keep(int(r["eid"]), float(r["score"]))
                    self._artifact_hits(expr, limit, keep)
                if scores:
                    return _ranked(scores, limit)
            except sqlite3.Error:
                pass  # malformed MATCH must degrade, not explode into the chat path

        like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM events WHERE text LIKE ? ESCAPE '\\' "
                "ORDER BY length(text) ASC, id ASC LIMIT ?",
                (f"%{like}%", limit),
            ).fetchall()
            for r in rows:
                keep(int(r["id"]), -1e-12)
            rows = self._db.execute(
                "SELECT id FROM artifacts WHERE text LIKE ? ESCAPE '\\' "
                "ORDER BY length(text) ASC, id ASC LIMIT ?",
                (f"%{like}%", limit),
            ).fetchall()
            aids = [int(r["id"]) for r in rows]
            if aids:
                ph = _placeholders(len(aids))
                for ev in self._db.execute(
                    f"SELECT id, art_id FROM events WHERE art_id IN ({ph})", tuple(aids)
                ).fetchall():
                    keep(int(ev["id"]), -1e-12)
        return _ranked(scores, limit)

    def _artifact_hits(self, expr: str, limit: int, keep: Callable[[int, float], None]) -> None:
        """Map an ``artifacts_fts`` MATCH onto the events that carry those artifacts."""
        rows = self._db.execute(
            "SELECT rowid AS aid, bm25(artifacts_fts) AS score FROM artifacts_fts "
            "WHERE artifacts_fts MATCH ? ORDER BY score LIMIT ?",
            (expr, limit),
        ).fetchall()
        if not rows:
            return
        by_aid = {int(r["aid"]): float(r["score"]) for r in rows}
        ph = _placeholders(len(by_aid))
        for ev in self._db.execute(
            f"SELECT id, art_id FROM events WHERE art_id IN ({ph})", tuple(by_aid)
        ).fetchall():
            aid = ev["art_id"]
            if aid is not None:
                keep(int(ev["id"]), by_aid[int(aid)])

    def iter_vecs(self) -> list[tuple[int, bytes]]:
        """``[(event_id, vec)]`` for events that have an embedding, id ascending."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, vec FROM events WHERE vec IS NOT NULL ORDER BY id ASC"
            ).fetchall()
        return [(int(r["id"]), bytes(r["vec"])) for r in rows]

    # ------------------------------------------------------------------- forget

    def forget(self, eid: int) -> None:
        """物理删除一条事件：事件、进出的边、event_projects、独占 artifact、墓碑。

        Tombstone stores only ``(id, sha256(text), ts)`` — never the text, so the same sentence
        cannot be re-extracted from an old message later. WAL is truncated so the deleted text
        does not linger in a side file.
        """
        eid = int(eid)
        with self._lock:
            row = self._db.execute("SELECT text, art_id FROM events WHERE id = ?", (eid,)).fetchone()
            if row is None:
                return  # already gone — forget is idempotent
            sha = _sha256(row["text"])
            art_id = row["art_id"]

            self._db.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (eid, eid))
            self._db.execute("DELETE FROM event_projects WHERE event_id = ?", (eid,))
            self._db.execute("DELETE FROM events WHERE id = ?", (eid,))  # FTS trigger fires here
            if art_id is not None:
                left = self._db.execute(
                    "SELECT count(*) FROM events WHERE art_id = ?", (art_id,)
                ).fetchone()[0]
                if not left:
                    self._db.execute("DELETE FROM artifacts WHERE id = ?", (art_id,))
            self._db.execute(
                "INSERT INTO tombstones(id, sha, ts) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET sha = excluded.sha, ts = excluded.ts",
                (eid, sha, time.time()),
            )
            self._db.commit()
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    def is_tombstoned(self, eid: int) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM tombstones WHERE id = ?", (int(eid),)).fetchone()
        return row is not None

    def is_tombstoned_text(self, text: str) -> bool:
        """True when this exact text was forgotten — the backfill/re-extraction guard (design §8)."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM tombstones WHERE sha = ?", (_sha256(text),)
            ).fetchone()
        return row is not None
