#!/usr/bin/env python3
"""Threshold and historical recalibration contract; only temporary databases."""
import os
import sys
import tempfile
import json
import threading
import urllib.request
import urllib.error
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from eventmem_core.settings import CoreSettings, apply
from eventmem_core.daemon import Daemon, make_server
from eventmem_core.schema import SCHEMA_VERSION
from eventmem_core.store import Store

AXIS = {'改变': .7, '否定': .1, '重复': .1, '都不是': .1}
DETAIL = {'因为': .6, '细化': .2, '都不是': .2}

class RecalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'test.db')
        self.store = Store(self.path)
        self.store.add_event('决定', 'old')
        self.store.add_event('变更', 'new')
        self.d = Daemon(CoreSettings(db_path=self.path))
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def route(self, action, body):
        return self.d.route('POST', ['api','recalibrate',action], body)
    def test_conditional_scale_is_explicit_and_bound_to_preview(self):
        axis = {'改变': .8, '否定': .1, '重复': .1, '都不是': .3}
        detail = {'因为': .8, '细化': .2, '都不是': .3}
        self.store.add_pair_eval(2, 1, axis_probs=axis, detail_probs=detail)
        status, conservative = self.route('preview', {'edges': {}})
        self.assertEqual((status, conservative['skipped']), (200, 1))
        status, explicit = self.route('preview', {'edges': {}, 'historical_scale': 'conditional'})
        self.assertEqual((status, explicit['skipped'], explicit['added']['改变']), (200, 0, 1))
        self.assertEqual(self.route('apply', {'edges': {}, 'token': explicit['token']})[0], 409)
        self.assertEqual(self.route('apply', {'edges': {}, 'historical_scale': 'conditional', 'token': conservative['token']})[0], 409)
        status, _ = self.route('apply', {'edges': {}, 'historical_scale': 'conditional', 'token': explicit['token']})
        self.assertEqual(status, 200)
        self.assertAlmostEqual(self.store._db.execute("SELECT conf FROM edges WHERE rel='改变'").fetchone()[0], .56)

    def test_scale_validation_is_strict_on_preview_and_apply(self):
        _, p = self.route('preview', {'edges': {}})
        for value in ('raw', '', False, True, 0, [], {}):
            for action in ('preview', 'apply'):
                with self.subTest(value=value, action=action):
                    self.assertEqual(self.route(action, {'edges': {}, 'token': p['token'], 'historical_scale': value})[0], 400)

    def test_conditional_scale_http_round_trip(self):
        self.store.add_pair_eval(2, 1, axis_probs={'改变': .8, '否定': .1, '重复': .1, '都不是': .3},
                                 detail_probs={'因为': .8, '细化': .2, '都不是': .3})
        server = make_server(self.d, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(action, body):
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/recalibrate/{action}',
                                         data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as response:
                with response:
                    return response.code, json.load(response)
        try:
            _, default = request('preview', {'edges': {}})
            self.assertEqual(default['skipped'], 1)
            _, preview = request('preview', {'edges': {}, 'historical_scale': 'conditional'})
            self.assertEqual(preview['added']['改变'], 1)
            self.assertEqual(request('apply', {'edges': {}, 'token': preview['token']})[0], 409)
            self.assertEqual(request('apply', {'edges': {}, 'token': preview['token'], 'historical_scale': 'conditional'})[0], 200)
            self.assertAlmostEqual(self.store._db.execute("SELECT conf FROM edges WHERE rel='改变'").fetchone()[0], .56)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_unarchived_jev_edges_are_reported_even_when_probs_present(self):
        self.store.add_edge(2, 1, '改变', .7, 'jev', probs=AXIS)
        self.store.add_edge(2, 1, '因为', .6, 'jev', probs=DETAIL)
        _, p = self.route('preview', {'edges': {}})
        self.assertEqual(p['uncovered_jev_edges'], 2)
        self.assertTrue(p['legacy_deletions_unknown'])
        self.assertEqual(p['removed']['改变'], 0)

    def test_manual_deletion_survives_repeat_evaluation_and_invalidates_preview(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL,
                                 axis_label='改变', detail_label='因为')
        self.store.add_edge(2, 1, '改变', .7, 'jev', probs=AXIS)
        self.store.add_edge(2, 1, '因为', .6, 'jev', probs=DETAIL)
        _, old = self.route('preview', {'edges': {}})
        self.assertTrue(self.store.delete_edge(2, 1, '改变'))
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL,
                                 axis_label=None, detail_label='因为')
        self.store.add_edge(2, 1, '改变', .7, 'jev', probs=AXIS)
        self.assertIsNone(self.store._db.execute("select 1 from edges where rel='改变'").fetchone())
        self.assertEqual(self.route('apply', {'edges': {}, 'token': old['token']})[0], 409)
        _, p = self.route('preview', {'edges': {}})
        self.assertEqual(p['skipped'], 1)
        self.assertEqual(p['manual_deletion_pairs'], 1)
        self.assertEqual(p['added']['改变'], 0)
        self.assertEqual(self.route('apply', {'edges': {}, 'token': p['token']})[0], 200)
        self.assertIsNone(self.store._db.execute("select 1 from edges where rel='改变'").fetchone())

    def test_direct_jev_insert_cannot_bypass_manual_deletion(self):
        self.store.add_edge(2, 1, '改变', .7, 'jev')
        self.store.delete_edge(2, 1, '改变')
        self.store._db.execute("INSERT OR IGNORE INTO edges(src,dst,rel,conf,decided_by,cross) VALUES(2,1,'因为',.6,'jev',0)")
        self.store._db.commit()
        self.assertIsNone(self.store._db.execute('SELECT 1 FROM edges').fetchone())

    def test_v8_migration_keeps_existing_data_and_is_idempotent(self):
        self.store.add_edge(2, 1, '改变', .7, 'jev')
        self.store._db.execute('DROP TRIGGER edge_deletions_block_jev')
        self.store._db.execute('DROP TABLE edge_deletions')
        self.store._db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
        self.store._db.commit()
        from eventmem_core.schema import ensure_schema
        ensure_schema(self.store._db)
        ensure_schema(self.store._db)
        self.assertEqual(self.store._db.execute('SELECT rel FROM edges').fetchone()[0], '改变')
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edge_deletions').fetchone()[0], 0)
        self.assertEqual(self.store._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))

    def test_manual_delete_rolls_back_evidence_when_delete_fails(self):
        self.store.add_edge(2, 1, '改变', .7, 'jev')
        self.store._db.execute("CREATE TRIGGER reject_manual_delete BEFORE DELETE ON edges BEGIN SELECT RAISE(ABORT, 'reject'); END")
        self.store._db.commit()
        with self.assertRaises(Exception):
            self.store.delete_edge(2, 1, '改变')
        self.assertFalse(self.store._db.in_transaction)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edge_deletions').fetchone()[0], 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edges').fetchone()[0], 1)

    def test_thresholds_apply_to_raw_winner(self):
        from eventmem_core.nightly import _accepted
        pol = apply(CoreSettings(), {'edges': {'min_conf_change': .8}}, strict=True).edges
        answer = {'probabilities': {k:v for k,v in AXIS.items() if k != '都不是'}, 'none': .1}
        self.assertIsNone(_accepted(answer, ('重复','改变','否定'), pol.margin, pol))
        self.assertIsNotNone(_accepted(answer, ('重复','改变','否定'), pol.margin))
        with self.assertRaises(ValueError):
            apply(CoreSettings(), {'edges': {'min_conf_change': 1.1}}, strict=True)
    def test_repeat_removes_detail_and_new_edges_have_unknown_backend(self):
        repeat = {'重复': .7, '改变': .1, '否定': .1, '都不是': .1}
        self.store.add_pair_eval(2, 1, axis_probs=repeat, detail_probs=DETAIL)
        self.store.add_edge(2, 1, '因为', .6, 'jev', probs=DETAIL, backend='known')
        status, p = self.route('preview', {'edges': {}})
        self.assertEqual((status, p['added']['重复'], p['removed']['因为']), (200, 1, 1))
        status, _ = self.route('apply', {'edges': {}, 'token': p['token']})
        self.assertEqual(status, 200)
        row = self.store._db.execute('select rel, backend from edges').fetchone()
        self.assertEqual(tuple(row), ('重复', ''))
    def test_invalid_distribution_and_edited_events_are_skipped(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs={'因为': .9})
        self.store.add_edge(2, 1, '改变', .7, 'jev', probs=AXIS)
        status, p = self.route('preview', {'edges': {'min_conf_change': .9}})
        self.assertEqual((status, p['skipped'], p['removed']['改变']), (200, 1, 0))
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL)
        self.store.update_event(1, text='edited')
        status, p = self.route('preview', {'edges': {}})
        self.assertEqual((status, p['skipped']), (200, 1))
    def test_changed_parameters_busy_and_reused_tokens_rejected(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL)
        self.assertEqual(self.route('apply', {'edges': {}, 'token': 'made-up'})[0], 409)
        _, p = self.route('preview', {'edges': {}})
        self.assertEqual(self.route('apply', {'edges': {'margin': .3}, 'token': p['token']})[0], 409)
        self.d._pass_lock.acquire()
        try:
            self.assertEqual(self.route('apply', {'edges': {}, 'token': p['token']})[0], 409)
        finally:
            self.d._pass_lock.release()
        self.assertEqual(self.route('apply', {'edges': {}, 'token': p['token']})[0], 200)
        self.assertEqual(self.route('apply', {'edges': {}, 'token': p['token']})[0], 409)
    def test_existing_jev_old_distribution_is_not_attributed_to_newer_evaluation(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL)
        older = {'改变': .8, '否定': .05, '重复': .05, '都不是': .1}
        self.store.add_edge(2, 1, '改变', .8, 'jev', probs=older, backend='known')
        _, p = self.route('preview', {'edges': {}})
        self.assertEqual(p['skipped'], 1)
        self.assertEqual(p['added']['因为'], 0)
    def test_apply_rolls_back_all_edges_on_insert_failure(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL)
        self.store.add_edge(2, 1, '因为', .6, 'jev', probs=DETAIL)
        _, p = self.route('preview', {'edges': {'min_conf_because': .7}})
        self.store._db.execute("CREATE TRIGGER reject_recalibration BEFORE INSERT ON edges BEGIN SELECT RAISE(ABORT, 'reject'); END")
        self.store._db.commit()
        with self.assertRaises(Exception):
            self.route('apply', {'edges': {'min_conf_because': .7}, 'token': p['token']})
        rows = self.store._db.execute('select rel from edges').fetchall()
        self.assertEqual([r[0] for r in rows], ['因为'])
    def test_all_relation_thresholds_and_repeat_suppression(self):
        from eventmem_core.nightly import _accepted
        cases = [('改变', 'min_conf_change', ('重复','改变','否定')),
                 ('否定', 'min_conf_negate', ('重复','改变','否定')),
                 ('重复', 'min_conf_repeat', ('重复','改变','否定')),
                 ('因为', 'min_conf_because', ('因为','细化')),
                 ('细化', 'min_conf_refine', ('因为','细化'))]
        for rel, key, labels in cases:
            probs = {label: (.7 if label == rel else .1) for label in labels}
            answer = {'probabilities': probs, 'none': .1}
            policy = apply(CoreSettings(), {'edges': {key: .71}}, strict=True).edges
            self.assertIsNone(_accepted(answer, labels, policy.margin, policy), rel)
        repeat = {'重复': .7, '改变': .1, '否定': .1, '都不是': .1}
        self.store.add_pair_eval(2, 1, axis_probs=repeat, detail_probs=DETAIL)
        _, p = self.route('preview', {'edges': {'min_conf_repeat': .8}})
        self.assertEqual(p['added']['重复'], 0)
        self.assertEqual(p['added']['因为'], 1)
    def test_missing_previously_accepted_edge_is_not_recreated(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL,
                                 axis_label='改变', detail_label='因为')
        self.store.add_edge(2, 1, '因为', .6, 'jev', probs=DETAIL)
        _, p = self.route('preview', {'edges': {}})
        self.assertEqual(p['skipped'], 1)
        self.assertEqual(p['added']['改变'], 0)
    def test_recalibrated_labels_allow_a_second_adjustment(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL,
                                 axis_label='改变', detail_label='因为')
        self.store.add_edge(2, 1, '改变', .7, 'jev', probs=AXIS)
        self.store.add_edge(2, 1, '因为', .6, 'jev', probs=DETAIL)
        _, p = self.route('preview', {'edges': {'min_conf_change': .8}})
        self.assertEqual(self.route('apply', {'edges': {'min_conf_change': .8}, 'token': p['token']})[0], 200)
        _, p = self.route('preview', {'edges': {}})
        self.assertEqual((p['skipped'], p['added']['改变']), (0, 1))
    def test_preview_apply_and_staleness(self):
        self.store.add_pair_eval(2, 1, axis_probs=AXIS, detail_probs=DETAIL)
        status, preview = self.route('preview', {'edges': {}})
        self.assertEqual(status, 200)
        self.assertEqual(preview['added']['改变'], 1)
        self.assertEqual(preview['added']['因为'], 1)
        self.assertEqual(preview['skipped'], 0)
        self.store.add_edge(2, 1, '改变', .4, 'human')
        status, _ = self.route('apply', {'edges': {}, 'token': preview['token']})
        self.assertEqual(status, 409)
        status, fresh = self.route('preview', {'edges': {}})
        self.assertEqual(fresh['skipped'], 1)
        self.assertEqual(fresh['added']['改变'], 0)
        status, result = self.route('apply', {'edges': {}, 'token': fresh['token']})
        self.assertEqual(status, 200)
        rows = self.store._db.execute('select rel, decided_by from edges').fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [('改变','human')])

if __name__ == '__main__':
    unittest.main()
