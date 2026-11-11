"""Exercise the packaged API implementation without a provider connection."""
import sys
import shutil
from contextlib import contextmanager
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench'))
from live_pool import RealPool


@contextmanager
def fixture_directory():
    parent = Path(__file__).resolve().parent
    root = parent / ('.fixture-' + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        assert root.resolve().parent == parent and root.name.startswith('.fixture-')
        shutil.rmtree(root)


class ImageCreditCompatibility(unittest.TestCase):
    def test_optional_credit_field_and_duplicate_submission(self):
        with fixture_directory() as directory:
            root = Path(directory)
            pool = RealPool(root / 'state', root, execute=lambda *args: None)
            project = str(uuid.uuid4())
            snapshot = {
                'projects': [{'key': 'project-key', 'source_profile': 'account-1', 'source_id': project}],
                'real_accounts': {'items': [{'profile': 'account-1', 'flow_login': 'verified'}]},
                'flow_models': {'image': [{'id': 'nano-banana-2.1'}]},
            }
            try:
                for credits in ('omitted', None, 0):
                    with self.subTest(credits=credits):
                        params = dict(prompt='Local fixture only', model='nano-banana-2.1',
                                      aspect='16:9', count=1, resolution='360p', duration=4)
                        if credits != 'omitted':
                            params['max_credits'] = credits
                        data = dict(project_key='project-key', kind='image', confirm=True, params=params)
                        calls = []
                        def send(body):
                            calls.append(body)
                            return dict(id='fixture-task', profile='account-1', project=project, kind='image')
                        rid = str(uuid.uuid4())
                        first, created = pool.submit_flow(data, rid, snapshot, send)
                        again, repeated = pool.submit_flow(data, rid, snapshot, send)
                        self.assertTrue(created)
                        self.assertFalse(repeated)
                        self.assertEqual(first['task_id'], again['task_id'])
                        self.assertEqual(first['status'], 'submitted')
                        self.assertEqual(len(calls), 1)
                        self.assertNotIn('max_credits', calls[0]['params'])
            finally:
                pool.close()


class VideoAutoDispatchCompatibility(unittest.TestCase):
    def test_original_video_format_and_idempotency_without_provider(self):
        with fixture_directory() as directory:
            (directory / 'state').mkdir()
            pool = RealPool(directory / 'state', directory, execute=lambda *args: None)
            project = str(uuid.uuid4())
            chosen = dict(key='project-key', source_profile='account-1', source_id=project)
            pool._auto_flow_project = lambda *args: chosen
            snapshot = {'flow_models': {'video': [{'id':'omni-flash'}]}}
            payload = dict(kind='video', confirm=True, params=dict(prompt='Local fixture only',
                model='omni-flash', aspect='16:9', count=1, resolution='720p', duration=10))
            calls=[]
            def send(body):
                calls.append(body)
                return dict(id='fixture-video', profile='account-1', project=project, kind='video')
            try:
                rid=str(uuid.uuid4())
                first,created=pool.submit_flow(payload,rid,snapshot,send)
                repeated,created_again=pool.submit_flow(payload,rid,snapshot,send)
                self.assertTrue(created)
                self.assertFalse(created_again)
                self.assertEqual(first['task_id'],repeated['task_id'])
                self.assertEqual(first['status'],'submitted')
                self.assertEqual(len(calls),1)
                self.assertEqual(calls[0]['params']['duration'],10)
                self.assertEqual(calls[0]['params']['model'],'omni-flash')
                self.assertEqual(calls[0]['request_key'],rid)
            finally:
                pool.close()
