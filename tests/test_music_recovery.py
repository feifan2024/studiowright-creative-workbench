import sys,sqlite3,tempfile,json,unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from music_recovery import recover
from music_jobs import MusicJobs

class Recovery(unittest.TestCase):
    def test_match_requires_unique_request_time_prompt_and_model(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'upstream.db'
            body={'job_id':'provider-job','clips':[{'id':'clip','audio':{'url':'https://storage.googleapis.com/test'}}]}
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE request_logs(id INTEGER,operation TEXT,request_body TEXT,response_body TEXT,status_code INTEGER,created_at TEXT)')
                values=(1,'music.generate',json.dumps({'prompt':'test','model':'lyria-fast'}),json.dumps(body),200,'1970-01-01T00:16:40Z')
                db.execute('INSERT INTO request_logs VALUES(?,?,?,?,?,?)',values)
            row={'created':1000,'prompt':'test','model':'lyria-fast'}
            self.assertEqual(recover(row,path)['result']['clips'][0]['job_id'],'provider-job')
            self.assertIsNone(recover(dict(row,prompt='other'),path))
            self.assertIsNone(recover(dict(row,created=2000),path))
            with sqlite3.connect(path) as db:db.execute('INSERT INTO request_logs VALUES(?,?,?,?,?,?)',values)
            self.assertIsNone(recover(row,path))

    def test_restart_recovers_result_and_ingests_without_generation(self):
        with tempfile.TemporaryDirectory() as d:
            generate=Mock(side_effect=AssertionError('must not generate'))
            ingest=Mock(return_value={'ingested':[{'ok':True}]})
            jobs=MusicJobs(d,generate,ingest)
            with jobs.db() as db:db.execute("INSERT INTO jobs VALUES('id','hash','test','lyria-fast','generating',NULL,NULL,1000,1000)")
            restarted=MusicJobs(d,generate,ingest)
            restarted.recover=Mock(return_value={'status':'recovering'})
            restarted.reconcile();self.assertEqual(restarted.get('id')['status'],'recovering')
            restarted.recover.return_value={'status':'completed','result':{'clips':[{'clip_id':'clip'}]}}
            restarted.reconcile();restarted.reconcile()
            self.assertEqual(restarted.get('id')['status'],'completed')
            generate.assert_not_called();ingest.assert_called_once()
