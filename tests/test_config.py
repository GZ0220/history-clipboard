from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from clipboard_history.config import ConfigManager


class ConfigTests(unittest.TestCase):
    def test_new_practical_defaults(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            config = ConfigManager(Path(temp_dir)).config

            self.assertFalse(config.paused)
            self.assertEqual(config.max_image_mb, 5)
            self.assertEqual(config.max_history_items, 500)
            self.assertFalse(config.show_ocr_in_cards)
            self.assertTrue(config.global_hotkey_enabled)
            self.assertEqual(config.global_hotkey, "ctrl+alt+v")
            self.assertFalse(config.start_with_windows)
            self.assertFalse(config.sensitive_filter_enabled)
            self.assertIsNone(config.pause_until)


if __name__ == "__main__":
    unittest.main()
