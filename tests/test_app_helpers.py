from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from PIL import Image

from clipboard_history.app import (
    build_diagnostic_report,
    card_render_snapshot,
    diff_item_order,
    format_timestamp,
    image_path_from_clipboard_text,
    looks_sensitive_text,
    page_item_range,
    seed_perf_data,
    sequence_status_text,
)
from clipboard_history.config import AppConfig
from clipboard_history.store import HistoryItem


class AppHelperTests(unittest.TestCase):
    def test_format_timestamp_uses_readable_spacing(self) -> None:
        self.assertEqual(format_timestamp("2026-07-06T11:15:10"), "2026-07-06 11:15:10")

    def test_diff_item_order_reports_added_removed_and_kept(self) -> None:
        added, removed, kept = diff_item_order(["a", "b", "c"], ["c", "a", "d"])

        self.assertEqual(added, ["d"])
        self.assertEqual(removed, ["b"])
        self.assertEqual(kept, ["c", "a"])

    def test_card_snapshot_tracks_local_visual_fields(self) -> None:
        item = HistoryItem(
            id="item-1",
            type="image",
            created_at="2026-07-06T10:00:00",
            last_copied_at="2026-07-06T10:01:00",
            pinned=False,
            note="备注",
            content_hash="image:abc",
            thumb_path="thumb.png",
            source_name="source.png",
            ocr_text="识别文字",
        )

        before = card_render_snapshot(item, selected=False, search="", show_ocr=False, match_sources=[])
        item.pinned = True
        after = card_render_snapshot(item, selected=False, search="", show_ocr=False, match_sources=[])

        self.assertFalse(before.pinned)
        self.assertTrue(after.pinned)
        self.assertEqual(before.ocr_text, after.ocr_text)
        self.assertEqual(before.note, after.note)

    def test_sensitive_text_detection_is_conservative(self) -> None:
        self.assertTrue(looks_sensitive_text("password=abc123"))
        self.assertTrue(looks_sensitive_text("123456"))
        self.assertTrue(looks_sensitive_text("Abc12345!"))
        self.assertFalse(looks_sensitive_text("学习资料 2026"))
        self.assertFalse(looks_sensitive_text("这是一段普通复制内容"))

    def test_image_path_from_clipboard_text_accepts_existing_image_file(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            path = Path(temp_dir) / "screenclip.png"
            Image.new("RGB", (8, 8), "white").save(path)

            self.assertEqual(image_path_from_clipboard_text(str(path)), path)
            self.assertEqual(image_path_from_clipboard_text(f'"{path}"'), path)

    def test_image_path_from_clipboard_text_rejects_normal_text(self) -> None:
        self.assertIsNone(image_path_from_clipboard_text("学习资料"))
        self.assertIsNone(image_path_from_clipboard_text("C:/not-exist/screenclip.png"))
        self.assertIsNone(image_path_from_clipboard_text("C:/one.png\nC:/two.png"))

    def test_sequence_status_text(self) -> None:
        self.assertEqual(sequence_status_text(True), "可用")
        self.assertIn("回退", sequence_status_text(False))
        self.assertEqual(sequence_status_text(None), "尚未检测")

    def test_seed_perf_data_creates_requested_records(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            count = seed_perf_data(Path(temp_dir), 12)
            self.assertEqual(count, 12)

    def test_page_item_range_clamps_to_valid_page(self) -> None:
        self.assertEqual(page_item_range(0, 100, page_size=60), (0, 0))
        self.assertEqual(page_item_range(20, 100, page_size=60), (0, 20))
        self.assertEqual(page_item_range(500, -10, page_size=60), (0, 60))
        self.assertEqual(page_item_range(500, 480, page_size=60), (440, 500))

    def test_diagnostic_report_contains_key_state(self) -> None:
        config = AppConfig(storage_dir="D:/data")
        app = SimpleNamespace(
            store=SimpleNamespace(items=[]),
            config=config,
            config_manager=SimpleNamespace(path=Path("config.json")),
            app_dir=Path("app"),
            visible_item_ids=[],
            selected_ids=set(),
            pending_store_save=False,
            clipboard_sequence_supported=True,
            perf_events=["10:00:00 refresh_items: 1.0ms"],
            is_recording_paused=lambda: False,
        )

        report = build_diagnostic_report(app)

        self.assertIn("历史粘贴板 诊断报告", report)
        self.assertIn("OCR 组件", report)
        self.assertIn("refresh_items", report)


if __name__ == "__main__":
    unittest.main()
