from __future__ import annotations

import ctypes
import io
import platform
from pathlib import Path
from typing import Any

from PIL import Image, ImageGrab

from .store import HistoryStore


CF_DIB = 8
GMEM_MOVEABLE = 0x0002


class ClipboardAccessError(RuntimeError):
    pass


def get_clipboard_sequence_number() -> int | None:
    if platform.system() != "Windows":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def read_clipboard(root: Any) -> tuple[str, Any] | None:
    image_result = read_image_clipboard()
    if image_result:
        return image_result
    return read_text_clipboard(root)


def read_text_clipboard(root: Any) -> tuple[str, str] | None:
    try:
        text = root.clipboard_get()
    except Exception:
        return None
    if isinstance(text, str) and text.strip():
        return ("text", text)
    return None


def read_image_clipboard() -> tuple[str, tuple[Image.Image, str | None]] | None:
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return None
    if isinstance(data, Image.Image):
        return ("image", (data.copy(), None))
    if isinstance(data, list):
        for filename in data:
            path = Path(filename)
            if path.exists() and path.is_file() and HistoryStore.is_supported_image_path(path):
                try:
                    image = HistoryStore.load_image_from_file(path)
                except Exception:
                    continue
                return ("image", (image, path.name))
    return None


def copy_text(root: Any, text: str) -> None:
    root.clipboard_clear()
    root.clipboard_append(text)


def copy_image(path: Path) -> None:
    if platform.system() != "Windows":
        raise ClipboardAccessError("图片复制回剪贴板目前仅支持 Windows。")
    with Image.open(path) as image:
        _copy_pil_image_to_windows_clipboard(image)


def _copy_pil_image_to_windows_clipboard(image: Image.Image) -> None:
    output = io.BytesIO()
    image.convert("RGB").save(output, "BMP")
    dib = output.getvalue()[14:]
    output.close()

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
    if not handle:
        raise ClipboardAccessError("无法分配剪贴板内存。")
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise ClipboardAccessError("无法写入剪贴板内存。")
    ctypes.memmove(locked, dib, len(dib))
    kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise ClipboardAccessError("无法打开系统剪贴板。")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, handle):
            kernel32.GlobalFree(handle)
            raise ClipboardAccessError("无法设置图片剪贴板内容。")
    finally:
        user32.CloseClipboard()
