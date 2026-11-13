"""eventmem — edge building (design §5; pure local, no LLM API).

**Selection is the whole cost model.**  A new event is only ever shown to the local decision
model next to:

* **Q1 — active heads by vector**: heads (nothing has 改变 / 否定 / 重复-ed them) with
  ``cosine >= 0.35``, the top 10;
* **bridged heads**: the same session's 10 most recent heads, plus every head sharing an
  artifact (same ``art_id`` / sha) or the same source file (``events.art_path`` — the sha
  changes with the content, the path does not, so "the same config edited three times"
  only bridges on the path);
* **tier 3 — cross project**: only when the new event is 变更/决定; the other end must also be
  a 变更/决定 head in a different project, ``cosine > 0.7``, top 3;

and, for the elaboration question only:

* the same session's 3 most recent events;
* the 2 most recent 教训 with no later 结果/变更 in their project (still-open lessons);
* the project's 5 most recent 决定 that nothing has 改变/否定-ed.

Both questions ride one request per pair: on this hybrid-attention model a read-out costs a full
prefill and no prefix can be reused, so the pair — not the question — is the unit of cost.

A label is written only when it beats the runner-up by at least ``MARGIN`` in the **raw**
distribution, the server's 「都不是」 counted as one of the options: 0.42 vs 0.38 is not a
decision, and the old "any real label wins" rule wrote an edge for two thirds of all pairs.
``重复`` suppresses the elaboration axis.  Every edge keeps the distribution it came from in
``edges.probs`` (raw, four or three entries), so a later margin change can be replayed from the
database instead of re-running the model.

Direction is always older → newer (``src = later``, ``dst = earlier``); candidates are strictly
older than the event being processed, so a replay in ts order sees the head set evolve exactly as
it would have in real time.

SQLite stores skip any previously archived pair or pair with an existing edge before asking.
New archives and edges commit atomically, with a second check after the model call. Historical
incomplete archives are conservatively preserved because a deleted edge has no tombstone.

Run one pass with::

    python -m eventmem.nightly --since-hours 24

The ``nightly_since_ts`` cursor lives in the ``meta`` table; a failed pass
(``jev-unavailable`` / ``jev-error``) does not advance it.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import struct
import time
from typing import Callable, Optional

from .i18n import is_zh  # noqa: E402
from .providers import NONE_OF_ABOVE  # noqa: E402
from .settings import CoreSettings, EdgePolicy, describe  # noqa: E402

_log = logging.getLogger(__name__)

__all__ = [
    "build_edges",
    "main",
    "AXIS_OPTIONS",
    "DETAIL_OPTIONS",
    "HEADS_COS_MIN",
    "HEADS_VEC_TOP_K",
    "SESSION_HEADS_TOP_K",
    "CROSS_KINDS",
    "CROSS_COS_MIN",
    "CROSS_TOP_K",
    "MARGIN",
    "CURSOR_KEY",
]

#: Question 1 — the conflict axis.  One of these, or the server's implicit
#: "none of the above" (``none_probability``), which is not listed here.
#: The definitions are deliberately operational: 改变 vs 否定 is decided by
#: "did it give a replacement of the same kind?", not by whether the wording
#: sounds like a rejection.
AXIS_KEY = "axis"
AXIS_TEXT = "后一条相对前一条是什么关系？"
AXIS_TEXT_EN = "What is the later event's relation to the earlier one?"
#: The "none of the above" option is sent as a **real label**.  It used to be left to the
#: server (``allow_none``), which only works where the server implements it: LM Studio's
#: systemone has no ``none_probability``, so 改变 was the only thing left to pick and the graph
#: turned into one 356-node blob.  A real label makes every backend behave the same.
NONE_LABEL = NONE_OF_ABOVE
AXIS_OPTIONS = (
    ("重复", "repeat", "语义等价，同一件事换种说法，未引入实质性新变化",
     "Same meaning, reworded; nothing substantively new"),
    ("改变", "change",
     "同一个对象的同一条取值被替换：前一条点名的参数/开关/版本/数量/路径/配置项，后一条给出了它的新值"
     "（「系统盘 256G」→「系统盘换成 1T」）。必须能指出被替换的是哪一条具体取值",
     "Replaces one specific value of the same object: the parameter, flag, version, count, "
     "path or config item named by the earlier event gets a new value (\"system disk 256G\" "
     "→ \"system disk now 1T\"). You must be able to name which single value was replaced"),
    ("否定", "negate", "方向性推翻或废止（明确表达不再执行、禁止或推翻前一条的做法，且没有给出同类平替参数）",
     "Reverses or repeals the earlier behaviour (it is no longer done, or it is forbidden or "
     "overturned) and offers no like-for-like replacement"),
    (NONE_LABEL, "none",
     "后一条没有替换前一条里的任何取值：两条讲的是不同的事/不同对象，或只是先后做的两件不相干的活",
     "The later event does not replace any value named by the earlier one: they are about "
     "different things or objects, or are two unrelated pieces of work"),
)

#: Question 2 — the elaboration axis.  Independent of question 1, so a pair can
#: carry one conflict edge *and* one elaboration edge.
DETAIL_KEY = "detail"
DETAIL_TEXT = "后一条和前一条是什么补充关系？"
DETAIL_TEXT_EN = "What kind of elaboration does the later event add?"
DETAIL_OPTIONS = (
    ("因为", "because", "因果与动机解释（核心在于回答「为什么这样做」或背后的原因与目的）",
     "Explains cause or motive — it answers why this was done"),
    ("细化", "refine", "执行层细节展开（核心在于回答「具体怎么做」、包含哪些子步骤或局部参数，不涉及原因解释）",
     "Adds implementation detail — it answers how, with sub-steps or local parameters, without "
     "explaining why"),
)

#: The pre-split defaults.  Adapters pass an :class:`~eventmem_core.settings.EdgePolicy`
#: instead of editing these; the names stay importable for older callers and tests.
_POLICY = EdgePolicy()

#: Q1: an active head must be at least this close to compete, and at most this many are asked.
HEADS_COS_MIN = _POLICY.heads_cos_min
HEADS_VEC_TOP_K = _POLICY.heads_vec_top_k

#: Bridged heads: the same session's most recent heads (artifacts bring all of theirs).
SESSION_HEADS_TOP_K = _POLICY.session_heads_top_k

#: Tier 3 — cross project, both ends 变更/决定, stricter cosine, tiny top-k.
CROSS_KINDS = _POLICY.cross_kinds
CROSS_COS_MIN = _POLICY.cross_cos_min
CROSS_TOP_K = _POLICY.cross_top_k

#: Q2 sets: same session's recent events / open lessons / the decision skeleton.
SESSION_RECENT = _POLICY.session_recent
UNCLOSED_LESSONS = _POLICY.unclosed_lessons
DECISION_SKELETON = _POLICY.decision_skeleton

#: A label must beat the runner-up by this much, 「都不是」 included in the ranking.
MARGIN = _POLICY.margin

#: Stop the night after this many consecutive ``decide()`` failures: the server
#: is down/hung and each remaining pair would cost another timeout.
MAX_FAIL_STREAK = _POLICY.max_fail_streak

#: Meta key holding the timestamp the next run starts from.
CURSOR_KEY = "nightly_since_ts"

_AXIS_RELS = tuple(rel for rel, *_rest in AXIS_OPTIONS if rel != NONE_LABEL)
#: What one axis answer actually carries on the wire: the three relations **and** the
#: real "none" label (the question is sent with ``allow_none: false``).
_AXIS_WIRE = _AXIS_RELS + (NONE_LABEL,)
_DETAIL_RELS = tuple(rel for rel, *_rest in DETAIL_OPTIONS)

#: English label -> canonical (stored) label, for answers that come back in English.
_LABEL_BACK = {en: zh for zh, en, *_rest in (AXIS_OPTIONS + DETAIL_OPTIONS)}


def _question(key: str, text_zh: str, text_en: str, options: tuple) -> tuple:
    """``(key, question text, {label: definition})`` in the active language.

    Only the *prompt* follows the language setting; the labels coming back are normalized to
    the stored Chinese ones by :func:`_canonical_answer`, so the database and the parsers are
    language-independent.
    """
    if is_zh():
        return (key, text_zh, {o[0]: o[2] for o in options})
    return (key, text_en, {o[1]: o[3] for o in options})


def _canonical_answer(answer: object) -> object:
    """Map an English-labelled answer back to the stored labels (a no-op under ``zh``)."""
    if not isinstance(answer, dict):
        return answer
    out = dict(answer)
    choice = out.get("choice")
    if isinstance(choice, str) and choice in _LABEL_BACK:
        out["choice"] = _LABEL_BACK[choice]
    probs = out.get("probabilities")
    if isinstance(probs, dict) and any(k in _LABEL_BACK for k in probs):
        out["probabilities"] = {_LABEL_BACK.get(k, k): v for k, v in probs.items()}
    return out


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #


def build_edges(store, jev, since_ts: float, policy: "EdgePolicy | None" = None, *,
                on_progress: Optional[Callable[[dict], None]] = None,
                should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """Build relation edges for events newer than ``since_ts``.

    Returns ``{"pairs": n, "edges": m, "skipped": reason}`` where ``pairs`` is
    the number of pairs actually sent to the decision model and ``edges`` the number of newly
    inserted edges.  ``skipped`` is ``None`` on a normal run, otherwise one of
    ``"jev-unavailable"``, ``"no-new-events"``, ``"jev-error"`` or ``"stopped"``.

    ``on_progress`` is called after every answered pair (the console's live counter) and
    ``should_stop`` is asked before each one: a stop is therefore graceful — the pair in
    flight finishes, the cursor stays put, and the next pass skips completed SQLite pairs.
    Any model or persistence failure returns ``jev-error`` to retain the cursor for retry.
    """
    try:
        if not jev.available():
            return {"pairs": 0, "edges": 0, "skipped": "jev-unavailable"}
    except Exception:  # noqa: BLE001 - a broken client is an unavailable one
        return {"pairs": 0, "edges": 0, "skipped": "jev-unavailable"}

    return _Nightly(store, jev, policy, on_progress=on_progress,
                    should_stop=should_stop).run(since_ts)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


class _Nightly:
    def __init__(self, store, jev, policy: "EdgePolicy | None" = None, *,
                 on_progress: Optional[Callable[[dict], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None):
        self.store = store
        self.jev = jev
        self.pol = policy or _POLICY
        self.on_progress = on_progress
        self.should_stop = should_stop

        self.pairs = 0
        self.edges = 0
        self._seen_pairs: set[tuple[int, int]] = set()
        self._fail_streak = 0
        self._had_failure = False
        self._aborted = False
        self._stopped = False

        self._events: dict[int, dict] | None = None
        self._vecs: dict[int, tuple[float, ...]] | None = None
        self._member_idx: dict[int, list[int]] | None = None
        self._project_cache: dict[int, tuple[int, ...]] = {}
        self._artifact_cache: dict[int, dict | None] = {}
        # project_events() returns full rows (vec blob included); one query per project per
        # night is enough — tier 1 and _membership() read the same rows.
        self._proj_events: dict[int, list[dict]] = {}

    # ------------------------------------------------------------------ run

    def run(self, since_ts: float) -> dict:
        try:
            new = list(self.store.recent_events(since_ts) or [])
        except Exception:  # noqa: BLE001
            _log.exception("eventmem nightly: failed to fetch recent events")
            return {"pairs": 0, "edges": 0, "skipped": "jev-error"}
        if not new:
            return {"pairs": 0, "edges": 0, "skipped": "no-new-events"}

        new.sort(key=lambda ev: (_ts(ev), _id(ev)))
        for ev in new:
            if self._aborted or self._stopped:
                break
            try:
                self._consider(ev)
            except Exception:  # noqa: BLE001 - do not advance past a failed candidate
                self._had_failure = True
                _log.exception("eventmem nightly: failed to consider event")
                continue

        result = {
            "pairs": self.pairs,
            "edges": self.edges,
            "backend": self._backend_name(),
            "skipped": "stopped" if self._stopped else ("jev-error" if self._had_failure else None),
            "stopped": self._stopped,
        }
        if self._aborted:
            _log.warning("eventmem nightly: aborted after %d decide failures (pairs=%d edges=%d)",
                         self.pol.max_fail_streak, self.pairs, self.edges)
        else:
            _log.info("eventmem nightly: events=%d pairs=%d edges=%d",
                      len(new), self.pairs, self.edges)
        return result

    # -------------------------------------------------------------- pairing

    def _consider(self, ev: dict) -> None:
        """Ask the model about every candidate worth a pair (see the module docstring)."""
        eid = _id(ev)
        if eid is None:
            return
        ets = _ts(ev)
        session = _field(ev, "session")
        projects = set(self._project_ids(ev))
        heads = self._heads()
        kind = str(_field(ev, "kind") or "")

        picked: dict[int, dict] = {}

        def add(other, *, other_project_only: bool = False) -> bool:
            oid = _id(other)
            if oid is None or oid == eid or oid in picked:
                return False
            if not _is_older(other, ev):
                return False
            if other_project_only and projects & set(self._project_ids(other)):
                return False
            picked[oid] = other
            return True

        # Q1 — active heads by vector, closest first, up to HEADS_VEC_TOP_K.
        asked = 0
        for cos, oid in self._scored_heads(eid, heads):
            if asked >= self.pol.heads_vec_top_k or cos < self.pol.heads_cos_min:
                break
            other = self._event(oid)
            if other is not None and add(other):
                asked += 1

        # Tier 2 — bridged heads: same session (recent) and same artifact (all).
        for other in self._bridged_heads(ev, heads):
            add(other)

        # Tier 3 — cross-project value changes, both ends 变更/决定, tighter cosine.
        if kind in self.pol.cross_kinds:
            seen = 0
            for cos, oid in self._scored_heads(eid, heads, kinds=self.pol.cross_kinds):
                if cos <= self.pol.cross_cos_min or seen >= self.pol.cross_top_k:
                    break
                other = self._event(oid)
                if other is not None and add(other, other_project_only=True):
                    seen += 1

        # Q2 — the elaboration sets; these need not be heads (a 教训 is closed by definition).
        for other in self._rows("session_before", session, ets, eid, self.pol.session_recent):
            add(other)
        for other in self._rows("unclosed_lessons", ets, eid, self.pol.unclosed_lessons):
            add(other)
        for other in self._rows("active_decisions", sorted(projects), ets, eid, self.pol.decision_skeleton):
            add(other)

        for other in picked.values():
            if self._aborted:
                return
            if self.should_stop is not None and self.should_stop():
                self._stopped = True
                _log.info("eventmem nightly: stop requested, %d pairs done", self.pairs)
                return
            previous_pairs = self.pairs
            self._ask_and_write(other, ev)
            if self.pairs != previous_pairs:
                self._tick()

    # -------------------------------------------------------------- asking

    def _ask_and_write(self, old: dict, new: dict) -> None:
        """One request for both questions; each axis independent, ``重复`` silences detail."""
        src, dst = _id(new), _id(old)
        if src is None or dst is None or self._aborted:
            return
        pair = (src, dst)
        if pair in self._seen_pairs:
            return
        self._seen_pairs.add(pair)
        if self._already_recorded(src, dst):
            return

        db = getattr(self.store, "_db", None)
        lock = getattr(self.store, "_lock", None)
        snapshot = None
        if db is not None and lock is not None:
            with lock:
                snapshot = self._event_snapshot(db, src, dst)
            by_id = {row[0]: row for row in snapshot}
            if any(eid not in by_id or
                   tuple(_field(ev, key) for key in ("ts", "kind", "text", "project_id")) != by_id[eid][1:5]
                   for eid, ev in ((src, new), (dst, old))):
                self._had_failure = True
                return
        self.pairs += 1
        answers = self._decide_choices(old, new)
        if answers is None:
            return
        axis, detail = answers
        if not (_complete_answer(axis, _AXIS_RELS) and _complete_answer(detail, _DETAIL_RELS)):
            self._fail()
            return
        self._fail_streak = 0
        db = getattr(self.store, "_db", None)
        lock = getattr(self.store, "_lock", None)
        if db is not None and lock is not None:
            self._commit_pair(src, dst, axis, detail, snapshot)
            return

        # Compatibility path for non-SQLite stores and test doubles.
        self._record_pair(src, dst, axis, detail)

        hit = _accepted(axis, _AXIS_RELS, self.pol.margin, self.pol)
        if hit is not None:
            rel, conf, probs = hit
            self._write(src, dst, rel, conf, probs)
            if rel == "重复":
                return  # 换种说法没有新信息：补充轴不写
        hit = _accepted(detail, _DETAIL_RELS, self.pol.margin, self.pol)
        if hit is not None:
            rel, conf, probs = hit
            self._write(src, dst, rel, conf, probs)

    @staticmethod
    def _event_snapshot(db, src, dst):
        return tuple(tuple(row) for row in db.execute(
            "SELECT id,ts,kind,text,project_id,edited_ts FROM events WHERE id IN (?,?) ORDER BY id",
            (src, dst)))

    def _commit_pair(self, src: int, dst: int, axis, detail, snapshot) -> None:
        """Atomically preserve the first writer and its complete decision."""
        axis_hit = _accepted(axis, _AXIS_RELS, self.pol.margin, self.pol)
        detail_hit = _accepted(detail, _DETAIL_RELS, self.pol.margin, self.pol)
        hits = [axis_hit] if axis_hit else []
        if detail_hit and (not axis_hit or axis_hit[0] != "重复"):
            hits.append(detail_hit)
        db = self.store._db
        with self.store._lock:
            try:
                db.execute("BEGIN IMMEDIATE")
                if len(snapshot) != 2 or self._event_snapshot(db, src, dst) != snapshot:
                    db.rollback()
                    self._had_failure = True
                    return
                if db.execute("SELECT 1 FROM pair_evals WHERE src=? AND dst=? "
                              "UNION ALL SELECT 1 FROM edges WHERE src=? AND dst=? "
                              "UNION ALL SELECT 1 FROM edge_deletions WHERE "
                              "(src=? AND dst=?) OR (src=? AND dst=?) LIMIT 1",
                              (src, dst, src, dst, src, dst, dst, src)).fetchone():
                    db.rollback()
                    return
                inserted = db.execute("INSERT INTO pair_evals(src,dst,axis_probs,detail_probs,axis_label,detail_label,ts) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (src, dst, json.dumps(_dist(axis, _AXIS_RELS), ensure_ascii=False, sort_keys=True),
                            json.dumps(_dist(detail, _DETAIL_RELS), ensure_ascii=False, sort_keys=True),
                            axis_hit[0] if axis_hit else None, detail_hit[0] if detail_hit else None,
                            time.time()))
                if inserted.rowcount != 1:
                    raise RuntimeError('pair archive insert was ignored')
                cross = self.store._infer_cross(src, dst)
                for rel, conf, probs in hits:
                    inserted = db.execute("INSERT INTO edges(src,dst,rel,conf,decided_by,cross,probs,backend) "
                               "VALUES(?,?,?,?,?,?,?,?)",
                               (src, dst, rel, conf, "jev", cross,
                                json.dumps(probs, ensure_ascii=False, sort_keys=True), self._backend_name()))
                    if inserted.rowcount != 1:
                        raise RuntimeError('edge insert was ignored')
                db.commit()
                self.edges += len(hits)
            except Exception:  # noqa: BLE001 - don't advance the cursor after a persistence failure
                db.rollback()
                self._had_failure = True
                _log.exception("eventmem nightly: pair persistence failed")

    def _already_recorded(self, src: int, dst: int) -> bool:
        """Skip a previously archived pair; adapters without SQLite retain legacy behavior."""
        db = getattr(self.store, "_db", None)
        lock = getattr(self.store, "_lock", None)
        if db is None or lock is None:
            return False
        with lock:
            return db.execute("SELECT 1 FROM pair_evals WHERE src=? AND dst=? "
                              "UNION ALL SELECT 1 FROM edges WHERE src=? AND dst=? "
                              "UNION ALL SELECT 1 FROM edge_deletions WHERE "
                              "(src=? AND dst=?) OR (src=? AND dst=?) LIMIT 1",
                              (src, dst, src, dst, src, dst, dst, src)).fetchone() is not None

    def _record_pair(self, src: int, dst: int, axis, detail) -> None:
        """Persist both raw distributions for a pair (tolerant of fakes/old stores)."""
        fn = _method(self.store, ("add_pair_eval",))
        if fn is None:
            return
        try:
            fn(
                src,
                dst,
                axis_probs=_dist(axis, _AXIS_RELS),
                detail_probs=_dist(detail, _DETAIL_RELS),
                axis_label=_top_label(axis, _AXIS_RELS, self.pol.margin, self.pol),
                detail_label=_top_label(detail, _DETAIL_RELS, self.pol.margin, self.pol),
                ts=time.time(),
            )
        except Exception:  # noqa: BLE001 - an archive miss must not cost the pair
            return

    def _decide_choices(self, old: dict, new: dict) -> list[dict | None] | None:
        """Ask the two choice questions; ``None`` on a failed pair."""
        if self._aborted:
            return None
        try:
            raw = self.jev.decide_choice(
                _state_text(old, new),
                [
                    _question(AXIS_KEY, AXIS_TEXT, AXIS_TEXT_EN, AXIS_OPTIONS),
                    _question(DETAIL_KEY, DETAIL_TEXT, DETAIL_TEXT_EN, DETAIL_OPTIONS),
                ],
            )
        except Exception:  # noqa: BLE001
            raw = None
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return self._fail()
        return [_canonical_answer(item) for item in raw]

    def _fail(self):
        """Count one failed decide; after a few in a row, stop the night."""
        self._had_failure = True
        self._fail_streak += 1
        if self._fail_streak >= self.pol.max_fail_streak:
            self._aborted = True
        return None

    def _tick(self) -> None:
        """Publish progress after a pair.  A broken listener loses itself, not the pass."""
        if self.on_progress is None:
            return
        try:
            self.on_progress({
                "pairs": self.pairs,
                "edges": self.edges,
                "backend": self._backend_name(),
            })
        except Exception:  # noqa: BLE001 - a UI hook must never cost a pair
            _log.warning("eventmem nightly: progress hook failed; disabled")
            self.on_progress = None

    def _backend_name(self) -> str:
        """Which backend decided this edge; the legacy client has no name at all."""
        return str(getattr(self.jev, "active_name", "") or "")

    def _write(
        self, src: int, dst: int, rel: str, conf: float, probs: dict | None = None
    ) -> bool:
        backend = self._backend_name()
        # 由强到弱试：带 backend + probs → 只带 probs → 什么也不带。
        # 后两档是为了老的 store/测试替身；真实 Store 第一档就成。
        for extra in (
            {"probs": probs, "backend": backend},
            {"probs": probs},
            {},
        ):
            try:
                self.store.add_edge(src, dst, rel, float(conf), decided_by="jev", **extra)
                break
            except TypeError:
                continue
            except Exception:  # noqa: BLE001 - one bad row must not kill the night
                return False
        else:
            return False
        self.edges += 1
        return True

    # ------------------------------------------------------------- indexes

    def _all_events(self) -> dict[int, dict]:
        if self._events is not None:
            return self._events
        rows = []
        fn = _method(self.store, ("all_events", "iter_events"))
        if fn is not None:
            try:
                rows = list(fn() or [])
            except Exception:  # noqa: BLE001
                rows = []
        if not rows:
            try:
                rows = list(self.store.recent_events(0.0) or [])
            except Exception:  # noqa: BLE001
                rows = []
        out: dict[int, dict] = {}
        for ev in rows:
            eid = _id(ev)
            if eid is not None:
                out[eid] = ev
        self._events = out
        return out

    def _event(self, eid: int) -> dict | None:
        known = self._all_events()
        ev = known.get(eid)
        if ev is not None:
            return ev
        try:
            rows = list(self.store.get_events([eid]) or [])
        except Exception:  # noqa: BLE001
            return None
        for row in rows:
            rid = _id(row)
            if rid is not None:
                known[rid] = row
        return known.get(eid)

    def _vecs_map(self) -> dict[int, tuple[float, ...]]:
        if self._vecs is not None:
            return self._vecs
        out: dict[int, tuple[float, ...]] = {}
        try:
            rows = list(self.store.iter_vecs() or [])
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            try:
                eid, blob = row
            except (TypeError, ValueError):
                continue
            vec = _decode_vec(blob)
            if vec:
                out[int(eid)] = vec
        self._vecs = out

        # Bulk-fill metadata for any vector whose event is not indexed yet.
        known = self._all_events()
        missing = [eid for eid in out if eid not in known]
        if missing:
            try:
                rows = list(self.store.get_events(missing) or [])
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                rid = _id(row)
                if rid is not None:
                    known[rid] = row
        return out

    def _project_events(self, pid: int) -> list[dict]:
        cached = self._proj_events.get(pid)
        if cached is not None:
            return cached
        try:
            rows = list(self.store.project_events(pid) or [])
        except Exception:  # noqa: BLE001
            rows = []
        self._proj_events[pid] = rows
        return rows

    def _project_ids(self, ev: dict) -> tuple[int, ...]:
        eid = _id(ev)
        if eid is None:
            return tuple(_fallback_project_ids(ev))
        cached = self._project_cache.get(eid)
        if cached is not None:
            return cached
        ids = list(_fallback_project_ids(ev))
        fn = _method(
            self.store,
            ("event_project_ids", "event_projects", "projects_of_event", "get_event_projects"),
        )
        if fn is not None:
            try:
                extra = fn(eid)
            except Exception:  # noqa: BLE001
                extra = None
            for item in extra or []:
                if isinstance(item, dict):
                    item = item.get("project_id", item.get("id"))
                if item is None:
                    continue
                try:
                    item = int(item)
                except (TypeError, ValueError):
                    continue
                if item not in ids:
                    ids.append(item)
        # The Store's event dict only carries the primary project.  Secondary
        # memberships have to be recovered from project_events() (which returns
        # primary *and* secondary members of a project).
        for pid in self._membership().get(eid, ()):
            if pid not in ids:
                ids.append(pid)
        out = tuple(ids)
        self._project_cache[eid] = out
        return out

    def _membership(self) -> dict[int, list[int]]:
        if self._member_idx is not None:
            return self._member_idx
        idx: dict[int, list[int]] = {}
        fn = _method(self.store, ("list_projects",))
        rows = []
        if fn is not None:
            try:
                rows = list(fn(include_archived=True) or [])
            except TypeError:
                try:
                    rows = list(fn() or [])
                except Exception:  # noqa: BLE001
                    rows = []
            except Exception:  # noqa: BLE001
                rows = []
        for row in rows:
            pid = row.get("id") if isinstance(row, dict) else row
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            for member in self._project_events(pid):
                mid = _id(member)
                if mid is None:
                    continue
                bucket = idx.setdefault(mid, [])
                if pid not in bucket:
                    bucket.append(pid)
        self._member_idx = idx
        return idx

    def _heads(self) -> set[int]:
        """Active chain heads right now.

        Recomputed per event so a replay in ts order sees the head set evolve exactly as it would
        have in real time.  A store without ``head_ids`` (tests, older hosts) treats every event
        as a head.
        """
        fn = _method(self.store, ("head_ids",))
        if fn is not None:
            try:
                return {int(i) for i in (fn() or ())}
            except Exception:  # noqa: BLE001
                pass
        return set(self._all_events())

    def _scored_heads(self, eid: int, heads: set[int], kinds=None) -> list[tuple[float, int]]:
        """``[(cosine, id)]`` over active heads, closest first; ``eid`` itself excluded."""
        mine = self._vecs_map().get(eid)
        if not mine:
            return []
        out: list[tuple[float, int]] = []
        for oid, other_vec in self._vecs_map().items():
            if oid == eid or oid not in heads:
                continue
            if kinds is not None:
                other = self._event(oid)
                if other is None or str(_field(other, "kind") or "") not in kinds:
                    continue
            out.append((_cos(mine, other_vec), oid))
        out.sort(key=lambda item: (-item[0], item[1]))
        return out

    def _bridged_heads(self, ev: dict, heads: set[int]) -> list[dict]:
        """Heads sharing this event's session (most recent few) or one of its artifacts."""
        out: list[dict] = []
        eid = _id(ev)
        session = _field(ev, "session")
        if session:
            for other in self._rows("session_before", session, _ts(ev), eid, self.pol.session_heads_top_k):
                if _id(other) in heads:
                    out.append(other)
        keys = set(self._artifact_keys(ev))
        if keys:
            for oid, other in self._all_events().items():
                if oid == eid or oid not in heads:
                    continue
                if keys & set(self._artifact_keys(other)):
                    out.append(other)
        return out

    def _rows(self, method: str, *args) -> list[dict]:
        """Call an optional store query; a store without it simply contributes no candidates."""
        fn = getattr(self.store, method, None)
        if not callable(fn):
            return []
        try:
            return list(fn(*args) or [])
        except Exception:  # noqa: BLE001 - selection is best-effort
            return []

    def _artifact_keys(self, ev: dict) -> list[str]:
        """Identity keys two events can share, in priority order.

        ``id:``/``sha:`` are content-addressed (identical bytes only); ``path:`` is the
        file's identity across edits, which is why ``events.art_path`` exists (schema v7).
        """
        keys: list[str] = []
        aid = _field(ev, "art_id")
        if aid is not None:
            keys.append("id:%s" % aid)
            art = self._artifact(aid)
            if art:
                for name in ("path", "artifact_path", "sha", "sha256"):
                    value = _field(art, name)
                    if value:
                        keys.append("%s:%s" % (name, value))
        for name in ("art_path", "artifact_path"):
            value = _field(ev, name)
            if value:
                keys.append("path:%s" % value)
        return keys

    def _artifact(self, aid) -> dict | None:
        key = aid
        if key in self._artifact_cache:
            return self._artifact_cache[key]
        fn = _method(self.store, ("get_artifact",))
        art = None
        if fn is not None:
            try:
                art = fn(aid)
            except Exception:  # noqa: BLE001
                art = None
        self._artifact_cache[key] = art
        return art


# --------------------------------------------------------------------------- #
# small helpers (all tolerant of dict / sqlite3.Row / simple objects)
# --------------------------------------------------------------------------- #


def _complete_answer(answer, labels: tuple[str, ...]) -> bool:
    """Reject missing, non-numeric or out-of-range model probabilities before writing."""
    if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
        return False
    probs = answer["probabilities"]
    if any(label not in probs for label in labels) or "none" not in answer:
        return False
    try:
        values = [probs[label] for label in labels] + [answer["none"]]
        if any(isinstance(value, bool) for value in values):
            return False
        return all(math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0
                   for value in values)
    except (TypeError, ValueError, OverflowError):
        return False


def _dist(answer, labels: tuple[str, ...]) -> dict | None:
    """The raw distribution ``{label: p, ..., 都不是: p}`` for one question, or None."""
    if not isinstance(answer, dict):
        return None
    raw = answer.get("probabilities")
    if not isinstance(raw, dict):
        return None
    dist: dict[str, float] = {}
    for label in labels:
        try:
            dist[label] = float(raw.get(label) or 0.0)
        except (TypeError, ValueError):
            dist[label] = 0.0
    try:
        dist[NONE_LABEL] = float(answer.get("none") or 0.0)
    except (TypeError, ValueError):
        dist[NONE_LABEL] = 0.0
    # 4 标签的 axis 问法把「都不是」当**真标签**发下去（服务端的 allow_none 关掉），
    # 这时候它的概率在 probabilities 里；answer["none"] 只是壳（0.0），别拿它盖掉真值。
    if isinstance(raw.get(NONE_LABEL), (int, float)):
        try:
            dist[NONE_LABEL] = float(raw[NONE_LABEL])
        except (TypeError, ValueError):
            pass
    return dist


def _top_label(answer, labels: tuple[str, ...], margin: float = MARGIN, policy=None) -> str | None:
    """The label that clears the policy, else None (used to annotate the archive)."""
    hit = _accepted(answer, labels, margin, policy)
    return hit[0] if hit is not None else None


def _accepted(answer, labels: tuple[str, ...], margin: float = MARGIN, policy=None):
    """``(label, probability, distribution)`` when the winner clears ``MARGIN``.

    Rank on the common unconditional scale, converting a marked conditional distribution
    before comparing it with 「都不是」.  Keep the original values in the returned distribution
    so the edge and pair archive remain comparable.

    Returns ``None`` when a real label did not win, or won by less than ``MARGIN``.
    """
    dist = _dist(answer, labels)
    if dist is None:
        return None

    ranked_dist = dist
    if answer.get("probability_scale") == "conditional":
        ranked_dist = {key: (value if key == NONE_LABEL else value * (1.0 - dist[NONE_LABEL]))
                       for key, value in dist.items()}
    ranked = sorted(ranked_dist.items(), key=lambda item: (-item[1], item[0]))
    label, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if label not in labels:
        return None
    if top <= runner_up:
        return None  # Never break a top tie by label spelling, even when margin is zero.
    gap = top - runner_up
    if gap < margin and not math.isclose(gap, margin, rel_tol=0.0, abs_tol=1e-15):
        return None
    threshold_key = {'改变': 'min_conf_change', '否定': 'min_conf_negate',
                     '重复': 'min_conf_repeat', '因为': 'min_conf_because',
                     '细化': 'min_conf_refine'}[label]
    if policy is not None and top < getattr(policy, threshold_key, 0.0):
        return None
    return label, top, dist


def _field(ev, name: str, default=None):
    try:
        if hasattr(ev, "keys"):
            return ev[name] if name in ev.keys() else default
        return getattr(ev, name, default)
    except Exception:  # noqa: BLE001
        return default


def _id(ev) -> int | None:
    value = _field(ev, "id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ts(ev) -> float:
    value = _field(ev, "ts")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_older(candidate, reference) -> bool:
    """True when ``candidate`` is strictly earlier than ``reference``.

    Ties on ``ts`` are broken by id, so a pair is only ever asked once and the
    edge always points from later to earlier.
    """
    cid, rid = _id(candidate), _id(reference)
    if cid is None or rid is None:
        return False
    return (_ts(candidate), cid) < (_ts(reference), rid)


def _fallback_project_ids(ev) -> list[int]:
    ids: list[int] = []
    raw = _field(ev, "project_ids")
    if raw:
        for item in raw:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
    if not ids:
        pid = _field(ev, "project_id")
        if pid is not None:
            try:
                ids.append(int(pid))
            except (TypeError, ValueError):
                pass
    return ids


def _method(obj, names: tuple[str, ...]):
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            return fn
    return None


def _decode_vec(blob) -> tuple[float, ...] | None:
    """Decode a float32 blob (or a sequence of numbers) into a unit vector."""
    if blob is None:
        return None
    values: list[float] = []
    try:
        if isinstance(blob, (bytes, bytearray, memoryview)):
            raw = bytes(blob)
            if len(raw) < 4 or len(raw) % 4:
                return None
            values = list(struct.unpack("<%df" % (len(raw) // 4), raw))
        else:
            values = [float(v) for v in blob]
    except (TypeError, ValueError, struct.error):
        return None
    if not values:
        return None
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0.0:
        return None
    return tuple(v / norm for v in values)


def _cos(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    for x, y in zip(a, b):
        dot += x * y
    return max(-1.0, min(1.0, dot))


def _describe(ev) -> str:
    ts = _ts(ev)
    stamp = "?"
    if ts:
        try:
            stamp = time.strftime("%Y-%m-%d", time.localtime(ts))
        except (OSError, ValueError, OverflowError):
            stamp = "?"
    kind = _field(ev, "kind") or ""
    project = _field(ev, "project") or "未归类"
    text = _field(ev, "text") or ""
    return "[%s] [%s] %s：%s" % (stamp, project, kind, text)


def _state_text(old, new) -> str:
    return "前一条（较早）：%s\n后一条（较晚）：%s" % (_describe(old), _describe(new))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None, *, settings: "CoreSettings | None" = None) -> int:
    """One nightly pass: ``python -m eventmem_core.nightly --since-hours 24``.

    The ``nightly_since_ts`` cursor in ``meta`` is advanced only when the pass
    did not fail on the decision service, with a two-minute overlap because
    edges are idempotent but a gap is not.  Prints one JSON line on stdout.

    ``settings`` is how an adapter hands in the database path, the decision
    service and the edge policy; the flags above still win when given.
    """
    st = settings or CoreSettings()
    parser = argparse.ArgumentParser(
        prog="python -m eventmem_core.nightly",
        description="Build eventmem relation edges for events newer than the cursor.",
    )
    parser.add_argument("--db", default=st.db_path or os.environ.get("EVENTMEM_DB", ""),
                        help="events.db path (default: from settings / $EVENTMEM_DB)")
    parser.add_argument("--since-hours", type=float, default=24.0,
                        help="first-run window when there is no cursor (default: 24)")
    parser.add_argument("--since-ts", type=float, default=None,
                        help="explicit start timestamp; overrides the cursor")
    parser.add_argument("--no-cursor", action="store_true",
                        help="ignore the stored cursor and do not write one")
    parser.add_argument("--url", default=os.environ.get("EVENTMEM_JEV_URL") or st.decision.url,
                        help="decision server root (default: settings / $EVENTMEM_JEV_URL)")
    parser.add_argument("--timeout", type=float, default=st.decision.timeout,
                        help="per-request timeout in seconds (default: 900, a full thought)")
    parser.add_argument("--print-settings", action="store_true",
                        help="print the effective core settings as JSON and exit")
    args = parser.parse_args(argv)

    if args.print_settings:
        print(json.dumps(describe(st), ensure_ascii=False, sort_keys=True))
        return 0
    if not args.db:
        parser.error("--db is required (no db_path in settings and $EVENTMEM_DB is unset)")
    args.db = os.path.expanduser(args.db)

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        from .jev import JevClient
        from .store import Store
    except ImportError:  # run as a plain script (tests import modules by path)
        from jev import JevClient  # type: ignore[no-redef]
        from store import Store  # type: ignore[no-redef]

    db = os.path.abspath(os.path.expanduser(args.db))
    store = Store(db)
    try:
        run_start = time.time()
        cursor = args.since_ts
        if cursor is None and not args.no_cursor:
            raw = store.get_meta(CURSOR_KEY)
            if raw:
                try:
                    cursor = float(raw)
                except (TypeError, ValueError):
                    cursor = None
        if cursor is None:
            cursor = run_start - max(0.0, float(args.since_hours)) * 3600.0

        jev = JevClient(base_url=args.url, timeout=args.timeout,
                        path=st.decision.path, model=st.decision.model,
                        think_mode=st.decision.think_mode or None)
        result = build_edges(store, jev, cursor, st.edges)
        ok = result.get("skipped") not in ("jev-unavailable", "jev-error")
        if ok and not args.no_cursor:
            store.advance_meta_ts(CURSOR_KEY, run_start - 120.0)

        result["db"] = db
        result["since_ts"] = round(cursor, 3)
        result["cursor"] = store.get_meta(CURSOR_KEY)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 1
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover - exercised through bin/nightly.sh
    raise SystemExit(main())
