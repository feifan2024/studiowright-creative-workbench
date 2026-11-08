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
