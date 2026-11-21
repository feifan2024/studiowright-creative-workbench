import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1] / 'packaging'
sys.path[:0] = [str(ROOT / name) for name in ('windows', 'ubuntu/snapshot/workbench',
                                            'ubuntu/snapshot/flow/source/src')]
from flow_warning_archive import current_window_screenshots, read_png, records
from flow_warning_desktop import capture_name, sync_once

try:
    from studiowright.studio import Studio, FLOW_ROOT, SubmissionRejectedUnusual
except ImportError:
    Studio = None  # The console backend dependencies are installed on Ubuntu.


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMuoAAAAASUVORK5CYII='
)
JOB_ID = '123456789abc'


@unittest.skipIf(Studio is None, 'Flow console runtime unavailable')
class Capture(unittest.IsolatedAsyncioTestCase):
    async def test_failure_uses_the_active_project_page_and_preserves_retry_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {'project_id': 'project-a', 'params': '{"origin_console_task":"task-a","count":1}',
                   'result': '{"stage":"submitting","media_ids":[]}'}
            studio = Studio.__new__(Studio)
            studio.store = SimpleNamespace(job=Mock(return_value=job), update_job=Mock())
            page = SimpleNamespace(url=FLOW_ROOT + 'project/project-a', screenshot=AsyncMock(return_value=PNG))
            error = SubmissionRejectedUnusual('PUBLIC_ERROR_UNUSUAL_ACTIVITY')
            with patch('studiowright.studio.paths.incidents_dir', return_value=Path(directory)):
                await studio._fail(JOB_ID, error, FLOW_ROOT, page=page)
            self.assertEqual((Path(directory) / (JOB_ID + '.png')).read_bytes(), PNG)
            result = studio.store.update_job.call_args.kwargs['result']
            self.assertEqual(result['warning_screenshot'], JOB_ID + '.png')
            self.assertEqual(result['retry_evidence'], 'provider_rejected_unusual')

            page.url = FLOW_ROOT + 'project/project-b'
            page.screenshot.reset_mock()
            await studio._fail(JOB_ID, error, FLOW_ROOT, page=page)
            page.screenshot.assert_not_called()
            self.assertNotIn('warning_screenshot', studio.store.update_job.call_args.kwargs['result'])


class Delivery(unittest.TestCase):
    def test_only_new_recorded_screenshots_are_synced_once(self):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(':memory:')) as state:
            root = Path(directory)
            incidents = root / 'data/incidents'
            incidents.mkdir(parents=True)
            (root / 'console-data').mkdir()
            desktop = root / 'desktop'
            desktop.mkdir()
            with closing(sqlite3.connect(root / 'console-data/console.db')) as db, db:
                db.execute('CREATE TABLE accounts(profile TEXT, email TEXT)')
                db.execute("INSERT INTO accounts VALUES('profile-a','new@example.test')")
            with closing(sqlite3.connect(root / 'data/studiowright.db')) as db, db:
                db.execute('CREATE TABLE jobs(id TEXT, profile TEXT, kind TEXT, status TEXT, finished REAL, error TEXT, params TEXT, result TEXT)')
                for ident, marker in ((JOB_ID, JOB_ID + '.png'), ('0123456789ab', None)):
                    db.execute("INSERT INTO jobs VALUES(?,'profile-a','image','failed',1795249440,?,?,?)",
                               (ident, 'PUBLIC_ERROR_UNUSUAL_ACTIVITY', '{"origin_console_task":"task-a"}',
                                json.dumps({'warning_screenshot': marker})))
                    (incidents / (ident + '.png')).write_bytes(PNG)

            def read(action, ident=None):
                if action == 'list':
                    return json.dumps([row for row, _ in records(root)]).encode()
                return read_png(root, ident)

            windows = {'windows': [
                {'profile': 'profile-a', 'task_id': 'task-a'},
                {'profile': 'profile-b', 'task_id': 'task-a'},
            ]}
            self.assertEqual(current_window_screenshots(windows, root),
                             {'profile-a': {'id': JOB_ID, 'task_id': 'task-a'}})
            self.assertEqual(sync_once(read, desktop, state), (1, 0))
            saved = desktop / capture_name(next(records(root))[0])
            self.assertEqual(saved.read_bytes(), PNG)
            self.assertIn('new_异常活动_task-a', saved.name)
            saved.unlink()
            self.assertEqual(sync_once(read, desktop, state), (0, 0))
            self.assertEqual(list(desktop.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
