from __future__ import annotations

import hashlib
import json
import shutil
import re
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


INDEX_FILE = "history_index.json"
IMAGE_DIR = "images"
THUMB_DIR = "thumbs"
THUMB_SIZE = (220, 160)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class HistoryItem:
    id: str
    type: str
    created_at: str
    last_copied_at: str
    pinned: bool
    note: str
    content_hash: str
    text: str | None = None
    image_path: str | None = None
    thumb_path: str | None = None
    source_name: str | None = None
    ocr_text: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryItem":
        return cls(
            id=data["id"],
            type=data["type"],
            created_at=data["created_at"],
            last_copied_at=data["last_copied_at"],
            pinned=bool(data.get("pinned", False)),
            note=data.get("note", ""),
            content_hash=data["content_hash"],
            text=data.get("text"),
            image_path=data.get("image_path"),
            thumb_path=data.get("thumb_path"),
            source_name=data.get("source_name"),
            ocr_text=data.get("ocr_text", ""),
        )


class HistoryStore:
    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir
        self.images_dir = storage_dir / IMAGE_DIR
        self.thumbs_dir = storage_dir / THUMB_DIR
        self.index_path = storage_dir / INDEX_FILE
        self.items: list[HistoryItem] = []
        self._search_cache: dict[str, str] = {}
        self._time_cache: dict[tuple[str, str], datetime] = {}
        self.ensure_dirs()
        self.load()

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        if not self.index_path.exists():
            self.items = []
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.items = []
            return
        self.items = [HistoryItem.from_dict(item) for item in payload.get("items", [])]
        self._reset_caches()

    def save(self) -> None:
        payload = {"items": [asdict(item) for item in self.items]}
        temp_path = self.index_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.index_path)

    def add_text(self, text: str) -> HistoryItem | None:
        cleaned = text.strip("\x00")
        if not cleaned.strip():
            return None
        content_hash = self.hash_text(cleaned)
        duplicate = self.find_by_hash(content_hash)
        timestamp = now_iso()
        if duplicate:
            duplicate.last_copied_at = timestamp
            duplicate.text = cleaned
            self.invalidate_item_cache(duplicate.id)
            self.save()
            return duplicate
        item = HistoryItem(
            id=str(uuid.uuid4()),
            type="text",
            created_at=timestamp,
            last_copied_at=timestamp,
            pinned=False,
            note="",
            content_hash=content_hash,
            text=cleaned,
        )
        self.items.append(item)
        self.invalidate_item_cache(item.id)
        self.save()
        return item

    def add_image(self, image: Image.Image, source_name: str | None = None, content_hash: str | None = None) -> HistoryItem:
        image_copy = image.convert("RGBA")
        content_hash = content_hash or self.hash_image(image_copy)
        duplicate = self.find_by_hash(content_hash)
        timestamp = now_iso()
        if duplicate:
            duplicate.last_copied_at = timestamp
            self.invalidate_item_cache(duplicate.id)
            self.save()
            return duplicate

        item_id = str(uuid.uuid4())
        image_path = self.images_dir / f"{item_id}.png"
        thumb_path = self.thumbs_dir / f"{item_id}.png"
        image_copy.save(image_path, format="PNG")
        thumb = image_copy.copy()
        thumb.thumbnail(THUMB_SIZE)
        thumb.save(thumb_path, format="PNG")
        item = HistoryItem(
            id=item_id,
            type="image",
            created_at=timestamp,
            last_copied_at=timestamp,
            pinned=False,
            note="",
            content_hash=content_hash,
            image_path=str(image_path),
            thumb_path=str(thumb_path),
            source_name=source_name,
        )
        self.items.append(item)
        self.invalidate_item_cache(item.id)
        self.save()
        return item

    def find_by_hash(self, content_hash: str) -> HistoryItem | None:
        return next((item for item in self.items if item.content_hash == content_hash), None)

    def update_pin(self, item_id: str, pinned: bool, save: bool = True) -> None:
        item = self.get(item_id)
        if item:
            item.pinned = pinned
            self.invalidate_item_cache(item_id)
            if save:
                self.save()

    def update_note(self, item_id: str, note: str, save: bool = True) -> None:
        item = self.get(item_id)
        if item and item.type == "image":
            item.note = note
            self.invalidate_item_cache(item_id)
            if save:
                self.save()

    def update_ocr_text(self, item_id: str, ocr_text: str, save: bool = True) -> None:
        item = self.get(item_id)
        if item and item.type == "image":
            item.ocr_text = ocr_text.strip()
            self.invalidate_item_cache(item_id)
            if save:
                self.save()

    def touch(self, item_id: str, save: bool = True) -> None:
        item = self.get(item_id)
        if item:
            item.last_copied_at = now_iso()
            self.invalidate_item_cache(item_id)
            if save:
                self.save()

    def delete(self, item_id: str, save: bool = True) -> None:
        item = self.get(item_id)
        if not item:
            return
        self.items = [candidate for candidate in self.items if candidate.id != item_id]
        for path_text in (item.image_path, item.thumb_path):
            if path_text:
                path = Path(path_text)
                try:
                    if path.exists() and path.is_file():
                        path.unlink()
                except OSError:
                    pass
        self.invalidate_item_cache(item_id)
        if save:
            self.save()

    def get(self, item_id: str) -> HistoryItem | None:
        return next((item for item in self.items if item.id == item_id), None)

    def expired_items(self, retention_days: int) -> list[HistoryItem]:
        if retention_days <= 0:
            return []
        cutoff = datetime.now() - timedelta(days=retention_days)
        return [
            item
            for item in self.items
            if not item.pinned and parse_time(item.last_copied_at) < cutoff
        ]

    def delete_many(self, item_ids: Iterable[str]) -> None:
        changed = False
        for item_id in list(item_ids):
            if self.get(item_id):
                self.delete(item_id, save=False)
                changed = True
        if changed:
            self.save()

    def selected_items(self, item_ids: Iterable[str]) -> list[HistoryItem]:
        selected = set(item_ids)
        return [item for item in self.items if item.id in selected]

    def clear_unpinned(self) -> int:
        unpinned_ids = [item.id for item in self.items if not item.pinned]
        self.delete_many(unpinned_ids)
        return len(unpinned_ids)

    def enforce_limit(self, max_items: int) -> int:
        if max_items <= 0 or len(self.items) <= max_items:
            return 0
        removable = sorted(
            [item for item in self.items if not item.pinned],
            key=lambda item: parse_time(item.last_copied_at),
        )
        deleted = 0
        while len(self.items) > max_items and removable:
            item = removable.pop(0)
            self.delete(item.id, save=False)
            deleted += 1
        if deleted:
            self.save()
        return deleted

    def query(self, search: str, type_filter: str, sort_order: str) -> list[HistoryItem]:
        search_text = normalize_search_text(search)
        visible = self.items
        if type_filter in {"text", "image"}:
            visible = [item for item in visible if item.type == type_filter]
        if search_text:
            visible = [
                item
                for item in visible
                if search_text in self.cached_search_blob(item)
            ]
        time_factor = 1 if sort_order == "asc" else -1
        return sorted(
            visible,
            key=lambda item: (
                0 if item.pinned else 1,
                self.cached_time(item).timestamp() * time_factor,
            ),
        )

    @staticmethod
    def search_blob(item: HistoryItem) -> str:
        parts = [item.note or "", item.source_name or "", item.ocr_text or ""]
        if item.text:
            parts.append(item.text)
        return "\n".join(parts).lower()

    def cached_search_blob(self, item: HistoryItem) -> str:
        cached = self._search_cache.get(item.id)
        if cached is None:
            cached = normalize_search_text(self.search_blob(item))
            self._search_cache[item.id] = cached
        return cached

    def cached_time(self, item: HistoryItem) -> datetime:
        key = (item.id, item.last_copied_at)
        cached = self._time_cache.get(key)
        if cached is None:
            cached = parse_time(item.last_copied_at)
            self._time_cache[key] = cached
        return cached

    def invalidate_item_cache(self, item_id: str) -> None:
        self._search_cache.pop(item_id, None)
        for key in [key for key in self._time_cache if key[0] == item_id]:
            self._time_cache.pop(key, None)

    def _reset_caches(self) -> None:
        self._search_cache = {}
        self._time_cache = {}

    @staticmethod
    def match_sources(item: HistoryItem, search: str) -> list[str]:
        search_text = normalize_search_text(search)
        if not search_text:
            return []
        sources: list[str] = []
        fields = [
            ("正文命中", item.text or ""),
            ("备注命中", item.note or ""),
            ("OCR命中", item.ocr_text or ""),
            ("文件名命中", item.source_name or ""),
        ]
        for label, value in fields:
            if value and search_text in normalize_search_text(value):
                sources.append(label)
        return sources

    @staticmethod
    def hash_text(text: str) -> str:
        return "text:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def hash_image(image: Image.Image) -> str:
        normalized = image.convert("RGBA")
        digest = hashlib.sha256()
        digest.update(str(normalized.size).encode("ascii"))
        digest.update(normalized.tobytes())
        return "image:" + digest.hexdigest()

    @staticmethod
    def is_supported_image_path(path: Path) -> bool:
        return path.suffix.lower() in IMAGE_EXTENSIONS

    @staticmethod
    def load_image_from_file(path: Path) -> Image.Image:
        with Image.open(path) as image:
            return image.copy()

    def export_backup(self, target_dir: Path) -> None:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(self.storage_dir, target_dir)

    def restore_backup(self, source_dir: Path) -> None:
        if not (source_dir / INDEX_FILE).exists():
            raise ValueError("选择的文件夹不是有效备份，未找到 history_index.json。")
        temp_dir = self.storage_dir.with_name(f"{self.storage_dir.name}_restore_tmp_{uuid.uuid4().hex}")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        shutil.copytree(source_dir, temp_dir)
        if self.storage_dir.exists():
            shutil.rmtree(self.storage_dir)
        temp_dir.replace(self.storage_dir)
        self.ensure_dirs()
        self.load()

    def export_items(self, item_ids: Iterable[str], target_dir: Path) -> int:
        items = self.selected_items(item_ids)
        target_dir.mkdir(parents=True, exist_ok=False)
        images_dir = target_dir / IMAGE_DIR
        thumbs_dir = target_dir / THUMB_DIR
        images_dir.mkdir(exist_ok=True)
        thumbs_dir.mkdir(exist_ok=True)

        exported_items: list[dict[str, Any]] = []
        for item in items:
            data = asdict(item)
            if item.image_path:
                copied = self._copy_export_file(Path(item.image_path), images_dir)
                if copied:
                    data["image_path"] = str(Path(IMAGE_DIR) / copied.name)
            if item.thumb_path:
                copied = self._copy_export_file(Path(item.thumb_path), thumbs_dir)
                if copied:
                    data["thumb_path"] = str(Path(THUMB_DIR) / copied.name)
            exported_items.append(data)

        (target_dir / "selected_index.json").write_text(
            json.dumps({"items": exported_items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (target_dir / "selected_text.txt").write_text(
            build_items_summary(items),
            encoding="utf-8",
        )
        return len(items)

    @staticmethod
    def _copy_export_file(source: Path, target_dir: Path) -> Path | None:
        if not source.exists() or not source.is_file():
            return None
        target = target_dir / source.name
        shutil.copy2(source, target)
        return target


def normalize_search_text(value: str) -> str:
    normalized = value.casefold().strip()
    return re.sub(r"\s+", "", normalized)


def build_items_summary(items: Iterable[HistoryItem]) -> str:
    sections: list[str] = []
    for index, item in enumerate(items, start=1):
        header = f"[{index}] {'图片' if item.type == 'image' else '文字'} | {item.last_copied_at}"
        lines = [header]
        if item.type == "text":
            lines.append(item.text or "")
        else:
            if item.image_path:
                lines.append(f"图片路径：{item.image_path}")
            if item.source_name:
                lines.append(f"来源文件：{item.source_name}")
            if item.note:
                lines.append(f"备注：{item.note}")
            if item.ocr_text:
                lines.append(f"识别文字：{item.ocr_text}")
        sections.append("\n".join(lines).strip())
    return "\n\n".join(section for section in sections if section)
