from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class OcrError(RuntimeError):
    pass


OCR_RUNTIME_DIR = "ocr_runtime"
OCR_COMPONENT_DIR = "OCR组件"
OCR_WORKER_EXE = "ocr_worker.exe"
OCR_WORKER_SCRIPT = "ocr_worker.py"
OCR_TEXT_MARKER = "__OCR_TEXT_BEGIN__"


def is_ocr_available() -> bool:
    return find_ocr_worker() is not None


def recognize_image_text(image_path: Path) -> str:
    if not image_path.exists():
        raise OcrError("图片文件不存在，无法识别。")
    worker = find_ocr_worker()
    if not worker:
        raise OcrError("未安装 OCR 组件。请把 ocr_runtime 文件夹，或包含它的 OCR组件 文件夹，放到软件目录旁边后重试。")

    command = [str(worker), str(image_path)]
    if worker.suffix.lower() == ".py":
        command = [sys.executable, str(worker), str(image_path)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=_subprocess_creation_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrError("OCR 识别超时，请稍后重试或换一张更清晰的图片。") from exc
    except OSError as exc:
        raise OcrError(f"OCR 组件启动失败：{exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise OcrError(detail or "OCR 组件运行失败。")
    output = completed.stdout
    if OCR_TEXT_MARKER in output:
        return output.split(OCR_TEXT_MARKER, 1)[1].strip()
    return output.strip()


def find_ocr_worker() -> Path | None:
    base_dir = app_base_dir()
    runtime_dirs = [
        base_dir / OCR_RUNTIME_DIR,
        base_dir / OCR_COMPONENT_DIR / OCR_RUNTIME_DIR,
    ]
    candidates = [
        runtime_dir / worker_name
        for runtime_dir in runtime_dirs
        for worker_name in (OCR_WORKER_EXE, OCR_WORKER_SCRIPT)
    ]
    return next((path for path in candidates if path.exists()), None)


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _subprocess_creation_flags() -> int:
    if sys.platform.startswith("win"):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0
