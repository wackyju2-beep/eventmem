#!/usr/bin/env python3
"""Manual graph edits persist evidence in a temporary database."""
import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from eventmem_core.store import Store
from eventmem_core.schema import ensure_schema, SCHEMA_VERSION

try:
    import fastapi  # noqa: F401
except ImportError:
    graph = None
else:
    spec = importlib.util.spec_from_file_location('graph_api', ROOT / 'adapters/hermes/eventmem-graph/dashboard/plugin_api.py')
    graph = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(graph)

@unittest.skipIf(graph is None, 'FastAPI is required for the graph adapter')
class GraphDeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.db'
        self.store = Store(str(self.path))
        self.store.add_event('决定', 'old')
        self.store.add_event('变更', 'new')
        self.original = graph._db_path
        graph._db_path = lambda: self.path
    def tearDown(self):
        graph._db_path = self.original
        self.store.close()
        self.tmp.cleanup()
    def test_delete_migrates_old_db_and_blocks_reinsert(self):
        self.store.add_edge(2, 1, '改变', .7, 'jev')
        self.store._db.execute('DROP TABLE edge_deletions')
        self.store._db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
        self.store._db.commit()
        result = asyncio.run(graph.delete_edge(2, 1, '改变'))
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(self.store._db.execute('SELECT rel FROM edge_deletions').fetchone()[0], '改变')
        ensure_schema(self.store._db)
        self.assertEqual(self.store._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
        self.store.add_edge(2, 1, '改变', .7, 'jev')
        self.assertIsNone(self.store._db.execute('SELECT 1 FROM edges').fetchone())
    def test_patch_replacement_leaves_evidence_after_human_edge_deleted(self):
        self.store.add_edge(2, 1, '改变', .7, 'jev')
        asyncio.run(graph.patch_edge({'src': 2, 'dst': 1, 'rel': '因为', 'old_rel': '改变'}))
        asyncio.run(graph.delete_edge(2, 1, '因为'))
        self.store.add_edge(2, 1, '改变', .7, 'jev')
        self.assertIsNone(self.store._db.execute('SELECT 1 FROM edges').fetchone())
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM edge_deletions').fetchone()[0], 2)

if __name__ == '__main__':
    unittest.main()
