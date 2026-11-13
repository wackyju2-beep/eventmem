#!/usr/bin/env python3
"""HTTP integration tests for historical backfill, isolated temporary databases."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from eventmem_core import daemon as daemon_mod
from eventmem_core.store import Store
from eventmem_core.settings import CoreSettings, DecisionPolicy


class BackfillAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'events.db')
        with Store(self.path) as store:
            for n in range(3):
                store.add_event('尝试', str(n), ts=n + 1, session='s')
        self.d = daemon_mod.Daemon(CoreSettings(db_path=self.path, decision=replace(DecisionPolicy(), url='')))
        self.server = daemon_mod.make_server(self.d, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if getattr(self.d, '_backfill_thread', None):
            self.d._backfill_thread.join(5)

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request('http://127.0.0.1:%s/api/%s' % (self.server.server_address[1], path), data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_preview_http_estimate_and_token(self):
        status, body = self.request('backfill/preview', {'edges': {'session_recent': 2, 'margin': .8, 'poll_seconds': 12}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['event_count'], 3)
        self.assertEqual(body['observed_pairs'], 3)
        self.assertIsNone(body['exact_total'])
        self.assertTrue(body['token'])

    def test_background_stop_resume_and_exclusive_recalibration(self):
        entered, release = threading.Event(), threading.Event()
        class Client:
            active_name = 'fake'
            def __init__(self): self.calls = 0
            def available(self): return True
            def decide_choice(self, state, questions):
                self.calls += 1
                entered.set()
                if not release.wait(5): raise RuntimeError('test timed out')
                return [{'probabilities': {'重复': 0, '改变': .9, '否定': 0}, 'none': .1},
                        {'probabilities': {'因为': 0, '细化': 0.0}, 'none': 1.0}]
        client = Client()
        original = daemon_mod.W.from_settings
        daemon_mod.W.from_settings = lambda *a, **k: client
        self.addCleanup(lambda: setattr(daemon_mod.W, 'from_settings', original))
        self.addCleanup(release.set)
        edges = {'session_recent': 3}
        _, preview = self.request('backfill/preview', {'edges': edges})
        status, started = self.request('backfill/start', {'edges': edges, 'token': preview['token']})
        self.assertEqual(status, 202, started)
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.request('run-once', {})[0], 409)
        self.assertEqual(self.request('recalibrate/preview', {'edges': {}})[0], 409)
        status, live = self.request('status')
        self.assertTrue(live['busy'])
        self.assertEqual(live['backfill']['state'], 'running')
        self.assertEqual(self.request('stop', {})[1]['stopping'], True)
        release.set()
        self.d._backfill_thread.join(5)
        job = self.request('status')[1]['backfill']
        self.assertEqual((job['state'], job['result']['status'], job['result']['pairs']), ('done', 'stopped', 1))
        with Store(self.path) as store:
            self.assertEqual(store._db.execute('select count(*) from pair_evals').fetchone()[0], 1)
            self.assertIsNone(store.get_meta('nightly_since_ts'))
        _, again = self.request('backfill/preview', {'edges': edges})
        self.assertEqual(again['observed_pairs'], 2)
        self.assertEqual(self.request('backfill/start', {'edges': edges, 'token': again['token']})[0], 202)
        self.d._backfill_thread.join(5)
        self.assertEqual(self.request('status')[1]['backfill']['result']['status'], 'completed')
        self.assertEqual(client.calls, 3)

    def test_reject_stale_and_changed_policy(self):
        _, p = self.request('backfill/preview', {'edges': {'session_recent': 3}})
        self.assertEqual(self.request('backfill/start', {'edges': {'session_recent': 2}, 'token': p['token']})[0], 409)
        with Store(self.path) as store:
            store.add_event('尝试', 'new', ts=4, session='s')
        self.assertEqual(self.request('backfill/start', {'edges': {'session_recent': 3}, 'token': p['token']})[0], 409)

    def test_unavailable_backend_reported_in_status(self):
        _, p = self.request('backfill/preview', {'edges': {}})
        self.assertEqual(self.request('backfill/start', {'edges': {}, 'token': p['token']})[0], 202)
        self.d._backfill_thread.join(5)
        self.assertEqual(self.request('status')[1]['backfill']['result']['status'], 'jev-unavailable')

    def test_legacy_backend_start_script_is_ensured(self):
        self.d.settings = replace(self.d.settings, decision=replace(self.d.settings.decision,
                                  url='http://example.invalid', start_script='fake-script'))
        calls = []
        original = daemon_mod.W.ensure_service
        daemon_mod.W.ensure_service = lambda script, url: calls.append((script, url)) or False
        self.addCleanup(lambda: setattr(daemon_mod.W, 'ensure_service', original))
        _, p = self.request('backfill/preview', {'edges': {}})
        self.assertEqual(self.request('backfill/start', {'edges': {}, 'token': p['token']})[0], 202)
        self.d._backfill_thread.join(5)
        self.assertEqual(calls, [('fake-script', 'http://example.invalid')])
        self.assertEqual(self.request('status')[1]['backfill']['result']['status'], 'jev-unavailable')

    def test_invalid_candidate_overrides_rejected(self):
        for edges in ({'poll_seconds': True}, {'session_recent': -1}, {'session_recent': .5},
                      {'heads_cos_min': 2}, {'cross_top_k': '3'}, {'max_fail_streak': 1},
                      {'margin': 1.1}, {'heads_cos_min': float('nan')}):
            with self.subTest(edges=edges):
                self.assertEqual(self.request('backfill/preview', {'edges': edges})[0], 400)

    def test_cross_kinds_is_supported_and_validated(self):
        self.assertEqual(self.request('backfill/preview', {'edges': {'cross_kinds': ['决定']}})[0], 200)
        self.assertEqual(self.request('backfill/preview', {'edges': {'cross_kinds': ['invalid']}})[0], 400)

    def test_live_progress_updates_after_committed_pair(self):
        entered, release = threading.Event(), threading.Event()
        class Client:
            active_name = 'fake'
            def __init__(self): self.calls = 0
            def available(self): return True
            def decide_choice(self, state, questions):
                self.calls += 1
                if self.calls == 2:
                    entered.set()
                    if not release.wait(5): raise RuntimeError('test timed out')
                return [{'probabilities': {'重复': 0, '改变': .9, '否定': 0}, 'none': .1},
                        {'probabilities': {'因为': 0, '细化': 0.0}, 'none': 1.0}]
        original = daemon_mod.W.from_settings
        daemon_mod.W.from_settings = lambda *a, **k: Client()
        self.addCleanup(lambda: setattr(daemon_mod.W, 'from_settings', original))
        self.addCleanup(release.set)
        _, p = self.request('backfill/preview', {'edges': {}})
        self.assertEqual(self.request('backfill/start', {'edges': {}, 'token': p['token']})[0], 202)
        self.assertTrue(entered.wait(5))
        live = self.request('status')[1]
        self.assertEqual(live['backfill']['pairs'], 1)
        self.assertEqual(live['live']['pairs'], 1)
        self.assertEqual(live['live']['backend'], 'fake')
        release.set()

    def test_configured_confidence_used_not_unsaved_ui_threshold(self):
        class Client:
            active_name = 'fake'
            def available(self): return True
            def decide_choice(self, state, questions):
                return [{'probabilities': {'重复': 0, '改变': .9, '否定': 0}, 'none': .1},
                        {'probabilities': {'因为': 0, '细化': 0.0}, 'none': 1.0}]
        self.d.settings = replace(self.d.settings, edges=replace(self.d.settings.edges, min_conf_change=.95))
        original = daemon_mod.W.from_settings
        daemon_mod.W.from_settings = lambda *a, **k: Client()
        self.addCleanup(lambda: setattr(daemon_mod.W, 'from_settings', original))
        edges = {'min_conf_change': 0, 'margin': 0}
        _, p = self.request('backfill/preview', {'edges': edges})
        self.assertEqual(self.request('backfill/start', {'edges': edges, 'token': p['token']})[0], 202)
        self.d._backfill_thread.join(5)
        self.assertEqual(self.request('status')[1]['backfill']['result']['status'], 'completed')
        with Store(self.path) as store:
            self.assertEqual(store._db.execute('select count(*) from edges').fetchone()[0], 0)
            self.assertEqual(store._db.execute('select count(*) from pair_evals').fetchone()[0], 3)

if __name__ == '__main__':
    unittest.main(verbosity=2)
