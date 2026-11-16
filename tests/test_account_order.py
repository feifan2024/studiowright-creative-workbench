import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from official_pool import OfficialPool

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


if __name__ == '__main__':
    unittest.main()
