#!/usr/bin/env python3
"""eventmem — tests for edge building (§5 selection rules) and jev.JevClient.

Self-contained: FakeStore / FakeJev are defined here, nothing outside the
standard library is imported, no HTTP request is ever made.

Run directly:  python tests/test_nightly.py
"""

from __future__ import annotations

import math
import os
import struct
import sys

os.environ["EVENTMEM_LANG"] = "zh"
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import jev as jev_mod  # noqa: E402
import nightly as nightly_mod  # noqa: E402

build_edges = nightly_mod.build_edges

T = 1_700_000_000.0

PROJ = {1: "项目甲", 2: "项目乙", 3: "项目丙", 4: "项目丁"}

#: Test values are keyed by short names; choices use the Chinese labels.
LABEL_KEY = {"重复": "dup", "改变": "change", "因为": "because", "否定": "negate", "细化": "refine"}


# --------------------------------------------------------------------------- #
# tiny test harness
# --------------------------------------------------------------------------- #

_passed = 0
_failed = 0
_failures: list[tuple[str, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        _failures.append((name, detail))
        print("  FAIL %s%s" % (name, ("  <- " + detail) if detail else ""))


def run(name: str, fn) -> None:
    global _passed, _failed
    before_p, before_f = _passed, _failed
    try:
        fn()
    except Exception:  # noqa: BLE001 - a raising test is a failing test
        _failed += 1
        _failures.append((name, "exception"))
        print("FAIL %s (exception)" % name)
        print(traceback.format_exc())
        return
    ok = _passed - before_p
    bad = _failed - before_f
    print("%s %s (%d checks)" % ("PASS" if bad == 0 else "FAIL", name, ok + bad))


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


def event_project_ids(ev) -> list[int]:
    ids = list(ev.get("project_ids") or [])
    if not ids and ev.get("project_id") is not None:
        ids = [ev["project_id"]]
    return ids


def make_event(
    eid: int,
    ts: float,
    text: str,
    project_id: int | None = None,
    *,
    session: str | None = None,
    art_id: int | None = None,
    art_path: str | None = None,
    extra_projects=(),
    kind: str = "变更",
) -> dict:
    ids = []
    if project_id is not None:
        ids.append(project_id)
    ids.extend(extra_projects)
    return {
        "id": eid,
        "ts": float(ts),
        "kind": kind,
        "text": text,
        "project": PROJ.get(project_id),
        "project_id": project_id,
        "project_ids": ids,
        "session": session,
        "art_id": art_id,
        "art_path": art_path,
        "src_msg": None,
        "vec": None,
    }


class FakeStore:
    """The store surface build_edges touches, including the head/closure queries."""

    def __init__(self, events=(), vecs=None, artifacts=None):
        self.events = {e["id"]: dict(e) for e in events}
        self._vecs = dict(vecs or {})
        self.artifacts = dict(artifacts or {})
        self.edges: list[dict] = []
        self.pair_evals: list[dict] = []
        self.recent_calls = 0
        self.get_events_calls = 0

    # --- reads
    def recent_events(self, since_ts):
        self.recent_calls += 1
        return [dict(e) for e in self.events.values() if e["ts"] > since_ts]

    def get_events(self, ids):
        self.get_events_calls += 1
        return [dict(self.events[i]) for i in ids if i in self.events]

    def event_project_ids(self, eid):
        return event_project_ids(self.events[eid])

    def list_projects(self, include_archived=False):
        return [{"id": pid, "name": PROJ.get(pid, "p%d" % pid), "status": "active"}
                for pid in sorted({p for e in self.events.values() for p in event_project_ids(e)})]

    def iter_vecs(self):
        return list(self._vecs.items())

    def get_artifact(self, aid):
        return self.artifacts.get(aid)

    def head_ids(self):
        dead = {e["dst"] for e in self.edges if e["rel"] in ("改变", "否定", "重复")}
        return {i for i in self.events if i not in dead}

    def _older(self, ev, ts, eid):
        return (ev["ts"], ev["id"]) < (float(ts), int(eid))

    def session_before(self, session, ts, eid, limit, kinds=None):
        if not session:
            return []
        rows = [
            e for e in self.events.values()
            if e.get("session") == session and self._older(e, ts, eid)
            and (not kinds or e["kind"] in kinds)
        ]
        rows.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
        return [dict(e) for e in rows[: max(0, int(limit))]]

    def unclosed_lessons(self, ts, eid, limit=2, scan=30):
        rows = [
            e for e in self.events.values()
            if e["kind"] == "教训" and self._older(e, ts, eid)
        ]
        rows.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
        out = []
        for ev in rows[:scan]:
            closed = any(
                x["ts"] > ev["ts"] and x["kind"] in ("结果", "变更")
                and (
                    (ev["project_id"] is not None and x["project_id"] == ev["project_id"])
                    or (ev["project_id"] is None and x.get("session") == ev.get("session"))
                )
                for x in self.events.values()
            )
            if closed:
                continue
            out.append(dict(ev))
            if len(out) >= limit:
                break
        return out

    def active_decisions(self, project_ids, ts, eid, limit=5):
        ids = set(int(p) for p in project_ids or ())
        dead = {e["dst"] for e in self.edges if e["rel"] in ("改变", "否定")}
        rows = [
            e for e in self.events.values()
            if e["kind"] == "决定" and self._older(e, ts, eid)
            and (set(event_project_ids(e)) & ids) and e["id"] not in dead
        ]
        rows.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
        return [dict(e) for e in rows[: max(0, int(limit))]]

    # --- writes
    def add_edge(self, src, dst, rel, conf, decided_by, cross=None, probs=None):
        for edge in self.edges:
            if edge["src"] == src and edge["dst"] == dst and edge["rel"] == rel:
                return  # contract: duplicate ignored
        if cross is None:
            a = self.events.get(src, {}).get("project_id")
            b = self.events.get(dst, {}).get("project_id")
            cross = 1 if (a is not None and b is not None and a != b) else 0
        self.edges.append(
            {
                "src": src,
                "dst": dst,
                "rel": rel,
                "conf": float(conf),
                "decided_by": decided_by,
                "cross": int(cross),
                "probs": probs,
            }
        )

    def add_pair_eval(self, src, dst, *, axis_probs=None, detail_probs=None,
                      axis_label=None, detail_label=None, ts=None):
        self.pair_evals.append(
            {
                "src": src,
                "dst": dst,
                "axis_probs": axis_probs,
                "detail_probs": detail_probs,
                "axis_label": axis_label,
                "detail_label": detail_label,
                "ts": ts,
            }
        )

    # --- helpers
    def rels(self):
        return [(e["src"], e["dst"], e["rel"]) for e in self.edges]


class FakeJev:
    def __init__(self, values=None, *, available=True, default=0.0, responder=None,
                 none_values=None):
        self._available = available
        self.values = dict(values or {})
        self.default = default
        self.responder = responder
        self.none_values = dict(none_values or {})
        self.calls: list[list[str]] = []
        self.states: list[str] = []

    def available(self):
        return self._available

    def decide(self, state, questions):
        keys = [k for k, _text in questions]
        self.calls.append(keys)
        self.states.append(state)
        return [float(self.values.get(key, self.default)) for key in keys]

    def decide_choice(self, state, questions):
        keys = [k for k, _text, _criteria in questions]
        index = len(self.calls)
        self.calls.append(keys)
        self.states.append(state)
        out = []
        for key, _text, criteria in questions:
            probs = {}
            for label in criteria:
                short = LABEL_KEY.get(label, label)
                if self.responder is not None:
                    value = self.responder(index, short, state)
                else:
                    value = self.values.get(short, self.default)
                probs[label] = float(value)
            best = max(probs, key=probs.get) if probs else None
            top = probs.get(best, 0.0) if best is not None else 0.0
            none = float(self.none_values.get(key, 0.0))
            # 「都不是」赢了时真实服务返回 choice=null；这里用 none 概率模拟。
            abstain = top <= 0.0 or none > top
            out.append(
                {
                    "choice": None if abstain else best,
                    "confidence": 0.0 if abstain else top,
                    "probabilities": probs,
                    "none": max(none, top) if abstain else none,
                }
            )
        return out


class ExplodingJev(FakeJev):
    def decide(self, state, questions):
        self.calls.append([k for k, _t in questions])
        raise RuntimeError("jev died")

    def decide_choice(self, state, questions):
        self.calls.append([k for k, _t, _c in questions])
        raise RuntimeError("jev died")


class NoneJev(FakeJev):
    def decide(self, state, questions):
        self.calls.append([k for k, _t in questions])
        return None

    def decide_choice(self, state, questions):
        self.calls.append([k for k, _t, _c in questions])
        return None


# --------------------------------------------------------------------------- #
# vectors
# --------------------------------------------------------------------------- #


def vec(*xs) -> bytes:
    return struct.pack("<%df" % len(xs), *[float(x) for x in xs])


def unit(i: int, dim: int = 8) -> bytes:
    xs = [0.0] * dim
    xs[i] = 1.0
    return vec(*xs)


def at_cos(cos: float, dim: int = 8) -> bytes:
    """A unit vector at cosine ``cos`` from ``unit(0)``."""
    cos = max(-1.0, min(1.0, float(cos)))
    xs = [0.0] * dim
    xs[0] = cos
    xs[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return vec(*xs)


# --------------------------------------------------------------------------- #
# selection — Q1 (active heads by vector)
# --------------------------------------------------------------------------- #


def test_jev_unavailable_short_circuits():
    store = FakeStore([make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)])
    jev = FakeJev(available=False)
    res = build_edges(store, jev, T - 1)
    check("unavailable: skipped", res["skipped"] == "jev-unavailable", repr(res))
    check("unavailable: no calls", jev.calls == [], repr(jev.calls))
    check("unavailable: no edges", store.edges == [], repr(store.edges))


def test_q1_asks_two_questions_and_writes_change():
    store = FakeStore(
        [make_event(1, T, "盘 256G", 1), make_event(2, T + 60, "盘 2T", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    check("q1: one pair", res["pairs"] == 1, repr(res))
    check("q1: two questions in one call", jev.calls == [["axis", "detail"]], repr(jev.calls))
    check("q1: one edge", store.rels() == [(2, 1, "改变")], repr(store.rels()))
    probs = store.edges[0]["probs"] or {}
    check("q1: probs stored (4 entries)", len(probs) == 4 and "都不是" in probs, repr(probs))
    check("q1: probs keeps the raw share", abs(probs.get("改变", 0) - 0.9) < 1e-9, repr(probs))


def test_q1_heads_only():
    # 2 改变了 1 → 1 不再是 head；3 与 1 向量相同也不该跟 1 比
    store = FakeStore(
        [
            make_event(1, T, "盘 256G", 1),
            make_event(2, T + 10, "盘 2T", 1),
            make_event(3, T + 20, "又回到 256G", 1),
        ],
        vecs={1: unit(0), 2: unit(1), 3: unit(0)},
    )
    store.add_edge(2, 1, "改变", 0.9, "jev")
    jev = FakeJev({"change": 0.9})
    build_edges(store, jev, T + 15)
    asked = [(e["src"], e["dst"]) for e in store.edges if e["decided_by"] == "jev" and e["src"] == 3]
    check("heads: superseded event not asked", asked == [], repr(asked))
    check("heads: only the seeded edge exists", store.rels() == [(2, 1, "改变")], repr(store.rels()))


def test_q1_negate_and_repeat_kill_head():
    for rel in ("否定", "重复"):
        store = FakeStore(
            [make_event(1, T, "旧", 1), make_event(2, T + 10, "评", 1),
             make_event(3, T + 20, "新", 1)],
            vecs={1: unit(0), 2: unit(1), 3: unit(0)},
        )
        store.add_edge(2, 1, rel, 0.9, "jev")
        jev = FakeJev({"change": 0.9})
        build_edges(store, jev, T + 15)
        asked = [(e["src"], e["dst"]) for e in store.edges if e["decided_by"] == "jev" and e["src"] == 3]
        check("%s: dst is not a head" % rel, asked == [], repr(asked))


def test_q1_cosine_floor():
    for cos, expected in ((0.30, 0), (0.40, 1)):
        store = FakeStore(
            [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
            vecs={1: at_cos(cos), 2: unit(0)},
        )
        jev = FakeJev({"change": 0.9})
        res = build_edges(store, jev, T - 1)
        check("cos %.2f -> %d pairs" % (cos, expected), res["pairs"] == expected, repr(res))


def test_q1_top10_cap():
    events = [make_event(1, T, "新", 1)]
    vecs = {1: unit(0)}
    for i in range(12):
        eid = 10 + i
        events.append(make_event(eid, T - 100 + i, "旧%d" % i, 1))
        vecs[eid] = unit(0)
    store = FakeStore(events, vecs=vecs)
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    check("top10: at most ten pairs", res["pairs"] == 10, repr(res))


# --------------------------------------------------------------------------- #
# selection — tier 2 bridges
# --------------------------------------------------------------------------- #


def test_tier2_session_heads():
    store = FakeStore(
        [make_event(1, T, "旧事", 1, session="s1"),
         make_event(2, T + 60, "新事", 1, session="s1")],
        vecs={1: unit(0), 2: unit(1)},  # 正交：Q1 不会碰它
    )
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    check("session: head asked despite cos 0", res["pairs"] == 1, repr(res))
    check("session: edge written", store.rels() == [(2, 1, "改变")], repr(store.rels()))


def test_tier2_artifact_bridge():
    store = FakeStore(
        [make_event(1, T, "旧", 2, art_id=77), make_event(2, T + 60, "新", 1, art_id=77)],
        vecs={1: unit(0), 2: unit(1)},
    )
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    check("artifact: bridged pair asked", res["pairs"] == 1, repr(res))
    check("artifact: cross edge", store.edges[0]["cross"] == 1, repr(store.edges))


def test_tier2_art_path_bridge():
    """同一份文件改两次：内容不同 → 两个 art_id、两个 sha，只有路径相同。"""
    path = "/home/u/.config/app.yaml"
    store = FakeStore(
        [make_event(1, T, "旧配置", 2, art_id=11, art_path=path),
         make_event(2, T + 60, "新配置", 1, art_id=12, art_path=path)],
        vecs={1: unit(0), 2: unit(1)},
        artifacts={11: {"id": 11, "sha": "aaa", "text": "a: 1"},
                   12: {"id": 12, "sha": "bbb", "text": "a: 2"}},
    )
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    check("path: bridged pair asked", res["pairs"] == 1, repr(res))
    check("path: edge written", store.rels() == [(2, 1, "改变")], repr(store.rels()))


def test_tier2_art_path_does_not_cross_files():
    store = FakeStore(
        [make_event(1, T, "甲文件", 2, art_id=11, art_path="/x/a.yaml"),
         make_event(2, T + 60, "乙文件", 1, art_id=12, art_path="/x/b.yaml")],
        vecs={1: unit(0), 2: unit(1)},
        artifacts={11: {"id": 11, "sha": "aaa"}, 12: {"id": 12, "sha": "bbb"}},
    )
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    check("path: different files never bridge", res["pairs"] == 0, repr(res))


# --------------------------------------------------------------------------- #
# selection — tier 3
# --------------------------------------------------------------------------- #


def test_tier3_adds_cross_project_beyond_top10():
    events = [make_event(1, T, "新值", 1)]
    vecs = {1: unit(0)}
    for i in range(10):  # 把 top-10 占满（同项目、cos 1.0）
        eid = 10 + i
        events.append(make_event(eid, T - 100 + i, "同项目 %d" % i, 1))
        vecs[eid] = unit(0)
    events.append(make_event(30, T - 50, "别的项目改了值", 2, kind="变更"))
    vecs[30] = at_cos(0.8)
    events.append(make_event(31, T - 40, "别的项目一条结果", 2, kind="结果"))
    vecs[31] = at_cos(0.75)
    store = FakeStore(events, vecs=vecs)
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    pairs = [(e["src"], e["dst"]) for e in store.edges if e["decided_by"] == "jev"]
    check("tier3: 10 + 1 cross-project pair", res["pairs"] == 11, repr(res))
    check("tier3: cross-project 变更 asked", (1, 30) in pairs, repr(pairs))
    check("tier3: 结果 not pulled in", (1, 31) not in pairs, repr(pairs))


def test_tier3_cosine_floor():
    events = [make_event(1, T, "新值", 1)]
    vecs = {1: unit(0)}
    for i in range(10):
        eid = 10 + i
        events.append(make_event(eid, T - 100 + i, "同项目 %d" % i, 1))
        vecs[eid] = unit(0)
    events.append(make_event(30, T - 50, "别项目 0.6", 2, kind="变更"))
    vecs[30] = at_cos(0.6)
    store = FakeStore(events, vecs=vecs)
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    pairs = [(e["src"], e["dst"]) for e in store.edges if e["decided_by"] == "jev"]
    check("tier3: cos 0.6 rejected", (1, 30) not in pairs, repr(pairs))
    check("tier3: only the ten heads", res["pairs"] == 10, repr(res))


# --------------------------------------------------------------------------- #
# selection — Q2 sets
# --------------------------------------------------------------------------- #


def test_q2_session_recent_includes_non_heads():
    store = FakeStore(
        [make_event(1, T, "旧", 1, session="s1"),
         make_event(2, T + 10, "否掉它", 1, session="s1"),
         make_event(3, T + 20, "新", 1, session="s1")],
        vecs={1: unit(0), 2: unit(1), 3: unit(2)},
    )
    store.add_edge(2, 1, "否定", 0.9, "jev")
    jev = FakeJev({"change": 0.9})
    build_edges(store, jev, T + 15)
    asked = [(e["src"], e["dst"]) for e in store.edges if e["decided_by"] == "jev"]
    check("q2: superseded session event still asked", (3, 1) in asked, repr(asked))
    check("q2: recent head asked too", (3, 2) in asked, repr(asked))


def test_q2_unclosed_lessons_only():
    store = FakeStore(
        [make_event(1, T, "教训A（后来有结果）", 1, kind="教训"),
         make_event(2, T + 10, "结果", 1, kind="结果"),
         make_event(3, T + 20, "教训B（没闭环）", 1, kind="教训"),
         make_event(4, T + 30, "教训C（没闭环）", 1, kind="教训"),
         make_event(5, T + 40, "新事件", 2)],
        vecs={1: unit(0), 2: unit(1), 3: unit(2), 4: unit(3), 5: at_cos(0.1)},
    )
    jev = FakeJev({"change": 0.9})
    build_edges(store, jev, T + 35)
    asked = [(e["src"], e["dst"]) for e in store.edges if e["decided_by"] == "jev"]
    check("q2: open lesson C asked", (5, 4) in asked, repr(asked))
    check("q2: open lesson B asked", (5, 3) in asked, repr(asked))
    check("q2: closed lesson A skipped", (5, 1) not in asked, repr(asked))


def test_q2_decision_skeleton():
    events = [make_event(1, T, "决定1", 1, kind="决定")]
    for i in range(2, 8):
        events.append(make_event(i, T + i, "决定%d" % i, 1, kind="决定"))
    events.append(make_event(20, T + 30, "新事件", 1))
    vecs = {20: unit(0)}
    for ev in events:  # 与 Q1 正交，只让 Q2 的骨架把它带进来
        vecs.setdefault(ev["id"], unit(1))
    store = FakeStore(events, vecs=vecs)
    store.add_edge(8, 2, "改变", 0.9, "jev")  # 决定2 被取代
    jev = FakeJev({"change": 0.9})
    build_edges(store, jev, T + 25)
    asked = [(e["src"], e["dst"]) for e in store.edges
             if e["decided_by"] == "jev" and e["src"] == 20]
    check("q2: latest five decisions asked", len(asked) == 5, repr(asked))
    check("q2: superseded decision skipped", (20, 2) not in asked, repr(asked))
    check("q2: newest decision asked", (20, 7) in asked, repr(asked))


# --------------------------------------------------------------------------- #
# margin
# --------------------------------------------------------------------------- #


def test_margin_boundary():
    cases = [
        ({"change": 0.55, "negate": 0.40}, 1, "exactly 0.15 passes"),
        ({"change": 0.549, "negate": 0.40}, 0, "0.149 fails"),
        ({"change": 0.42, "negate": 0.38}, 0, "0.04 fails"),
    ]
    for values, expected, label in cases:
        store = FakeStore(
            [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
            vecs={1: unit(0), 2: unit(0)},
        )
        jev = FakeJev(values)
        res = build_edges(store, jev, T - 1)
        check("margin: %s -> %d edges" % (label, expected), res["edges"] == expected, repr(res))


def test_margin_none_wins():
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"change": 0.42}, none_values={"axis": 0.45})
    res = build_edges(store, jev, T - 1)
    check("none: 0.45 beats 0.42 -> no edge", res["edges"] == 0, repr(res))


def test_every_asked_pair_is_archived():
    """灰线的数据源：问过的对都留一份两问分布，不管最后连不连边。"""
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"change": 0.42}, none_values={"axis": 0.45})
    build_edges(store, jev, T - 1)

    check("archive: no edge but one pair", not store.edges and len(store.pair_evals) == 1,
          repr(store.pair_evals))
    row = store.pair_evals[0]
    check("archive: axis distribution keeps 都不是",
          set(row["axis_probs"]) == {"重复", "改变", "否定", "都不是"}, repr(row))
    check("archive: detail distribution asked too",
          set(row["detail_probs"]) == {"因为", "细化", "都不是"}, repr(row))
    check("archive: labels stay null when none wins", row["axis_label"] is None, repr(row))


def test_repeat_suppresses_detail():
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 60, "同一句", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"dup": 0.9, "refine": 0.9})
    build_edges(store, jev, T - 1)
    check("repeat: only one edge", store.rels() == [(2, 1, "重复")], repr(store.rels()))


def test_two_axes_two_edges():
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"change": 0.8, "because": 0.7})
    build_edges(store, jev, T - 1)
    check("axes: two edges",
          sorted(store.rels()) == sorted([(2, 1, "改变"), (2, 1, "因为")]), repr(store.rels()))
    detail_probs = [e["probs"] for e in store.edges if e["rel"] == "因为"][0] or {}
    check("axes: detail probs (3 entries)", len(detail_probs) == 3 and "都不是" in detail_probs,
          repr(detail_probs))


# --------------------------------------------------------------------------- #
# direction / dedupe / replay
# --------------------------------------------------------------------------- #


def test_direction_only_older():
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"change": 0.9})
    build_edges(store, jev, T - 1)
    check("direction: src is the later event", store.edges[0]["src"] == 2, repr(store.edges))
    check("direction: dst is the earlier event", store.edges[0]["dst"] == 1, repr(store.edges))


def test_pair_asked_once_across_sets():
    store = FakeStore(
        [make_event(1, T, "旧", 1, session="s1"), make_event(2, T + 60, "新", 1, session="s1")],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T - 1)
    check("dedupe: one request", len(jev.calls) == 1, repr(jev.calls))
    check("dedupe: one pair", res["pairs"] == 1, repr(res))


def test_replay_evolves_heads():
    # 2 否掉 1；3 与新事件向量相同，只该跟仍然生效的 2 比
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 10, "否", 1),
         make_event(3, T + 20, "新", 1)],
        vecs={1: unit(0), 2: unit(0), 3: unit(0)},
    )

    def responder(index, short, state):
        if index == 0:  # 2 vs 1
            return 0.9 if short == "negate" else 0.0
        return 0.9 if short == "change" else 0.0

    jev = FakeJev(responder=responder)
    res = build_edges(store, jev, T - 1)
    asked = [(e["src"], e["dst"]) for e in store.edges if e["decided_by"] == "jev"]
    check("replay: 2 negates 1", (2, 1, "否定") in store.rels(), repr(store.rels()))
    check("replay: 3 pairs with 2 only", (3, 2) in asked and (3, 1) not in asked, repr(asked))
    check("replay: two pairs total", res["pairs"] == 2, repr(res))


def test_no_new_events():
    store = FakeStore([make_event(1, T, "旧", 1)])
    jev = FakeJev({"change": 0.9})
    res = build_edges(store, jev, T + 10)
    check("no new: skipped", res["skipped"] == "no-new-events", repr(res))


def test_decide_none_is_tolerated():
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    res = build_edges(store, NoneJev(), T - 1)
    check("none: one failed pair", res["pairs"] == 1 and res["skipped"] is None, repr(res))
    check("none: no edge", store.edges == [], repr(store.edges))


def test_jev_errors_abort_after_three():
    events = [make_event(1, T, "新", 1)]
    vecs = {1: unit(0)}
    for i in range(2, 7):
        events.append(make_event(i, T - 10 * i, "旧%d" % i, 1))
        vecs[i] = unit(0)
    store = FakeStore(events, vecs=vecs)
    res = build_edges(store, ExplodingJev(), T - 1)
    check("abort: three failures then stop", res["pairs"] == 3, repr(res))
    check("abort: skipped jev-error", res["skipped"] == "jev-error", repr(res))
    check("abort: no edges", store.edges == [], repr(store.edges))


def test_rerun_is_idempotent():
    store = FakeStore(
        [make_event(1, T, "旧", 1), make_event(2, T + 60, "新", 1)],
        vecs={1: unit(0), 2: unit(0)},
    )
    jev = FakeJev({"change": 0.9})
    build_edges(store, jev, T - 10)
    build_edges(store, jev, T - 10)
    check("rerun: store keeps one edge", len(store.edges) == 1, repr(store.rels()))


# --------------------------------------------------------------------------- #
# jev client (no sockets: _post is monkeypatched)
# --------------------------------------------------------------------------- #


def test_jev_default_is_noop():
    client = jev_mod.JevClient()
    check("jev default: unavailable", client.available() is False)
    check("jev default: decide None", client.decide("state", [("q", "问题")]) is None)

    client2 = jev_mod.JevClient(base_url="   ")
    check("jev blank url: unavailable", client2.available() is False)


def test_jev_payload_and_parsing():
    client = jev_mod.JevClient(base_url="http://127.0.0.1:9/", model="jev-style", timeout=1.5)
    captured = {}

    def fake_post(payload):
        captured.update(payload)
        return {"answers": {"a": {"noul": 0.8}, "b": {"probability": 0.25}, "c": 0.5}}

    client._post = fake_post
    out = client.decide("前一条：甲\n后一条：乙", [("a", "问题一"), ("b", "问题二"), ("c", "问题三")])

    check("jev payload: model", captured.get("model") == "jev-style", repr(captured.get("model")))
    check("jev payload: state", captured.get("state", "").startswith("前一条") is True,
          repr(captured.get("state")))
    qs = captured.get("questions", {})
    check("jev payload: keys", sorted(qs) == ["a", "b", "c"], repr(qs))
    check("jev payload: type noul", all(v.get("type") == "noul" for v in qs.values()), repr(qs))
    check("jev payload: instructions", qs.get("a", {}).get("instructions") == "问题一", repr(qs.get("a")))
    check("jev payload: criteria", "true" in qs.get("a", {}).get("criteria", {}), repr(qs.get("a")))
    # Instant 模型一次出结果，服务端会拒绝 think_mode=auto/always（422）：默认一个控制字段都不发。
    check("jev payload: no think controls",
          all(k not in captured for k in ("think_mode", "gate", "think_budget")),
          repr({k: captured.get(k) for k in ("think_mode", "gate", "think_budget")}))
    check("jev parse: noul/probability/bare", out == [0.8, 0.25, 0.5], repr(out))

    # explicit controls: single read-out and a tight budget are both sent when asked for
    bare = jev_mod.JevClient(base_url="http://127.0.0.1:9/", think_mode="never", think_budget=None)
    seen = {}
    bare._post = lambda p: seen.update(p) or {"answers": {"a": {"noul": 0.5}}}
    bare.decide("x", [("a", "q")])
    check("jev payload: never mode", seen.get("think_mode") == "never", repr(seen.get("think_mode")))
    check("jev payload: budget omitted", "think_budget" not in seen, repr(seen))

    tight = jev_mod.JevClient(base_url="http://127.0.0.1:9/", think_budget=512)
    seen2 = {}
    tight._post = lambda p: seen2.update(p) or {"answers": {"a": {"noul": 0.5}}}
    tight.decide("x", [("a", "q")])
    check("jev payload: explicit budget", seen2.get("think_budget") == 512, repr(seen2.get("think_budget")))

    # two-question design: one choice call carries the axis and the detail question
    captured2 = {}
    choice = jev_mod.JevClient(base_url="http://127.0.0.1:9/")
    choice._post = lambda p: captured2.update(p) or {
        "answers": {
            "axis": {"choice": "改变", "confidence": 0.77,
                     "probabilities": {"重复": 0.01, "改变": 0.77, "否定": 0.21},
                     "none_probability": 0.017},
            "detail": {"choice": None, "confidence": 0.03,
                       "probabilities": {"因为": 0.03, "细化": 0.02},
                       "none_probability": 0.95},
        }
    }
    got = choice.decide_choice(
        "前一条：甲\n后一条：乙",
        [("axis", "关系？", {"重复": "甲", "改变": "乙", "否定": "丙"}),
         ("detail", "补充？", {"因为": "甲", "细化": "乙"})],
    )
    qs2 = captured2.get("questions", {})
    check("choice payload: keys", sorted(qs2) == ["axis", "detail"], repr(qs2))
    check("choice payload: type", qs2.get("axis", {}).get("type") == "choice", repr(qs2.get("axis")))
    check("choice payload: allow_none", qs2.get("axis", {}).get("allow_none") is True,
          repr(qs2.get("axis")))
    check("choice payload: criteria",
          qs2.get("axis", {}).get("criteria") == {"重复": "甲", "改变": "乙", "否定": "丙"},
          repr(qs2.get("axis")))
    check("choice payload: no think controls",
          all(k not in captured2 for k in ("think_mode", "gate", "think_budget")),
          repr({k: captured2.get(k) for k in ("think_mode", "gate", "think_budget")}))
    check("choice parse: chosen + probabilities",
          got and got[0]["choice"] == "改变" and abs(got[0]["confidence"] - 0.77) < 1e-9
          and abs(got[0]["none"] - 0.017) < 1e-9 and got[0]["probabilities"]["否定"] == 0.21, repr(got))
    check("choice parse: null choice", got and got[1] and got[1]["choice"] is None
          and abs(got[1]["none"] - 0.95) < 1e-9, repr(got))


def test_jev_choice_parsing():
    def one(payload):
        c = jev_mod.JevClient(base_url="http://127.0.0.1:9")
        c._post = lambda _p: {"answers": {"a": payload}}
        return c.decide_choice("s", [("a", "q", {"改变": "x", "否定": "y"})])

    check("choice parse: confidence from probabilities",
          one({"choice": "改变", "probabilities": {"改变": 0.66}, "none_probability": 0.1})[0]["confidence"] == 0.66,
          repr(one({"choice": "改变", "probabilities": {"改变": 0.66}, "none_probability": 0.1})))
    check("choice parse: unknown label -> None",
          one({"choice": "也许", "confidence": 0.9}) == [None],
          repr(one({"choice": "也许", "confidence": 0.9})))
    check("choice parse: bad confidence -> None",
          one({"choice": "改变", "confidence": "nope"}) == [None],
          repr(one({"choice": "改变", "confidence": "nope"})))
    check("choice parse: missing choice -> None",
          one({"confidence": 0.9}) == [None], repr(one({"confidence": 0.9})))

    c = jev_mod.JevClient(base_url="http://127.0.0.1:9")
    c._post = lambda _p: (_ for _ in ()).throw(TimeoutError("boom"))
    check("choice exception -> None", c.decide_choice("s", [("a", "q", {"改变": "x"})]) is None)


def test_jev_malformed_responses():
    def client_with(payload):
        c = jev_mod.JevClient(base_url="http://127.0.0.1:9")
        c._post = lambda _p: payload
        return c

    cases = {
        "no answers": ({"result": {}}, [("a", "q")]),
        "missing key": ({"answers": {"a": {"noul": 0.5}}}, [("a", "q"), ("b", "q2")]),
        "out of range": ({"answers": {"a": {"noul": 2.0}}}, [("a", "q")]),
        "junk value": ({"answers": {"a": {"noul": "nope"}}}, [("a", "q")]),
        "not a dict": (["nope"], [("a", "q")]),
    }
    for label, (payload, questions) in cases.items():
        out = client_with(payload).decide("s", questions)
        check("jev malformed (%s) -> None" % label, out is None, repr(out))

    c = jev_mod.JevClient(base_url="http://127.0.0.1:9")
    c._post = lambda _p: (_ for _ in ()).throw(TimeoutError("boom"))
    check("jev exception -> None", c.decide("s", [("a", "q")]) is None)


def test_jev_probability_helper():
    prob = jev_mod._probability
    check("probability: noul", prob({"noul": 0.7}) == 0.7)
    check("probability: probability", prob({"probability": 0.1}) == 0.1)
    check("probability: bool", prob(True) == 1.0 and prob(False) == 0.0)
    check("probability: bare float", prob(0.42) == 0.42)
    check("probability: range reject", prob({"noul": -0.5}) is None)
    check("probability: empty", prob({}) is None and prob(None) is None)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

TESTS = [
    ("jev unavailable", test_jev_unavailable_short_circuits),
    ("q1 two questions", test_q1_asks_two_questions_and_writes_change),
    ("q1 heads only", test_q1_heads_only),
    ("q1 negate/repeat kill head", test_q1_negate_and_repeat_kill_head),
    ("q1 cosine floor", test_q1_cosine_floor),
    ("q1 top10 cap", test_q1_top10_cap),
    ("tier2 session heads", test_tier2_session_heads),
    ("tier2 artifact bridge", test_tier2_artifact_bridge),
    ("tier2 art_path bridge", test_tier2_art_path_bridge),
    ("tier2 art_path isolation", test_tier2_art_path_does_not_cross_files),
    ("tier3 beyond top10", test_tier3_adds_cross_project_beyond_top10),
    ("tier3 cosine floor", test_tier3_cosine_floor),
    ("q2 session recent", test_q2_session_recent_includes_non_heads),
    ("q2 unclosed lessons", test_q2_unclosed_lessons_only),
    ("q2 decision skeleton", test_q2_decision_skeleton),
    ("margin boundary", test_margin_boundary),
    ("margin none wins", test_margin_none_wins),
    ("pair archive", test_every_asked_pair_is_archived),
    ("repeat suppresses detail", test_repeat_suppresses_detail),
    ("two axes two edges", test_two_axes_two_edges),
    ("direction older only", test_direction_only_older),
    ("pair asked once", test_pair_asked_once_across_sets),
    ("replay evolves heads", test_replay_evolves_heads),
    ("no new events", test_no_new_events),
    ("decide None", test_decide_none_is_tolerated),
    ("jev errors abort", test_jev_errors_abort_after_three),
    ("rerun idempotent", test_rerun_is_idempotent),
    ("jev default noop", test_jev_default_is_noop),
    ("jev payload/parsing", test_jev_payload_and_parsing),
    ("jev malformed", test_jev_malformed_responses),
    ("jev probability helper", test_jev_probability_helper),
]


def main() -> int:
    print("eventmem nightly/jev tests")
    print("-" * 60)
    for name, fn in TESTS:
        run(name, fn)
    print("-" * 60)
    if _failures:
        print("failed checks:")
        for name, detail in _failures:
            print("  - %s%s" % (name, ("  <- " + detail) if detail else ""))
    print("PASS %d / FAIL %d" % (_passed, _failed))
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
