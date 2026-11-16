import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path

WORKBENCH = Path(__file__).resolve().parents[1] / 'packaging' / 'ubuntu' / 'snapshot' / 'workbench'


def load_quota_module():
    # Keep the module import hermetic: the real official_runtime only matters
    # at runtime, so a stub satisfies the import without spawning anything.
    module_type = sys.modules.setdefault('official_runtime', types.ModuleType('official_runtime'))
    if not hasattr(module_type, 'official_network_env'):
        module_type.official_network_env = lambda **kwargs: {}
    spec = importlib.util.spec_from_file_location('official_quota_under_test', WORKBENCH / 'official_quota.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bare_reader(rows):
    module = load_quota_module()
    reader = module.QuotaReader.__new__(module.QuotaReader)  # skip __init__: no daemon thread
    reader.lock = threading.Lock()
    reader.running = False
    reader.attempted = 0
    reader.pending_recovery = ()
    reader.rows = rows
    return reader


class AutoRefreshTickTest(unittest.TestCase):
    def test_tick_reads_only_readable_accounts_and_resets_running(self):
        reader = bare_reader({'a': {'status': 'ok'}, 'b': {'status': 'verification_required'},
                              'c': {'status': 'not_read'}, 'd': {'status': 'network_error'}})
        read = []
        reader.check = lambda key: read.append(key) or {'status': 'ok', 'checked_at': 0}

        self.assertEqual(reader._auto_refresh_tick(), 2)
        self.assertEqual(read, ['a', 'c'])
        self.assertFalse(reader.running)

    def test_tick_skips_while_a_refresh_is_already_running(self):
        reader = bare_reader({'a': {'status': 'ok'}})
        reader.running = True
        read = []
        reader.check = lambda key: read.append(key) or {'status': 'ok'}

        self.assertEqual(reader._auto_refresh_tick(), 0)
        self.assertEqual(read, [])
        self.assertTrue(reader.running)

    def test_tick_does_nothing_without_known_accounts(self):
        reader = bare_reader({})
        read = []
        reader.check = lambda key: read.append(key) or {'status': 'ok'}

        self.assertEqual(reader._auto_refresh_tick(), 0)
        self.assertEqual(read, [])
