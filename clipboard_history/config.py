from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


CONFIG_FILE = "clipboard_history_config.json"


@dataclass
class AppConfig:
    storage_dir: str | None = None
    retention_days: int = 7
    sort_order: str = "desc"
    type_filter: str = "all"
    last_cleanup_prompt_date: str | None = None
    paused: bool = False
    pause_until: str | None = None
    max_image_mb: int = 5
    max_history_items: int = 500
    show_ocr_in_cards: bool = False
    global_hotkey_enabled: bool = True
    global_hotkey: str = "ctrl+alt+v"
    start_with_windows: bool = False
    sensitive_filter_enabled: bool = False


class ConfigManager:
    def __init__(self, app_dir: Path) -> None:
        self.path = app_dir / CONFIG_FILE
        self.config = self.load()

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AppConfig()
        return AppConfig(
            storage_dir=data.get("storage_dir"),
            retention_days=_safe_int(data.get("retention_days"), 7),
            sort_order=str(data.get("sort_order", "desc")),
            type_filter=str(data.get("type_filter", "all")),
            last_cleanup_prompt_date=data.get("last_cleanup_prompt_date"),
            paused=bool(data.get("paused", False)),
            pause_until=data.get("pause_until"),
            max_image_mb=_safe_int(data.get("max_image_mb"), 5),
            max_history_items=_safe_int(data.get("max_history_items"), 500),
            show_ocr_in_cards=bool(data.get("show_ocr_in_cards", False)),
            global_hotkey_enabled=bool(data.get("global_hotkey_enabled", True)),
            global_hotkey=str(data.get("global_hotkey", "ctrl+alt+v")),
            start_with_windows=bool(data.get("start_with_windows", False)),
            sensitive_filter_enabled=bool(data.get("sensitive_filter_enabled", False)),
        )

    def save(self) -> None:
        self.path.write_text(
            json.dumps(asdict(self.config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
