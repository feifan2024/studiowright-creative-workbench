import ast
import hashlib
import math
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/workbench/server.py'

class AccountInventory(unittest.TestCase):
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
