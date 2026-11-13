#!/usr/bin/env python3
"""Historical candidate backfill tests, using only in-memory Stores."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from eventmem_core.store import Store
from eventmem_core.candidate_backfill import preview, execute


class Jev:
    active_name = 'test'

    def __init__(self, axis='改变'):
        self.axis = axis
        self.calls = []

    def available(self):
        return True

    def decide_choice(self, state, questions):
        self.calls.append(state)
        probs = {label: 0.0 for label in ('重复', '改变', '否定', '都不是')}
        probs[self.axis] = 0.9
        probs['都不是'] = 0.1          # 4 个真标签，和必须为 1
        return [{'probabilities': probs, 'none': 0.0},
                {'probabilities': {'因为': 0.0, '细化': 0.0}, 'none': 1.0}]


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)

    def test_startlux_wire_conditional_backfill_converts_confidence(self):
        import json
        from unittest.mock import patch
        from eventmem_core.jev import JevClient
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        client = JevClient('http://localhost:18090')
        # axis 现在是 4 个真标签（都不是 在 probabilities 里，服务端 allow_none 关掉）；
        # detail 仍然靠服务端的 none_probability，所以这里两种尺度各覆盖一次。
        raw = {'answers': {
            'axis': {'choice': '改变', 'confidence': .8,
                     'probabilities': {'重复': .1, '改变': .8, '否定': .05, '都不是': .05}},
            'detail': {'choice': None, 'confidence': .8,
                       'probabilities': {'因为': .5, '细化': .5}, 'none_probability': .8}}}
        plan = preview(self.store)
        with patch.dict('os.environ', {'EVENTMEM_LANG': 'zh'}), \
             patch.object(client, 'available', return_value=True), \
             patch.object(client, '_post', return_value=raw):
            result = execute(self.store, client, plan, token=plan.token)
        self.assertEqual((result['status'], result['pairs'], result['edges']), ('completed', 1, 1))
        edge = self.store._db.execute('SELECT rel,conf,probs FROM edges').fetchone()
        self.assertEqual(edge[0], '改变')
        self.assertAlmostEqual(edge[1], .8)
        self.assertEqual(json.loads(edge[2])['改变'], .8)

    def test_wire_parsers_reject_missing_invalid_and_boolean_probabilities(self):
        from eventmem_core.providers import _parse_systemone, _parse_decisions
        from eventmem_core.jev import _choice_answer
        labels = ('a', 'b')
        for bad in (None, True, '0', float('nan'), -1, 2, -0.0000001, 1.0000001):
            raw = {'choice': 'a', 'probabilities': {'a': 1, 'b': bad}, 'none_probability': 0}
            with self.subTest(bad=bad):
                self.assertIsNone(_parse_systemone(raw, labels))
                self.assertIsNone(_choice_answer(raw, labels))
                decisions = {'choice': 'a', 'probabilities': [
                    {'value': 'a', 'probability': 1}, {'value': 'b', 'probability': bad},
                    {'value': 'none', 'probability': 0}]}
                self.assertIsNone(_parse_decisions(decisions, labels))
        for field in ('none_probability',):
            raw = {'choice': 'a', 'probabilities': {'a': 1, 'b': 0}, field: False}
            self.assertIsNone(_parse_systemone(raw, labels))
            self.assertIsNone(_choice_answer(raw, labels))

    def test_wire_rejects_unexpected_probability_entries_instead_of_dropping_them(self):
        from eventmem_core.providers import _parse_systemone, _parse_decisions
        from eventmem_core.jev import _choice_answer
        raw = {'choice': 'a', 'probabilities': {'a': .9, 'b': 0, 'extra': 'bad'},
               'none_probability': .1}
        self.assertIsNone(_parse_systemone(raw, ('a', 'b')))
        self.assertIsNone(_choice_answer(raw, ('a', 'b')))
        decisions = {'choice': 'a', 'probabilities': [
            {'value': 'a', 'probability': .9}, {'value': 'b', 'probability': 0},
            {'value': 'none', 'probability': .1}, {'value': 'extra', 'probability': 'bad'}]}
        self.assertIsNone(_parse_decisions(decisions, ('a', 'b')))

    def test_deleted_pair_is_not_previewed_asked_or_archived(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                with Store(':memory:') as store:
                    a = store.add_event('尝试', 'a', ts=1, session='s')
                    b = store.add_event('尝试', 'b', ts=2, session='s')
                    src, dst = (a, b) if reverse else (b, a)
                    store.add_edge(src, dst, '改变', .9, 'human')
                    self.assertTrue(store.delete_edge(src, dst, '改变'))
                    plan = preview(store)
                    self.assertEqual(plan.observed_pairs, ())
                    client = Jev()
                    result = execute(store, client, plan, token=plan.token)
                    self.assertEqual((result['pairs'], len(client.calls)), (0, 0))
                    self.assertEqual(store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
                    self.assertEqual(store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_tombstone_invalidates_preview_even_if_graph_is_unchanged(self):
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        b = self.store.add_event('尝试', 'b', ts=2, session='s')
        plan = preview(self.store)
        self.store.add_edge(b, a, '改变', .9, 'human')
        self.store.delete_edge(b, a, '改变')
        self.assertNotEqual(plan.token, preview(self.store).token)
        client = Jev()
        with self.assertRaises(ValueError):
            execute(self.store, client, plan, token=plan.token)
        self.assertEqual(client.calls, [])

    def test_ignored_edge_insert_rolls_back_entire_pair(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store._db.execute("CREATE TRIGGER ignore_edge BEFORE INSERT ON edges BEGIN SELECT RAISE(IGNORE); END")
        self.store._db.commit()
        plan = preview(self.store)
        with self.assertRaisesRegex(RuntimeError, 'insert'):
            execute(self.store, Jev(), plan, token=plan.token)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_ignored_evaluation_insert_rolls_back_entire_pair(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store._db.execute("CREATE TRIGGER ignore_eval BEFORE INSERT ON pair_evals BEGIN SELECT RAISE(IGNORE); END")
        self.store._db.commit()
        plan = preview(self.store)
        with self.assertRaisesRegex(RuntimeError, 'insert'):
            execute(self.store, Jev(), plan, token=plan.token)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_archive_refuses_deleted_pair_even_with_no_accepted_edges(self):
        from eventmem_core.candidate_backfill import _archive
        from eventmem_core.settings import EdgePolicy
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        b = self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store.add_edge(a, b, '改变', .9, 'human')
        self.store.delete_edge(a, b, '改变')
        answers = [{'probabilities': {}, 'none': 1}, {'probabilities': {}, 'none': 1}]
        self.assertIsNone(_archive(self.store, b, a, answers, EdgePolicy(), 'test'))
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)

    def test_concurrent_deletion_after_last_pair_reports_conflict(self):
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        b = self.store.add_event('尝试', 'b', ts=2, session='s')
        plan = preview(self.store)
        def progress(_):
            self.store.add_edge(a, b, '因为', .9, 'human')
            self.store.delete_edge(a, b, '因为')
        result = execute(self.store, Jev(), plan, token=plan.token, on_progress=progress)
        self.assertEqual((result['status'], result['pairs'], result['edges']), ('conflict', 1, 1))

    def test_v9_guard_ignored_edge_rolls_back_pair_and_tombstone(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store._db.execute("CREATE TRIGGER tombstone_on_eval AFTER INSERT ON pair_evals "
                               "BEGIN INSERT INTO edge_deletions VALUES (NEW.src,NEW.dst,'改变',1); END")
        self.store._db.commit()
        plan = preview(self.store)
        with self.assertRaisesRegex(RuntimeError, 'edge insert'):
            execute(self.store, Jev(), plan, token=plan.token)
        for table in ('pair_evals', 'edges', 'edge_deletions'):
            self.assertEqual(self.store._db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_concurrent_tombstone_from_second_connection_aborts(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            path = str(Path(directory) / 'test.db')
            with Store(path) as primary, Store(path) as other:
                a = primary.add_event('尝试', 'a', ts=1, session='s')
                b = primary.add_event('尝试', 'b', ts=2, session='s')
                plan = preview(primary)
                class Racing(Jev):
                    def decide_choice(self, state, questions):
                        other.add_edge(b, a, '改变', .9, 'human')
                        other.delete_edge(b, a, '改变')
                        return super().decide_choice(state, questions)
                result = execute(primary, Racing(), plan, token=plan.token)
                self.assertEqual((result['status'], result['pairs'], result['edges']), ('conflict', 0, 0))
                self.assertEqual(primary._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
                self.assertEqual(primary._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)
                self.assertEqual(primary._db.execute('SELECT count(*) FROM edge_deletions').fetchone()[0], 1)

    def test_direct_fingerprint_reads_one_sqlite_snapshot(self):
        import os
        import tempfile
        from eventmem_core.candidate_backfill import _fingerprint
        from eventmem_core.settings import EdgePolicy
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            path = str(Path(directory) / 'test.db')
            with Store(path) as primary, Store(path) as other:
                a = primary.add_event('尝试', 'a', ts=1, session='s')
                b = primary.add_event('尝试', 'b', ts=2, session='s')
                policy = EdgePolicy()
                before = _fingerprint(primary, policy)
                fired = []
                def trace(sql):
                    if sql == 'SELECT * FROM edge_deletions' and not fired:
                        fired.append(True)
                        other.add_edge(b, a, '改变', .9, 'human')
                        other.delete_edge(b, a, '改变')
                primary._db.set_trace_callback(trace)
                try:
                    observed = _fingerprint(primary, policy)
                finally:
                    primary._db.set_trace_callback(None)
                self.assertEqual(fired, [True])
                self.assertEqual(observed, before)
                self.assertNotEqual(_fingerprint(primary, policy), before)

    def test_second_edge_ignored_rolls_back_first_edge_and_evaluation(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store._db.execute("CREATE TRIGGER ignore_detail BEFORE INSERT ON edges "
                               "WHEN NEW.rel='细化' BEGIN SELECT RAISE(IGNORE); END")
        self.store._db.commit()
        class TwoHits(Jev):
            def decide_choice(self, state, questions):
                return [{'probabilities': {'重复': 0, '改变': .9, '否定': 0, '都不是': .1},
                         'none': 0.0},
                        {'probabilities': {'因为': 0, '细化': .9}, 'none': .1}]
        plan = preview(self.store)
        with self.assertRaisesRegex(RuntimeError, 'edge insert'):
            execute(self.store, TwoHits(), plan, token=plan.token)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_backfill_honours_minimum_confidence_policy(self):
        from dataclasses import replace
        from eventmem_core.settings import EdgePolicy
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        b = self.store.add_event('尝试', 'b', ts=2, session='s')
        policy = replace(EdgePolicy(), min_conf_change=0.95)
        plan = preview(self.store, policy)
        result = execute(self.store, Jev(), plan, token=plan.token)
        self.assertEqual(result['pairs'], 1)
        self.assertIsNone(self.store._db.execute('SELECT 1 FROM edges WHERE src=? AND dst=?', (b,a)).fetchone())
        self.assertIsNone(self.store._db.execute('SELECT axis_label FROM pair_evals WHERE src=? AND dst=?', (b,a)).fetchone()[0])

    def test_preview_discovers_missing_without_writes(self):
        a = self.store.add_event('尝试', 'earlier', ts=1, session='s')
        b = self.store.add_event('尝试', 'later', ts=2, session='s')
        before = self.store._db.total_changes
        plan = preview(self.store)
        self.assertEqual(plan.observed_pairs, ((b, a),))
        self.assertIsNone(plan.exact_total)
        self.assertEqual(self.store._db.total_changes, before)


    def test_execution_reuses_evaluations_and_deduplicates_candidates(self):
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        b = self.store.add_event('尝试', 'b', ts=2, session='s')
        c = self.store.add_event('尝试', 'c', ts=3, session='s')
        self.store.add_pair_eval(b, a, axis_probs={'都不是': 1}, ts=42)
        plan = preview(self.store)
        client = Jev()
        result = execute(self.store, client, plan, token=plan.token)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['pairs'], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.store._db.execute('SELECT ts FROM pair_evals WHERE src=? AND dst=?', (b, a)).fetchone()[0], 42)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 3)


    def test_future_heads_do_not_hide_past_and_judgments_change_later_candidates(self):
        from dataclasses import replace
        from eventmem_core.settings import EdgePolicy
        import struct
        policy = replace(EdgePolicy(), session_recent=0, session_heads_top_k=0,
                         unclosed_lessons=0, decision_skeleton=0, cross_top_k=0)
        v = struct.pack('<2f', 1, 0)
        a = self.store.add_event('尝试', 'a', ts=1, vec=v)
        b = self.store.add_event('尝试', 'b', ts=2, vec=v)
        c = self.store.add_event('尝试', 'c', ts=3, vec=v)
        self.store.add_edge(c, a, '改变', 0.99, 'human')
        p = preview(self.store, policy)
        self.assertEqual(p.observed_pairs, ((b, a), (c, b)))
        result = execute(self.store, Jev(), p, token=p.token)
        self.assertEqual(result['pairs'], 2)
        self.assertEqual({tuple(r) for r in self.store._db.execute('SELECT src,dst FROM pair_evals')}, {(b, a), (c, b)})
        self.assertTrue(self.store._db.execute("SELECT 1 FROM edges WHERE src=? AND dst=? AND decided_by='human'", (c,a)).fetchone())


    def test_confirmation_stale_and_changed_policy_are_rejected(self):
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        p = preview(self.store)
        client = Jev()
        with self.assertRaises(ValueError):
            execute(self.store, client, p, token='wrong')
        self.store.add_event('尝试', 'c', ts=3, session='s')
        with self.assertRaises(ValueError):
            execute(self.store, client, p, token=p.token)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)

    def test_stop_and_failure_leave_only_completed_pairs_and_retry(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store.add_event('尝试', 'c', ts=3, session='s')
        p = preview(self.store)
        client = Jev()
        result = execute(self.store, client, p, token=p.token, should_stop=lambda: len(client.calls) >= 1)
        self.assertEqual((result['status'], result['pairs']), ('stopped', 1))
        class Broken(Jev):
            def decide_choice(self, state, questions):
                return [None, None]
        p = preview(self.store)
        failed = execute(self.store, Broken(), p, token=p.token)
        self.assertEqual((failed['status'], failed['pairs']), ('jev-error', 0))
        p = preview(self.store)
        retried = execute(self.store, Jev(), p, token=p.token)
        self.assertEqual((retried['status'], retried['pairs']), ('completed', 2))

    def test_timestamp_ties_order_ids_not_insertion_order(self):
        late = self.store.add_event('尝试', 'late', ts=20, session='s')
        first = self.store.add_event('尝试', 'first', ts=10, session='s')
        second = self.store.add_event('尝试', 'second', ts=10, session='s')
        p = preview(self.store)
        self.assertEqual(p.observed_pairs, ((second, first), (late, second), (late, first)))

    def test_concurrent_event_edit_aborts_without_archiving(self):
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        store = self.store
        class Editing(Jev):
            def decide_choice(self, state, questions):
                store._db.execute('UPDATE events SET text=? WHERE id=?', ('edited', a))
                store._db.commit()
                return super().decide_choice(state, questions)
        plan = preview(store)
        result = execute(store, Editing(), plan, token=plan.token)
        self.assertEqual((result['status'], result['pairs'], result['edges']), ('conflict', 0, 0))
        self.assertEqual(store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
        self.assertEqual(store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_human_edge_without_evaluation_is_not_rejudged(self):
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        b = self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store.add_edge(b, a, '否定', .9, 'human')
        plan = preview(self.store)
        self.assertEqual(plan.observed_pairs, ())
        client = Jev()
        result = execute(self.store, client, plan, token=plan.token)
        self.assertEqual((result['pairs'], len(client.calls)), (0, 0))
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 1)

    def test_empty_distribution_is_retryable(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        class Empty(Jev):
            def decide_choice(self, state, questions):
                return [{'probabilities': {}}, {'probabilities': {}}]
        plan = preview(self.store)
        result = execute(self.store, Empty(), plan, token=plan.token)
        self.assertEqual((result['status'], result['pairs']), ('jev-error', 0))
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
        retry = preview(self.store)
        self.assertEqual(execute(self.store, Jev(), retry, token=retry.token)['pairs'], 1)

    def test_current_result_does_not_close_lesson_before_pairing(self):
        from dataclasses import replace
        from eventmem_core.settings import EdgePolicy
        policy = replace(EdgePolicy(), session_recent=0, session_heads_top_k=0,
                         heads_vec_top_k=0, cross_top_k=0, decision_skeleton=0)
        a = self.store.add_event('教训', 'lesson', ts=1, session='s')
        b = self.store.add_event('结果', 'result', ts=2, session='s')
        self.assertEqual(preview(self.store, policy).observed_pairs, ((b, a),))

    def test_concurrent_forget_eval_and_human_edge_abort(self):
        for mutation in ('forget-old', 'forget-new', 'edit-new', 'evaluation', 'human'):
            with self.subTest(mutation=mutation):
                store = Store(':memory:')
                try:
                    a = store.add_event('尝试', 'a', ts=1, session='s')
                    b = store.add_event('尝试', 'b', ts=2, session='s')
                    plan = preview(store)
                    class Racing(Jev):
                        def decide_choice(self, state, questions):
                            if mutation == 'forget-old':
                                store.forget(a)
                            elif mutation == 'forget-new':
                                store.forget(b)
                            elif mutation == 'edit-new':
                                store._db.execute('UPDATE events SET text=? WHERE id=?', ('new', b))
                                store._db.commit()
                            elif mutation == 'evaluation':
                                store.add_pair_eval(b, a, axis_probs={'都不是': 1}, ts=99)
                            else:
                                store.add_edge(b, a, '否定', .9, 'human')
                            return super().decide_choice(state, questions)
                    client = Racing()
                    result = execute(store, client, plan, token=plan.token)
                    self.assertEqual((result['status'], result['pairs'], result['edges']), ('conflict', 0, 0))
                    self.assertEqual(len(client.calls), 1)
                    self.assertEqual(store._db.execute("SELECT count(*) FROM edges WHERE decided_by='jev'").fetchone()[0], 0)
                    self.assertEqual(store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], mutation == 'evaluation')
                finally:
                    store.close()

    def test_pre_request_change_aborts_before_decision(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        store = self.store
        class Editing(Jev):
            def available(self):
                store._db.execute("UPDATE events SET text='changed' WHERE text='a'")
                store._db.commit()
                return True
        plan = preview(store)
        client = Editing()
        result = execute(store, client, plan, token=plan.token)
        self.assertEqual((result['status'], result['pairs'], len(client.calls)), ('conflict', 0, 0))

    def test_malformed_distributions_never_archive(self):
        invalid = [
            {'probabilities': {}, 'none': 0},
            {'probabilities': {'改变': float('nan')}, 'none': 0},
            {'probabilities': {'改变': float('inf')}, 'none': 0},
            {'probabilities': {'改变': -1}, 'none': 1},
            {'probabilities': {'改变': 1.1}, 'none': 0},
            {'probabilities': {'改变': '1'}, 'none': 0},
            {'probabilities': {'改变': True}, 'none': 0},
            {'probabilities': {'unknown': 1}, 'none': 0},
            {'probabilities': {'改变': .8}, 'none': .8},
            {'probabilities': {'改变': .1}, 'none': .1},
            {'probabilities': {'改变': 1}, 'none': None},
        ]
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        for answer in invalid:
            with self.subTest(answer=answer):
                class Bad(Jev):
                    def decide_choice(self, state, questions):
                        return [answer, {'probabilities': {'因为': 0, '细化': 0}, 'none': 1}]
                plan = preview(self.store)
                result = execute(self.store, Bad(), plan, token=plan.token)
                self.assertEqual((result['status'], result['pairs']), ('jev-error', 0))
                self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)

    def test_ambiguous_or_partial_complete_looking_answers_never_archive(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        answers = [
            {'probabilities': {'改变': .9}, 'none': .1},
            {'probabilities': {'重复': .1, '改变': .8, '否定': .1}, 'none': 0.0000005},
            {'probabilities': {'重复': 0, '改变': .9, '否定': 0}, 'none': True},
            {'probabilities': {'重复': 0, '改变': .9, '否定': 0}, 'none': .1,
             'probability_scale': 'conditional'},
            {'probabilities': {'repeat': 0, '重复': 0, '改变': .9, '否定': 0}, 'none': .1},
        ]
        for answer in answers:
            with self.subTest(answer=answer):
                class Bad(Jev):
                    def decide_choice(self, state, questions):
                        return [answer, {'probabilities': {'因为': 0, '细化': 0}, 'none': 1}]
                p = preview(self.store)
                result = execute(self.store, Bad(), p, token=p.token)
                self.assertEqual((result['status'], result['pairs'], result['edges']), ('jev-error', 0, 0))
                for table in ('pair_evals', 'edges'):
                    self.assertEqual(self.store._db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_normalized_provider_responses_and_none_only(self):
        from eventmem_core.providers import _parse_systemone, _parse_decisions
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        from eventmem_core import providers
        class Provider(Jev):
            def decide_choice(self, state, questions):
                axis = _parse_systemone({'choice': 'change',
                                         'probabilities': {'change': .9, 'repeat': 0,
                                                           'negate': 0, 'none': .1}},
                                        ('change', 'repeat', 'negate', 'none'))
                detail = _parse_decisions({'type': 'choice', 'choice': None, 'probabilities': [
                    {'value': 'because', 'probability': 0}, {'value': 'refine', 'probability': 0},
                    {'value': providers.NONE_LABEL, 'probability': 1}]}, ('because', 'refine'))
                return [axis, detail]
        plan = preview(self.store)
        result = execute(self.store, Provider(), plan, token=plan.token)
        self.assertEqual((result['status'], result['pairs']), ('completed', 1))
        self.assertEqual(self.store._db.execute('SELECT rel FROM edges').fetchone()[0], '改变')

    def test_lesson_tie_order_and_future_closure(self):
        from dataclasses import replace
        from eventmem_core.settings import EdgePolicy
        policy = replace(EdgePolicy(), session_recent=0, session_heads_top_k=0,
                         heads_vec_top_k=0, cross_top_k=0, decision_skeleton=0)
        lesson = self.store.add_event('教训', 'lesson', ts=1, session='s')
        closure = self.store.add_event('变更', 'closure', ts=1, session='s')
        later = self.store.add_event('尝试', 'later', ts=1, session='s')
        pairs = preview(self.store, policy).observed_pairs
        self.assertIn((closure, lesson), pairs)
        self.assertNotIn((later, lesson), pairs)

    def test_second_connection_inserts_evaluation_during_request(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            path = str(Path(directory) / 'test.db')
            with Store(path) as primary, Store(path) as other:
                a = primary.add_event('尝试', 'a', ts=1, session='s')
                b = primary.add_event('尝试', 'b', ts=2, session='s')
                primary.add_event('尝试', 'c', ts=3, session='s')
                plan = preview(primary)
                class Racing(Jev):
                    def decide_choice(self, state, questions):
                        other.add_pair_eval(b, a, axis_probs={'都不是': 1}, ts=99)
                        return super().decide_choice(state, questions)
                client = Racing()
                result = execute(primary, client, plan, token=plan.token)
                self.assertEqual((result['status'], result['pairs'], len(client.calls)), ('conflict', 0, 1))
                self.assertEqual(primary._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 1)
                self.assertEqual(primary._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)

    def test_reverse_or_edited_edge_blocks_pair(self):
        for source, edited in [('human', None), ('jev', 123)]:
            with self.subTest(source=source):
                store = Store(':memory:')
                try:
                    a = store.add_event('尝试', 'a', ts=1, session='s')
                    b = store.add_event('尝试', 'b', ts=2, session='s')
                    store.add_edge(a, b, '否定', .9, source)
                    if edited:
                        store._db.execute('UPDATE edges SET edited_ts=?', (edited,))
                        store._db.commit()
                    plan = preview(store)
                    self.assertEqual(plan.observed_pairs, ())
                    self.assertEqual(execute(store, Jev(), plan, token=plan.token)['pairs'], 0)
                finally:
                    store.close()

    def test_conflict_after_success_preserves_only_completed_pair(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store.add_event('尝试', 'c', ts=3, session='s')
        store = self.store
        class Racing(Jev):
            def decide_choice(self, state, questions):
                if self.calls:
                    store._db.execute("UPDATE events SET text='changed' WHERE text='c'")
                    store._db.commit()
                return super().decide_choice(state, questions)
        client = Racing()
        plan = preview(store)
        result = execute(store, client, plan, token=plan.token)
        self.assertEqual((result['status'], result['pairs'], result['edges']), ('conflict', 1, 1))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 1)
        self.assertEqual(store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 1)

    def test_archive_write_failure_is_atomic(self):
        self.store.add_event('尝试', 'a', ts=1, session='s')
        self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store._db.execute("CREATE TRIGGER reject_edge BEFORE INSERT ON edges BEGIN SELECT RAISE(ABORT, 'edge failed'); END")
        self.store._db.commit()
        p = preview(self.store)
        with self.assertRaises(Exception):
            execute(self.store, Jev(), p, token=p.token)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)


    def test_new_policy_must_match_confirmation(self):
        from dataclasses import replace
        p = preview(self.store)
        changed = replace(p.policy, session_recent=0)
        with self.assertRaises(ValueError):
            execute(self.store, Jev(), p, token=p.token, policy=changed)

    def test_preview_rejects_open_write_transaction(self):
        self.store._db.execute("INSERT INTO meta(key,value) VALUES ('pending','value')")
        with self.assertRaises(RuntimeError):
            preview(self.store)
        self.store._db.rollback()


    def test_existing_edge_and_evaluation_survive_tighter_policy(self):
        from dataclasses import replace
        from eventmem_core.settings import EdgePolicy
        a = self.store.add_event('尝试', 'a', ts=1, session='s')
        b = self.store.add_event('尝试', 'b', ts=2, session='s')
        self.store.add_edge(b, a, '细化', .83, 'human')
        self.store.add_pair_eval(b, a, axis_probs={'都不是': 1}, ts=7)
        before = [tuple(r) for r in self.store._db.execute('SELECT * FROM edges')]
        policy = replace(EdgePolicy(), session_heads_top_k=0, session_recent=0,
                         heads_vec_top_k=0, cross_top_k=0, unclosed_lessons=0,
                         decision_skeleton=0)
        p = preview(self.store, policy)
        self.assertEqual(p.observed_pairs, ())
        execute(self.store, Jev(), p, token=p.token)
        self.assertEqual(before, [tuple(r) for r in self.store._db.execute('SELECT * FROM edges')])
        self.assertEqual(self.store._db.execute('SELECT ts FROM pair_evals').fetchone()[0], 7)


if __name__ == '__main__':
    unittest.main(verbosity=2)
