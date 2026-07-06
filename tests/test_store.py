from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from clipboard_history.store import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def test_text_duplicates_are_merged_and_searchable(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = HistoryStore(Path(temp_dir))
            first = store.add_text("你好，历史粘贴板")
            second = store.add_text("你好，历史粘贴板")

            self.assertIsNotNone(first)
            self.assertEqual(first.id, second.id)
            self.assertEqual(len(store.items), 1)
            self.assertEqual(store.query("历史", "text", "desc")[0].id, first.id)

    def test_chinese_search_ignores_spacing(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = HistoryStore(Path(temp_dir))
            item = store.add_text("vibe coding入门学习记录")

            self.assertEqual(store.query("学 习", "all", "desc")[0].id, item.id)

    def test_image_is_saved_with_thumbnail_and_note_search(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = HistoryStore(Path(temp_dir))
            item = store.add_image(Image.new("RGBA", (24, 24), "skyblue"), "sample.png")
            store.update_note(item.id, "蓝色截图")
            store.update_ocr_text(item.id, "识别到的学习文字")

            self.assertTrue(Path(item.image_path or "").exists())
            self.assertTrue(Path(item.thumb_path or "").exists())
            self.assertEqual(store.query("截图", "image", "desc")[0].id, item.id)
            self.assertEqual(store.query("学习", "image", "desc")[0].id, item.id)
            self.assertEqual(store.match_sources(item, "学习"), ["OCR命中"])

    def test_pinned_items_sort_first_and_do_not_expire(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = HistoryStore(Path(temp_dir))
            old_item = store.add_text("旧内容")
            new_item = store.add_text("新内容")
            self.assertIsNotNone(old_item)
            self.assertIsNotNone(new_item)

            old_item.last_copied_at = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
            store.update_pin(old_item.id, True)
            store.save()

            results = store.query("", "all", "desc")
            self.assertEqual(results[0].id, old_item.id)
            self.assertEqual(store.expired_items(7), [])

    def test_enforce_limit_removes_oldest_unpinned_only(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = HistoryStore(Path(temp_dir))
            pinned = store.add_text("置顶")
            old = store.add_text("旧")
            new = store.add_text("新")
            self.assertIsNotNone(pinned)
            self.assertIsNotNone(old)
            self.assertIsNotNone(new)

            pinned.last_copied_at = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
            old.last_copied_at = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
            store.update_pin(pinned.id, True)
            store.save()

            deleted = store.enforce_limit(2)
            self.assertEqual(deleted, 1)
            self.assertIsNotNone(store.get(pinned.id))
            self.assertIsNone(store.get(old.id))
            self.assertIsNotNone(store.get(new.id))

    def test_export_selected_items_writes_index_summary_and_images(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(root / "data")
            text_item = store.add_text("批量复制文字")
            image_item = store.add_image(Image.new("RGBA", (24, 24), "skyblue"), "source.png")
            self.assertIsNotNone(text_item)
            store.update_note(image_item.id, "图片备注")
            store.update_ocr_text(image_item.id, "OCR 文字")

            target = root / "exported"
            count = store.export_items([text_item.id, image_item.id], target)

            self.assertEqual(count, 2)
            self.assertTrue((target / "selected_index.json").exists())
            summary = (target / "selected_text.txt").read_text(encoding="utf-8")
            self.assertIn("批量复制文字", summary)
            self.assertIn("图片备注", summary)
            self.assertIn("OCR 文字", summary)
            self.assertTrue(any((target / "images").iterdir()))
            self.assertTrue(any((target / "thumbs").iterdir()))

    def test_search_cache_updates_after_note_and_ocr_changes(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = HistoryStore(Path(temp_dir))
            item = store.add_image(Image.new("RGBA", (24, 24), "skyblue"), "source.png")

            self.assertEqual(store.query("后续备注", "image", "desc"), [])
            store.update_note(item.id, "后续备注")
            self.assertEqual(store.query("后续备注", "image", "desc")[0].id, item.id)

            self.assertEqual(store.query("后续识别", "image", "desc"), [])
            store.update_ocr_text(item.id, "后续识别")
            self.assertEqual(store.query("后续识别", "image", "desc")[0].id, item.id)

    def test_delete_many_saves_once(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = CountingHistoryStore(Path(temp_dir))
            first = store.add_text("第一条")
            second = store.add_text("第二条")
            store.save_count = 0

            store.delete_many([first.id, second.id])

            self.assertEqual(store.save_count, 1)
            self.assertEqual(store.items, [])

    def test_enforce_limit_saves_once_when_deleting_many(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = CountingHistoryStore(Path(temp_dir))
            for index in range(5):
                item = store.add_text(f"记录 {index}")
                item.last_copied_at = (datetime.now() - timedelta(days=10 - index)).isoformat(timespec="seconds")
            store.save()
            store.save_count = 0

            deleted = store.enforce_limit(2)

            self.assertEqual(deleted, 3)
            self.assertEqual(store.save_count, 1)
            self.assertEqual(len(store.items), 2)

    def test_metadata_updates_can_skip_immediate_save(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            store = CountingHistoryStore(Path(temp_dir))
            item = store.add_text("metadata")
            store.save_count = 0

            store.update_pin(item.id, True, save=False)
            store.touch(item.id, save=False)

            self.assertEqual(store.save_count, 0)
            store.save()
            self.assertEqual(store.save_count, 1)

    def test_restore_backup_reloads_items(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = HistoryStore(root / "source")
            source.add_text("backup item")
            backup = root / "backup"
            source.export_backup(backup)

            target = HistoryStore(root / "target")
            target.add_text("old item")
            target.restore_backup(backup)

            self.assertEqual(len(target.items), 1)
            self.assertEqual(target.items[0].text, "backup item")


class CountingHistoryStore(HistoryStore):
    def __init__(self, storage_dir: Path) -> None:
        self.save_count = 0
        super().__init__(storage_dir)

    def save(self) -> None:
        self.save_count += 1
        super().save()


if __name__ == "__main__":
    unittest.main()
