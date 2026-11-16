import ast
import importlib.util
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest.mock import Mock


def import_nodes(label, imports, nodes):
    """Import extracted console source as a temporary module file.

    The console modules import Playwright and other Ubuntu-only dependencies at
    the top, so offline tests isolate single methods instead of importing the
    whole module."""
    safe = re.sub(r'[^A-Za-z0-9_]', '_', str(label))
    with tempfile.TemporaryDirectory() as tmp:
        module_file = Path(tmp) / (safe + '.py')
        module_file.write_text(imports + '\n\n' + '\n\n'.join(ast.unparse(node) for node in nodes), encoding='utf-8')
        spec = importlib.util.spec_from_file_location('extracted_' + safe, module_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {key: value for key, value in vars(module).items() if not key.startswith('__')}


class FailedNoResultDisplay(unittest.TestCase):
    def test_bounded_no_image_lookup_is_failed_without_enabling_replay(self):
        path=Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/flow/console/server.py'
        tree=ast.parse(path.read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Console')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='task_view')
        namespace=import_nodes('console_server_task_view','import re',[method])
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
