#!/usr/bin/env python3
"""图谱的批量编辑：项目不是一个标签。

一次批量改项目必须同时改到四处，否则「看着改了、连边却按旧项目走」：
  * ``events.project_id``（图谱与召回读到的主项目）
  * ``event_projects`` 的 primary 行（夜间选候选读的是成员关系）
  * ``events.vec``（向量 payload 是 ``[项目] 类型 正文``）
  * ``edges.cross``（跨项目标记跟着项目走，关系本身不动）
最后还要留下 ``reseat_since_ts``：下一轮连边按新项目重选候选，判过的对不重问。
"""
import asyncio
import importlib.util
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from eventmem_core.embed import FakeEncoder  # noqa: E402
from eventmem_core.store import Store  # noqa: E402

try:
    import fastapi  # noqa: F401
except ImportError:  # pragma: no cover - the API needs FastAPI
    graph = None
else:
    spec = importlib.util.spec_from_file_location(
        'graph_api_batch', ROOT / 'adapters/hermes/eventmem-graph/dashboard/plugin_api.py'
    )
    graph = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(graph)


def as_bytes(vec):
    return struct.pack('<%df' % len(vec), *vec)


def call(body):
    return asyncio.run(graph.batch_events(body))


@unittest.skipIf(graph is None, 'FastAPI is required for the graph API')
class BatchEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'graph.db'
        self.store = Store(str(self.path))
        self.encoder = FakeEncoder(dim=8)
        self.store.add_project('eventmem')
        self.store.add_project('小主机')
        self.a = self.store.add_event('决定', '把系统盘换成新的 NVMe',
                                      project='eventmem', ts=1000.0,
                                      vec=as_bytes(self.encoder.encode('a')))
        self.b = self.store.add_event('结果', '克隆之后启动正常',
                                      project='eventmem', ts=2000.0,
                                      vec=as_bytes(self.encoder.encode('b')))
        self.c = self.store.add_event('变更', '换到小主机上跑',
                                      project='小主机', ts=3000.0,
                                      vec=as_bytes(self.encoder.encode('c')))
        # 有向：src 是后来的事件。
        self.store.add_edge(self.b, self.a, '细化', 0.8, 'jev', cross=0)
        self.store.add_edge(self.c, self.b, '改变', 0.9, 'jev', cross=1)
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

    # -- helpers ---------------------------------------------------------
    def project_of(self, eid):
        row = self.store._db.execute(
            'SELECT p.name AS name FROM events e LEFT JOIN projects p ON p.id = e.project_id '
            'WHERE e.id = ?', (eid,)).fetchone()
        return row['name'] if row else None

    def primaries(self, eid):
        return [r['project_id'] for r in self.store._db.execute(
            'SELECT project_id FROM event_projects WHERE event_id = ? AND is_primary = 1', (eid,))]

    def cross_of(self, src, dst):
        row = self.store._db.execute(
            'SELECT cross FROM edges WHERE src = ? AND dst = ?', (src, dst)).fetchone()
        return None if row is None else int(row['cross'])

    def edge_count(self):
        return int(self.store._db.execute('SELECT count(*) FROM edges').fetchone()[0])

    # -- cases -----------------------------------------------------------
    def test_project_change_moves_membership_vector_and_cross(self):
        before_edges = self.edge_count()
        res = call({'ids': [self.a], 'project': '小主机'})
        self.assertTrue(res['ok'])
        self.assertEqual(res['updated'], 1)
        self.assertEqual(res['vector'], {'updated': 1, 'skipped': 0, 'note': ''})
        self.assertEqual(res['reseat'], 1000.0 - 1.0, 'reseat is just before the oldest touched event')

        self.assertEqual(self.project_of(self.a), '小主机')
        pid = self.store._db.execute("SELECT id FROM projects WHERE name = '小主机'").fetchone()['id']
        self.assertEqual(self.primaries(self.a), [pid], 'the primary membership row moved with the column')

        vec = self.store._db.execute('SELECT vec FROM events WHERE id = ?', (self.a,)).fetchone()['vec']
        self.assertEqual(len(vec), len(self.encoder.encode('[小主机] 决定 把系统盘换成新的 NVMe')) * 4,
                         'the vector was re-encoded from the new payload')

        self.assertEqual(self.cross_of(self.b, self.a), 1, 'b(eventmem) → a(小主机) is now cross-project')
        self.assertEqual(self.cross_of(self.c, self.b), 1, 'unrelated edges keep their mark')
        self.assertEqual(self.edge_count(), before_edges, 'no edge is ever deleted by a batch edit')

        self.assertEqual(self.store.get_meta('reseat_since_ts'), '%.3f' % (1000.0 - 1.0))

    def test_unfiled_clears_the_primary_row(self):
        res = call({'ids': [self.c], 'project': None})
        self.assertEqual(res['updated'], 1)
        self.assertIsNone(self.project_of(self.c))
        self.assertEqual(self.primaries(self.c), [])
        self.assertEqual(self.cross_of(self.c, self.b), 0, 'no project on one end means not cross-project')

    def test_kind_only_keeps_the_project_rows(self):
        before = self.primaries(self.b)
        res = call({'ids': [self.b], 'kind': '教训'})
        self.assertEqual(res['updated'], 1)
        row = self.store._db.execute('SELECT kind FROM events WHERE id = ?', (self.b,)).fetchone()
        self.assertEqual(row['kind'], '教训')
        self.assertEqual(self.primaries(self.b), before)
        self.assertEqual(self.project_of(self.b), 'eventmem')

    def test_reseat_never_moves_forward(self):
        call({'ids': [self.a], 'kind': '结果'})
        first = float(self.store.get_meta('reseat_since_ts'))
        self.assertEqual(first, 1000.0 - 1.0)
        call({'ids': [self.c], 'kind': '结果'})   # 更晚的一条：不能把标记往后推
        self.assertEqual(float(self.store.get_meta('reseat_since_ts')), first)
        call({'ids': [self.a], 'kind': '结果'})   # 同样早：保持
        self.assertEqual(float(self.store.get_meta('reseat_since_ts')), first)

    def test_edited_ts_is_stamped(self):
        before = self.store._db.execute('SELECT edited_ts FROM events WHERE id = ?', (self.b,)).fetchone()[0]
        self.assertIsNone(before, 'a fresh event has never been edited')
        call({'ids': [self.b], 'kind': '结果'})
        after = self.store._db.execute('SELECT edited_ts FROM events WHERE id = ?', (self.b,)).fetchone()[0]
        self.assertTrue(after, 'a batch edit is a human edit: the rainbow ring must show')

    def test_rejects_bad_input(self):
        for body in ({'ids': []}, {'ids': 'x'}, {'ids': [1.5]}, {'ids': [self.a]}, {'ids': [True, 2]}):
            with self.assertRaises(fastapi.HTTPException) as caught:
                call(body)
            self.assertEqual(caught.exception.status_code, 400, body)
        with self.assertRaises(fastapi.HTTPException) as caught:
            call({'ids': [self.a], 'project': '不存在的项目'})
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(fastapi.HTTPException) as caught:
            call({'ids': [self.a], 'kind': '瞎写'})
        self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(fastapi.HTTPException) as caught:
            call({'ids': [999999], 'kind': '结果'})
        self.assertEqual(caught.exception.status_code, 404)

    def test_missing_ids_are_reported_not_fatal(self):
        res = call({'ids': [self.a, 999999], 'kind': '结果'})
        self.assertEqual(res['updated'], 1)
        self.assertEqual(res['missing'], [999999])


if __name__ == '__main__':
    unittest.main(verbosity=2)
