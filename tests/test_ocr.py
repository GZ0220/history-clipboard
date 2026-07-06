from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clipboard_history.ocr import OcrError, recognize_image_text


class OcrComponentTests(unittest.TestCase):
    def test_worker_output_uses_marker_and_ignores_logs(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            runtime = root / "ocr_runtime"
            runtime.mkdir()
            worker = runtime / "ocr_worker.py"
            worker.write_text(
                "import sys\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "print('RapidOCR log')\n"
                "print('__OCR_TEXT_BEGIN__')\n"
                "print('识别文本')\n",
                encoding="utf-8",
            )
            image_path = root / "image.png"
            image_path.write_bytes(b"fake")

            with patch("clipboard_history.ocr.app_base_dir", return_value=root):
                self.assertEqual(recognize_image_text(image_path), "识别文本")

    def test_worker_can_live_inside_ocr_component_folder(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            runtime = root / "OCR组件" / "ocr_runtime"
            runtime.mkdir(parents=True)
            worker = runtime / "ocr_worker.py"
            worker.write_text(
                "import sys\n"
                "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
                "print('__OCR_TEXT_BEGIN__')\n"
                "print('组件内识别文本')\n",
                encoding="utf-8",
            )
            image_path = root / "image.png"
            image_path.write_bytes(b"fake")

            with patch("clipboard_history.ocr.app_base_dir", return_value=root):
                self.assertEqual(recognize_image_text(image_path), "组件内识别文本")

    def test_missing_component_has_friendly_error(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            image_path = root / "image.png"
            image_path.write_bytes(b"fake")

            with patch("clipboard_history.ocr.app_base_dir", return_value=root):
                with self.assertRaisesRegex(OcrError, "未安装 OCR 组件"):
                    recognize_image_text(image_path)


if __name__ == "__main__":
    unittest.main()
