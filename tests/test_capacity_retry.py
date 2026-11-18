import sys, sqlite3, tempfile, threading, unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace as S
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from official_pool import OfficialPool

class CapacityRetries(unittest.TestCase):
    def test_bound_account_attempts_and_guards(self):
        for scenario in ('success','exhaust','partial','unknown','never','maintenance'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as d:
                path=Path(d)/'db'
                with sqlite3.connect(path) as db:
                    db.executescript("CREATE TABLE requests(request_id TEXT,status TEXT,account_key TEXT,note TEXT);CREATE TABLE official_jobs(request_id TEXT,no_retry INTEGER);CREATE TABLE official_attempts(request_id TEXT,ordinal INTEGER,status TEXT,reason TEXT,submitted INTEGER,started REAL,finished REAL,total_ms REAL,response TEXT);INSERT INTO requests VALUES('r','dispatching','a',NULL);INSERT INTO official_jobs VALUES('r',0);INSERT INTO official_attempts VALUES('r',1,'running',NULL,1,0,NULL,NULL,NULL);")
                def connect():return closing(sqlite3.connect(path,isolation_level=None))
                p=OfficialPool.__new__(OfficialPool);p.pool=S(connection=connect,maintenance=lambda:scenario=='maintenance');p.runtime=S(inventory=lambda:{'a':True});p.reason=lambda *a:None;p.stop=threading.Event()
                job=dict(request_id='r',account_key='a',no_retry=scenario=='never')
                bad=dict(status='failed',reason='provider_unavailable',capacity_retry_safe=True,submitted=True,response=None)
                if scenario=='partial':bad['response']='partial'
                if scenario=='unknown':bad['status']='unknown'
                fn=Mock(side_effect=lambda n:dict(status='completed') if scenario=='success' else bad)
                with patch('official_pool.time.monotonic',side_effect=[0,100,100,200]*10):
                    p.capacity_retries(job,bad,fn)
                expected=1 if scenario=='success' else 2 if scenario=='exhaust' else 0
                self.assertEqual(fn.call_count,expected)
                with sqlite3.connect(path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM official_attempts').fetchone()[0],1+expected)
                    self.assertEqual(db.execute('SELECT account_key FROM requests').fetchone()[0],'a')

    @unittest.skipUnless(sys.platform=='linux','Linux pipe transport')
    @patch('official_runtime.official_network_env',return_value={})
    def test_real_cli_parser_only_flags_explicit_zero_reply_capacity(self,_):
        import json
        import test_gemini_stability as stability
        for partial,error in [(False,'UNAVAILABLE (503): No capacity available for model test on the server'),(True,'UNAVAILABLE (503): No capacity available for model test on the server'),(False,'503 network unavailable')]:
            with self.subTest(partial=partial,error=error),tempfile.TemporaryDirectory() as d:
                events=[]
                if partial:events.append(dict(event='step_update',step_update=dict(step_type='agent_response',text_delta='partial')))
                events.append(dict(event='result',result=dict(status='ERROR',finished_reason='error',error=error,response=None)))
                body='import json,sys\npackets=json.loads('+repr(json.dumps(events))+')\nfor p in packets:print(json.dumps(p),flush=True)\nsys.exit(1)\n'
                runtime,root=stability.LocalTransport().setup_runtime(d,body)
                r=runtime.run('a','test',None,root/'audit')
                self.assertEqual(r['capacity_retry_safe'],not partial and 'No capacity available' in error)

    @unittest.skipUnless(sys.platform=='linux','Linux pipe transport')
    @patch('material_vision.official_network_env',return_value={})
    def test_image_capacity_rejection_and_partial_reply(self,_):
        import material_vision
        import test_gemini_stability as stability
        for partial in (False,True):
            with self.subTest(partial=partial),tempfile.TemporaryDirectory() as d:
                body="""import json,sys
for line in sys.stdin:
 q=json.loads(line);i=q['id']
 if i==1:r={'agentCapabilities':{'promptCapabilities':{'image':True}}}
 elif i==2:r={'sessionId':'test','models':{'currentModelId':'gemini-3.8-flash-medium'}}
 elif i==4:
  if PARTIAL:print(json.dumps({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'partial'}}}}),flush=True)
  print(json.dumps({'id':i,'error':{'code':503,'message':'UNAVAILABLE: No capacity available for model test on the server'}}),flush=True);continue
 else:r={}
 print(json.dumps({'id':i,'result':r}),flush=True)
""".replace('PARTIAL',repr(partial))
                runtime,root=stability.LocalTransport().setup_runtime(d,body)
                result=material_vision.analyze(runtime,'a',[],root/'receipt.json',keep_partial=True)
                self.assertEqual(result.get('capacity_retry_safe',False),not partial)
                self.assertEqual(result['status'],'unknown' if partial else 'failed')
