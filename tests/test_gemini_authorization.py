import sys
import io
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from gemini_authorization import GeminiAuthorization
from official_reauth import close_authorization_tab
from official_pool import OfficialPool


class GeminiAuthorizationTest(unittest.TestCase):
    def test_unknown_business_request_only_allows_independent_probe(self):
        pool = OfficialPool.__new__(OfficialPool)
        with sqlite3.connect(':memory:') as db:
            db.executescript("""
                CREATE TABLE requests(request_id TEXT,account_key TEXT,status TEXT,finished REAL);
                CREATE TABLE official_jobs(request_id TEXT,reason TEXT,readiness_probe INTEGER);
                CREATE TABLE official_account_blocks(account_key TEXT,reason TEXT,created REAL);
                CREATE TABLE account_unknown_release(request_id TEXT,released REAL,reason TEXT);
                CREATE TABLE account_preferences(account_key TEXT,gemini_enabled INTEGER);
                CREATE TABLE official_reauth(request_id TEXT,account_key TEXT,state TEXT);
                CREATE TABLE conversation_turns(request_id TEXT,conversation_id TEXT);
                CREATE TABLE conversations(id TEXT,native_json TEXT);
                INSERT INTO requests VALUES('old','a','unknown',1);
                INSERT INTO official_jobs VALUES('old','timeout_unconfirmed',0);
                INSERT INTO official_account_blocks VALUES('a','timeout_unconfirmed',1);
            """)
            inventory = {'a': True}
            self.assertEqual(pool.reason(db, 'a', inventory), 'unresolved_request')
            block = pool.readiness_block(db, 'a')
            self.assertIsNone(pool.reason(db, 'a', inventory, block, readiness_probe=True))
            self.assertEqual(db.execute("SELECT status FROM requests").fetchone()[0], 'unknown')
            db.execute("UPDATE official_account_blocks SET reason='verification_required'")
            self.assertEqual(pool.reason(db, 'a', inventory, pool.readiness_block(db, 'a'),
                                         readiness_probe=True), 'official_call_blocked')

    def make_activation(self):
        return GeminiAuthorization(SimpleNamespace(pool=None, runtime=None))

    def test_only_safe_accounts_and_login_only_probes_are_selected(self):
        activation = self.make_activation()
        accounts = {k: {'root': str(Path.cwd() / ('fixture-' + k)), 'email': k + '@example.test'}
                    for k in ('missing', 'expired', 'unknown', 'business')}
        activation.runtime = SimpleNamespace(settings=lambda: {'accounts': accounts, 'binary': '/fixture/cli'})
        activation.reauth.dashboard = lambda: {'items': [
            {'account_key': 'expired', 'request_id': 'probe', 'mode': 'login_only', 'can_start': True},
            {'account_key': 'business', 'request_id': 'message', 'can_start': True},
        ]}
        activation.allowed = lambda key, **_: key != 'unknown'
        with patch.object(Path, 'exists', lambda p: 'fixture-missing' not in str(p) and 'fixture-unknown' not in str(p)):
            self.assertEqual([(k, rid) for k, _, _, rid in activation.candidates()],
                             [('missing', None), ('expired', 'probe')])

    def test_next_account_starts_only_after_previous_save(self):
        activation = self.make_activation()
        activation.candidates = lambda: [(key, {'email': key + '@example.test'}, '/fixture/cli', None)
                                         for key in ('first', 'second')]
        activation.allowed = lambda _, **kwargs: True
        seen = []
        def bootstrap(key, _, __):
            seen.append((key, activation.status()['completed']))
        activation.bootstrap = bootstrap
        activation.run()
        self.assertEqual(seen, [('first', 0), ('second', 1)])
        self.assertEqual(activation.status()['state'], 'completed')

    def test_wrong_account_or_closed_window_stops_sequence(self):
        activation = self.make_activation()
        activation.candidates = lambda: [(key, {'email': key + '@example.test'}, '/fixture/cli', None)
                                         for key in ('first', 'second')]
        activation.allowed = lambda _, **kwargs: True
        seen = []
        def bootstrap(key, _, __):
            seen.append(key)
            raise ValueError('login not completed')
        activation.bootstrap = bootstrap
        activation.run()
        self.assertEqual(seen, ['first'])
        self.assertEqual(activation.status()['state'], 'stopped')

    def test_only_recorded_google_tab_is_closed(self):
        binding = {'browser_pid': 42, 'port': 9300, 'profile': 'a', 'process_start': '1', 'page_target': 'FLOW'}
        registry = SimpleNamespace(TaskWindowRegistry=lambda: SimpleNamespace(get=lambda _: binding),
                                   process_identity=lambda *_: ('a', '1'))
        pages = [{'type': 'page', 'id': 'FLOW', 'url': 'https://flow.google.com/project/x'},
                 {'type': 'page', 'id': 'LOGIN', 'url': 'https://antigravity.google/oauth-callback'},
                 {'type': 'page', 'id': 'OTHER', 'url': 'https://accounts.google.com/'}]
        calls = []
        def open_url(url, timeout):
            calls.append(url)
            return io.BytesIO(json.dumps(pages).encode() if url.endswith('/json/list') else b'')
        with patch.dict(sys.modules, {'task_window_registry': registry}), \
                patch('urllib.request.build_opener', return_value=SimpleNamespace(open=open_url)), \
                patch('official_reauth.private_bytes', return_value=b'LOGIN'):
            self.assertTrue(close_authorization_tab(Path.cwd(), 'account-key'))
            self.assertEqual(calls[-1], 'http://127.0.0.1:9300/json/close/LOGIN')
            pages[1]['url'] = 'https://example.test/user-page'
            self.assertFalse(close_authorization_tab(Path.cwd(), 'account-key'))
            self.assertTrue(calls[-1].endswith('/json/list'))
