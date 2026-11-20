import ast
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing


SOURCE = Path(__file__).resolve().parents[1] / 'packaging/ubuntu/snapshot/flow/console/browser_rendering.py'


class FlowUploadConsent(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_bytes())
        names = {'reference_dialog_choice', 'is_same_project_image'}
        self.scope = dict(sqlite3=sqlite3, closing=closing)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), 'exec'), self.scope)

    def test_option_count_and_labels(self):
        choose = self.scope['reference_dialog_choice']
        self.assertEqual(choose(['我同意']), '我同意')
        self.assertEqual(choose(['取消', '我同意']), '我同意')
        self.assertEqual(choose(['我同意', '查看说明', '取消']), '取消')
        for labels in (['删除'], ['删除', '取消'], ['我同意', '同意'], ['取消', '取消', '我同意'],
                       ['我同意', '取消', '稍后', '帮助']):
            self.assertIsNone(choose(labels))

    def test_generated_image_must_match_both_account_and_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbpath, image = root/'library.db', root/'frame.jpg'
            image.write_bytes(b'generated image bytes')
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            with sqlite3.connect(dbpath) as db:
                db.executescript("""CREATE TABLE assets(id,sha256,media_type);
                    CREATE TABLE origins(asset_id,source,account_id,account_email,project_id);""")
                db.execute("INSERT INTO assets VALUES('id',?,'image')", (digest,))
                db.execute("INSERT INTO origins VALUES('id','studiowright','profile','me@example.test','project')")
            match = self.scope['is_same_project_image']
            self.assertTrue(match(dbpath, image, 'profile', 'me@example.test', 'project'))
            self.assertFalse(match(dbpath, image, 'profile', 'me@example.test', 'other'))
            self.assertFalse(match(dbpath, image, 'other', 'me@example.test', 'project'))
            image.write_bytes(b'external image bytes')
            self.assertFalse(match(dbpath, image, 'profile', 'me@example.test', 'project'))
