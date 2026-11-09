#!/usr/bin/env python3
"""eventmem store tests — plain script, no pytest: ``python tests/test_store.py``.

Every case gets its own temporary database (never ``~/.hermes``). Output ends with
``PASS n / FAIL m``; the exit code is non-zero if anything failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from eventmem_core.schema import SCHEMA_VERSION, ensure_schema  # noqa: E402
from eventmem_core.store import KINDS, RELS, Store, UnknownProject  # noqa: E402

CASES: list = []
PASSED: list[str] = []
FAILED: list[str] = []

EVENT_KEYS = {"id", "ts", "kind", "text", "project", "project_id", "session", "art_id",
              "art_path", "src_msg", "vec"}


def case(fn):
    CASES.append(fn)
    return fn


class raises:
    """Context manager: asserts the block raises ``exc`` (and nothing else)."""

    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"expected {self.exc.__name__}, nothing was raised")
        if issubclass(exc_type, self.exc):
            return True
        return False


def raw(store: Store) -> sqlite3.Connection:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- projects

@case
def test_unknown_project_rejected(store: Store) -> None:
    store.add_project("小主机")

    with raises(UnknownProject):
        store.add_event("变更", "换 2T 盘", project="不存在的项目")
    assert store.recent_events(0) == [], "rejected write must not leave an event behind"

    # a bad secondary project must reject the whole write, not just skip that project
    with raises(UnknownProject):
        store.add_event("变更", "换 2T 盘", project="小主机", extra_projects=["也没有"])
    assert store.recent_events(0) == []

    pid = store.require_project("小主机")
    assert isinstance(pid, int) and pid > 0
    with raises(UnknownProject):
        store.require_project("nope")


@case
def test_project_crud(store: Store) -> None:
    a = store.add_project("Qwen训练")
    assert store.add_project("Qwen训练") == a, "add_project must be idempotent"
    b = store.add_project("小主机")

    assert store.get_project("Qwen训练")["id"] == a
    assert store.get_project("Qwen训练")["status"] == "active"
    assert store.get_project("missing") is None

    with raises(ValueError):
        store.add_project("   ")

    names = [p["name"] for p in store.list_projects()]
    assert names == ["Qwen训练", "小主机"], names

    store.archive_project("Qwen训练")
    assert store.get_project("Qwen训练")["status"] == "archived"
    assert [p["name"] for p in store.list_projects()] == ["小主机"]
    assert sorted(p["name"] for p in store.list_projects(include_archived=True)) == ["Qwen训练", "小主机"]
    with raises(UnknownProject):
        store.archive_project("missing")


# ----------------------------------------------------------------------------- events

@case
def test_event_add_get(store: Store) -> None:
    pid = store.add_project("Qwen训练")
    art = store.add_artifact("命令", "python train_sft.py --bs 2 --lr 2e-5")
    eid = store.add_event(
        "结果",
        "两条路线都撞在 0.7301（零训练 0.6963）",
        ts=1000.5,
        project="Qwen训练",
        session="s-1",
        src_msg=42,
        art_id=art,
        vec=b"\x01\x02\x03\x04",
    )

    ev = store.get_event(eid)
    assert set(ev) == EVENT_KEYS, set(ev) ^ EVENT_KEYS
    assert ev["id"] == eid
    assert ev["ts"] == 1000.5
    assert ev["kind"] == "结果"
    assert ev["text"] == "两条路线都撞在 0.7301（零训练 0.6963）"
    assert ev["project"] == "Qwen训练"
    assert ev["project_id"] == pid
    assert ev["session"] == "s-1"
    assert ev["src_msg"] == 42
    assert ev["art_id"] == art
    assert ev["vec"] == b"\x01\x02\x03\x04"

    assert store.get_event(999_999) is None
    assert store.get_events([]) == []
    assert [e["id"] for e in store.get_events([eid, 999_999])] == [eid]

    # unclassified event: project is NULL, ts defaults to now
    eid2 = store.add_event("决定", "不上云，纯本地 CPU")
    ev2 = store.get_event(eid2)
    assert ev2["project"] is None and ev2["project_id"] is None
    assert abs(ev2["ts"] - time.time()) < 5, ev2["ts"]

    # "unfiled" is the vocabulary's word for no project (§4.3), not a project name
    eid3 = store.add_event("尝试", "随手试了一下", project="unfiled")
    assert store.get_event(eid3)["project_id"] is None

    # update_vec
    store.update_vec(eid2, b"\xff" * 8)
    assert store.get_event(eid2)["vec"] == b"\xff" * 8
    store.update_vec(999_999, b"\x00")  # no-op, must not raise

    with raises(ValueError):
        store.add_event("瞎写", "类型不在五种里")
    with raises(ValueError):
        store.add_event("决定", "   ")

    assert set(KINDS) == {"决定", "尝试", "结果", "变更", "教训"}
    assert set(RELS) == {"改变", "否定", "重复", "因为", "细化"}


@case
def test_multi_project_and_primary_index(store: Store) -> None:
    a = store.add_project("A")
    b = store.add_project("B")
    c = store.add_project("C")
    eid = store.add_event("尝试", "同时属于两个项目", ts=10, project="A", extra_projects=["B", "B"])

    conn = raw(store)
    try:
        rows = conn.execute(
            "SELECT project_id, is_primary FROM event_projects WHERE event_id = ? ORDER BY project_id",
            (eid,),
        ).fetchall()
        assert [(r["project_id"], r["is_primary"]) for r in rows] == [(a, 1), (b, 0)], rows

        # partial unique index: a second primary row for the same event must be rejected
        try:
            conn.execute(
                "INSERT INTO event_projects(event_id, project_id, is_primary) VALUES(?,?,1)",
                (eid, c),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("a second is_primary=1 row was accepted")
        conn.rollback()

        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_ep_primary'"
        ).fetchone()
        assert idx is not None and "WHERE is_primary = 1" in idx["sql"], idx
    finally:
        conn.close()

    assert [e["id"] for e in store.project_events(a)] == [eid]
    assert [e["id"] for e in store.project_events(b)] == [eid], "secondary project must count"
    assert store.project_events(c) == []


@case
def test_recent_events_and_iter_vecs(store: Store) -> None:
    a = store.add_project("A")
    b = store.add_project("B")
    e1 = store.add_event("决定", "一", ts=100, project="A")
    e2 = store.add_event("尝试", "二", ts=200, project="B", vec=b"v2")
    e3 = store.add_event("结果", "三", ts=300, project="A", extra_projects=["B"], vec=b"v3")
    e4 = store.add_event("教训", "四", ts=400)

    assert [e["id"] for e in store.recent_events(200)] == [e2, e3, e4]
    assert [e["id"] for e in store.recent_events(0)] == [e1, e2, e3, e4]
    assert [e["id"] for e in store.project_events(a)] == [e1, e3]
    assert [e["id"] for e in store.project_events(b)] == [e2, e3]

    assert store.iter_vecs() == [(e2, b"v2"), (e3, b"v3")]


# ------------------------------------------------------------------------------ edges

@case
def test_add_edge_idempotent(store: Store) -> None:
    a = store.add_project("A")
    b = store.add_project("B")
    old = store.add_event("决定", "当时只有 256G", ts=100, project="A")
    new = store.add_event("变更", "换 2T NVMe", ts=200, project="B")
    sib = store.add_event("变更", "换 4T NVMe", ts=300, project="B")

    store.add_edge(new, old, "改变", 0.91, "llm_batch")
    store.add_edge(new, old, "改变", 0.42, "jev")  # duplicate → ignored, first wins
    store.add_edge(new, old, "改变", 0.99, "human")

    assert store.inbound_degree(old) == 1
    into = store.edges_into(old)
    assert len(into) == 1
    assert into[0]["src"] == new and into[0]["dst"] == old and into[0]["rel"] == "改变"
    assert into[0]["conf"] == 0.91, into[0]
    assert into[0]["cross"] == 1, "A→B must be inferred as cross-project"
    assert into[0]["src_text"] == "换 2T NVMe"

    # a second relation between the same pair is a different edge, not a duplicate
    store.add_edge(new, old, "细化", 0.7, "jev")
    assert store.inbound_degree(old) == 2

    # explicit cross wins over inference
    store.add_edge(sib, old, "否定", 0.8, "jev", cross=0)
    assert store.edges_into(old)[0]["cross"] in (0, 1)
    neg = [e for e in store.edges_into(old) if e["rel"] == "否定"][0]
    assert neg["cross"] == 0

    with raises(ValueError):
        store.add_edge(new, old, "胡说", 0.9, "human")

    assert store.inbound_degree(999_999) == 0
    assert store.edges_into(999_999) == []


@case
def test_chain_full_component(store: Store) -> None:
    p = store.add_project("小主机")
    e1 = store.add_event("决定", "当时只有 256G，训练数据不能放这儿", ts=100, project="小主机")
    e2 = store.add_event("变更", "换 2T NVMe 作系统盘", ts=200, project="小主机")
    e3 = store.add_event("变更", "系统盘再扩到 4T", ts=300, project="小主机")
    e4 = store.add_event("教训", "盘位不够，扩展卡白买", ts=400, project="小主机")

    store.add_edge(e2, e1, "改变", 0.95, "llm_batch")
    store.add_edge(e3, e2, "改变", 0.93, "jev")
    store.add_edge(e4, e3, "因为", 0.80, "jev")  # not a chain relation

    for center in (e1, e2, e3):
        ch = store.chain(center)
        assert [e["id"] for e in ch] == [e1, e2, e3], (center, [e["id"] for e in ch])
        assert len({e["id"] for e in ch}) == 3

    ch = store.chain(e2)
    assert [e["ts"] for e in ch] == [100.0, 200.0, 300.0], "chain must be ts ascending"
    by_id = {e["id"]: e for e in ch}
    assert by_id[e1]["invalidated"] is True
    assert by_id[e1]["superseded_by"] == [{"id": e2, "ts": 200.0, "rel": "改变"}]
    assert by_id[e2]["invalidated"] is True
    assert by_id[e2]["superseded_by"] == [{"id": e3, "ts": 300.0, "rel": "改变"}]
    assert by_id[e3]["invalidated"] is False and by_id[e3]["superseded_by"] == []

    # 因为 is not part of the default chain; asking for it explicitly gets e3→e4
    assert [e["id"] for e in store.chain(e3, rels=("因为",))] == [e3, e4]
    assert [e["id"] for e in store.chain(e1, rels=("否定",))] == [e1]
    assert [e["id"] for e in store.chain(e4)][0] == e4

    assert store.chain(999_999) == []
    assert [e["id"] for e in store.chain(e1, max_depth=0)] == [e1]


@case
def test_chain_cycle_terminates(store: Store) -> None:
    e1 = store.add_event("变更", "甲", ts=1)
    e2 = store.add_event("变更", "乙", ts=2)
    e3 = store.add_event("变更", "丙", ts=3)

    store.add_edge(e2, e1, "改变", 0.9, "jev")
    store.add_edge(e3, e2, "改变", 0.9, "jev")
    store.add_edge(e1, e3, "改变", 0.9, "jev")  # closes the cycle e1→e3→e2→e1
    store.add_edge(e1, e1, "改变", 0.9, "jev")  # self-loop

    for center in (e1, e2, e3):
        ch = store.chain(center, max_depth=20)
        ids = [e["id"] for e in ch]
        assert ids == sorted(ids), ids
        assert sorted(ids) == sorted({e1, e2, e3}), ids
        assert len(ids) == len(set(ids)), "duplicate events in a cyclic chain"

    # depth cap: a 5-link line, two hops from the oldest end
    c = [store.add_event("变更", f"链{i}", ts=10 + i) for i in range(5)]
    for i in range(1, 5):
        store.add_edge(c[i], c[i - 1], "改变", 0.9, "jev")
    assert [e["id"] for e in store.chain(c[0], max_depth=2)] == c[:3]
    assert [e["id"] for e in store.chain(c[0], max_depth=10)] == c


# ------------------------------------------------------------------------ session window

@case
def test_session_window(store: Store) -> None:
    ids = [store.add_event("尝试", f"第{i}步", ts=1000 + i, session="s1") for i in range(1, 7)]
    store.add_event("尝试", "别的会话", ts=1003.5, session="s2")

    w = store.session_window("s1", ids[2], before=2, after=2)
    assert [e["id"] for e in w] == ids[0:5], [e["id"] for e in w]
    assert [e["ts"] for e in w] == sorted(e["ts"] for e in w)

    assert [e["id"] for e in store.session_window("s1", ids[0], before=2, after=2)] == ids[0:3]
    assert [e["id"] for e in store.session_window("s1", ids[5], before=2, after=2)] == ids[3:6]
    assert [e["id"] for e in store.session_window("s1", ids[2], before=0, after=0)] == [ids[2]]

    assert store.session_window("s1", 999_999) == []
    assert store.session_window("nope", ids[2]) == []


# ---------------------------------------------------------------------------------- FTS

@case
def test_fts_search_chinese_substring(store: Store) -> None:
    store.add_project("小主机")
    e1 = store.add_event("变更", "小主机换 2T NVMe 作系统盘，原 256G 退成备份盘", ts=100, project="小主机")
    e2 = store.add_event("教训", "训练数据不能放小主机，盘位不够", ts=200, project="小主机")
    e3 = store.add_event("结果", "4060 显存只有 8G，跑不动 7B", ts=300, project="小主机")

    hits = store.fts_search("系统盘")
    assert [h[0] for h in hits] == [e1], hits
    assert isinstance(hits[0][1], float)
    assert hits[0][1] <= 0, "SQLite bm25() is <= 0 (more negative = better)"

    assert [h[0] for h in store.fts_search("256G")] == [e1]
    assert [h[0] for h in store.fts_search("4060")] == [e3]
    assert [h[0] for h in store.fts_search("训练数据不能放")] == [e2]

    # two Chinese characters are below the trigram floor → LIKE fallback still finds it
    assert [h[0] for h in store.fts_search("显存")] == [e3]

    # short latin token ("2T") is also below the floor; the fallback covers it
    assert [h[0] for h in store.fts_search("2T")] == [e1]

    # FTS5 syntax characters in the raw query must never escape into MATCH
    assert store.fts_search('" OR * NEAR(') == []
    assert store.fts_search("小主机 AND (") != []

    assert store.fts_search("量子纠缠") == []
    assert store.fts_search("") == []
    assert store.fts_search("   ") == []
    assert len(store.fts_search("小主机", limit=1)) == 1

    # FTS rows are tied to live events: forget removes the lexical trace
    store.forget(e1)
    assert e1 not in [h[0] for h in store.fts_search("系统盘")]

    conn = raw(store)
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='events_fts'").fetchone()["sql"]
        assert "content='events'" in sql.replace('"', "'"), sql
        assert "trigram" in sql, sql
    finally:
        conn.close()


# ------------------------------------------------------------------- artifact lexical arm

@case
def test_artifact_text_is_searchable(store: Store) -> None:
    store.add_project("训练")
    art = store.add_artifact("命令", "python train_sft.py --base qwen --bs 2 --lr 2e-5")
    other = store.add_artifact("日志", "CUDA out of memory at step 1200")
    e1 = store.add_event("尝试", "启动第三阶段精修", ts=100, project="训练", art_id=art)
    e2 = store.add_event("教训", "训练崩了", ts=200, project="训练", art_id=other)

    # 命令只存在于 artifact，事件正文一个字都不含；词法臂必须靠 artifacts_fts 找到它
    assert "train_sft" not in store.get_event(e1)["text"]
    assert [h[0] for h in store.fts_search("train_sft")] == [e1], store.fts_search("train_sft")
    assert [h[0] for h in store.fts_search("out of memory")] == [e2]

    # 两条事件共享同一个 artifact 时都要被召回
    shared = store.add_artifact("配置", "long_budget=3072")
    e3 = store.add_event("结果", "A 路线复现", ts=300, project="训练", art_id=shared)
    e4 = store.add_event("结果", "B 路线复现", ts=400, project="训练", art_id=shared)
    assert sorted(h[0] for h in store.fts_search("long_budget")) == sorted([e3, e4])
    assert sorted(h[0] for h in store.fts_search("3072")) == sorted([e3, e4])

    # forget 独占 artifact 的事件后，artifact 与它的词法痕迹一起消失
    store.forget(e2)
    assert store.fts_search("memory") == []


@case
def test_artifacts_fts_rebuilds_on_open(store: Store) -> None:
    """老库（artifacts_fts 还不存在）打开时要自己补索引，不需要重新 backfill。"""
    store.add_project("训练")
    art = store.add_artifact("命令", "python train_sft.py --bs 2")
    eid = store.add_event("尝试", "启动训练", ts=100, project="训练", art_id=art)

    store.close()
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("DROP TABLE artifacts_fts")
        conn.commit()
    finally:
        conn.close()

    reopened = Store(store.db_path)
    try:
        # ``count(*)`` on the FTS table itself is answered from ``artifacts`` (external content),
        # so it cannot prove a rebuild; the docsize shadow table can. A LIKE-fallback hit would
        # score exactly -1e-12, so require a real bm25 score too.
        indexed = reopened._db.execute(
            "SELECT count(*) FROM artifacts_fts_docsize").fetchone()[0]
        assert indexed == 1
        hits = reopened.fts_search("train_sft")
        assert [h[0] for h in hits] == [eid]
        assert hits[0][1] < -1e-9
    finally:
        reopened.close()


@case
def test_chain_ignores_backwards_edges(store: Store) -> None:
    old = store.add_event("决定", "旧值", ts=100)
    new = store.add_event("变更", "新值", ts=200)
    store.add_edge(old, new, "改变", 1.0, "llm_batch")  # 坏数据：src 比 dst 早

    by_id = {e["id"]: e for e in store.chain(old)}
    assert by_id[new]["invalidated"] is False, "更新的一条不能被标成已失效"
    assert by_id[new]["superseded_by"] == []
    assert by_id[old]["invalidated"] is False


@case
def test_tombstone_blocks_backfill_readd(store: Store) -> None:
    store.add_project("P")
    eid = store.add_event("教训", "别在 /tmp 里训练", ts=100, project="P", src_msg=42)
    store.forget(eid)
    assert store.is_tombstoned_text("别在 /tmp 里训练")

    # backfill 从同一条旧消息重新抽取 → 必须拒绝
    with raises(ValueError):
        store.add_event("教训", "别在 /tmp 里训练", ts=200, project="P", src_msg=42)

    # 模型在当下重新写同一句话是合法的新事件（没有 src_msg）
    again = store.add_event("教训", "别在 /tmp 里训练", ts=300, project="P")
    assert store.get_event(again) is not None


# ------------------------------------------------------------------------------- forget

@case
def test_forget_cascade(store: Store) -> None:
    store.add_project("P")
    shared = store.add_artifact("命令", "echo shared")
    solo = store.add_artifact("日志", "traceback: boom")
    e1 = store.add_event("尝试", "跑训练", ts=100, project="P", art_id=shared)
    e2 = store.add_event("结果", "撞在 0.7301", ts=200, project="P", art_id=shared)
    e3 = store.add_event("教训", "不该用旧配置", ts=300, project="P", art_id=solo, session="s1")
    store.add_edge(e2, e1, "改变", 0.9, "llm_batch")
    store.add_edge(e3, e2, "否定", 0.9, "jev")

    text = store.get_event(e3)["text"]
    store.forget(e3)

    assert store.get_event(e3) is None
    assert store.is_tombstoned(e3)
    assert not store.is_tombstoned(e1)
    assert store.get_artifact(solo) is None, "artifact only e3 referenced must go"
    assert store.get_artifact(shared) is not None, "artifact still referenced by e1/e2 stays"
    assert store.inbound_degree(e1) == 1, "e2→e1 survives"
    assert [e["id"] for e in store.edges_into(e2)] == [], "e3→e2 must be gone"

    conn = raw(store)
    try:
        assert conn.execute("SELECT count(*) FROM edges WHERE src=? OR dst=?", (e3, e3)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM event_projects WHERE event_id=?", (e3,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM events WHERE id=?", (e3,)).fetchone()[0] == 0
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tombstones)").fetchall()]
        assert cols == ["id", "sha", "ts"], cols
        row = conn.execute("SELECT sha FROM tombstones WHERE id=?", (e3,)).fetchone()
        assert row["sha"] == hashlib.sha256(text.encode("utf-8")).hexdigest(), row["sha"]
        # the tombstone holds no text: the row's only string is the 64-char hash
        vals = conn.execute("SELECT * FROM tombstones WHERE id=?", (e3,)).fetchone()
        assert all(not (isinstance(v, str) and text in v) for v in tuple(vals)), "tombstone leaked text"
    finally:
        conn.close()

    # forgetting a missing id is a no-op, not an error
    store.forget(999_999)
    # forgetting an already-forgotten event keeps the tombstone intact
    store.forget(e3)
    assert store.is_tombstoned(e3)


# ---------------------------------------------------------------------------- artifacts

@case
def test_artifacts(store: Store) -> None:
    text = "python train_sft.py --base qwen --bs 2 --lr 2e-5"
    a1 = store.add_artifact("命令", text, src_msg=7)
    a2 = store.add_artifact("命令", text)
    assert a1 == a2, "sha256 dedupe"

    art = store.get_artifact(a1)
    assert art["sha"] == hashlib.sha256(text.encode()).hexdigest()
    assert art["kind"] == "命令" and art["text"] == text and art["src_msg"] == 7
    assert store.get_artifact(999_999) is None


# ------------------------------------------------------------------ art_path / edges_from

@case
def test_art_path(store: Store) -> None:
    """schema v7: 路径是文件的身份，sha 只是内容的身份。"""
    cols = {row[1] for row in store._db.execute("PRAGMA table_info(events)")}
    assert "art_path" in cols, cols

    eid = store.add_event("变更", "改了配置", art_path="  /home/u/.config/app.yaml  ")
    assert store.get_event(eid)["art_path"] == "/home/u/.config/app.yaml", "trimmed"
    plain = store.add_event("结果", "没有 artifact")
    assert store.get_event(plain)["art_path"] is None
    empty = store.add_event("结果", "空路径", art_path="   ")
    assert store.get_event(empty)["art_path"] is None, "blank collapses to NULL"


@case
def test_schema_v6_db_gains_art_path(store: Store) -> None:
    """老库（没有 art_path 的 v6 events 表）打开时补列并记版本，不丢行。"""
    tmpdir = tempfile.mkdtemp(prefix="eventmem-v6-")
    try:
        db = os.path.join(tmpdir, "old.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " kind TEXT NOT NULL, text TEXT NOT NULL, project_id INTEGER, art_id INTEGER,"
            " src_msg INTEGER, session TEXT, vec BLOB, edited_ts REAL);"
            "INSERT INTO events(ts, kind, text) VALUES(1.0, '决定', '老事件');"
        )
        ensure_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "art_path" in cols, cols
        assert conn.execute("SELECT text FROM events").fetchone()[0] == "老事件"
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "7"
        ensure_schema(conn)          # 幂等：第二次不再 ALTER
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@case
def test_edges_from_mirrors_edges_into(store: Store) -> None:
    a = store.add_event("决定", "老决定", ts=1000.0)
    b = store.add_event("变更", "新变更", ts=2000.0)
    store.add_edge(src=b, dst=a, rel="改变", conf=0.9, decided_by="jev",
                   probs={"改变": 0.6})
    out = store.edges_from(b)
    assert len(out) == 1 and out[0]["dst"] == a and out[0]["rel"] == "改变"
    assert out[0]["dst_text"] == "老决定" and out[0]["dst_kind"] == "决定"
    into = store.edges_into(a)
    assert len(into) == 1 and into[0]["src"] == b and into[0]["src_text"] == "新变更"
    assert store.edges_from(a) == [] and store.edges_into(b) == []


# ------------------------------------------------------------------- session stickiness

@case
def test_session_project(store: Store) -> None:
    store.add_project("A")
    store.set_session_project("s1", "A")
    got = store.get_session_project("s1")
    assert got["project"] == "A" and got["project_id"] == store.require_project("A")

    store.set_session_project("s1", None)
    assert store.get_session_project("s1")["project"] is None

    with raises(UnknownProject):
        store.set_session_project("s1", "nope")
    assert store.get_session_project("missing") is None


# ------------------------------------------------------------------------------- meta

@case
def test_meta_cursor(store: Store) -> None:
    assert store.get_meta("nightly_since_ts") is None
    assert store.get_meta("missing", "fallback") == "fallback"
    store.set_meta("nightly_since_ts", "123.5")
    assert store.get_meta("nightly_since_ts") == "123.5"
    store.set_meta("nightly_since_ts", "456.25")   # upsert, not a duplicate row
    assert store.get_meta("nightly_since_ts") == "456.25"


@case
def test_advance_meta_ts_is_a_high_water_mark(store: Store) -> None:
    """几个进程各跑一个 worker：先开始的 pass 后结束，不能把游标改回去。"""
    assert store.advance_meta_ts("nightly_since_ts", 100.0) is True
    assert store.get_meta("nightly_since_ts") == "100.000"
    assert store.advance_meta_ts("nightly_since_ts", 50.0) is False
    assert store.get_meta("nightly_since_ts") == "100.000", "回退必须被拒绝"
    assert store.advance_meta_ts("nightly_since_ts", 150.0) is True
    assert store.get_meta("nightly_since_ts") == "150.000"


# ------------------------------------------------------------------------------- schema

@case
def test_schema_pragmas_and_idempotence(store: Store) -> None:
    assert SCHEMA_VERSION == 7

    conn = raw(store)
    try:
        ensure_schema(conn)
        ensure_schema(conn)  # must be safe to re-run
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in ("projects", "session_project", "events", "event_projects", "edges",
                  "artifacts", "tombstones", "events_fts", "artifacts_fts", "pair_evals"):
            assert t in tables, t
        assert "pending_turns" not in tables, "v3 的待补表必须在 v4 里被清掉"

        cols = [r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
        assert "vec" in cols, cols
        assert "edited_ts" in cols, cols

        edge_cols = [r["name"] for r in conn.execute("PRAGMA table_info(edges)").fetchall()]
        assert "probs" in edge_cols, edge_cols
        assert "edited_ts" in edge_cols, edge_cols

        pair_cols = [r["name"] for r in conn.execute("PRAGMA table_info(pair_evals)").fetchall()]
        for c in ("src", "dst", "axis_probs", "detail_probs", "axis_label", "detail_label", "ts"):
            assert c in pair_cols, pair_cols

        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
        assert ver == str(SCHEMA_VERSION)
    finally:
        conn.close()


@case
def test_v4_database_gains_probs_column(store: Store) -> None:
    """A v4 edges table (no probs) must be upgraded in place, keeping its rows."""
    tmpdir = tempfile.mkdtemp()
    conn = sqlite3.connect(os.path.join(tmpdir, "old.db"))
    try:
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE edges (src INTEGER NOT NULL, dst INTEGER NOT NULL, rel TEXT NOT NULL,"
            " conf REAL NOT NULL, decided_by TEXT NOT NULL, cross INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (src, dst, rel));"
            "INSERT INTO edges VALUES(2, 1, '改变', 0.9, 'jev', 0);"
        )
        conn.commit()
        ensure_schema(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()]
        assert "probs" in cols, cols
        row = conn.execute("SELECT probs FROM edges").fetchone()
        assert row[0] is None, row
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


@case
def test_head_ids_exclude_superseded(store: Store) -> None:
    store.add_project("p1")
    a = store.add_event("变更", "盘 256G", ts=1000.0, project="p1")
    b = store.add_event("变更", "盘 2T", ts=1001.0, project="p1")
    store.add_edge(b, a, "改变", 0.9, "jev")
    assert a not in store.head_ids(), "被改变的那条不再算 head"
    assert b in store.head_ids()

    c = store.add_event("教训", "这条路不通", ts=1002.0, project="p1")
    store.add_edge(c, b, "否定", 0.9, "jev")
    assert b not in store.head_ids(), "被否定的不再算 head"

    d = store.add_event("变更", "同一句", ts=1003.0, project="p1")
    store.add_edge(d, c, "细化", 0.9, "jev")
    assert c in store.head_ids(), "细化 不摘 head"
    store.add_edge(d, c, "重复", 0.9, "jev")
    assert c not in store.head_ids(), "重复 也摘 head"


@case
def test_session_before_orders_and_filters(store: Store) -> None:
    store.add_event("尝试", "一", ts=1000.0, session="s1")
    b = store.add_event("结果", "二", ts=1001.0, session="s1")
    c = store.add_event("变更", "三", ts=1002.0, session="s1")
    store.add_event("变更", "别会话", ts=1003.0, session="s2")

    got = store.session_before("s1", 1003.0, 10**9, 2)
    assert [e["id"] for e in got] == [c, b], got

    only = store.session_before("s1", 1003.0, 10**9, 5, kinds=["结果"])
    assert [e["id"] for e in only] == [b], only

    assert store.session_before(None, 1003.0, 10**9, 5) == []


@case
def test_unclosed_lessons_skip_closed(store: Store) -> None:
    store.add_project("p1")
    store.add_event("教训", "A", ts=1000.0, project="p1")
    store.add_event("结果", "A 解决了", ts=1001.0, project="p1")
    b = store.add_event("教训", "B", ts=1002.0, project="p1")
    c = store.add_event("教训", "C", ts=1003.0, project="p1")

    got = store.unclosed_lessons(1010.0, 10**9, limit=2)
    assert [e["id"] for e in got] == [c, b], got


@case
def test_active_decisions_skips_superseded(store: Store) -> None:
    store.add_project("p1")
    ids = [store.add_event("决定", "决定%d" % i, ts=1000.0 + i, project="p1")
           for i in range(1, 7)]
    store.add_edge(ids[2], ids[1], "改变", 0.9, "jev")  # 决定2 被取代

    got = store.active_decisions([1], 1100.0, 10**9, limit=5)
    got_ids = [e["id"] for e in got]
    assert ids[1] not in got_ids, got_ids
    assert len(got_ids) == 5, got_ids
    assert got_ids[0] == ids[5], got_ids


@case
def test_add_edge_stores_probs(store: Store) -> None:
    store.add_project("p1")
    a = store.add_event("变更", "旧", ts=1000.0, project="p1")
    b = store.add_event("变更", "新", ts=1001.0, project="p1")
    probs = {"重复": 0.01, "改变": 0.55, "否定": 0.40, "都不是": 0.04}
    store.add_edge(b, a, "改变", 0.55, "jev", probs=probs)

    conn = raw(store)
    try:
        row = conn.execute("SELECT probs FROM edges").fetchone()
    finally:
        conn.close()
    assert json.loads(row[0]) == probs, row[0]


@case
def test_manual_edits_and_pair_evals(store: Store) -> None:
    """v6: 人工改正文/关系留 edited_ts；每一对问过模型的都留档（灰线靠它）。"""
    store.add_project("p1")
    a = store.add_event("变更", "旧", ts=1000.0, project="p1")
    b = store.add_event("变更", "新", ts=1001.0, project="p1")

    assert store.update_event(a, text="旧（已改）", kind="决定") is True
    ev = store.get_event(a)
    assert ev["text"] == "旧（已改）" and ev["kind"] == "决定"

    store.add_edge(b, a, "改变", 0.55, "jev", probs={"改变": 0.55})
    store.set_edge(b, a, "因为", 0.4, old_rel="改变")
    conn = raw(store)
    try:
        rows = {r["rel"]: dict(r) for r in conn.execute("SELECT * FROM edges").fetchall()}
    finally:
        conn.close()
    assert "改变" not in rows and "因为" in rows, rows
    assert rows["因为"]["decided_by"] == "human" and rows["因为"]["edited_ts"]

    store.add_pair_eval(b, a, axis_probs={"改变": 0.4, "都不是": 0.35},
                        detail_probs={"因为": 0.6}, axis_label=None, detail_label="因为")
    conn = raw(store)
    try:
        row = conn.execute("SELECT * FROM pair_evals").fetchone()
        assert json.loads(row["axis_probs"]) == {"改变": 0.4, "都不是": 0.35}
        assert row["axis_label"] is None and row["detail_label"] == "因为"
        # re-evaluation overwrites the same pair instead of duplicating it
        store.add_pair_eval(b, a, axis_probs={"否定": 0.7}, axis_label="否定")
        assert conn.execute("SELECT count(*) FROM pair_evals").fetchone()[0] == 1
    finally:
        conn.close()

    assert store.delete_edge(b, a, "因为") is True
    assert store.delete_edge(b, a, "因为") is False


@case
def test_count_events_since(store: Store) -> None:
    """The link worker's "is there work?" probe counts only strictly newer events."""
    assert store.count_events_since(0.0) == 0
    store.add_event("变更", "第一条", ts=1000.0)
    store.add_event("变更", "第二条", ts=2000.0)
    assert store.count_events_since(0.0) == 2
    assert store.count_events_since(1500.0) == 1
    assert store.count_events_since(2000.0) == 0, "the cursor itself is already handled"
    assert store.count_events_since(5000.0) == 0


# -------------------------------------------------------------------------------- runner


def new_store() -> tuple[Store, str]:
    tmpdir = tempfile.mkdtemp(prefix="eventmem-test-")
    return Store(os.path.join(tmpdir, "mem.db")), tmpdir


def main() -> int:
    for fn in CASES:
        tmpdir = None
        store = None
        try:
            store, tmpdir = new_store()
            fn(store)
            PASSED.append(fn.__name__)
            print(f"PASS {fn.__name__}")
        except Exception:
            FAILED.append(fn.__name__)
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        finally:
            if store is not None:
                store.close()
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"PASS {len(PASSED)} / FAIL {len(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
