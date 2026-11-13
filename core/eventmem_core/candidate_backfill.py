"""Read-only historical candidate preview and explicitly confirmed incremental backfill.

The preview is an observation with the existing historical graph, *not* a queue of
future requests: each new judgment can change heads and hence later candidates.
No cursor, settings, or existing edges/evaluations are changed.  The caller must
serialize this job against other graph writers.  A plan is bound to the exact
snapshot/policy and is rejected if relevant data changed before execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import time

from . import nightly
from .providers import probability_scale
from .settings import EdgePolicy
from .store import Store


@dataclass(frozen=True)
class Preview:
    observed_pairs: tuple[tuple[int, int], ...]
    event_count: int
    exact_total: None = None
    # Token is a snapshot/policy fingerprint, not an authorization secret.
    token: str = ''
    policy: EdgePolicy = field(default_factory=EdgePolicy, repr=False)


def _snapshot(store):
    copy = Store(':memory:')
    try:
        with store._lock:
            if store._db.in_transaction:
                raise RuntimeError('cannot preview an open transaction')
            store._db.backup(copy._db)
    except BaseException:
        copy.close()
        raise
    return copy


def _fingerprint(store, policy):
    from dataclasses import asdict
    digest = hashlib.sha256()
    digest.update(repr(asdict(policy)).encode())
    with store._lock:
        db = store._db
        own_transaction = not db.in_transaction
        if own_transaction:
            db.execute('BEGIN')
        try:
            for table in ('events', 'projects', 'event_projects', 'artifacts', 'edges', 'pair_evals', 'edge_deletions'):
                rows = db.execute('SELECT * FROM ' + table).fetchall()
                # SQLite blobs can be bytes; repr is stable for these values.
                digest.update(table.encode())
                for row in sorted((tuple(r) for r in rows), key=repr):
                    digest.update(repr(row).encode())
        finally:
            if own_transaction:
                db.rollback()
    return digest.hexdigest()


def _matches_snapshot(store, policy, expected):
    with store._lock:
        db = store._db
        if db.in_transaction:
            raise RuntimeError('cannot validate an open transaction')
        try:
            db.execute('BEGIN')
            return _fingerprint(store, policy) == expected
        finally:
            db.rollback()


def _history_view(master, event):
    view = _snapshot(master)
    ts, eid = event['ts'], event['id']
    # Later events cannot close lessons, and edges from this event or the future
    # cannot remove heads until this event's candidates have been selected.
    future = 'SELECT id FROM events WHERE ts > ? OR (ts = ? AND id > ?)'
    current_or_future = 'SELECT id FROM events WHERE ts > ? OR (ts = ? AND id >= ?)'
    params = (ts, ts, eid)
    view._db.execute('DELETE FROM edges WHERE src IN (' + current_or_future + ')', params)
    view._db.execute('DELETE FROM event_projects WHERE event_id IN (' + future + ')', params)
    view._db.execute('DELETE FROM events WHERE id IN (' + future + ')', params)
    view._db.commit()
    return view


class _Collector(nightly._Nightly):
    def _rows(self, name, *args):
        if name == 'unclosed_lessons':
            ts, eid, limit = args
            if int(limit) <= 0:
                return []
            with self.store._lock:
                db = self.store._db
                rows = self.store._select_events(
                    "e.kind = '教训' AND (e.ts < ? OR (e.ts = ? AND e.id < ?))",
                    (ts, ts, eid), order='e.ts DESC, e.id DESC', limit=30)
                result = []
                for lesson in rows:
                    closed = db.execute(
                        "SELECT 1 FROM events x WHERE "
                        "(x.ts > ? OR (x.ts = ? AND x.id > ?)) AND "
                        "(x.ts < ? OR (x.ts = ? AND x.id < ?)) AND "
                        "x.kind IN ('结果','变更') AND "
                        "((? IS NOT NULL AND x.project_id = ?) OR "
                        "(? IS NULL AND x.session = ?)) LIMIT 1",
                        (lesson['ts'], lesson['ts'], lesson['id'], ts, ts, eid,
                         lesson['project_id'], lesson['project_id'],
                         lesson['project_id'], lesson['session'])).fetchone()
                    if not closed:
                        result.append(lesson)
                        if len(result) >= int(limit):
                            break
                return result
        return super()._rows(name, *args)

    def __init__(self, store, policy, evaluated):
        super().__init__(store, None, policy)
        self.evaluated = evaluated
        self.picked = []

    def _ask_and_write(self, old, new):
        pair = (nightly._id(new), nightly._id(old))
        if pair not in self.evaluated and pair not in self._seen_pairs:
            self._seen_pairs.add(pair)
            self.picked.append(pair)


def _events(store):
    with store._lock:
        return store._select_events()


def _evaluated(store):
    with store._lock:
        pairs = {(int(r[0]), int(r[1])) for r in store._db.execute(
            "SELECT src,dst FROM pair_evals UNION SELECT src,dst FROM edges "
            "UNION SELECT src,dst FROM edge_deletions")}
        return pairs | {(dst, src) for src, dst in pairs}


def _candidates(master, event, policy, evaluated):
    view = _history_view(master, event)
    try:
        collector = _Collector(view, policy, evaluated)
        collector._consider(event)
        return collector.picked
    finally:
        view.close()


def preview(store, policy: EdgePolicy | None = None) -> Preview:
    """Observe missing pairs against historical existing edges; no model or writes.

    ``observed_pairs`` is only a current-graph estimate, neither bound nor exact.
    ``token`` is suitable for matching a stored server-side Preview object.
    """
    policy = policy or EdgePolicy()
    master = _snapshot(store)
    try:
        token = _fingerprint(master, policy)
        events = _events(master)
        evaluated = _evaluated(master)
        pairs = []
        for event in events:
            pairs.extend(_candidates(master, event, policy, evaluated))
        return Preview(tuple(pairs), len(events), token=token, policy=policy)
    finally:
        master.close()


class _BackfillWorker(nightly._Nightly):
    def _decide_choices(self, old, new):
        # Canonicalisation must not collapse two differently named labels.
        try:
            raw = self.jev.decide_choice(
                nightly._state_text(old, new),
                [nightly._question(nightly.AXIS_KEY, nightly.AXIS_TEXT,
                                   nightly.AXIS_TEXT_EN, nightly.AXIS_OPTIONS),
                 nightly._question(nightly.DETAIL_KEY, nightly.DETAIL_TEXT,
                                   nightly.DETAIL_TEXT_EN, nightly.DETAIL_OPTIONS)])
        except Exception:
            return None
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None
        canonical = [nightly._canonical_answer(item) for item in raw]
        for before, after in zip(raw, canonical):
            if isinstance(before, dict) and isinstance(before.get('probabilities'), dict):
                if len(before['probabilities']) != len(after['probabilities']):
                    return None
        return canonical


def _valid_answer(answer, labels):
    if not nightly._complete_answer(answer, labels):
        return False
    raw = answer['probabilities']
    if set(raw) != set(labels):
        return False
    values = list(raw.values()) + [answer['none']]
    try:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) or not 0 <= v <= 1 for v in values):
            return False
        scale = probability_scale(raw.values(), answer['none'])
        if scale is None or (answer.get('probability_scale') is not None and
                             answer['probability_scale'] != scale):
            return False
        answer['probability_scale'] = scale
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _archive(store, src, dst, answers, policy, backend, *, expected=None, ts=None):
    axis, detail = answers
    hits = []
    hit = nightly._accepted(axis, nightly._AXIS_RELS, policy.margin, policy)
    if hit:
        hits.append(hit)
    if not hit or hit[0] != '重复':
        detail_hit = nightly._accepted(detail, nightly._DETAIL_RELS, policy.margin, policy)
        if detail_hit:
            hits.append(detail_hit)
    with store._lock:
        db = store._db
        try:
            db.execute('BEGIN IMMEDIATE')
            if expected is not None and _fingerprint(store, policy) != expected:
                db.rollback()
                return None
            if db.execute('SELECT 1 FROM pair_evals WHERE (src=? AND dst=?) OR (src=? AND dst=?) '
                          'UNION ALL SELECT 1 FROM edges WHERE (src=? AND dst=?) OR (src=? AND dst=?) '
                          'UNION ALL SELECT 1 FROM edge_deletions WHERE (src=? AND dst=?) OR (src=? AND dst=?) LIMIT 1',
                          (src, dst, dst, src, src, dst, dst, src, src, dst, dst, src)).fetchone():
                db.rollback()
                return None
            cur = db.execute('INSERT INTO pair_evals(src,dst,axis_probs,detail_probs,axis_label,detail_label,ts) VALUES(?,?,?,?,?,?,?)',
                       (src, dst, json.dumps(nightly._dist(axis, nightly._AXIS_RELS), ensure_ascii=False, sort_keys=True),
                        json.dumps(nightly._dist(detail, nightly._DETAIL_RELS), ensure_ascii=False, sort_keys=True),
                        nightly._top_label(axis, nightly._AXIS_RELS, policy.margin, policy),
                        nightly._top_label(detail, nightly._DETAIL_RELS, policy.margin, policy), time.time() if ts is None else ts))
            if cur.rowcount != 1:
                raise RuntimeError('pair evaluation insert was ignored')
            cross = store._infer_cross(src, dst)
            count = 0
            for rel, conf, probs in hits:
                cur = db.execute('INSERT INTO edges(src,dst,rel,conf,decided_by,cross,probs,backend) VALUES(?,?,?,?,?,?,?,?)',
                                 (src, dst, rel, conf, 'jev', cross,
                                  json.dumps(probs, ensure_ascii=False, sort_keys=True), backend))
                if cur.rowcount != 1:
                    raise RuntimeError('edge insert was ignored')
                count += cur.rowcount
            db.commit()
            return count
        except BaseException:
            db.rollback()
            raise


def execute(store, jev, plan: Preview, *, token: str, policy: EdgePolicy | None = None, should_stop=None, on_progress=None):
    """Run a confirmed plan synchronously; caller queues it and serializes graph writers.

    A stale/wrong token raises ValueError before any model call or write. A stop
    is checked before each pair; successful pairs commit atomically and are reusable
    on a fresh preview after stopping or failure. A concurrent snapshot change
    returns status 'conflict' and requires a fresh preview. No nightly cursor changes.
    """
    if not isinstance(plan, Preview) or not token or token != plan.token:
        raise ValueError('preview token does not match')
    if policy is not None and policy != plan.policy:
        raise ValueError('preview policy does not match')
    master = _snapshot(store)
    try:
        if _fingerprint(master, plan.policy) != token:
            raise ValueError('preview is stale; preview again')
        if should_stop and should_stop():
            return {'status': 'stopped', 'pairs': 0, 'edges': 0}
        try:
            available = jev.available()
        except Exception:
            available = False
        if not available:
            return {'status': 'jev-unavailable', 'pairs': 0, 'edges': 0}
        result = {'status': 'completed', 'pairs': 0, 'edges': 0}
        evaluated = _evaluated(master)
        for event in _events(master):
            for src, dst in _candidates(master, event, plan.policy, evaluated):
                if should_stop and should_stop():
                    result['status'] = 'stopped'
                    return result
                expected = _fingerprint(master, plan.policy)
                if not _matches_snapshot(store, plan.policy, expected):
                    result['status'] = 'conflict'
                    return result
                old = master.get_event(dst)
                worker = _BackfillWorker(master, jev, plan.policy)
                answers = worker._decide_choices(old, event)
                # An incomplete response cannot be recorded as a completed pair.
                if (answers is None or len(answers) != 2 or
                        not _valid_answer(answers[0], nightly._AXIS_RELS) or
                        not _valid_answer(answers[1], nightly._DETAIL_RELS)):
                    result['status'] = 'jev-error'
                    return result
                stamp = time.time()
                count = _archive(store, src, dst, answers, plan.policy, worker._backend_name(),
                                 expected=expected, ts=stamp)
                if count is None:
                    result['status'] = 'conflict'
                    return result
                _archive(master, src, dst, answers, plan.policy, worker._backend_name(), ts=stamp)
                result['edges'] += count
                evaluated.add((src, dst))
                result['pairs'] += 1
                if on_progress:
                    on_progress(dict(result))
        if not _matches_snapshot(store, plan.policy, _fingerprint(master, plan.policy)):
            result['status'] = 'conflict'
        return result
    finally:
        master.close()
