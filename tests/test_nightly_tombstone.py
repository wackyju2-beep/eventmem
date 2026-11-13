#!/usr/bin/env python3
"""Nightly must respect manual deletion and atomically record a pair."""
import tempfile
import unittest
from pathlib import Path
import test_nightly as fixtures
from eventmem_core.store import Store
from eventmem_core.nightly import build_edges

class TombstoneTests(unittest.TestCase):
    def run_mode(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'db')
            with Store(path) as primary, Store(path) as other:
                old = primary.add_event('变更', 'old', ts=fixtures.T, session='s')
                new = primary.add_event('变更', 'new', ts=fixtures.T+60, session='s')
                def erase(reverse=False):
                    src, dst = (old, new) if reverse else (new, old)
                    other.add_edge(src, dst, '改变', .9, 'human')
                    self.assertTrue(other.delete_edge(src, dst, '改变'))
                if mode.startswith('pre_'):
                    erase(mode.endswith('reverse'))
                if mode == 'ignore_edge':
                    primary._db.execute("CREATE TRIGGER ignore_detail BEFORE INSERT ON edges WHEN NEW.rel='因为' BEGIN SELECT RAISE(IGNORE); END")
                if mode == 'ignore_eval':
                    primary._db.execute('CREATE TRIGGER ignore_eval BEFORE INSERT ON pair_evals BEGIN SELECT RAISE(IGNORE); END')
                primary._db.commit()
                class Racing(fixtures.FakeJev):
                    def decide_choice(self, state, questions):
                        if mode.startswith('flight_'):
                            erase(mode.endswith('reverse'))
                        return super().decide_choice(state, questions)
                client = Racing({'change': .9, 'because': .8})
                result = build_edges(primary, client, fixtures.T-1)
                self.assertEqual(primary._db.execute('SELECT count(*) FROM pair_evals').fetchone()[0], 0)
                self.assertEqual(primary._db.execute('SELECT count(*) FROM edges').fetchone()[0], 0)
                self.assertEqual(result['edges'], 0)
                if mode.startswith('pre_'):
                    self.assertEqual(len(client.calls), 0)
                if mode.startswith('ignore_'):
                    self.assertEqual(result['skipped'], 'jev-error')

    def test_preexisting_tombstones(self):
        for mode in ('pre_same', 'pre_reverse'):
            with self.subTest(mode=mode): self.run_mode(mode)
    def test_tombstones_created_during_inference(self):
        for mode in ('flight_same', 'flight_reverse'):
            with self.subTest(mode=mode): self.run_mode(mode)
    def test_ignored_inserts_roll_back(self):
        for mode in ('ignore_edge', 'ignore_eval'):
            with self.subTest(mode=mode): self.run_mode(mode)

if __name__ == '__main__': unittest.main()
