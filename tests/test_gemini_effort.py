import json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from gemini_effort import resolve,DEFAULT,NEON
from official_pool import OfficialPool
import test_gemini_stability as fixtures

class Effort(unittest.TestCase):
    def test_supported_and_fallback(self):
        for level in ('low','medium','high'):
            self.assertEqual(resolve(effort=level),'gemini-3.8-flash-'+level)
        for invalid in (None,'','max','HIGH',False,123,{},[]):
            self.assertEqual(resolve(effort=invalid),DEFAULT)

    def test_neon_requires_explicit_supported_account_and_is_text_only(self):
        pool=OfficialPool.__new__(OfficialPool)
        pool.pool=S(conversation_id=Mock(),allowed=Mock())
        pool.runtime=S(inventory=lambda:{'ready':True},neon_text_accounts=lambda:{'enabled'})
        self.assertEqual(resolve(NEON,'high'),NEON)
        with self.assertRaises(ValueError):
            resolve(NEON,'low')
        for account,probe in (('auto',False),('other',False),('enabled',True)):
            with self.assertRaisesRegex(ValueError,'手动选择'):
                pool.generate(account,'test','request',[],model=NEON,readiness_probe=probe)

    def test_capacity_retry_keeps_level(self):
        p=OfficialPool.__new__(OfficialPool);p.pool=S(state_dir=Path('/unused'))
        p.runtime=S(run=Mock(return_value=dict(status='failed',capacity_retry_safe=True)))
        p.capacity_retries=lambda job,result,call:call(2)
        p.run_text(dict(request_id='x',account_key='a',prompt='test',native_before=None,model='gemini-3.8-flash-high'))
        self.assertEqual(p.runtime.run.call_count,2)
        for c in p.runtime.run.call_args_list:self.assertEqual(c.kwargs['model'],'gemini-3.8-flash-high')

    @unittest.skipUnless(os.name=='posix','CLI pipes require Linux')
    @patch('official_runtime.official_network_env',return_value={})
    def test_cli_receipt_and_selection(self,_):
        body="""import sys,json
model=sys.argv[sys.argv.index('--model')+1]
print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':'11111111-1111-4111-8111-111111111111','response':model}}),flush=True)
"""
        for level in ('low','medium','high'):
            with tempfile.TemporaryDirectory() as d:
                runtime,root=fixtures.LocalTransport().setup_runtime(d,body)
                model=resolve(effort=level);r=runtime.run('a','fixture',None,root/'audit',model=model)
                self.assertEqual(r['response'],model)
                self.assertEqual(json.loads((root/'audit/submission.json').read_text())['model'],model)

    @unittest.skipUnless(os.name=='posix','CLI pipes require Linux')
    @patch('official_runtime.official_network_env',return_value={})
    def test_neon_uses_ssh_auth_context_and_never_launches_for_other_accounts(self,_):
        body="""import os, sys, json
assert os.environ['SSH_TTY'] and sys.argv[sys.argv.index('--model')+1]=='gemini-4-neon-high'
print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':'11111111-1111-4111-8111-111111111111','response':'neon ok'}}),flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            runtime,root=fixtures.LocalTransport().setup_runtime(directory,body)
            account=runtime.account('fixture')
            blocked=runtime.run('fixture','test',None,root/'blocked',model=NEON)
            self.assertEqual(blocked['reason'],'model_account_unavailable')
            self.assertFalse((root/'blocked').exists())
            runtime.account=lambda _:{**account,'neon_text_enabled':True}
            result=runtime.run('fixture','test',None,root/'allowed',model=NEON)
            self.assertEqual(result['response'],'neon ok')
