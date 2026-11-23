import base64
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


SNAPSHOT = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot'
sys.path[:0] = [str(SNAPSHOT / 'flow/console'), str(SNAPSHOT / 'workbench')]
from console_store import Store
from flow_warning_archive import read_page_png, view_screenshot_once
from login_readiness import resume_google_session
from page_activity_guard import PageActivityWarning, stop_on_page_warning
from preflight import check_profile, dispatch_check


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMuoAAAAASUVORK5CYII='
)


class LocalState(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root / 'console-data')
        self.store.add_account('account-a', 'A')
        self.store.add_account('account-b', 'B')

    def test_account_locale_policy_passes_expected_values_and_rejects_missing_policy(self):
        profile = self.root / 'data/profiles/account-a/Default'
        profile.mkdir(parents=True)
        (profile / 'Preferences').write_text('{"intl":{"accept_languages":"en-US,en"}}', encoding='utf-8')
        self.assertEqual(dispatch_check(self.root, 'account-a'), (False, ['dispatch_locale_missing']))
        path = self.root / 'console-data/dispatch-locales.json'
        path.write_text(json.dumps({'account-a': {'languages': ['zh-CN'], 'timezone': 'Asia/Shanghai'}}),
                        encoding='utf-8')
        with patch('preflight.mapped_ports', return_value={'account-a': 3101}), \
                patch('preflight._system_timezone', return_value='Etc/UTC'), \
                patch('preflight.check_profile', side_effect=lambda *args, **kwargs:
                      check_profile(*args, proc_root=self.root, **kwargs)):
            ok, reasons = dispatch_check(self.root, 'account-a')
            path.write_text(json.dumps({'account-a': {'languages': ['en-US', 'en'], 'timezone': 'Etc/UTC'}}),
                            encoding='utf-8')
            self.assertEqual(dispatch_check(self.root, 'account-a'), (True, []))
        self.assertFalse(ok)
        self.assertIn('profile_languages_mismatch', reasons)
        self.assertIn('system_timezone_mismatch', reasons)
        self.assertEqual(dispatch_check(self.root, 'account-b'), (False, ['dispatch_locale_missing']))

    @staticmethod
    def warning_page(*, text='Our systems have detected unusual traffic from your computer network.',
                     url='https://accounts.google.com/sorry'):
        region = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                                 inner_text=AsyncMock(return_value=text))
        return SimpleNamespace(url=url, goto=AsyncMock(), close=AsyncMock(),
                               screenshot=AsyncMock(return_value=PNG),
                               locator=Mock(return_value=SimpleNamespace(
                                   count=AsyncMock(return_value=1), nth=Mock(return_value=region))))

    async def test_login_saves_original_before_close_and_holds_only_the_affected_account(self):
        page = self.warning_page()

        async def capture(**_):
            self.assertEqual(self.store.page_incidents(), {})
            page.close.assert_not_awaited()
            return PNG

        async def close():
            hold = self.store.page_incidents()['account-a']
            self.assertEqual(read_page_png(self.root, hold['id']), PNG)

        page.screenshot.side_effect = capture
        page.close.side_effect = close
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        with self.assertRaises(PageActivityWarning):
            await resume_google_session(context, 'account-a@example.test', None,
                check_warning=lambda current: stop_on_page_warning(current, self.root, 'account-a', 'login'))
        page.close.assert_awaited_once()
        holds = self.store.page_incidents()
        self.assertEqual(set(holds), {'account-a'})
        self.assertEqual(holds['account-a']['phase'], 'login')
        self.assertFalse(self.store.review_page_incident('account-b', holds['account-a']['id']))
        self.assertTrue(self.store.review_page_incident('account-a', holds['account-a']['id']))
        self.assertEqual(self.store.page_incidents(), {})
        self.assertIsNone(self.store.rows('SELECT screenshot FROM flow_page_incidents')[0]['screenshot'])

    def test_destroy_page_evidence_preserves_stop_and_prevents_capture_from_reappearing(self):
        self.store.record_page_incident('account-a', 'login', PNG)
        self.store.record_page_incident('account-b', 'browse', PNG)
        ident = self.store.page_incidents()['account-a']['id']
        self.assertEqual(read_page_png(self.root, ident), PNG)
        def open_original():
            try:
                return view_screenshot_once(self.root, ident)
            except FileNotFoundError:
                return None

        with ThreadPoolExecutor(max_workers=2) as clients:
            first = clients.submit(open_original)
            second = clients.submit(open_original)
            self.assertCountEqual([first.result(), second.result()], [PNG, None])
        self.store.record_page_incident('account-a', 'browse', PNG)
        holds = self.store.page_incidents()
        self.assertEqual(holds['account-a']['id'], ident)
        self.assertEqual(holds['account-a']['screenshot_destroyed'], 1)
        self.assertEqual(holds['account-a']['has_screenshot'], 0)
        with self.assertRaises(FileNotFoundError):
            read_page_png(self.root, ident)
        self.assertEqual(read_page_png(self.root, holds['account-b']['id']), PNG)

    def test_workbench_previews_without_burning_and_serves_the_original_once(self):
        spec = spec_from_file_location('workbench_screenshot_test', SNAPSHOT / 'workbench/server.py')
        workbench = module_from_spec(spec)
        spec.loader.exec_module(workbench)
        self.store.record_page_incident('account-a', 'login', PNG)
        ident = self.store.page_incidents()['account-a']['id']
        handler = workbench.make_handler(Mock(), deployment='ubuntu')
        with ThreadingHTTPServer(('127.0.0.1', 0), handler) as service:
            worker = threading.Thread(target=service.serve_forever, daemon=True)
            worker.start()
            try:
                with patch('flow_warning_archive.FLOW_ROOT', self.root), \
                        closing(HTTPConnection('127.0.0.1', service.server_port, timeout=4)) as client:
                    def request(method, path, body=None, headers=None):
                        client.request(method, path, body=body, headers=headers or {})
                        response = client.getresponse()
                        return response.status, response.read()

                    image = f'/api/flow-warning-screenshots/{ident}.png'
                    view = f'/api/flow-warning-screenshots/{ident}/view'
                    headers = {'Origin': f'http://127.0.0.1:{service.server_port}',
                               'Content-Type': 'application/x-www-form-urlencoded'}
                    self.assertEqual(request('GET', image), (200, PNG))
                    self.assertEqual(request('GET', view)[0], 404)
                    self.assertEqual(request('POST', view, 'view=original')[0], 403)
                    self.assertEqual(request('POST', view, 'view=preview!', headers)[0], 400)
                    self.assertEqual(request('GET', image), (200, PNG))
                    self.assertEqual(request('POST', view, 'view=original', headers), (200, PNG))
                    self.assertEqual(request('GET', image)[0], 404)
                    self.assertEqual(request('POST', view, 'view=original', headers)[0], 404)
            finally:
                service.shutdown()
                worker.join()
        self.assertEqual(self.store.page_incidents()['account-a']['id'], ident)

    async def test_capture_failure_still_blocks_and_an_unrelated_page_does_not(self):
        page = self.warning_page(text='检测到异常活动。', url='https://flow.google.com/project/example')
        page.screenshot.side_effect = OSError('unavailable')
        with self.assertRaises(PageActivityWarning):
            await stop_on_page_warning(page, self.root, 'account-a', 'browse')
        incident = self.store.page_incidents()['account-a']
        self.assertEqual(incident['has_screenshot'], 0)
        with self.assertRaises(FileNotFoundError):
            read_page_png(self.root, incident['id'])
        unrelated = self.warning_page(url='https://example.test/')
        await stop_on_page_warning(unrelated, self.root, 'account-b', 'browse')
        unrelated.locator.assert_not_called()

    async def test_execute_returns_claim_to_queue_before_touching_adapter(self):
        # Load explicitly since both Ubuntu components have a server.py.
        spec = spec_from_file_location('flow_console_dispatch_test', SNAPSHOT / 'flow/console/server.py')
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        console = module.Console.__new__(module.Console)
        console.root, console.store, console.test = self.root, self.store, False
        console.adapter = Mock()
        console.busy, console.generation_busy, console.generation_projects = {'account-a'}, {}, set()
        console.desktop_keyboard_ready = Mock(return_value=True)
        database = self.root / 'data/studiowright.db'
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript('CREATE TABLE pacing(profile TEXT,cooldown_until REAL);'
                             'CREATE TABLE flow_risk(profile TEXT,paused INTEGER,consecutive INTEGER);')
        task = self.store.create_task('account-a', 'fixed-project', 'image', {}, 'dispatch-fixture')
        claim = self.store.claim(set(), {'account-a'})
        self.assertEqual(claim['id'], task['id'])
        await console.execute(claim)
        self.assertEqual(self.store.task(task['id'])['status'], 'queued')
        self.assertEqual(console.busy, set())
        self.assertEqual(console.adapter.mock_calls, [])
