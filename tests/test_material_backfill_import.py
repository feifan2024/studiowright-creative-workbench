from contextlib import nullcontext
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging/ubuntu/snapshot/workbench"))
from material_backfill import MaterialBackfill


class PoolFixture:
    def __init__(self, db):
        self.db = db

    def connection(self):
        return nullcontext(self.db)

    def maintenance(self):
        return False

    def conversation_id(self, request_id):
        return request_id


class ImportedBackfillTest(unittest.TestCase):
    def test_adopts_only_unsubmitted_imports_from_paused_queue(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE material_analyses(asset_id TEXT);
            CREATE TABLE auto_material_jobs(asset_id TEXT,status TEXT);
            CREATE TABLE material_extra(asset_id TEXT,manual_json TEXT,suggested_json TEXT);
        """)
        backfill = MaterialBackfill(PoolFixture(db))
        db.execute("INSERT INTO material_backfill_runs VALUES('prior','untagged',0,'paused')")
        db.executemany("""INSERT INTO material_backfill_jobs(asset_id,run_id,status,added,updated)
                        VALUES(?,'prior',?,0,0)""", [("1" * 64, "waiting"), ("2" * 64, "waiting"), ("3" * 64, "failed")])
        def item(n, source, mime="video/mp4"):
            return {"id": str(n) * 64, "size": 200 * 1024**2, "mime": mime, "media_type": "video",
                    "origins": [{"source": source}], "tags": ["本地目录"], "structured_tags": {}}
        items = [item(1, "windows_local"), item(2, "flow"), item(3, "windows_local"),
                 item(4, "windows_local", "video/quicktime"), item(5, "gift_music")]
        self.assertEqual(backfill.preview(items)["windows_import"], 2)
        self.assertEqual(backfill.start("windows_import", "new", items)["counts"], {"waiting": 2})
        self.assertEqual([tuple(row) for row in db.execute(
            "SELECT substr(asset_id,1,1),run_id,status FROM material_backfill_jobs ORDER BY asset_id"
        )], [("1", "new", "waiting"), ("2", "prior", "waiting"), ("3", "prior", "failed"), ("4", "new", "waiting")])
