from contextlib import closing
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from official_pool import OfficialPool
from live_pool import RealPool
from material_dispatch import claim as claim_material

KEYS = [f'{i:064x}' for i in range(1, 5)]


def bare_pool(tmp, configured=None):
    pool = OfficialPool.__new__(OfficialPool)
    pool.pool = SimpleNamespace(state_dir=Path(tmp))
    accounts = {key: {} for key in (configured if configured is not None else KEYS)}
    pool.runtime = SimpleNamespace(settings=lambda: {'accounts': accounts})
    return pool


class AccountOrderTest(unittest.TestCase):
    def test_save_roundtrip_and_circle_follows_saved_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = bare_pool(tmp)
            order = [KEYS[2], KEYS[0], KEYS[3], KEYS[1]]
            self.assertEqual(pool.save_account_order(list(order)), order)
            saved = json.loads((Path(tmp) / 'account-order.json').read_text(encoding='utf-8'))
            self.assertEqual(saved, {'schema_version': 1, 'order': order})
            self.assertEqual(pool.saved_account_order(), order)
            self.assertEqual(pool.circle_order(sorted(KEYS)), order)
            # Eligible keys missing from the saved order join at the end.
            partial = [KEYS[2], KEYS[1]]
            self.assertEqual(pool.circle_order(partial), [KEYS[2], KEYS[1]])

    def test_save_rejects_duplicates_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = bare_pool(tmp)
            for bad in ([KEYS[0], KEYS[0]], ['not-hex'], [KEYS[0], f'{9:064x}'], [], 'nope'):
                with self.assertRaises(ValueError):
                    pool.save_account_order(bad)
            # Nothing persisted after the failed attempts.
            self.assertEqual(pool.saved_account_order(), [])

    def test_save_requires_permutation_of_configured_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = bare_pool(tmp, configured=KEYS[:3])
            with self.assertRaises(ValueError):
                pool.save_account_order(KEYS)  # one key too many
            pool = bare_pool(tmp, configured=KEYS)
            with self.assertRaises(ValueError):
                pool.save_account_order(KEYS[:3])  # one key missing

    def test_circle_falls_back_to_sorted_without_saved_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = bare_pool(tmp)
            eligible = sorted(KEYS, reverse=True)
            self.assertEqual(pool.circle_order(eligible), eligible)
            self.assertEqual(pool.saved_account_order(), [])

    def test_rotation_continues_after_the_last_account_becomes_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(':memory:')) as db:
            pool = bare_pool(directory)
            pool.save_account_order([KEYS[2], KEYS[0], KEYS[3], KEYS[1]])
            db.executescript("""
                CREATE TABLE official_pool_cursor(id INTEGER PRIMARY KEY,last_account TEXT);
                CREATE TABLE official_advanced_pool_cursor(id INTEGER PRIMARY KEY,last_account TEXT);
            """)

            self.assertEqual(pool.rotate_account(db, KEYS, advanced=False), KEYS[2])
            self.assertEqual(pool.rotate_account(db, KEYS, advanced=False), KEYS[0])
            self.assertEqual(pool.rotate_account(db, [KEYS[1], KEYS[3]], advanced=True), KEYS[3])
            still_available = [KEYS[1], KEYS[2], KEYS[3]]
            self.assertEqual(pool.rotate_account(db, still_available, advanced=False), KEYS[3])
            self.assertEqual(pool.rotate_account(db, KEYS, advanced=False), KEYS[1])
            self.assertEqual(pool.rotate_account(db, [KEYS[1], KEYS[3]], advanced=True), KEYS[1])

    @unittest.skipUnless(os.name == 'posix', 'Workbench account pool requires Linux')
    def test_flash_chat_and_material_jobs_share_the_saved_order(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = RealPool(directory, directory)
            try:
                accounts = dict.fromkeys(KEYS, True)
                official = pool.official
                official.runtime = SimpleNamespace(
                    settings=lambda: {'accounts': accounts}, inventory=lambda: accounts,
                    text_models=lambda: {},
                )
                official.save_account_order([KEYS[2], KEYS[0], KEYS[3], KEYS[1]])
                items = [{'key': 'flow-account-' + key} for key in KEYS]

                first, _ = official.generate('auto', 'fixture', str(uuid.uuid4()), items)
                self.assertEqual(first['account_key'], KEYS[2])
                with pool.connection() as db:
                    db.execute("UPDATE requests SET status='completed' WHERE request_id=?", (first['request_id'],))
                    db.execute("INSERT INTO auto_material_jobs(asset_id,status,added,updated) VALUES(?,'waiting',1,1)", ('a' * 64,))

                material = claim_material(pool, auto=True)
                self.assertIsNotNone(material)
                self.assertEqual(material['account'], KEYS[0])
                third, _ = official.generate('auto', 'fixture', str(uuid.uuid4()), items)
                self.assertEqual(third['account_key'], KEYS[3])
            finally:
                pool.close()


if __name__ == '__main__':
    unittest.main()
