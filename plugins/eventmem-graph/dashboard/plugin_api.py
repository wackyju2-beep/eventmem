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
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

KINDS = ("决定", "尝试", "结果", "变更", "教训")
RELS = ("改变", "否定", "重复", "因为", "细化")
MAX_TEXT = 500

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


# --------------------------------------------------------------------------- write


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
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"事件 #{eid} 不存在")
        return {"ok": True, "id": int(eid)}
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
