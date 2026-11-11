import ast
import re
import unittest
from pathlib import Path
base=Path(__file__).parents[1]/'packaging/ubuntu/snapshot/flow/console'
def load_function(file,name):
    source=(base/file).read_text(encoding='utf-8')
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name==name)
    scope={'Path':Path,'re':re}
    exec(compile(ast.Module(body=[node],type_ignores=[]),file,'exec'),scope)
    return scope[name]
class PreparationCompatibility(unittest.TestCase):
    def test_profile_path_normalization_and_isolation(self):
        matches=load_function('adapter.py','browser_profile_matches')
        target=base/'data/profiles/account-a'
        alternate=base/'bin/../data/profiles/account-a'
        self.assertTrue(matches(['chrome','--user-data-dir='+str(alternate)],target))
        self.assertFalse(matches(['chrome','--user-data-dir='+str(base/'data/profiles/account-b')],target))
        self.assertFalse(matches(['chrome'],target))
    def test_deprecation_subtitle_keeps_exact_model(self):
        pattern=load_function('browser_rendering.py','offered_model_pattern')('Omni 1.1 Flash')
        self.assertIsNotNone(pattern.search('volume_off Omni 1.1 Flash\n将于12月5日停用'))
        self.assertIsNotNone(pattern.search('Omni 1.1 Flash'))
        self.assertIsNone(pattern.search('Omni 1.2 Flash'))
        self.assertIsNone(pattern.search('Omni 1.1 Flash Extended'))

    def test_public_failure_retains_confirmed_submission_boundary(self):
        source=(base/'server.py').read_text(encoding='utf-8')
        cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and any(isinstance(m,ast.FunctionDef) and m.name=='task_view' for m in n.body))
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='task_view')
        scope={'re':re}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'server.py','exec'),scope)
        from types import SimpleNamespace
        retry={'ordinal':0,'state':'needs_review','attempts':[{'id':'original','attempt_ordinal':0,'status':'failed'}]}
        owner=SimpleNamespace(store=SimpleNamespace(retry_detail=lambda _:retry))
        row=dict(id='original',profile='a',project='p',status='failed',error='preparation error',finished=1,
                 result={'submission':{'status':'not_submitted','stage':'preparing'}})
        out,_=scope['task_view'](owner,row)
        self.assertEqual(out['status'],'failed')
        self.assertEqual(out['submission']['status'],'not_submitted')
        row['result']={}
        out,_=scope['task_view'](owner,row)
        self.assertEqual(out['status'],'interrupted')
