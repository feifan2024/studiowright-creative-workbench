import sys
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging/ubuntu/snapshot/workbench'))
from music_metadata import attach_metadata
from music_category_migration import migrate
from test_flow_regressions import fixture_directory
class Categories(unittest.TestCase):
    def test_grouped_prompt_and_exact_manual(self):
        r=attach_metadata({'clips':[{}]},'轻快钢琴爵士背景音乐，无人声','lyria-fast','id',{'tags':[' Exact '],'style_id':' S '})
        g=r['clips'][0]['music_tags']
        self.assertEqual(g['music_instrument'],['钢琴']);self.assertEqual(g['music_style'],['爵士'])
        self.assertEqual(g['music_mood'],['轻快']);self.assertEqual(g['music_use'],['背景音乐'])
        self.assertEqual(r['tags'],[' Exact ']);self.assertEqual(r['style_id'],' S ')
    def test_migration_only_moves_automatic_and_respects_manual_dimensions(self):
        with fixture_directory() as d:
            path=d/'db'
            with closing(sqlite3.connect(path)) as db,db:
                db.executescript("""CREATE TABLE assets(id,media_type);CREATE TABLE dimension_tags(asset_id,dimension,value,source,PRIMARY KEY(asset_id,dimension,value,source));CREATE TABLE manual_dimensions(asset_id,dimension);
                INSERT INTO assets VALUES('a','audio'),('b','audio');
                INSERT INTO dimension_tags VALUES('a','keywords','钢琴','music_prompt'),('a','keywords','手动','manual'),('b','keywords','钢琴','music_prompt');
                INSERT INTO manual_dimensions VALUES('b','music_instrument');""")
            self.assertEqual(migrate(path)['moved'],1);self.assertEqual(migrate(path,True)['manual_skipped'],1)
            self.assertEqual(migrate(path,True)['moved'],0)
            with closing(sqlite3.connect(path)) as db:
                self.assertTrue(db.execute("SELECT 1 FROM dimension_tags WHERE dimension='music_instrument' AND value='钢琴'").fetchone())
                self.assertTrue(db.execute("SELECT 1 FROM dimension_tags WHERE value='手动'").fetchone())
                self.assertTrue(db.execute("SELECT 1 FROM dimension_tags WHERE asset_id='b' AND dimension='keywords'").fetchone())
