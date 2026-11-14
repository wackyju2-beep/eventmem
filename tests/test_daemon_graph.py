#!/usr/bin/env python3
"""Tests for the graph data plane (``/api/mem/graph/*``).

Run directly:  <python> tests/test_daemon_graph.py

The graph is the one caller that reads the whole event/edge tables and the one place a human
edits memory by hand.  What must hold, and is checked here:

* the read payload has the shape the 3D view expects (keys, kinds, rels, positions, vectors);
* an edit stamps ``edited_ts`` and re-encodes the vector payload — a missing encoder skips the
  vector and says so, it never blocks the text edit;
* moving an event's project also moves the ``is_primary`` membership row, re-marks
  ``edges.cross`` and marks a candidate reseat — and deletes no edge;
* a batch is capped, reports missing ids, and never invents events;
* edge write/delete and saved positions round-trip.

The daemon runs for real (``ThreadingHTTPServer`` on port 0) and every call is real HTTP.
"""

from __future__ import annotations

import json
import os
import struct
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
import eventmem_core.nightly as nightly  # noqa: E402
from eventmem_core.settings import CoreSettings, DecisionPolicy  # noqa: E402

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


# --------------------------------------------------------------------------- helpers

def fresh_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # Store() creates it
    return path


def remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except PermissionError:  # Windows keeps the file locked while the daemon still holds it
        pass


def finish(daemon, db: str) -> None:
    """Close the long-lived ``EventMem`` (the db file is locked on Windows) and drop the db."""
    try:
        daemon.close_mem()
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass
    remove(db)


def settings_for(db: str) -> CoreSettings:
    from dataclasses import replace

    return replace(CoreSettings(db_path=db), decision=replace(DecisionPolicy(), url=""))


class running_server:
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

    def request(self, path: str, *, method: str = "GET", body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {} if data is None else {"Content-Type": "application/json"}
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 4xx/5xx come back as an exception
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        return False


def seed(daemon, kind: str, text: str, project: str = "") -> int:
    """Write one event through the store (no encoder involved)."""
    store = daemon.mem().store
    return store.add_event(kind, text, project=project or None)


def vec(*values: float) -> bytes:
    return struct.pack("<%df" % len(values), *values)


def edited_ts(store, eid: int):
    row = store._db.execute("SELECT edited_ts FROM events WHERE id = ?", (eid,)).fetchone()
    return row["edited_ts"] if row is not None else None


# --------------------------------------------------------------------------- read

@case
def test_graph_read_has_the_shape_the_view_needs():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        with running_server(daemon) as api:
            status, body = api.request("/api/mem/graph")
            check_eq(status, 200, "empty graph still answers")
            for key in ("events", "edges", "pairs", "positions", "projects", "sessions",
                        "kinds", "rels", "vectors", "generated_at", "db"):
                check(key in body, f"graph payload is missing {key!r}: {sorted(body)}")
            check_eq(body["events"], [], "no events yet")
            check_eq(body["kinds"], list(daemon_mod.KINDS), "kinds come from the store vocabulary")
            check_eq(body["rels"], list(daemon_mod.RELS), "rels too")
            check_eq(body["vectors"], {"events": 0, "dim": 0}, "no vectors yet")
            check_eq(body["db"], db, "db path echoed")

            first = seed(daemon, "决定", "先定方向", project="")
            second = seed(daemon, "结果", "跑通了", project="")
            daemon.mem().store.set_edge(second, first, "因为")

            status, body = api.request("/api/mem/graph")
            check_eq(status, 200, "read after seeding")
            check_eq([e["id"] for e in body["events"]], [first, second], "every event is drawn")
            check_eq(body["edges"][0]["rel"], "因为", "edge carries its relation")
            check_eq(body["edges"][0]["decided_by"], "human", "and who decided it")
            check("text" in body["events"][0], "the card text is part of the payload")
    finally:
        finish(daemon, db)


# --------------------------------------------------------------------------- edits

@case
def test_edit_stamps_edited_ts_and_reports_the_vector():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        with running_server(daemon) as api:
            eid = seed(daemon, "决定", "旧正文")

            status, body = api.request("/api/mem/graph/event", method="POST",
                                       body={"id": eid, "text": "新正文"})
            check_eq(status, 200, "edit ok")
            check_eq(body["id"], eid, "same event")
            check(body["vector"] in ("updated", "skipped"), f"vector state: {body['vector']}")
            if body["vector"] == "skipped":
                check(bool(body["note"]), "a skipped vector must explain itself")

            event = daemon.mem().store.get_event(eid)
            check_eq(event["text"], "新正文", "the text landed")
            check(edited_ts(daemon.mem().store, eid), "edited_ts is stamped (rainbow rim)")

            check_eq(api.request("/api/mem/graph/event", method="POST",
                                 body={"id": 9999, "text": "x"})[0], 404, "unknown event")
            check_eq(api.request("/api/mem/graph/event", method="POST",
                                 body={"id": eid})[0], 400, "nothing to change")
            check_eq(api.request("/api/mem/graph/event", method="POST",
                                 body={"id": eid, "text": "   "})[0], 400, "empty text")
            check_eq(api.request("/api/mem/graph/event", method="POST",
                                 body={"id": eid, "kind": "幻想"})[0], 400, "unknown kind")
            check_eq(api.request("/api/mem/graph/event", method="POST",
                                 body={"id": eid, "text": "x" * 501})[0], 400, "too long")
    finally:
        finish(daemon, db)


@case
def test_moving_a_project_moves_membership_and_cross_flags():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        store = daemon.mem().store
        store.add_project("甲项目")
        store.add_project("乙项目")
        with running_server(daemon) as api:
            a = seed(daemon, "决定", "甲的旧结论", project="甲项目")
            b = seed(daemon, "结果", "乙的新结果", project="乙项目")
            store.set_edge(b, a, "细化")
            check_eq(store._infer_cross(b, a), 1, "different projects → cross")

            status, body = api.request("/api/mem/graph/event", method="POST",
                                       body={"id": a, "project": "乙项目"})
            check_eq(status, 200, "project move ok")
            check_eq(body["vector"] in ("updated", "skipped"), True, "vector reported")
            check_eq(body["reseat"] is not None, True, "a project change marks a reseat")

            moved = store.get_event(a)
            check_eq(moved["project"], "乙项目", "events.project_id followed")
            primary = store._db.execute(
                "SELECT project_id, is_primary FROM event_projects WHERE event_id = ?", (a,)
            ).fetchall()
            check_eq(len(primary), 1, f"exactly one membership row, got {[dict(r) for r in primary]}")
            check_eq(primary[0]["is_primary"], 1, "and it is the primary one")
            check_eq(primary[0]["project_id"], store.get_project("乙项目")["id"], "in the new project")

            cross = store._db.execute(
                "SELECT cross FROM edges WHERE src = ? AND dst = ?", (b, a)
            ).fetchone()
            check_eq(int(cross["cross"]), 0, "same project now → cross cleared")
            check_eq(store._db.execute(
                "SELECT count(*) FROM edges WHERE src = ? AND dst = ?", (b, a)
            ).fetchone()[0], 1, "the edge itself is never deleted")

            meta = store.get_meta(nightly.RESEAT_KEY)
            check(meta is not None, "the reseat marker is written")
            check(float(meta) <= float(moved["ts"]) - 1.0 + 0.001,
                  f"and it starts before the edited event ({meta} vs {moved['ts']})")

            check_eq(api.request("/api/mem/graph/event", method="POST",
                                 body={"id": a, "project": "不存在的项目"})[0], 400,
                     "an unknown project is rejected, not invented")

            status, body = api.request("/api/mem/graph/event", method="POST",
                                       body={"id": a, "project": None})
            check_eq(status, 200, "clearing the project is allowed")
            check_eq(store.get_event(a)["project"], None, "back to 未归类")
            check_eq(store._db.execute(
                "SELECT count(*) FROM event_projects WHERE event_id = ?", (a,)
            ).fetchone()[0], 0, "and the membership row is gone")
    finally:
        finish(daemon, db)


@case
def test_batch_edits_the_selection_and_reports_missing_ids():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        store = daemon.mem().store
        store.add_project("批量项目")
        with running_server(daemon) as api:
            ids = [seed(daemon, "尝试", f"第 {i} 条", project="") for i in range(3)]

            status, body = api.request("/api/mem/graph/events", method="POST",
                                       body={"ids": ids, "kind": "教训", "project": "批量项目"})
            check_eq(status, 200, "batch ok")
            check_eq(body["updated"], 3, "all three changed")
            check_eq(body["missing"], [], "nothing missing")
            check_eq(body["cross"], 0, "no cross-project edges to fix")
            for eid in ids:
                event = store.get_event(eid)
                check_eq(event["kind"], "教训", f"#{eid} kind")
                check_eq(event["project"], "批量项目", f"#{eid} project")
                check(edited_ts(store, eid), f"#{eid} stamped")

            status, body = api.request("/api/mem/graph/events", method="POST",
                                       body={"ids": [ids[0], 424242], "kind": "结果"})
            check_eq(status, 200, "partial batch ok")
            check_eq(body["updated"], 1, "one real event")
            check_eq(body["missing"], [424242], "the unknown id is reported")

            check_eq(api.request("/api/mem/graph/events", method="POST",
                                 body={"ids": []})[0], 400, "empty selection")
            check_eq(api.request("/api/mem/graph/events", method="POST",
                                 body={"ids": [True]})[0], 400, "bools are not ids")
            check_eq(api.request("/api/mem/graph/events", method="POST",
                                 body={"ids": ids})[0], 400, "no change requested")
            big = list(range(1, 502))
            check_eq(api.request("/api/mem/graph/events", method="POST",
                                 body={"ids": big, "kind": "结果"})[0], 400, "batch cap")
    finally:
        finish(daemon, db)


# --------------------------------------------------------------------------- edges

@case
def test_human_edges_can_be_written_and_deleted():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        store = daemon.mem().store
        with running_server(daemon) as api:
            a = seed(daemon, "决定", "先做甲")
            b = seed(daemon, "变更", "后来改成乙")

            status, body = api.request("/api/mem/graph/edge", method="POST",
                                       body={"src": b, "dst": a, "rel": "改变", "conf": 0.9})
            check_eq(status, 200, "edge written")
            row = store._db.execute(
                "SELECT rel, conf, decided_by, edited_ts FROM edges WHERE src=? AND dst=?",
                (b, a),
            ).fetchone()
            check_eq(row["decided_by"], "human", "a hand-drawn edge says so")
            check_eq(float(row["conf"]), 0.9, "confidence kept")
            check(row["edited_ts"], "and it is stamped")

            check_eq(api.request("/api/mem/graph/edge", method="POST",
                                 body={"src": b, "dst": a, "rel": "胡说"})[0], 400, "unknown rel")

            status, body = api.request("/api/mem/graph/edge/delete", method="POST",
                                       body={"src": b, "dst": a, "rel": "改变"})
            check_eq(status, 200, "delete ok")
            check_eq(body["deleted"], 1, "one row gone")
            check_eq(store._db.execute(
                "SELECT count(*) FROM edges WHERE src=? AND dst=?", (b, a)
            ).fetchone()[0], 0, "edge is gone")
            check_eq(api.request("/api/mem/graph/edge/delete", method="POST",
                                 body={"src": b, "dst": a, "rel": "改变"})[1]["deleted"], 0,
                     "deleting twice is a no-op, not an error")
    finally:
        finish(daemon, db)


# --------------------------------------------------------------------------- positions

@case
def test_saved_positions_round_trip():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        with running_server(daemon) as api:
            eid = seed(daemon, "决定", "被拖走的原子")

            status, body = api.request("/api/mem/graph/positions", method="POST",
                                       body={"positions": [{"id": eid, "x": 1.5, "y": -0.5, "z": 2}]})
            check_eq(status, 200, "positions saved")
            check_eq(body["saved"], 1, "one position")

            _, graph = api.request("/api/mem/graph")
            check_eq(graph["positions"], [{"id": eid, "x": 1.5, "y": -0.5, "z": 2}],
                     "the graph reads the saved position back")

            check_eq(api.request("/api/mem/graph/positions", method="POST",
                                 body={"positions": [{"id": eid, "x": "nan", "y": 0, "z": 0}]})[0],
                     400, "non-finite coordinates are rejected")
            check_eq(api.request("/api/mem/graph/positions", method="POST",
                                 body={"positions": "nope"})[0], 400, "positions must be a list")

            status, body = api.request("/api/mem/graph/positions/clear", method="POST")
            check_eq(status, 200, "clear ok")
            check_eq(body["cleared"], 1, "one row dropped")
            _, graph = api.request("/api/mem/graph")
            check_eq(graph["positions"], [], "nothing saved")
    finally:
        finish(daemon, db)


# --------------------------------------------------------------------------- semantic

@case
def test_semantic_ranking_uses_stored_vectors():
    db = fresh_db()
    try:
        daemon = daemon_mod.Daemon(settings_for(db))
        store = daemon.mem().store
        near = seed(daemon, "决定", "靠得很近")
        far = seed(daemon, "决定", "离得很远")
        store.update_vec(near, vec(1.0, 0.0))
        store.update_vec(far, vec(0.0, 1.0))

        found = store.semantic_hits(vec(1.0, 0.0), top=2)
        check_eq([hit["id"] for hit in found["hits"]], [near, far], "cosine ordering")
        check_eq(found["hits"][0]["score"], 1.0, "an identical vector scores 1.0")
        check_eq(found["dim"], 2, "dimension reported")
        check_eq(found["scanned"], 2, "both events scanned")

        filtered = store.semantic_hits(vec(1.0, 0.0), top=2, min_score=0.5)
        check_eq([hit["id"] for hit in filtered["hits"]], [near], "min_score filters the rest")

        listed = store.semantic_hits([1.0, 0.0], top=1)
        check_eq([hit["id"] for hit in listed["hits"]], [near],
                 "a plain list query vector works too (the encoder may return one)")

        store.update_vec(far, vec(0.0, 1.0, 0.0))  # a different dimension
        mismatched = store.semantic_hits(vec(1.0, 0.0), top=2)
        check_eq([hit["id"] for hit in mismatched["hits"]], [near], "other dims are skipped")
        check_eq(mismatched["skipped"], 1, "and counted as skipped")
    finally:
        finish(daemon, db)


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
    raise SystemExit(main())
