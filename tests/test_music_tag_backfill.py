from contextlib import closing
import sys
import sqlite3
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from music_tag_backfill import backfill
from test_flow_regressions import fixture_directory

class Backfill(unittest.TestCase):
    def test_additive_idempotent_preserves_manual_origins_and_non_audio(self):
        with fixture_directory() as d:
            path=d/'library.db'
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript("""
                CREATE TABLE assets(id TEXT,media_type TEXT);
                CREATE TABLE origins(asset_id TEXT,source TEXT,source_id TEXT,prompt TEXT,details TEXT);
                CREATE TABLE dimension_tags(asset_id TEXT,dimension TEXT,value TEXT,source TEXT,PRIMARY KEY(asset_id,dimension,value,source));
                CREATE TABLE manual_dimensions(asset_id TEXT,dimension TEXT);
                CREATE TABLE tags(asset_id TEXT,value TEXT);
                INSERT INTO assets VALUES('a','audio'),('b','audio'),('c','image');
                INSERT INTO origins VALUES('a','flow_music','one','爵士标题：gentle piano, no vocals','{"style_id":" Exact "}');
                INSERT INTO origins VALUES('b','flow_music','two','钢琴','{}');
                INSERT INTO origins VALUES('c','flow_music','three','钢琴','{}');
                INSERT INTO tags VALUES('a',' 原手动标签 ');
                INSERT INTO dimension_tags VALUES('a','keywords','已有','manual');
                INSERT INTO manual_dimensions VALUES('b','keywords');
                """)
                before=db.execute('SELECT * FROM origins').fetchall()
            preview=backfill(path);self.assertEqual(preview['updated_assets'],1)
            self.assertNotIn('爵士',preview['items'][0]['added_tags'])
            first=backfill(path,True);self.assertEqual(first['manual_skipped'],1)
            self.assertEqual(backfill(path,True)['added_tags'],0)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT * FROM origins').fetchall(),before)
                self.assertEqual(db.execute('SELECT value FROM tags').fetchone()[0],' 原手动标签 ')
                self.assertTrue(db.execute("SELECT 1 FROM dimension_tags WHERE value='已有'").fetchone())
                self.assertFalse(db.execute("SELECT 1 FROM dimension_tags WHERE asset_id IN ('b','c')").fetchone())
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='requests'").fetchone())

if __name__=='__main__':unittest.main()
