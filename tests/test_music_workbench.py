import ast
from pathlib import Path
import time
import unittest
from unittest.mock import Mock

SOURCE=Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench/server.py'

def functions():
    tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('music_generate','music_service_status')]
    scope={'time':time,'MUSIC_MODELS':{'lyria-fast'},'music_credentials':lambda:('user','password','test-key'),
           'music_admin_token':lambda:'test-admin','music_call':Mock()}
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(SOURCE),'exec'),scope)
    return scope

class MusicWorkbench(unittest.TestCase):
    def test_invalid_input_is_rejected_before_provider_call(self):
        s=functions()
        for prompt,model in [(None,None),(42,None),([],None),('test',{}),('test',[]),('test','bad'),(' '*2,None),('x'*2001,None)]:
            with self.subTest(prompt=type(prompt).__name__,model=model):
                with self.assertRaises(ValueError):s['music_generate'](prompt,model)
        s['music_call'].assert_not_called()

    def test_failed_status_is_unavailable(self):
        s=functions();s['music_call'].side_effect=OSError('offline')
        result=s['music_service_status']()
        self.assertFalse(result['available'])
        self.assertEqual(result['accounts'],[])

    def test_completed_clip_keeps_job_and_download_urls(self):
        s=functions();s['music_call'].return_value={'data':[{'id':'clip','title':'Test','audio':{'url':'https://example.test/a'},'wav':{'url':'https://example.test/w'}}],'flowmusic':{'job_id':'job'}}
        result=s['music_generate'](' test ','lyria-fast')
        self.assertEqual(result['clips'][0]['job_id'],'job')
        self.assertEqual(result['clips'][0]['clip_id'],'clip')
        self.assertEqual(result['clips'][0]['prompt'],'test')
        self.assertEqual(result['clips'][0]['wav_url'],'https://example.test/w')
        s['music_call'].assert_called_once()
