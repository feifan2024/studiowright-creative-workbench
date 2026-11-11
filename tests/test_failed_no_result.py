import ast
from pathlib import Path
import re
import types
import unittest
from unittest.mock import Mock


class FailedNoResultDisplay(unittest.TestCase):
    def test_bounded_no_image_lookup_is_failed_without_enabling_replay(self):
        path=Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/flow/console/server.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Console')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='task_view')
        namespace={'re':re}
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(path),'exec'),namespace)
        store=Mock()
        store.retry_detail.return_value=dict(root_id='original',ordinal=0,state='needs_review',
            attempts=[dict(id='original',ordinal=0,status='failed',finished=1)])
        target=types.SimpleNamespace(store=store)
        for error,expected in [('no image reply within 240 s, and no new image is in the project','failed'),
                               ('connection lost after submission','interrupted')]:
            row=dict(id='original',profile='fixture',project='project',status='failed',result={},finished=1,error=error)
            result,retry=namespace['task_view'](target,row)
            self.assertEqual(result['status'],expected)
            self.assertEqual(result['error'],error)
            self.assertEqual(retry['state'],'needs_review')
            store.execution.assert_not_called()
