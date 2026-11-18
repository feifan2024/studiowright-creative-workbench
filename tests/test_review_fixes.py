import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from official_pool import OfficialPool, DEFAULT_MAX_CONCURRENCY
from official_runtime import BAD_REPLY
from test_official_runtime_token import make_runtime, KEY, EMAIL
import gemini_image_input as vision
import material_vision


@unittest.skipUnless(os.name == 'posix', 'Linux credential storage')
class TokenRaces(unittest.TestCase):
    def test_partial_backup_never_replaces_valid_backup(self):
        with tempfile.TemporaryDirectory() as d:
            runtime, _, cli, original = make_runtime(Path(d))
            self.assertTrue(runtime.account(KEY))
            token = cli/'antigravity-oauth-token'
            token.write_text('{"id_token_')
            runtime._backup_token(KEY, token)
            self.assertEqual(runtime._token_backup(KEY).read_text(), original)
            self.assertIsNone(runtime.account(KEY))
            self.assertEqual(token.read_text(), '{"id_token_')

    def test_restore_never_overwrites_new_token(self):
        with tempfile.TemporaryDirectory() as d:
            runtime, _, cli, original = make_runtime(Path(d))
            self.assertTrue(runtime.account(KEY))
            token = cli/'antigravity-oauth-token'
            token.unlink()
            real_link = os.link
            def concurrent_refresh(src, dst):
                token.write_text('new authorization written by CLI')
                return real_link(src, dst)
            with patch('official_runtime.os.link', side_effect=concurrent_refresh):
                self.assertFalse(runtime._restore_token(KEY, token, {'email':EMAIL}))
            self.assertEqual(token.read_text(), 'new authorization written by CLI')
            self.assertFalse(list(cli.glob('.token-*.tmp')))

    def test_restore_uses_the_same_validated_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            runtime, _, cli, original = make_runtime(Path(d))
            self.assertTrue(runtime.account(KEY))
            token = cli/'antigravity-oauth-token';token.unlink()
            read = Path.read_bytes
            calls = []
            def changing_backup(path):
                if path == runtime._token_backup(KEY):
                    calls.append(1)
                    return original.encode() if len(calls)==1 else b'corrupt'
                return read(path)
            with patch.object(Path, 'read_bytes', changing_backup):
                self.assertTrue(runtime._restore_token(KEY, token, {'email':EMAIL}))
            self.assertEqual(token.read_text(), original)
            self.assertEqual(len(calls), 1)


class ReadinessAndImages(unittest.TestCase):
    def test_workers_start_without_authorization_and_readiness_updates(self):
        pool=OfficialPool.__new__(OfficialPool)
        available={'a':False}
        pool.runtime=S(inventory=lambda:available)
        pool.threads=[]
        self.assertFalse(pool.ready)
        with patch('official_pool.threading.Thread') as thread:
            pool.start()
            self.assertEqual(thread.call_count, DEFAULT_MAX_CONCURRENCY+2)
        available['a']=True
        self.assertTrue(pool.ready)

    def test_capacity_retry_refusal_is_failure(self):
        pool=OfficialPool.__new__(OfficialPool)
        pool.runtime=object();pool.pool=S(state_dir=Path('/unused'))
        pool.capacity_retries=lambda job,result,call:call(2)
        pool.reserve_retry=Mock(side_effect=AssertionError('No fourth attempt'))
        job=dict(request_id='fixture',account_key='a',prompt='describe',attachments='[]')
        for response, expected in [('作为一个语言模型，我无法提供这方面的帮助。','failed'),('红色矩形','completed')]:
            results=[dict(status='failed',capacity_retry_safe=True),dict(status='completed',response=response)]
            with patch.object(vision,'normalized_attachments',return_value=[]), patch.object(vision,'files_for',return_value=([],[])), patch.object(vision,'prompt_parts',return_value=[]), patch.object(material_vision,'analyze',side_effect=results):
                result=pool.run_image(job)
                self.assertEqual(result['status'],expected)
                if expected=='failed':self.assertEqual(result['reason'],BAD_REPLY)
