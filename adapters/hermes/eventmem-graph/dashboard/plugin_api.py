"""Read/write API for the eventmem 3D graph (desktop UI).

Mounted at ``/api/plugins/eventmem-graph/``. It talks to ``$HERMES_HOME/eventmem/events.db``
directly: the graph needs the whole事件/边表 (the memory provider's recall queries are all
top-k), and the two人工编辑 are plain UPDATEs. Keeping it out of ``eventmem/store.py`` means the
memory plugin never imports FastAPI and this plugin never registers a provider.

Schema v6 adds ``events.edited_ts`` / ``edges.edited_ts`` and ``pair_evals``. The migrations are
idempotent and run here too, so opening the graph before the next worker pass still works.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

KINDS = ("决定", "尝试", "结果", "变更", "教训")
RELS = ("改变", "否定", "重复", "因为", "细化")
MAX_TEXT = 500
#: 一次批量编辑最多碰多少条事件（右键框选的现实上限，也是别把单次事务拖成分钟级）。
MAX_BATCH = 500

#: ``core/`` sits four levels up from this file when the plugin is a symlink into the
#: repository (the normal install): adapters/hermes/eventmem-graph/dashboard/ → repo.
_CORE_ROOT = Path(__file__).resolve().parents[4] / "core"
if (_CORE_ROOT / "eventmem_core").is_dir() and str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

#: The ONNX encoder is ~50 MB of model + a session; build it once, lazily, and only when
#: the semantic filter is actually used.
_ENCODER: Any = None
_ENCODER_LOCK = threading.Lock()


def _encoder() -> Any:
    global _ENCODER
    if _ENCODER is None:
        with _ENCODER_LOCK:
            if _ENCODER is None:
                from eventmem_core.embed import Encoder

                _ENCODER = Encoder()
    return _ENCODER

_DDL = """
CREATE TABLE IF NOT EXISTS pair_evals (
  src          INTEGER NOT NULL,
  dst          INTEGER NOT NULL,
  axis_probs   TEXT,
  detail_probs TEXT,
  axis_label   TEXT,
  detail_label TEXT,
  ts           REAL NOT NULL,
  PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS idx_pair_evals_dst ON pair_evals(dst);
CREATE TABLE IF NOT EXISTS edge_deletions (
  src INTEGER NOT NULL, dst INTEGER NOT NULL, rel TEXT NOT NULL, ts REAL NOT NULL,
  PRIMARY KEY (src, dst, rel)
);
CREATE TRIGGER IF NOT EXISTS edge_deletions_block_jev BEFORE INSERT ON edges
WHEN NEW.decided_by = 'jev' AND EXISTS (
  SELECT 1 FROM edge_deletions WHERE src = NEW.src AND dst = NEW.dst
)
BEGIN
  SELECT RAISE(IGNORE);
END;
CREATE TABLE IF NOT EXISTS atom_pos (
  id         INTEGER PRIMARY KEY,
  x          REAL NOT NULL,
  y          REAL NOT NULL,
  z          REAL NOT NULL,
  updated_ts REAL NOT NULL
);
"""


# --------------------------------------------------------------------------- plumbing


def _db_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "eventmem" / "events.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    """Idempotent v6 migration — the API must work even before the worker reopens the DB."""
    try:
        conn.executescript(_DDL)
        event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        if "edited_ts" not in event_cols:
            conn.execute("ALTER TABLE events ADD COLUMN edited_ts REAL")
        edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
        if "edited_ts" not in edge_cols:
            conn.execute("ALTER TABLE edges ADD COLUMN edited_ts REAL")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()


def _loads(raw: Any) -> Optional[dict]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- read


def _read_graph() -> dict[str, Any]:
    conn = _connect()
    try:
        _ensure(conn)

        events = [
            {
                "id": int(r["id"]),
                "ts": float(r["ts"]),
                "kind": r["kind"],
                "text": r["text"],
                "project": r["project"],
                "project_id": r["project_id"],
                "session": r["session"],
                "edited_ts": r["edited_ts"],
            }
            for r in conn.execute(
                "SELECT e.id, e.ts, e.kind, e.text, e.project_id, e.session, e.edited_ts, "
                "       p.name AS project "
                "FROM events e LEFT JOIN projects p ON p.id = e.project_id "
                "ORDER BY e.id ASC"
            )
        ]
        known = {e["id"] for e in events}

        edges = [
            {
                "src": int(r["src"]),
                "dst": int(r["dst"]),
                "rel": r["rel"],
                "conf": float(r["conf"]),
                "decided_by": r["decided_by"],
                "cross": int(r["cross"]),
                "probs": _loads(r["probs"]),
                "edited_ts": r["edited_ts"],
            }
            for r in conn.execute(
                "SELECT src, dst, rel, conf, decided_by, cross, probs, edited_ts "
                "FROM edges ORDER BY src ASC, dst ASC"
            )
            if int(r["src"]) in known and int(r["dst"]) in known
        ]

        pairs = [
            {
                "src": int(r["src"]),
                "dst": int(r["dst"]),
                "axis_probs": _loads(r["axis_probs"]),
                "detail_probs": _loads(r["detail_probs"]),
                "axis_label": r["axis_label"],
                "detail_label": r["detail_label"],
                "ts": float(r["ts"]),
            }
            for r in conn.execute(
                "SELECT src, dst, axis_probs, detail_probs, axis_label, detail_label, ts "
                "FROM pair_evals ORDER BY ts DESC LIMIT 4000"
            )
            if int(r["src"]) in known and int(r["dst"]) in known
        ]

        projects = [
            {"id": int(r["id"]), "name": r["name"], "status": r["status"]}
            for r in conn.execute("SELECT id, name, status FROM projects ORDER BY name")
        ]

        sessions = [
            {
                "id": r["session"],
                "count": int(r["n"]),
                "first": float(r["first_ts"]),
                "last": float(r["last_ts"]),
            }
            for r in conn.execute(
                "SELECT session, count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts "
                "FROM events WHERE session IS NOT NULL AND session != '' "
                "GROUP BY session ORDER BY last_ts DESC"
            )
        ]

        positions = [
            {"id": int(r["id"]), "x": float(r["x"]), "y": float(r["y"]), "z": float(r["z"])}
            for r in conn.execute("SELECT id, x, y, z FROM atom_pos")
            if int(r["id"]) in known
        ]

        vec = conn.execute(
            "SELECT count(vec) AS n, max(length(vec)) AS bytes FROM events"
        ).fetchone()

        return {
            "ok": True,
            "db": str(_db_path()),
            "generated_at": time.time(),
            "events": events,
            "edges": edges,
            "pairs": pairs,
            "positions": positions,
            "projects": projects,
            "sessions": sessions,
            "kinds": list(KINDS),
            "rels": list(RELS),
            # 语义筛选的前提：已经有事件向量。空表 / 全 NULL 时前端会把入口标成不可用。
            "vectors": {"events": int(vec["n"] or 0), "dim": int((vec["bytes"] or 0) // 4)},
        }
    finally:
        conn.close()


@router.get("/graph")
async def graph() -> dict[str, Any]:
    try:
        return _read_graph()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"eventmem 数据库不存在：{exc}") from exc
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail=f"读取 eventmem 数据库失败：{exc}") from exc


@router.get("/health")
async def health() -> dict[str, Any]:
    try:
        conn = _connect()
        try:
            n = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        return {"ok": True, "db": str(_db_path()), "events": int(n)}
    except Exception as exc:  # noqa: BLE001 - health must answer, not raise
        return {"ok": False, "db": str(_db_path()), "error": str(exc)}


# --------------------------------------------------------------------------- semantic filter


@router.post("/semantic")
def semantic(body: dict[str, Any]) -> dict[str, Any]:
    """Rank events by embedding similarity to a free-text query.

    The query goes through the same encoder/instruction the recall path uses
    (``is_query=True``), and the stored ``events.vec`` are already L2-normalised, so a
    dot product is the cosine.  ``top`` is a hard cap on returned hits; the graph
    intersects them with its other filters.
    """
    query = body.get("q")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="q 不能为空")
    query = query.strip()
    if len(query) > MAX_TEXT:
        raise HTTPException(status_code=400, detail=f"q 最多 {MAX_TEXT} 字")
    try:
        top = int(body.get("top") if body.get("top") is not None else 16)
    except (TypeError, ValueError):
        top = 16
    top = max(1, min(top, 200))
    raw_min = body.get("min_score")
    try:
        min_score = float(raw_min) if raw_min is not None else None
    except (TypeError, ValueError):
        min_score = None

    try:
        encoder = _encoder()
    except Exception as exc:  # noqa: BLE001 - missing core / broken install must answer
        raise HTTPException(status_code=503, detail=f"Embedding 编码器不可用：{exc}") from exc
    check = getattr(encoder, "available", None)
    if callable(check) and not check():
        reason = getattr(encoder, "reason", None)
        detail = reason() if callable(reason) else "encoder unavailable"
        raise HTTPException(status_code=503, detail=f"Embedding 编码器不可用：{detail}")
    qvec = encoder.encode(query, is_query=True)
    if qvec is None:
        raise HTTPException(status_code=503, detail="Embedding 编码失败（模型文件或运行时缺失）")

    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"numpy 不可用：{exc}") from exc
    q = np.asarray(qvec, dtype=np.float32)
    norm = float(np.linalg.norm(q))
    if not norm or not math.isfinite(norm):
        raise HTTPException(status_code=503, detail="query 向量无效")
    q = q / norm

    try:
        conn = _connect()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"eventmem 数据库不存在：{exc}") from exc
    try:
        rows = conn.execute("SELECT id, vec FROM events WHERE vec IS NOT NULL").fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail=f"读取向量失败：{exc}") from exc
    finally:
        conn.close()

    hits: list[tuple[int, float]] = []
    skipped = 0
    for row in rows:
        blob = row["vec"]
        if not isinstance(blob, (bytes, bytearray)) or len(blob) % 4:
            skipped += 1
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.size != q.size:
            # 维度不同的旧向量不参与排序（编码器换代时会是这样），但不能当成 0 分。
            skipped += 1
            continue
        score = float(np.dot(vec, q))
        if not math.isfinite(score):
            skipped += 1
            continue
        if min_score is not None and score < min_score:
            continue
        hits.append((int(row["id"]), score))
    hits.sort(key=lambda item: (-item[1], item[0]))

    return {
        "ok": True,
        "query": query,
        "top": top,
        "dim": int(q.size),
        "scanned": len(rows),
        "skipped": skipped,
        "hits": [{"id": eid, "score": round(score, 4)} for eid, score in hits[:top]],
    }


# --------------------------------------------------------------------------- write


def _vec_blob(vec: Any) -> Optional[bytes]:
    """float32 bytes for ``events.vec``: numpy arrays, bytes, or a plain list."""
    if vec is None:
        return None
    raw = getattr(vec, "tobytes", None)
    if callable(raw):
        return raw()
    if isinstance(vec, (bytes, bytearray, memoryview)):
        return bytes(vec)
    try:
        import struct

        return struct.pack("<%df" % len(vec), *[float(x) for x in vec])
    except Exception:  # noqa: BLE001 - a bad vector must not fail the edit
        return None


def _reembed(conn: sqlite3.Connection, eid: int) -> tuple[str, int, str]:
    """Re-encode one event from its stored text/kind/project.

    Same payload as ``EventMem.add_event`` (``[项目] 类型 正文``) so an edited event keeps
    competing in the vector arm with the text a human just approved.  Returns
    ``(state, dim, note)`` where state is ``updated`` / ``skipped``; a missing encoder skips
    the vector only — the text edit always lands.
    """
    row = conn.execute(
        "SELECT e.kind AS kind, e.text AS text, p.name AS project FROM events e "
        "LEFT JOIN projects p ON p.id = e.project_id WHERE e.id = ?", (int(eid),)
    ).fetchone()
    if row is None:
        return "skipped", 0, "事件不存在"
    try:
        encoder = _encoder()
    except Exception as exc:  # noqa: BLE001
        return "skipped", 0, f"Embedding 编码器不可用：{exc}"
    check = getattr(encoder, "available", None)
    if callable(check) and not check():
        reason = getattr(encoder, "reason", None)
        detail = reason() if callable(reason) else "encoder unavailable"
        return "skipped", 0, f"Embedding 编码器不可用：{detail}"
    payload = "[%s] %s %s" % (row["project"] or "未归类", row["kind"], row["text"])
    vec = encoder.encode(payload)
    blob = _vec_blob(vec)
    if not blob:
        return "skipped", 0, "Embedding 编码失败（模型文件或运行时缺失）"
    conn.execute("UPDATE events SET vec = ? WHERE id = ?", (blob, int(eid)))
    return "updated", len(blob) // 4, ""


@router.patch("/events/{eid}")
async def patch_event(eid: int, body: dict[str, Any]) -> dict[str, Any]:
    text = body.get("text")
    kind = body.get("kind")
    if text is None and kind is None:
        raise HTTPException(status_code=400, detail="text 或 kind 至少要改一个")
    if text is not None:
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="正文不能为空")
        if len(text) > MAX_TEXT:
            raise HTTPException(status_code=400, detail=f"正文最多 {MAX_TEXT} 字")
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"类型必须是 {'/'.join(KINDS)}")

    conn = _connect()
    try:
        _ensure(conn)
        sets, params = [], []
        if text is not None:
            sets.append("text = ?")
            params.append(text.strip())
        if kind is not None:
            sets.append("kind = ?")
            params.append(kind)
        sets.append("edited_ts = ?")
        params.append(time.time())
        params.append(int(eid))
        cur = conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"事件 #{eid} 不存在")
        # 改完正文就重算向量：否则检索（向量臂）还在用旧文本，改了半天召回不到。
        state, dim, note = _reembed(conn, int(eid))
        conn.commit()
        return {"ok": True, "id": int(eid), "vector": state, "dim": int(dim), "note": note}
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入失败：{exc}") from exc
    finally:
        conn.close()


def _project_id(conn: sqlite3.Connection, name: Any) -> Optional[int]:
    """``None``/``""``/``unfiled``/``未归类`` → NULL (未归类); any other name must exist."""
    if name is None:
        return None
    name = str(name).strip()
    if not name or name.lower() == "unfiled" or name == "未归类":
        return None
    row = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail=f"没有项目 {name!r}；先在图谱里建（proj_add）")
    return int(row["id"])


def _refresh_cross(conn: sqlite3.Connection, ids: list[int]) -> int:
    """Re-mark ``edges.cross`` for the edges touching ``ids`` — the flag follows the projects.

    Moving an atom into its partner's project has to clear the cross mark, and moving it out has
    to set it.  The relation itself is untouched: a human edit is never destroyed by a re-mark.
    """
    ph = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT e.src AS src, e.dst AS dst, e.cross AS cross, "
        "       a.project_id AS pa, b.project_id AS pb "
        "FROM edges e JOIN events a ON a.id = e.src JOIN events b ON b.id = e.dst "
        f"WHERE e.src IN ({ph}) OR e.dst IN ({ph})",
        (*ids, *ids),
    ).fetchall()
    changed = 0
    for row in rows:
        pa, pb = row["pa"], row["pb"]
        want = 1 if (pa is not None and pb is not None and pa != pb) else 0
        if int(row["cross"] or 0) != want:
            conn.execute("UPDATE edges SET cross = ? WHERE src = ? AND dst = ?",
                         (want, int(row["src"]), int(row["dst"])))
            changed += 1
    return changed


def _mark_reseat(conn: sqlite3.Connection, ids: list[int]) -> float:
    """Ask the next link pass to start before the oldest touched event (0 = already covered).

    Candidate selection depends on the projects, so a project edit invalidates the *candidates*
    of every event from here on — but not the judgments already in ``pair_evals``.  The pass
    therefore re-selects (cheap) and asks the model only about pairs it has never seen.  Never
    moves forward: an earlier pending marker wins.
    """
    from eventmem_core.nightly import RESEAT_KEY

    ph = ",".join("?" * len(ids))
    row = conn.execute(f"SELECT min(ts) AS ts FROM events WHERE id IN ({ph})", ids).fetchone()
    stamp = row["ts"] if row is not None else None
    if stamp is None:
        return 0.0
    want = float(stamp) - 1.0
    current = conn.execute("SELECT value FROM meta WHERE key = ?", (RESEAT_KEY,)).fetchone()
    try:
        have = float(current["value"]) if current is not None else 0.0
    except (TypeError, ValueError):
        have = 0.0
    if 0 < have <= want:
        return have
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (RESEAT_KEY, "%.3f" % want),
    )
    return want


@router.post("/events/batch")
async def batch_events(body: dict[str, Any]) -> dict[str, Any]:
    """Batch-edit ``project`` / ``kind`` on a lasso selection.

    A project is not a cosmetic label: it is part of the re-encoded vector payload
    (``[项目] 类型 正文``), it is a membership row in ``event_projects`` (that is what candidate
    selection reads) and it decides ``edges.cross``.  So one call does all four things — the
    columns, the membership row, the vector, the cross flags — and then marks the candidate
    reseat.  No edge is deleted: existing relations stay, only never-asked pairs can appear.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="要一个 JSON 对象")
    raw = body.get("ids")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="ids 必须是非空数组")
    ids: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=400, detail="ids 只能是事件编号")
        if value not in ids:
            ids.append(value)
        if len(ids) > MAX_BATCH:
            raise HTTPException(status_code=400, detail=f"一次最多改 {MAX_BATCH} 条")
    has_project = "project" in body
    kind = body.get("kind")
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"类型必须是 {'/'.join(KINDS)}")
    if not has_project and kind is None:
        raise HTTPException(status_code=400, detail="project 或 kind 至少要改一个")

    conn = _connect()
    try:
        _ensure(conn)
        pid = _project_id(conn, body.get("project")) if has_project else None
        ph = ",".join("?" * len(ids))
        found = [int(r["id"]) for r in conn.execute(f"SELECT id FROM events WHERE id IN ({ph})", ids)]
        found.sort()
        if not found:
            raise HTTPException(status_code=404, detail="这些事件都不存在")
        missing = sorted(set(ids) - set(found))

        assigns: list[str] = []
        values: list[Any] = []
        if has_project:
            assigns.append("project_id = ?")
            values.append(pid)
        if kind is not None:
            assigns.append("kind = ?")
            values.append(kind)
        assigns.append("edited_ts = ?")
        stamp = time.time()
        for eid in found:
            conn.execute(f"UPDATE events SET {', '.join(assigns)} WHERE id = ?", (*values, stamp, eid))

        if has_project:
            # 主项目是 event_projects 里唯一一行 is_primary=1（有 partial unique index 盯着）。
            conn.execute(f"DELETE FROM event_projects WHERE is_primary = 1 AND event_id IN ({ph})", found)
            if pid is not None:
                for eid in found:
                    conn.execute(
                        "INSERT INTO event_projects(event_id, project_id, is_primary) VALUES(?,?,1) "
                        "ON CONFLICT(event_id, project_id) DO UPDATE SET is_primary = 1",
                        (eid, pid),
                    )

        # 项目进了向量 payload，不重算等于检索还按旧项目走。
        vector = {"updated": 0, "skipped": 0, "note": ""}
        for eid in found:
            state, _dim, note = _reembed(conn, eid)
            vector[state] = vector.get(state, 0) + 1
            if state == "skipped" and not vector["note"]:
                vector["note"] = note

        cross = _refresh_cross(conn, found)
        reseat = _mark_reseat(conn, found)
        conn.commit()
        return {
            "ok": True,
            "updated": len(found),
            "missing": missing,
            "vector": vector,
            "cross": cross,
            "reseat": reseat or None,
        }
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入失败：{exc}") from exc
    finally:
        conn.close()


@router.patch("/edges")
async def patch_edge(body: dict[str, Any]) -> dict[str, Any]:
    """Create or re-point a human relation. ``old_rel`` present = replace it."""
    try:
        src, dst = int(body["src"]), int(body["dst"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="src/dst 必须是事件编号") from exc
    rel = body.get("rel")
    old_rel = body.get("old_rel")
    if rel not in RELS:
        raise HTTPException(status_code=400, detail=f"关系必须是 {'/'.join(RELS)}")
    if old_rel is not None and old_rel not in RELS:
        raise HTTPException(status_code=400, detail="old_rel 不合法")
    conf = body.get("conf")
    try:
        conf = float(conf) if conf is not None else 0.5
    except (TypeError, ValueError):
        conf = 0.5

    conn = _connect()
    try:
        _ensure(conn)
        if old_rel and old_rel != rel:
            conn.execute(
                "INSERT OR REPLACE INTO edge_deletions(src,dst,rel,ts) "
                "SELECT src,dst,rel,? FROM edges WHERE src=? AND dst=? AND rel=?",
                (time.time(), src, dst, old_rel),
            )
            conn.execute(
                "DELETE FROM edges WHERE src = ? AND dst = ? AND rel = ?",
                (src, dst, old_rel),
            )
        cross = 0
        row = conn.execute(
            "SELECT (SELECT project_id FROM events WHERE id = ?) AS a, "
            "       (SELECT project_id FROM events WHERE id = ?) AS b",
            (src, dst),
        ).fetchone()
        if row["a"] is not None and row["b"] is not None and row["a"] != row["b"]:
            cross = 1
        conn.execute(
            "INSERT INTO edges(src, dst, rel, conf, decided_by, cross, probs, edited_ts) "
            "VALUES(?,?,?,?, 'human', ?, NULL, ?) "
            "ON CONFLICT(src, dst, rel) DO UPDATE SET "
            "decided_by = 'human', edited_ts = excluded.edited_ts",
            (src, dst, rel, conf, cross, time.time()),
        )
        conn.commit()
        return {"ok": True, "src": src, "dst": dst, "rel": rel}
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入失败：{exc}") from exc
    finally:
        conn.close()


@router.delete("/edges")
async def delete_edge(src: int, dst: int, rel: str) -> dict[str, Any]:
    if rel not in RELS:
        raise HTTPException(status_code=400, detail=f"关系必须是 {'/'.join(RELS)}")
    conn = _connect()
    try:
        _ensure(conn)
        conn.execute(
            "INSERT OR REPLACE INTO edge_deletions(src,dst,rel,ts) "
            "SELECT src,dst,rel,? FROM edges WHERE src=? AND dst=? AND rel=?",
            (time.time(), int(src), int(dst), rel),
        )
        cur = conn.execute(
            "DELETE FROM edges WHERE src = ? AND dst = ? AND rel = ?", (int(src), int(dst), rel)
        )
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}
    finally:
        conn.close()


@router.put("/positions")
async def put_positions(body: dict[str, Any]) -> dict[str, Any]:
    """摆过的位置存下来：刷新/重启后原子还在被拉到的位置。"""
    items = body.get("positions")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="positions 必须是数组")
    rows = []
    for item in items[:500]:
        try:
            pid = int(item["id"])
            x, y, z = float(item["x"]), float(item["y"]), float(item["z"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"位置条目不合法：{item!r}") from exc
        if not all(map(math.isfinite, (x, y, z))):
            raise HTTPException(status_code=400, detail="坐标必须是有限数")
        rows.append((pid, x, y, z, time.time()))
    if not rows:
        return {"ok": True, "saved": 0}
    conn = _connect()
    try:
        conn.executemany(
            "INSERT INTO atom_pos(id, x, y, z, updated_ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET x=excluded.x, y=excluded.y, z=excluded.z, "
            "updated_ts=excluded.updated_ts",
            rows,
        )
        conn.commit()
        return {"ok": True, "saved": len(rows)}
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入失败：{exc}") from exc
    finally:
        conn.close()


@router.delete("/positions")
async def clear_positions() -> dict[str, Any]:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM atom_pos")
        conn.commit()
        return {"ok": True, "cleared": cur.rowcount}
    finally:
        conn.close()
