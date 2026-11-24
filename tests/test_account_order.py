import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from official_pool import OfficialPool
from live_pool import RealPool
from dispatch_limit import DispatchBusy
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
            self.assertEqual(pool.priority_order(sorted(KEYS)), order)
            # Eligible keys missing from the saved order join at the end.
            partial = [KEYS[2], KEYS[1]]
            self.assertEqual(pool.priority_order(partial), [KEYS[2], KEYS[1]])

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

    def test_priority_follows_visible_order_without_saved_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = bare_pool(tmp)
            eligible = sorted(KEYS, reverse=True)
            self.assertEqual(pool.priority_order(eligible), eligible)
            self.assertEqual(pool.saved_account_order(), [])

    def test_shared_order_can_include_an_account_outside_gemini(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = bare_pool(directory, configured=KEYS[:3])
            order = list(reversed(KEYS))
            self.assertEqual(pool.save_account_order(order, visible_accounts=KEYS), order)
            self.assertEqual(pool.priority_order(KEYS[:3]), list(reversed(KEYS[:3])))

    @unittest.skipUnless(os.name == 'posix', 'Workbench account pool requires Linux')
    def test_flash_chat_and_material_jobs_share_the_saved_order(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = RealPool(directory, directory)
            try:
                accounts = dict.fromkeys(KEYS, True)
                official = pool.official
                quotas = {}
                official.account_quotas = lambda _: quotas
                official.runtime = SimpleNamespace(
                    settings=lambda: {'accounts': accounts}, inventory=lambda: accounts,
                    text_models=lambda: {},
                )
                official.save_account_order([KEYS[2], KEYS[0], KEYS[3], KEYS[1]])
                items = [{'key': 'flow-account-' + key} for key in KEYS]

                first, _ = official.generate('auto', 'fixture', str(uuid.uuid4()), items)
                self.assertEqual(first['account_key'], KEYS[2])
                another, _ = official.generate('auto', 'second fixture', str(uuid.uuid4()), items)
                self.assertEqual(another['account_key'], KEYS[2])
                job = official.claim()
                official.finish(job, {'status': 'completed', 'response': 'fixture reply', 'native': str(uuid.uuid4())})
                quotas[KEYS[2]] = {'status':'ok','checked_at':time.time(),'five_hour':{'remaining_percent':0}}
                with self.assertRaises(DispatchBusy):
                    official.generate('auto','follow up',str(uuid.uuid4()),items,conversation_id=first['conversation_id'])
                rerouted = official.claim()
                self.assertEqual(rerouted['request_id'], another['request_id'])
                self.assertEqual(rerouted['account_key'], KEYS[0])
                official.set_concurrency(1)
                with pool.connection() as db:
                    db.execute("INSERT INTO auto_material_jobs(asset_id,status,added,updated) VALUES(?,'waiting',1,1)", ('a' * 64,))

                self.assertIsNone(claim_material(pool, auto=True))
                official.finish(rerouted, {'status': 'completed', 'response': 'fixture reply', 'native': str(uuid.uuid4())})
                material = claim_material(pool, auto=True)
                self.assertIsNotNone(material)
                self.assertEqual(material['account'], KEYS[0])
                third, _ = official.generate('auto', 'fixture', str(uuid.uuid4()), items)
                self.assertEqual(third['account_key'], KEYS[0])
                self.assertIsNone(official.claim())
                quotas.clear()
                follow_up, _ = official.generate('auto','follow up',str(uuid.uuid4()),items,conversation_id=first['conversation_id'])
                self.assertEqual(follow_up['account_key'], KEYS[2])
            finally:
                pool.close()


if __name__ == '__main__':
    unittest.main()
