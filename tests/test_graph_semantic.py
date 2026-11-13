#!/usr/bin/env python3
"""The graph's semantic filter: embedding ranking over events.vec, plus honest degradation.

The encoder is faked (deterministic hashing) so this runs without ONNX; the interesting
contract is the ranking, the top cap, the dimension guard and the /graph vector census.
"""
import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from eventmem_core.embed import FakeEncoder
from eventmem_core.store import Store

try:
    import fastapi  # noqa: F401
    import numpy  # noqa: F401  - the endpoint scores with numpy; without it it answers 503
except ImportError:
    graph = None
else:
    spec = importlib.util.spec_from_file_location(
        'graph_api', ROOT / 'adapters/hermes/eventmem-graph/dashboard/plugin_api.py'
    )
    graph = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(graph)


def as_bytes(vec):
    if hasattr(vec, 'tobytes'):
        return vec.tobytes()
    return struct.pack('<%df' % len(vec), *vec)


def vec_for(encoder, text):
    vec = encoder.encode(text)
    if vec is None:
        raise AssertionError('fake encoder refused %r' % text)
    return as_bytes(vec)


@unittest.skipIf(graph is None, 'FastAPI + numpy are required for the semantic filter')
class GraphSemanticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'graph.db'
        self.store = Store(str(self.path))
        self.encoder = FakeEncoder(dim=64)
        self.store.add_project('eventmem')
        texts = {
            'disk': '把系统盘换成了新的 NVMe 盘，克隆后启动正常',
            'train': '训练 embedding 模型时把批次调小避免显存爆表',
            'graph': '图谱里按关系筛选边，按类型筛选原子',
        }
        self.ids = {}
        for key, text in texts.items():
            self.ids[key] = self.store.add_event('决定', text, project='eventmem',
                                                 vec=vec_for(self.encoder, text))
        self.no_vec = self.store.add_event('教训', '这条还没有向量')
        self.store._db.commit()

        self.orig_db = graph._db_path
        self.orig_encoder = graph._encoder
        graph._db_path = lambda: self.path
        graph._encoder = lambda: self.encoder

    def tearDown(self):
        graph._db_path = self.orig_db
        graph._encoder = self.orig_encoder
        self.store.close()
        self.tmp.cleanup()

    def test_ranks_the_matching_event_first(self):
        res = graph.semantic({'q': '系统盘 克隆 启动', 'top': 3})
        self.assertTrue(res['ok'])
        self.assertEqual(res['dim'], 64)
        self.assertEqual(res['scanned'], 3, 'only rows with a vector are scanned')
        self.assertEqual(res['skipped'], 0)
        self.assertEqual(res['hits'][0]['id'], self.ids['disk'])
        scores = [h['score'] for h in res['hits']]
        self.assertEqual(scores, sorted(scores, reverse=True), 'hits are sorted by score')

    def test_top_caps_the_answer(self):
        res = graph.semantic({'q': '训练 显存', 'top': 1})
        self.assertEqual(len(res['hits']), 1)
        self.assertEqual(res['hits'][0]['id'], self.ids['train'])

    def test_min_score_drops_weak_hits(self):
        full = graph.semantic({'q': '完全无关的天气预报', 'top': 8})
        if not full['hits']:
            self.skipTest('fake encoder produced no scores')
        cut = full['hits'][0]['score'] + 0.001
        res = graph.semantic({'q': '完全无关的天气预报', 'top': 8, 'min_score': cut})
        self.assertEqual(res['hits'], [])

    def test_empty_query_is_rejected(self):
        with self.assertRaises(fastapi.HTTPException) as caught:
            graph.semantic({'q': '   '})
        self.assertEqual(caught.exception.status_code, 400)

    def test_unavailable_encoder_is_reported_not_silently_empty(self):
        class Down:
            def available(self):
                return False

            def reason(self):
                return 'model not found under /nowhere'

        graph._encoder = lambda: Down()
        with self.assertRaises(fastapi.HTTPException) as caught:
            graph.semantic({'q': '系统盘', 'top': 4})
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn('model not found', caught.exception.detail)

    def test_dimension_mismatch_is_skipped_not_scored_zero(self):
        self.store.add_event('变更', '旧编码器留下的向量', project='eventmem',
                             vec=struct.pack('<4f', 1.0, 0.0, 0.0, 0.0))
        self.store._db.commit()
        res = graph.semantic({'q': '旧的向量', 'top': 8})
        self.assertEqual(res['scanned'], 4)
        self.assertEqual(res['skipped'], 1)
        for hit in res['hits']:
            self.assertNotEqual(hit['id'], self.no_vec + 1, 'the 4-dim vector is not ranked')

    def test_graph_reports_the_vector_census(self):
        payload = graph._read_graph()
        self.assertEqual(payload['vectors'], {'events': 3, 'dim': 64})

    def test_route_is_mounted_under_the_plugin_prefix(self):
        """The desktop reaches it as POST /api/plugins/eventmem-graph/semantic."""
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest('httpx is not installed')
        app = FastAPI()
        app.include_router(graph.router, prefix='/api/plugins/eventmem-graph')
        client = TestClient(app)
        res = client.post('/api/plugins/eventmem-graph/semantic', json={'q': '系统盘 克隆', 'top': 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['hits'][0]['id'], self.ids['disk'])
        bad = client.post('/api/plugins/eventmem-graph/semantic', json={'q': ''})
        self.assertEqual(bad.status_code, 400)


if __name__ == '__main__':
    unittest.main()
