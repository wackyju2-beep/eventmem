#!/usr/bin/env python3
"""Regression tests for decision probability scale and archive recalibration."""
import sys
import tempfile
import json
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core'))
from eventmem_core.providers import parse_answers
from eventmem_core.nightly import _accepted, _AXIS_RELS
from eventmem_core.settings import EdgePolicy
from eventmem_core.store import Store
from eventmem_core.jev import JevClient

QUESTIONS = [('axis', 'relation', {'重复': 'r', '改变': 'c', '否定': 'n'})]

class ProbabilityScaleTests(unittest.TestCase):
    def test_systemone_conditional_margin_and_threshold_use_unconditional_values(self):
        wire = {'answers': {'axis': {'type': 'choice', 'choice': '改变', 'confidence': 0.8,
                 'probabilities': {'重复': 0.1, '改变': 0.8, '否定': 0.1},
                 'none_probability': 0.4}}}
        answer = parse_answers('systemone', wire, QUESTIONS)[0]
        self.assertEqual(answer['probabilities'], wire['answers']['axis']['probabilities'])
        hit = _accepted(answer, _AXIS_RELS, 0.05)
        self.assertAlmostEqual(hit[1], 0.48)
        self.assertEqual(hit[2]['改变'], 0.8)  # edge stores original wire-scale distribution
        self.assertIsNone(_accepted(answer, _AXIS_RELS, 0.1))
        self.assertIsNone(_accepted(answer, _AXIS_RELS, 0.05, EdgePolicy(min_conf_change=0.5)))

    def test_conditional_archive_requires_explicit_policy_preserves_raw_edge_probs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            old = store.add_event('决定', 'old')
            new = store.add_event('变更', 'new')
            axis = {'重复': 0.1, '改变': 0.8, '否定': 0.1, '都不是': 0.4}
            detail = {'因为': 0.4, '细化': 0.6, '都不是': 0.4}
            store.add_pair_eval(new, old, axis_probs=axis, detail_probs=detail, axis_label='改变')
            store.add_edge(new, old, '改变', 0.8, 'jev', probs=axis)
            policy = EdgePolicy(margin=0.1)
            unknown = store.recalibrate(policy)
            self.assertEqual(unknown['skipped'], 1)
            preview = store.recalibrate(policy, historical_scale='conditional')
            self.assertEqual(preview['skipped'], 0)
            self.assertEqual(preview['removed']['改变'], 1)
            store.recalibrate(policy, historical_scale='conditional', token=preview['token'])
            row = store._db.execute('SELECT axis_probs, detail_probs FROM pair_evals').fetchone()
            self.assertEqual(json.loads(row[0]), axis)
            self.assertEqual(json.loads(row[1]), detail)

    def test_legacy_jev_client_marks_conditional_scale(self):
        client = JevClient(base_url='http://localhost:9')
        client._post = lambda _: {'answers': {'axis': {'choice': '改变', 'confidence': 0.8,
            'probabilities': {'重复': 0.1, '改变': 0.8, '否定': 0.1}, 'none_probability': 0.4}}}
        answer = client.decide_choice('dummy', QUESTIONS)[0]
        self.assertAlmostEqual(_accepted(answer, _AXIS_RELS, 0.05)[1], 0.48)

    def test_systemone_rejects_missing_none_and_invalid_complete_distributions(self):
        for none in (None, 'bad', 0.3):
            response = {'answers': {'axis': {'choice': '改变', 'probabilities':
                        {'重复': 0.1, '改变': 0.8 if none != 0.3 else 0.6, '否定': 0.1}, 'none_probability': none}}}
            self.assertIsNone(parse_answers('systemone', response, QUESTIONS)[0])

    def test_unconditional_and_zero_none(self):
        for probs, none, expected in [({'重复': .1, '改变': .6, '否定': .1}, .2, .6),
                                      ({'重复': .1, '改变': .8, '否定': .1}, 0, .8)]:
            response = {'answers': {'axis': {'choice': '改变', 'probabilities': probs,
                                             'none_probability': none}}}
            answer = parse_answers('systemone', response, QUESTIONS)[0]
            self.assertAlmostEqual(_accepted(answer, _AXIS_RELS, .1)[1], expected)

    def test_openai_none_is_explicit_and_unconditional(self):
        response = {'answers': [{'type': 'choice', 'name': 'axis', 'choice': '改变',
                    'probabilities': [{'value': k, 'probability': v} for k, v in
                                      [('重复', .1), ('改变', .6), ('否定', .1), ('none', .2)]]}]}
        answer = parse_answers('openai-decisions', response, QUESTIONS)[0]
        self.assertAlmostEqual(_accepted(answer, _AXIS_RELS, .1)[1], .6)

    def test_openai_missing_none_is_not_a_valid_distribution(self):
        response = {'answers': [{'type': 'choice', 'name': 'axis', 'choice': '改变',
                    'probabilities': [{'value': k, 'probability': v} for k, v in
                                      [('重复', .1), ('改变', .8), ('否定', .1)]]}]}
        self.assertIsNone(parse_answers('openai-decisions', response, QUESTIONS)[0])

    def test_near_overlap_is_not_guessed(self):
        response = {'answers': {'axis': {'choice': '改变',
                    'probabilities': {'重复': .1, '改变': .8, '否定': .1},
                    'none_probability': 0.0000001}}}
        self.assertIsNone(parse_answers('systemone', response, QUESTIONS)[0])

    def test_invalid_archive_stays_skipped_even_with_explicit_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            old = store.add_event('决定', 'old')
            new = store.add_event('变更', 'new')
            axis = {'重复': .1, '改变': .6, '否定': .1, '都不是': .5}
            detail = {'因为': .5, '细化': .5, '都不是': 0}
            store.add_pair_eval(new, old, axis_probs=axis, detail_probs=detail)
            self.assertEqual(store.recalibrate(EdgePolicy(), historical_scale='conditional')['skipped'], 1)

    def test_archive_policy_is_bound_to_preview_token(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            preview = store.recalibrate(EdgePolicy(), historical_scale='conditional')
            with self.assertRaisesRegex(ValueError, 'stale preview'):
                store.recalibrate(EdgePolicy(), token=preview['token'])

    def test_surviving_conditional_edge_confidence_is_rescaled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            old = store.add_event('决定', 'old')
            new = store.add_event('变更', 'new')
            axis = {'重复': .1, '改变': .8, '否定': .1, '都不是': .2}
            detail = {'因为': .5, '细化': .5, '都不是': 0}
            store.add_pair_eval(new, old, axis_probs=axis, detail_probs=detail, axis_label='改变')
            store.add_edge(new, old, '改变', .8, 'jev', probs=axis)
            policy = EdgePolicy(margin=.1)
            preview = store.recalibrate(policy, historical_scale='conditional')
            store.recalibrate(policy, historical_scale='conditional', token=preview['token'])
            edge = store._db.execute('SELECT conf,probs FROM edges').fetchone()
            self.assertAlmostEqual(edge[0], .64)
            self.assertEqual(json.loads(edge[1]), axis)

    def test_recalibration_respects_reverse_tombstone(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            old = store.add_event('决定', 'old')
            new = store.add_event('变更', 'new')
            axis = {'重复': .1, '改变': .8, '否定': .1, '都不是': .2}
            detail = {'因为': .5, '细化': .5, '都不是': 0}
            store.add_pair_eval(new, old, axis_probs=axis, detail_probs=detail)
            store.add_edge(old, new, '改变', .9, 'human')
            store.delete_edge(old, new, '改变')
            preview = store.recalibrate(EdgePolicy(margin=.1), historical_scale='conditional')
            self.assertEqual(preview['skipped'], 1)
            self.assertEqual(preview['added']['改变'], 0)

    def test_historical_insert_retains_raw_probs_and_matches_next_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / 'db'))
            old = store.add_event('决定', 'old')
            new = store.add_event('变更', 'new')
            axis = {'重复': .1, '改变': .8, '否定': .1, '都不是': .2}
            detail = {'因为': .5, '细化': .5, '都不是': 0}
            store.add_pair_eval(new, old, axis_probs=axis, detail_probs=detail)
            policy = EdgePolicy(margin=.1)
            preview = store.recalibrate(policy, historical_scale='conditional')
            self.assertEqual(preview['added']['改变'], 1)
            store.recalibrate(policy, historical_scale='conditional', token=preview['token'])
            edge = store._db.execute('SELECT probs,conf FROM edges').fetchone()
            self.assertEqual(json.loads(edge[0]), axis)
            self.assertAlmostEqual(edge[1], .64)
            self.assertEqual(store.recalibrate(policy, historical_scale='conditional')['skipped'], 0)

if __name__ == '__main__':
    unittest.main()
