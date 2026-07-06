from __future__ import annotations

import sys
import traceback
from pathlib import Path
from threading import Lock


_engine = None
_engine_lock = Lock()


def recognize_image_text(image_path: Path) -> str:
    if not image_path.exists():
        raise RuntimeError("图片文件不存在，无法识别。")
    engine = _get_engine()
    result = engine(str(image_path))
    texts = getattr(result, "txts", None)
    if not texts:
        return ""
    return "\n".join(text.strip() for text in texts if text and text.strip())


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            try:
                from rapidocr import RapidOCR
            except Exception as exc:
                raise RuntimeError("OCR 组件未安装或加载失败。") from exc
            _engine = RapidOCR()
        return _engine


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2 or sys.argv[1] in {"--help", "-h"}:
        print("用法：ocr_worker.exe <图片路径>", file=sys.stderr)
        return 2
    try:
        text = recognize_image_text(Path(sys.argv[1]))
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        return 1
    print("__OCR_TEXT_BEGIN__")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
