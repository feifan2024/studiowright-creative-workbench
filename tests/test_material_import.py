import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import wave

from PIL import Image

CONSOLE = Path(__file__).resolve().parents[1] / "packaging/ubuntu/snapshot/flow/console"
sys.path.insert(0, str(CONSOLE))
from material_import import SOURCE, archive_metadata, receive
from material_library import Library


def message(path, data):
    return json.dumps({
        "path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "modified": 0,
    }).encode() + b"\n"


class MaterialImportTest(unittest.TestCase):
    def test_search_combines_catalog_and_analysis_before_filtering_and_pagination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pool = root / "pool.sqlite3"
            library = Library(root / "library", pool)
            source = root / "sample.png"
            Image.new("RGB", (16, 20), "red").save(source)
            windows = library.archive(source, archive_metadata(json.loads(message("采集/原片.png", source.read_bytes()))))
            Image.new("RGB", (16, 20), "blue").save(source)
            ubuntu = library.archive(source, dict(source="flow", source_id="task", prompt="河岸夜景", media_type="image"))
            library.set_dimensions(ubuntu, {"subject": ["手动人物"]})
            with sqlite3.connect(pool) as db:
                db.execute("CREATE TABLE material_extra(asset_id TEXT,manual_json TEXT,suggested_json TEXT)")
                db.executemany("INSERT INTO material_extra VALUES(?,?,?)", [
                    (windows, '{"viewpoint":["高角度"]}', '{"scene":["河岸夜景"],"viewpoint":["高角度"]}'),
                    (ubuntu, '{}', '{"subject":["旧人物"]}'),
                    ("f" * 64, '{}', '{"scene":["赠品音乐包"],"viewpoint":["高角度"]}'),
                ])
            first, second = (library.search("河岸夜景", limit=1, page=page) for page in (1, 2))
            self.assertEqual(first["total"], 2)
            self.assertEqual({first["items"][0]["id"], second["items"][0]["id"]}, {windows, ubuntu})
            match = library.search("原片.png", kind="image", dimensions={"viewpoint": "高角度", "scene": "河岸夜景"})
            self.assertEqual([item["id"] for item in match["items"]], [windows])
            self.assertEqual(library.search("河岸夜景", kind="audio")["total"], 0)
            self.assertEqual(library.search("旧人物")["total"], 0)
            self.assertEqual(library.search("赠品音乐包")["total"], 0)
            self.assertEqual(match["facets"]["dimensions"]["viewpoint"], [{"value": "高角度", "count": 1}])

    def test_import_replay_and_second_source_share_one_file_and_search(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = io.BytesIO()
            Image.new("RGB", (16, 20), "red").save(image, format="PNG")
            content = image.getvalue()
            first = message("城市地标/上海外滩.png", content)
            second = message("片库/外滩别名.png", content)
            output = io.BytesIO()
            receive(root, io.BytesIO(first + content + first + second), output)
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([r["status"] for r in replies], ["send", "imported", "unchanged", "deduplicated"])
            library = Library(root)
            original = library.search("上海外滩", kind="image")
            alias = library.search("片库/外滩别名", kind="image")
            self.assertEqual(original["total"], 1)
            self.assertEqual(original["items"][0]["id"], alias["items"][0]["id"])
            self.assertEqual({o["source"] for o in alias["items"][0]["origins"]}, {SOURCE})
            self.assertEqual(len(alias["items"][0]["origins"]), 2)
            self.assertEqual(library.file(alias["items"][0]["id"]).read_bytes(), content)

    def test_existing_audio_switches_to_its_category_and_keeps_legacy_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = Library(root / "library")
            assets = {}
            for category, folder, sample in (("music", "音乐", b"\x01\0"), ("sound_effect", "音效", b"\x02\0")):
                path = root / (folder + ".wav")
                with wave.open(str(path), "wb") as audio:
                    audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                    audio.writeframes(sample * 8000)
                meta = archive_metadata(json.loads(message(folder + "/原音频.wav", path.read_bytes())))
                ident = library.archive(path, meta)
                current = library.file(ident)
                self.assertEqual(current.relative_to(library.root).parts[0:3], ("objects", "audio", category))
                legacy = library.root / library.object_path(ident, ".wav")
                legacy.parent.mkdir(exist_ok=True)
                current.rename(legacy)
                with library.connect() as connection:
                    connection.execute("UPDATE assets SET relative_path=? WHERE id=?", (legacy.relative_to(library.root).as_posix(), ident))
                assets[category] = (ident, legacy)
            self.assertEqual(library.organize_existing_audio(), 2)
            self.assertEqual(library.organize_existing_audio(), 0)
            for category, (ident, legacy) in assets.items():
                self.assertTrue(library.file(ident).samefile(legacy))
                self.assertEqual(library.search(audio_category=category)["items"][0]["id"], ident)

    def test_receiver_rejects_untrusted_source_path(self):
        with self.assertRaises(ValueError):
            archive_metadata(json.loads(message("../视频.mp4", b"sample")))
