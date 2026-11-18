import sys
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from music_jobs import MusicJobs

class MusicRecovery(unittest.TestCase):
    def test_reconnect_and_duplicate_submission_do_not_generate_twice(self):
        with tempfile.TemporaryDirectory() as d:
            started=threading.Event();release=threading.Event();finished=threading.Event()
            def generate(*args):started.set();release.wait(5);return {'clips':[{'title':'test'}]}
            gen=Mock(side_effect=generate)
            jobs=MusicJobs(d,gen,lambda clips:{'ingested':[{'ok':True}]})
            original=jobs.save
            def save(*args,**kwargs):
                original(*args,**kwargs)
                if args[1]=='completed':finished.set()
            jobs.save=save
            ident=str(uuid.uuid4())
            try:
                jobs.submit(ident,'test','lyria-fast');self.assertTrue(started.wait(2))
                for _ in range(5):
                    self.assertEqual(jobs.get(ident)['status'],'generating')
                    self.assertEqual(jobs.submit(ident,'test','lyria-fast')['id'],ident)
                with self.assertRaises(ValueError):jobs.submit(ident,'different','lyria-fast')
            finally:release.set()
            self.assertTrue(finished.wait(3));gen.assert_called_once()
            recovered=MusicJobs(d,Mock(),Mock())
            self.assertEqual(recovered.latest()['result']['ingested'],1)
            recovered.generate.assert_not_called()

    def test_restart_keeps_uncertain_job_without_replaying(self):
        with tempfile.TemporaryDirectory() as d:
            jobs=MusicJobs(d,Mock(),Mock());ident=str(uuid.uuid4())
            jobs.save(ident,'generating')
            with jobs.db() as db:db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL,NULL,1,1)',(ident,'digest','test','lyria-fast','generating'))
            resumed=MusicJobs(d,Mock(),Mock())
            self.assertEqual(resumed.get(ident)['status'],'unknown')
            resumed.generate.assert_not_called()
