import json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from gemini_effort import resolve,DEFAULT
from official_pool import OfficialPool
import test_gemini_stability as fixtures

class Effort(unittest.TestCase):
    def test_supported_and_fallback(self):
        for level in ('low','medium','high'):
            self.assertEqual(resolve(effort=level),'gemini-3.8-flash-'+level)
        for invalid in (None,'','max','HIGH',False,123,{},[]):
            self.assertEqual(resolve(effort=invalid),DEFAULT)

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
