import ast
import hashlib
import math
import re
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench/server.py'

class AccountInventory(unittest.TestCase):
    def test_browser_discovery_ignores_the_reserved_export_profile(self):
        adapter = SOURCE.parent.parent / 'flow/console/adapter.py'
        tree = ast.parse(adapter.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RealAdapter')
        discover = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'discover')
        scope = {'re': re}
        exec(compile(ast.Module(body=[discover], type_ignores=[]), str(adapter), 'exec'), scope)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('music-export', 'account-2f8797411d41', 'real-unlinked'):
                (root / 'data/profiles' / name).mkdir(parents=True)
            self.assertEqual(scope['discover'](SimpleNamespace(root=root)),
                             ['account-2f8797411d41', 'real-unlinked'])

    def test_export_window_excluded_but_unlinked_real_account_retained(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'account_rows')
        scope = dict(hashlib=hashlib, math=math, re=re,
                     identity_text=lambda value: str(value or '').strip(),
                     clean_text=lambda value, limit: str(value or '')[:limit],
                     numeric_time=lambda value: value if isinstance(value, (int, float)) else None)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SOURCE), 'exec'), scope)
        rows = scope['account_rows']({'accounts': [
            {'profile': 'music-export'}, {'profile': 'real-unlinked'},
            {'profile': 'real', 'email': 'a@example.test'}, {'profile': 'real'}]})
        self.assertEqual([row['profile'] for row in rows], ['real-unlinked', 'real'])
        self.assertEqual(rows[0]['gemini'], 'unlinked')

if __name__ == '__main__':
    unittest.main()
