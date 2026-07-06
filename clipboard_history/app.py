from __future__ import annotations

import io
import ctypes
import os
import platform
import shutil
import sys
import traceback
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from . import __version__
from .clipboard import (
    ClipboardAccessError,
    copy_image,
    copy_text,
    get_clipboard_sequence_number,
    read_image_clipboard,
    read_text_clipboard,
)
from .config import ConfigManager
from .ocr import OcrError, is_ocr_available, recognize_image_text
from .store import HistoryItem, HistoryStore, build_items_summary


APP_TITLE = "历史粘贴板"
POLL_MS = 1200
SAVE_DELAY_MS = 1200
RENDER_BATCH_SIZE = 14
THUMBNAIL_CACHE_LIMIT = 360
THUMBNAIL_PIL_CACHE_LIMIT = 360
VIRTUAL_PAGE_SIZE = 24
VIRTUAL_POOL_LIMIT = 36
THUMBNAIL_PRELOAD_AROUND = 18
PERF_LOG = os.environ.get("CLIPBOARD_HISTORY_PERF") == "1"
APP_RUN_REG_NAME = "HistoryClipboardPortable"
RETENTION_OPTIONS = ["1", "3", "5", "7", "30", "自定义"]
TYPE_LABELS = {"全部": "all", "文字": "text", "图片": "image"}
SORT_LABELS = {"时间降序": "desc", "时间升序": "asc"}
RETENTION_LABELS = {"1": 1, "3": 3, "5": 5, "7": 7, "30": 30, "自定义": -1}
MAX_IMAGE_LABELS = {"3MB": 3, "5MB": 5, "10MB": 10, "20MB": 20}
MAX_HISTORY_LABELS = {"100条": 100, "300条": 300, "500条": 500, "1000条": 1000}

COLORS = {
    "bg": "#eef7ff",
    "surface": "#ffffff",
    "surface_soft": "#f8fbff",
    "border": "#d7e7f5",
    "border_strong": "#a8c9e7",
    "primary": "#1e6fb8",
    "primary_dark": "#164b7c",
    "primary_soft": "#dff0ff",
    "text": "#1d2b3a",
    "muted": "#6f8296",
    "danger": "#b13f4a",
    "danger_soft": "#fff0f1",
}

FONT_NORMAL = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 18, "bold")
FONT_CARD_TITLE = ("Microsoft YaHei UI", 11, "bold")


@dataclass(frozen=True)
class CardSnapshot:
    item_id: str
    item_type: str
    pinned: bool
    selected: bool
    last_copied_at: str
    text_preview: str
    detail_text: str
    thumb_path: str
    source_name: str
    note: str
    ocr_text: str
    show_ocr: bool
    match_sources: tuple[str, ...]


def card_render_snapshot(item: HistoryItem, selected: bool, search: str, show_ocr: bool, match_sources: list[str] | None = None) -> CardSnapshot:
    text = (item.text or "").replace("\r", "")
    text_preview = text[:180] + ("..." if len(text) > 180 else "")
    detail_text = text[:260] + ("..." if len(text) > 260 else "")
    return CardSnapshot(
        item_id=item.id,
        item_type=item.type,
        pinned=item.pinned,
        selected=selected,
        last_copied_at=item.last_copied_at,
        text_preview=text_preview,
        detail_text=detail_text,
        thumb_path=item.thumb_path or "",
        source_name=item.source_name or "",
        note=item.note or "",
        ocr_text=item.ocr_text or "",
        show_ocr=show_ocr,
        match_sources=tuple(match_sources or []),
    )


def diff_item_order(old_ids: list[str], new_ids: list[str]) -> tuple[list[str], list[str], list[str]]:
    old_set = set(old_ids)
    new_set = set(new_ids)
    removed = [item_id for item_id in old_ids if item_id not in new_set]
    added = [item_id for item_id in new_ids if item_id not in old_set]
    kept = [item_id for item_id in new_ids if item_id in old_set]
    return added, removed, kept


def page_item_range(total: int, page_start: int, page_size: int = VIRTUAL_PAGE_SIZE) -> tuple[int, int]:
    if total <= 0:
        return 0, 0
    safe_page_size = max(1, page_size)
    max_start = max(0, total - safe_page_size)
    start = min(max(0, page_start), max_start)
    end = min(total, start + safe_page_size)
    return start, end


def parse_pause_until(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_pause_active(paused: bool, pause_until: str | None) -> bool:
    if not paused:
        return False
    until = parse_pause_until(pause_until)
    return until is None or until > datetime.now()


def looks_sensitive_text(text: str) -> bool:
    compact = "".join(text.split())
    if not compact:
        return False
    lower = compact.casefold()
    if any(token in lower for token in ("password=", "passwd=", "pwd=", "token=", "apikey=", "api_key=", "secret=")):
        return True
    if compact.isdigit() and 4 <= len(compact) <= 8:
        return True
    has_letter = any(ch.isalpha() for ch in compact)
    has_digit = any(ch.isdigit() for ch in compact)
    has_symbol = any(not ch.isalnum() for ch in compact)
    return 8 <= len(compact) <= 32 and has_letter and has_digit and has_symbol


def image_path_from_clipboard_text(text: str) -> Path | None:
    candidate = text.strip().strip('"')
    if not candidate or "\n" in candidate or "\r" in candidate:
        return None
    path = Path(candidate)
    if path.exists() and path.is_file() and HistoryStore.is_supported_image_path(path):
        return path
    return None


class _WinMsg(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_size_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class GlobalHotkeyManager:
    HOTKEY_ID = 0x4843
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008

    def __init__(self, hotkey: str, callback) -> None:
        self.hotkey = hotkey
        self.callback = callback
        self.thread: threading.Thread | None = None
        self.thread_id: int | None = None
        self.running = False
        self.last_error = ""

    def start(self) -> bool:
        self.last_error = ""
        if platform.system() != "Windows":
            self.last_error = "当前系统不支持"
            return False
        if self.running:
            return False
        parsed = self._parse_hotkey(self.hotkey)
        if parsed is None:
            self.last_error = "快捷键格式无效"
            return False
        modifiers, key_code = parsed
        ready = threading.Event()
        registered = {"ok": False}

        def worker() -> None:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            self.thread_id = kernel32.GetCurrentThreadId()
            if not user32.RegisterHotKey(None, self.HOTKEY_ID, modifiers, key_code):
                self.running = False
                self.last_error = "被占用或注册失败"
                ready.set()
                return
            self.running = True
            registered["ok"] = True
            ready.set()
            msg = _WinMsg()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                        self.callback()
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            finally:
                user32.UnregisterHotKey(None, self.HOTKEY_ID)
                self.running = False

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()
        ready.wait(0.8)
        if not registered["ok"] and not self.last_error:
            self.last_error = "注册超时"
        return registered["ok"]

    def stop(self) -> None:
        if platform.system() != "Windows" or self.thread_id is None:
            return
        try:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
        except Exception:
            pass

    @classmethod
    def _parse_hotkey(cls, value: str) -> tuple[int, int] | None:
        modifiers = 0
        key_code = 0
        key_map = {"ctrl": cls.MOD_CONTROL, "control": cls.MOD_CONTROL, "alt": cls.MOD_ALT, "shift": cls.MOD_SHIFT, "win": cls.MOD_WIN}
        for part in [segment.strip().casefold() for segment in value.split("+") if segment.strip()]:
            if part in key_map:
                modifiers |= key_map[part]
            elif len(part) == 1:
                key_code = ord(part.upper())
        if modifiers and key_code:
            return modifiers, key_code
        return None


class CardView:
    def __init__(self, app: "ClipboardHistoryApp", item: HistoryItem) -> None:
        self.app = app
        self.item_id = item.id
        self.snapshot: CardSnapshot | None = None
        self.preview_mode = ""
        self.detail_snapshot: tuple[str, str, str, bool, str] | None = None
        self.card = app._make_card_frame(item)
        self.card.columnconfigure(2, weight=1)
        self.card.bind("<Double-Button-1>", lambda _event: app.copy_item(self.item_id))

        self.select_var = tk.BooleanVar(value=False)
        self.selector = tk.Checkbutton(
            self.card,
            variable=self.select_var,
            command=lambda: app._on_selection_changed(self.item_id, self.select_var),
            bg=COLORS["surface"],
            activebackground=COLORS["surface"],
            selectcolor=COLORS["primary_soft"],
            cursor="hand2",
            takefocus=False,
        )
        self.selector.grid(row=0, column=0, rowspan=5, sticky="n", padx=(0, 10), pady=(4, 0))

        self.preview = tk.Frame(self.card, bg=COLORS["surface_soft"], width=216, height=136)
        self.preview.grid(row=0, column=1, rowspan=5, sticky="n", padx=(0, 18))
        self.preview.grid_propagate(False)
        self.preview.bind("<Double-Button-1>", lambda _event: app.copy_item(self.item_id))
        self.image_label = tk.Label(self.preview, bg=COLORS["surface_soft"], cursor="hand2")
        self.image_label.bind("<Double-Button-1>", lambda _event: app.open_image_viewer(self.item_id))
        self.preview_text = tk.Message(
            self.preview,
            text="",
            width=184,
            bg=COLORS["surface_soft"],
            fg=COLORS["text"],
            font=FONT_NORMAL,
        )
        self.preview_text.bind("<Double-Button-1>", lambda _event: app.copy_item(self.item_id))

        self.meta = tk.Frame(self.card, bg=COLORS["surface"])
        self.meta.grid(row=0, column=2, sticky="ew")
        self.meta.columnconfigure(99, weight=1)
        self.type_label = tk.Label(self.meta, bg=COLORS["primary_soft"], fg=COLORS["primary_dark"], font=FONT_SMALL, padx=9, pady=3)
        self.type_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.pin_label = tk.Label(self.meta, text="置顶", bg="#fff6dc", fg="#8a5a00", font=FONT_SMALL, padx=9, pady=3)
        self.ocr_label = tk.Label(self.meta, text="已识别", bg="#e8f8ec", fg="#287846", font=FONT_SMALL, padx=9, pady=3)
        self.date_label = tk.Label(self.meta, bg=COLORS["surface"], fg=COLORS["muted"], font=FONT_SMALL)
        self.date_label.grid(row=0, column=99, sticky="e")
        self.date_label.bind("<Double-Button-1>", lambda _event: app.copy_item(self.item_id))

        self.match_label = tk.Label(self.card, bg=COLORS["surface"], fg=COLORS["primary"], font=FONT_SMALL)
        self.match_label.bind("<Double-Button-1>", lambda _event: app.copy_item(self.item_id))

        self.detail_area = tk.Frame(self.card, bg=COLORS["surface"])
        self.detail_area.grid(row=2, column=2, sticky="ew", pady=(14, 14))
        self.detail_area.bind("<Double-Button-1>", lambda _event: app.copy_item(self.item_id))

        self.buttons = tk.Frame(self.card, bg=COLORS["surface"])
        self.buttons.grid(row=3, column=2, sticky="w")
        make_button(self.buttons, "复制", lambda: app.copy_item(self.item_id), primary=True).pack(side="left", padx=(0, 8))
        self.pin_button = make_button(self.buttons, "置顶", lambda: app.toggle_pin(self.item_id))
        self.pin_button.pack(side="left", padx=(0, 8))
        self.image_buttons: list[tk.Button] = []
        for text, command in (
            ("查看", lambda: app.open_image_viewer(self.item_id)),
            ("识别文字", lambda: app.open_ocr_placeholder(self.item_id)),
            ("备注", lambda: app.edit_note(self.item_id)),
        ):
            button = make_button(self.buttons, text, command)
            button.pack(side="left", padx=(0, 8))
            self.image_buttons.append(button)
        self.delete_button = make_button(self.buttons, "删除", lambda: app.delete_item(self.item_id), danger=True)
        self.delete_button.pack(side="left")
        self.update(item)

    def bind_item(self, item: HistoryItem) -> None:
        if self.item_id != item.id:
            self.app.selection_vars.pop(self.item_id, None)
            self.app.card_detail_frames.pop(self.item_id, None)
            self.item_id = item.id
            self.snapshot = None
            self.detail_snapshot = None
        self.app.selection_vars[item.id] = self.select_var
        self.app.card_detail_frames[item.id] = self.detail_area
        self.update(item)

    def update(self, item: HistoryItem) -> None:
        app = self.app
        if self.item_id != item.id:
            self.bind_item(item)
            return
        match_sources = app.store.match_sources(item, app.search_var.get()) if app.store else []
        snapshot = card_render_snapshot(item, item.id in app.selected_ids, app.search_var.get(), app.config.show_ocr_in_cards, match_sources)
        old = self.snapshot
        self.snapshot = snapshot

        if old is None or old.selected != snapshot.selected or old.pinned != snapshot.pinned:
            border_color = COLORS["primary"] if snapshot.selected else (COLORS["border_strong"] if snapshot.pinned else COLORS["border"])
            self.card.configure(highlightbackground=border_color)
            if self.select_var.get() != snapshot.selected:
                self.select_var.set(snapshot.selected)
        if old is None or old.item_type != snapshot.item_type:
            self.type_label.configure(
                text="图片" if item.type == "image" else "文字",
                bg=COLORS["primary_soft"] if item.type == "image" else "#edf5ff",
            )
        if old is None or old.pinned != snapshot.pinned:
            if snapshot.pinned:
                self.pin_label.grid(row=0, column=1, sticky="w", padx=(0, 10))
            else:
                self.pin_label.grid_remove()
            self.pin_button.configure(text="取消置顶" if snapshot.pinned else "置顶")
        if old is None or old.ocr_text != snapshot.ocr_text or old.item_type != snapshot.item_type:
            if item.type == "image" and snapshot.ocr_text:
                self.ocr_label.grid(row=0, column=2, sticky="w", padx=(0, 10))
            else:
                self.ocr_label.grid_remove()
        if old is None or old.item_type != snapshot.item_type:
            for button in self.image_buttons:
                if item.type == "image":
                    button.pack(side="left", padx=(0, 8), before=self.delete_button)
                else:
                    button.pack_forget()
        if old is None or old.last_copied_at != snapshot.last_copied_at:
            self.date_label.configure(text=f"最近复制  {format_timestamp(item.last_copied_at)}")
        if old is None or old.match_sources != snapshot.match_sources:
            if snapshot.match_sources:
                self.match_label.configure(text="命中：" + " / ".join(snapshot.match_sources))
                self.match_label.grid(row=1, column=2, sticky="w", pady=(10, 0))
            else:
                self.match_label.grid_remove()
        self._update_preview(item, snapshot, old)
        self._update_detail(item, snapshot)

    def _update_preview(self, item: HistoryItem, snapshot: CardSnapshot, old: CardSnapshot | None) -> None:
        if item.type == "image" and item.thumb_path and Path(item.thumb_path).exists():
            if self.preview_mode != "image" or old is None or old.thumb_path != snapshot.thumb_path:
                photo = self.app._get_thumbnail_photo(item.thumb_path)
                self.image_label.configure(image=photo)
                self.preview_text.place_forget()
                self.image_label.place(relx=0.5, rely=0.5, anchor="center")
                self.preview_mode = "image"
        else:
            if self.preview_mode != "text":
                self.image_label.place_forget()
                self.preview_text.place(relx=0.5, rely=0.5, anchor="center")
                self.preview_mode = "text"
            if old is None or old.text_preview != snapshot.text_preview:
                self.preview_text.configure(text=snapshot.text_preview)

    def _update_detail(self, item: HistoryItem, snapshot: CardSnapshot) -> None:
        detail_key = (snapshot.item_type, snapshot.detail_text, snapshot.note, snapshot.show_ocr, snapshot.ocr_text)
        if detail_key == self.detail_snapshot:
            return
        self.detail_snapshot = detail_key
        for child in self.detail_area.winfo_children():
            child.destroy()
        if item.type == "image":
            self.app._render_image_detail_area(self.detail_area, item)
            return
        detail_label = tk.Message(
            self.detail_area,
            text=snapshot.detail_text,
            width=760,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=FONT_NORMAL,
            justify="left",
        )
        detail_label.pack(anchor="w")
        detail_label.bind("<Double-Button-1>", lambda _event, item_id=item.id: self.app.copy_item(item_id))


class ClipboardHistoryApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.app_dir = app_base_dir()
        self.config_manager = ConfigManager(self.app_dir)
        self.config = self.config_manager.config
        self.store: HistoryStore | None = None
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="准备记录剪贴板")
        self.count_var = tk.StringVar(value="")
        self.page_var = tk.StringVar(value="")
        self.pause_button: tk.Menubutton | None = None
        self.ocr_display_button: tk.Button | None = None
        self.prev_page_button: tk.Button | None = None
        self.next_page_button: tk.Button | None = None
        self.last_seen_hash: str | None = None
        self.thumbnail_refs: list[ImageTk.PhotoImage] = []
        self.thumbnail_cache: OrderedDict[str, ImageTk.PhotoImage] = OrderedDict()
        self.thumbnail_pil_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self.thumbnail_preload_running = False
        self.thumbnail_preload_generation = 0
        self.selected_ids: set[str] = set()
        self.selection_vars: dict[str, tk.BooleanVar] = {}
        self.card_frames: dict[str, tk.Frame] = {}
        self.card_views: dict[str, "CardView"] = {}
        self.card_pool: list["CardView"] = []
        self.card_detail_frames: dict[str, tk.Frame] = {}
        self.visible_item_ids: list[str] = []
        self.rendered_range: tuple[int, int] = (0, 0)
        self.rendered_page_start = 0
        self.virtual_render_after_id: str | None = None
        self.scroll_sync_after_id: str | None = None
        self.render_generation = 0
        self.empty_state_frame: tk.Frame | None = None
        self.selection_count_var = tk.StringVar(value="已选 0 条")
        self.data_tools_expanded = False
        self.data_tools_panel: tk.Frame | None = None
        self.data_tools_button: tk.Button | None = None
        self.batch_bar_frame: tk.Frame | None = None
        self.max_image_dropdown: StableDropdown | None = None
        self.max_history_dropdown: StableDropdown | None = None
        self.refresh_pending = False
        self.search_after_id: str | None = None
        self.ocr_sync_generation = 0
        self.image_clipboard_worker_running = False
        self.copy_image_worker_running = False
        self.tray_icon = None
        self.tray_thread: threading.Thread | None = None
        self.is_quitting = False
        self.delayed_save_after_id: str | None = None
        self.pending_store_save = False
        self.last_clipboard_sequence: int | None = None
        self.clipboard_sequence_supported: bool | None = None
        self.perf_events: list[str] = []
        self.hotkey_manager: GlobalHotkeyManager | None = None
        self.hotkey_status = "未启动"
        self.restore_refresh_after_id: str | None = None
        self.window_is_iconic = False
        self.taskbar_restore_after_id: str | None = None

        self._setup_window()
        if not self._ensure_storage_dir():
            self.root.destroy()
            return
        self.store = HistoryStore(Path(self.config.storage_dir or ""))
        self._build_ui()
        self._prompt_cleanup_if_needed()
        self.refresh_items()
        self._start_global_hotkey()
        self.poll_clipboard()

    def _setup_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1180x760")
        self.root.minsize(940, 600)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.handle_close_request)
        self.root.bind("<Unmap>", self._on_root_unmap)
        self.root.bind("<Map>", self._on_root_map)
        icon_path = app_resource_path("assets", "app_icon.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass

        style = ttk.Style()
        style.theme_use("clam")

    def _on_root_unmap(self, event: tk.Event) -> None:
        if event.widget is not self.root or self.is_quitting:
            return
        try:
            state = self.root.state()
        except tk.TclError:
            return
        if state != "iconic":
            return
        started = time.perf_counter()
        self.window_is_iconic = True
        if self.virtual_render_after_id is not None:
            self.root.after_cancel(self.virtual_render_after_id)
            self.virtual_render_after_id = None
        self._release_all_rendered_cards(destroy=False)
        self._perf_log("taskbar_minimize", started)

    def _on_root_map(self, event: tk.Event) -> None:
        if event.widget is not self.root or self.is_quitting:
            return
        if not self.window_is_iconic:
            return
        self.window_is_iconic = False
        if self.taskbar_restore_after_id is not None:
            self.root.after_cancel(self.taskbar_restore_after_id)
        started = time.perf_counter()
        self.taskbar_restore_after_id = self.root.after(80, lambda: self._finish_taskbar_restore(started))

    def _finish_taskbar_restore(self, started: float) -> None:
        self.taskbar_restore_after_id = None
        self._schedule_virtual_render()
        self._perf_log("taskbar_restore", started)

    def handle_close_request(self) -> None:
        if self.is_quitting:
            return
        action = self._ask_close_action()
        if action == "tray":
            self.minimize_to_tray()
        elif action == "quit":
            self.quit_app()

    def _ask_close_action(self) -> str:
        dialog = tk.Toplevel(self.root)
        dialog.title("关闭方式")
        dialog.configure(bg=COLORS["bg"])
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        result = tk.StringVar(value="cancel")

        panel = tk.Frame(
            dialog,
            bg=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=22,
            pady=18,
        )
        panel.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(
            panel,
            text="要怎么关闭历史粘贴板？",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=FONT_CARD_TITLE,
        ).pack(anchor="w")
        tk.Label(
            panel,
            text="最小化到托盘后，软件会继续记录剪贴板；完全关闭后会停止记录。",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=FONT_SMALL,
            wraplength=360,
            justify="left",
        ).pack(anchor="w", pady=(8, 18))

        buttons = tk.Frame(panel, bg=COLORS["surface"])
        buttons.pack(fill="x")

        def choose(value: str) -> None:
            result.set(value)
            dialog.destroy()

        make_button(buttons, "最小化到托盘", lambda: choose("tray"), primary=True).pack(side="left", padx=(0, 8))
        make_button(buttons, "完全关闭", lambda: choose("quit"), danger=True).pack(side="left", padx=(0, 8))
        make_button(buttons, "取消", lambda: choose("cancel")).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max((self.root.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.root.winfo_rooty() + max((self.root.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")
        dialog.wait_window()
        return result.get()

    def minimize_to_tray(self) -> None:
        self.flush_pending_store_save()
        pystray = self._load_pystray()
        if pystray is None:
            messagebox.showwarning(
                APP_TITLE,
                "当前环境缺少系统托盘组件，将先改为普通最小化。\n\n"
                "打包发布版会包含托盘组件；开发环境可运行：python -m pip install -r requirements-dev.txt",
            )
            self.root.iconify()
            return
        if self.tray_icon is None:
            image = self._load_tray_image()
            menu = pystray.Menu(
                pystray.MenuItem("打开历史粘贴板", self._tray_restore, default=True),
                pystray.MenuItem("完全退出", self._tray_quit),
            )
            self.tray_icon = pystray.Icon("历史粘贴板", image, APP_TITLE, menu)
            self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            self.tray_thread.start()
        self.status_var.set("已最小化到系统托盘，仍会继续记录剪贴板。")
        self.root.withdraw()

    def _load_pystray(self):
        try:
            import pystray
        except ImportError:
            return None
        return pystray

    def _load_tray_image(self) -> Image.Image:
        for path in (
            app_resource_path("assets", "app_icon_preview.png"),
            app_resource_path("assets", "app_icon.ico"),
        ):
            if path.exists():
                try:
                    return Image.open(path).convert("RGBA")
                except OSError:
                    pass
        return Image.new("RGBA", (64, 64), COLORS["primary"])

    def _tray_restore(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.restore_from_tray)

    def _tray_quit(self, _icon=None, _item=None) -> None:
        self.root.after(0, self.quit_app)

    def restore_from_tray(self) -> None:
        self.show_main_window("restore_from_tray")

    def show_main_window(self, perf_label: str = "show_main_window") -> None:
        started = time.perf_counter()
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(120, lambda: self.root.attributes("-topmost", False))
            self.root.after(50, self.root.focus_force)
        except tk.TclError:
            return
        self._schedule_virtual_render()
        self._perf_log(perf_label, started)

    def quit_app(self) -> None:
        self.is_quitting = True
        self.flush_pending_store_save()
        self._stop_global_hotkey()
        self._stop_tray_icon()
        self.root.destroy()

    def _stop_tray_icon(self) -> None:
        icon = self.tray_icon
        self.tray_icon = None
        self.tray_thread = None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            pass

    def _stop_tray_icon_async(self) -> None:
        icon = self.tray_icon
        self.tray_icon = None
        self.tray_thread = None
        if icon is None:
            return

        def worker() -> None:
            try:
                icon.stop()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _ensure_storage_dir(self) -> bool:
        if self.config.storage_dir and Path(self.config.storage_dir).exists():
            return True
        messagebox.showinfo(APP_TITLE, "首次启动需要选择一个文件夹，用来保存剪贴板历史数据。")
        selected = filedialog.askdirectory(title="选择历史数据保存文件夹")
        if not selected:
            messagebox.showwarning(APP_TITLE, "未选择保存文件夹，程序已退出。")
            return False
        self.config.storage_dir = selected
        self.config_manager.save()
        return True

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True)

        toolbar = tk.Frame(shell, bg=COLORS["bg"], padx=24, pady=16)
        toolbar.pack(fill="x")
        toolbar.columnconfigure(1, weight=1)

        tk.Label(
            toolbar,
            text=APP_TITLE,
            bg=COLORS["bg"],
            fg=COLORS["primary_dark"],
            font=FONT_TITLE,
        ).grid(row=0, column=0, sticky="w", padx=(0, 22))

        search_panel = tk.Frame(toolbar, bg=COLORS["bg"])
        search_panel.grid(row=0, column=1, sticky="ew")
        search_panel.columnconfigure(1, weight=1)

        tk.Label(search_panel, text="搜索", bg=COLORS["bg"], fg=COLORS["muted"], font=FONT_SMALL).grid(
            row=0, column=0, sticky="e", padx=(0, 8)
        )
        search = tk.Entry(
            search_panel,
            textvariable=self.search_var,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            insertbackground=COLORS["primary"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["primary"],
            font=FONT_NORMAL,
        )
        search.grid(row=0, column=1, sticky="ew", ipady=8, padx=(0, 10))
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        make_button(search_panel, "清空", self.clear_search).grid(row=0, column=2, padx=(0, 10))

        self.sort_dropdown = StableDropdown(
            search_panel,
            [(label, value) for label, value in SORT_LABELS.items()],
            self.config.sort_order,
            self._save_view_options,
            width=92,
        )
        self.sort_dropdown.grid(row=0, column=3, padx=(0, 8))

        self.type_dropdown = StableDropdown(
            search_panel,
            [(label, value) for label, value in TYPE_LABELS.items()],
            self.config.type_filter,
            self._save_view_options,
            width=78,
        )
        self.type_dropdown.grid(row=0, column=4, padx=(0, 8))

        tk.Label(search_panel, text="保留", bg=COLORS["bg"], fg=COLORS["muted"], font=FONT_SMALL).grid(
            row=0, column=5, padx=(4, 4)
        )
        self.retention_dropdown = StableDropdown(
            search_panel,
            [(label, value) for label, value in RETENTION_LABELS.items()],
            self.config.retention_days,
            self._change_retention,
            width=76,
        )
        self.retention_dropdown.grid(row=0, column=6, padx=(0, 4))
        tk.Label(search_panel, text="天", bg=COLORS["bg"], fg=COLORS["muted"], font=FONT_SMALL).grid(
            row=0, column=7, padx=(0, 0)
        )

        quick_tools = tk.Frame(toolbar, bg=COLORS["bg"])
        quick_tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.pause_button = self._make_pause_menu(quick_tools)
        self.pause_button.pack(side="left", padx=(0, 8))
        self.ocr_display_button = make_button(quick_tools, self._ocr_display_button_text(), self.toggle_ocr_display)
        self.ocr_display_button.pack(side="left", padx=(0, 8))
        make_button(quick_tools, "打开数据文件夹", self.open_storage_folder).pack(side="left", padx=(0, 8))
        make_button(quick_tools, "设置", self.open_settings).pack(side="left", padx=(0, 8))
        self.data_tools_button = make_button(quick_tools, self._data_tools_button_text(), self.toggle_data_tools)
        self.data_tools_button.pack(side="left", padx=(0, 8))

        self.data_tools_panel = tk.Frame(toolbar, bg=COLORS["bg"])
        self.data_tools_panel.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        make_button(self.data_tools_panel, "导出备份", self.export_backup).pack(side="left", padx=(0, 8))
        make_button(self.data_tools_panel, "恢复备份", self.restore_backup).pack(side="left", padx=(0, 8))
        make_button(self.data_tools_panel, "更换保存位置", self.change_storage_dir).pack(side="left", padx=(0, 8))
        make_button(self.data_tools_panel, "状态诊断", self.open_diagnostics).pack(side="left", padx=(0, 8))
        make_button(self.data_tools_panel, "清理未置顶", self.clear_unpinned_items, danger=True).pack(side="left", padx=(0, 8))
        self._update_data_tools_visibility()

        self.batch_bar_frame = tk.Frame(toolbar, bg=COLORS["bg"])
        self.batch_bar_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        tk.Label(
            self.batch_bar_frame,
            textvariable=self.selection_count_var,
            bg=COLORS["bg"],
            fg=COLORS["primary_dark"],
            font=FONT_SMALL,
        ).pack(side="left", padx=(0, 12))
        make_button(self.batch_bar_frame, "全选当前结果", self.select_visible_items).pack(side="left", padx=(0, 8))
        make_button(self.batch_bar_frame, "取消选择", self.clear_selection).pack(side="left", padx=(0, 8))
        make_button(self.batch_bar_frame, "批量复制", self.copy_selected_items, primary=True).pack(side="left", padx=(0, 8))
        make_button(self.batch_bar_frame, "批量导出", self.export_selected_items).pack(side="left", padx=(0, 8))
        make_button(self.batch_bar_frame, "批量删除", self.delete_selected_items, danger=True).pack(side="left")
        self._update_batch_bar_visibility()

        body = tk.Frame(shell, bg=COLORS["bg"], padx=22)
        body.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self._on_scrollbar)
        self.list_frame = tk.Frame(self.canvas, bg=COLORS["bg"], padx=2, pady=4)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.list_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))

        footer = tk.Frame(shell, bg=COLORS["bg"], padx=22, pady=8)
        footer.pack(fill="x", pady=(0, 4))
        tk.Label(footer, textvariable=self.status_var, bg=COLORS["bg"], fg=COLORS["muted"], font=FONT_SMALL).pack(side="left")
        self.next_page_button = make_button(footer, "下一页", self.next_page)
        self.next_page_button.pack(side="right", padx=(8, 0))
        tk.Label(footer, textvariable=self.page_var, bg=COLORS["bg"], fg=COLORS["primary_dark"], font=FONT_SMALL).pack(side="right", padx=(8, 0))
        self.prev_page_button = make_button(footer, "上一页", self.previous_page)
        self.prev_page_button.pack(side="right", padx=(8, 0))
        tk.Label(footer, textvariable=self.count_var, bg=COLORS["bg"], fg=COLORS["primary_dark"], font=FONT_SMALL).pack(side="right")

    def _on_frame_configure(self, _event: tk.Event) -> None:
        if self.window_is_iconic:
            self._perf_log("scrollregion_skipped_iconic", time.perf_counter())
            return
        self._sync_canvas_scrollregion()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        self._schedule_virtual_render()

    def _on_mousewheel(self, event: tk.Event) -> None:
        direction = int(-1 * (event.delta / 120))
        first, last = self.canvas.yview()
        if direction > 0 and last >= 0.995 and self.can_go_next_page():
            self.next_page()
            return
        if direction < 0 and first <= 0.005 and self.can_go_previous_page():
            self.previous_page()
            return
        self.canvas.yview_scroll(direction, "units")

    def _on_scrollbar(self, *args: str) -> None:
        self.canvas.yview(*args)

    def _sync_canvas_scrollregion(self) -> None:
        if not hasattr(self, "canvas"):
            return
        bbox = self.canvas.bbox("all")
        if not bbox:
            self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), 0))
            self.canvas.yview_moveto(0)
            return
        self.canvas.configure(scrollregion=bbox)
        content_height = max(0, bbox[3] - bbox[1])
        viewport_height = max(1, self.canvas.winfo_height())
        if content_height <= viewport_height:
            self.canvas.yview_moveto(0)

    def _schedule_scrollregion_sync(self) -> None:
        if self.scroll_sync_after_id is not None:
            self.root.after_cancel(self.scroll_sync_after_id)
        self.scroll_sync_after_id = self.root.after_idle(self._run_scrollregion_sync)

    def _run_scrollregion_sync(self) -> None:
        self.scroll_sync_after_id = None
        self._sync_canvas_scrollregion()

    def can_go_previous_page(self) -> bool:
        return self.rendered_page_start > 0

    def can_go_next_page(self) -> bool:
        return bool(self.visible_item_ids) and self.rendered_page_start + VIRTUAL_PAGE_SIZE < len(self.visible_item_ids)

    def previous_page(self) -> None:
        if not self.can_go_previous_page():
            return
        self.rendered_page_start = max(0, self.rendered_page_start - VIRTUAL_PAGE_SIZE)
        self.canvas.yview_moveto(0)
        self._render_virtual_window("previous_page")

    def next_page(self) -> None:
        if not self.can_go_next_page():
            return
        max_start = max(0, len(self.visible_item_ids) - VIRTUAL_PAGE_SIZE)
        self.rendered_page_start = min(max_start, self.rendered_page_start + VIRTUAL_PAGE_SIZE)
        self.canvas.yview_moveto(0)
        self._render_virtual_window("next_page")

    def _update_page_controls(self) -> None:
        total = len(self.visible_item_ids)
        if total <= 0:
            self.page_var.set("")
        else:
            start, end = page_item_range(total, self.rendered_page_start, VIRTUAL_PAGE_SIZE)
            self.page_var.set(f"第 {start + 1}-{end} 条 / 共 {total} 条")
        if self.prev_page_button:
            self.prev_page_button.configure(state=("normal" if self.can_go_previous_page() else "disabled"))
        if self.next_page_button:
            self.next_page_button.configure(state=("normal" if self.can_go_next_page() else "disabled"))

    def schedule_refresh(self) -> None:
        if self.refresh_pending:
            return
        self.refresh_pending = True
        self.root.after_idle(self._run_scheduled_refresh)

    def _run_scheduled_refresh(self) -> None:
        self.refresh_pending = False
        started = time.perf_counter()
        self.refresh_items()
        self._perf_log("refresh_items", started)

    def _perf_log(self, label: str, started: float) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        event = f"{datetime.now().strftime('%H:%M:%S')} {label}: {elapsed_ms:.1f}ms"
        self.perf_events.append(event)
        if len(self.perf_events) > 80:
            self.perf_events = self.perf_events[-80:]
        if PERF_LOG:
            print(f"[perf] {label}: {elapsed_ms:.1f}ms")

    def _on_search_changed(self) -> None:
        if self.search_after_id is not None:
            self.root.after_cancel(self.search_after_id)
        self.search_after_id = self.root.after(180, self._run_delayed_search)

    def _run_delayed_search(self) -> None:
        self.search_after_id = None
        self.schedule_refresh()

    def _save_view_options(self) -> None:
        self.config.sort_order = self.sort_dropdown.get()
        self.config.type_filter = self.type_dropdown.get()
        self.config_manager.save()
        self.schedule_refresh()

    def _save_limits(self) -> None:
        if not self.max_image_dropdown or not self.max_history_dropdown:
            return
        self.update_limits(int(self.max_image_dropdown.get()), int(self.max_history_dropdown.get()))

    def update_limits(self, max_image_mb: int, max_history_items: int) -> None:
        self.config.max_image_mb = int(max_image_mb)
        self.config.max_history_items = int(max_history_items)
        self.config_manager.save()
        if self.store:
            deleted = self.store.enforce_limit(self.config.max_history_items)
            if deleted:
                self._drop_orphaned_thumbnail_cache()
                self.status_var.set(f"已按最大记录数清理 {deleted} 条未置顶旧记录。")
        self.schedule_refresh()

    def _change_retention(self) -> None:
        value = self.retention_dropdown.get()
        if value == -1:
            days = simpledialog.askinteger("自定义保存天数", "请输入保存天数：", minvalue=1, maxvalue=3650)
            if not days:
                self.retention_dropdown.set_value(self.config.retention_days)
                return
            self.retention_dropdown.set_options(
                list(RETENTION_LABELS.items()) + [(str(days), days)],
                days,
            )
            value = days
        self.config.retention_days = int(value)
        self.config_manager.save()
        self._prompt_cleanup_if_needed(force=True)

    def toggle_data_tools(self) -> None:
        self.data_tools_expanded = not self.data_tools_expanded
        if self.data_tools_button:
            self.data_tools_button.configure(text=self._data_tools_button_text())
        self._update_data_tools_visibility()

    def _data_tools_button_text(self) -> str:
        return "收起数据管理" if self.data_tools_expanded else "数据管理"

    def _make_pause_menu(self, parent: tk.Misc) -> tk.Menubutton:
        button = make_menu_button(parent, self._pause_button_text(), primary=self.config.paused)
        menu = tk.Menu(button, tearoff=False, bg=COLORS["surface"], fg=COLORS["text"], activebackground=COLORS["primary_soft"])
        menu.add_command(label="继续记录", command=self.resume_recording)
        menu.add_command(label="手动暂停", command=self.pause_until_manual)
        menu.add_separator()
        menu.add_command(label="暂停 5 分钟", command=lambda: self.pause_for_minutes(5))
        menu.add_command(label="暂停 30 分钟", command=lambda: self.pause_for_minutes(30))
        button.configure(menu=menu)
        return button

    def _pause_button_text(self) -> str:
        if self.is_recording_paused():
            until = parse_pause_until(self.config.pause_until)
            if until:
                return f"暂停至 {until.strftime('%H:%M')}"
            return "已暂停记录"
        return "正在记录"

    def _update_data_tools_visibility(self) -> None:
        if not self.data_tools_panel:
            return
        if self.data_tools_expanded:
            self.data_tools_panel.grid()
        else:
            self.data_tools_panel.grid_remove()

    def change_storage_dir(self) -> None:
        self.flush_pending_store_save()
        selected = filedialog.askdirectory(title="选择新的历史数据保存文件夹")
        if not selected:
            return
        self.config.storage_dir = selected
        self.config_manager.save()
        self.store = HistoryStore(Path(selected))
        self.last_seen_hash = None
        self.render_generation += 1
        self.thumbnail_cache.clear()
        self.thumbnail_pil_cache.clear()
        self._reset_virtual_cards()
        self.clear_selection()
        self.schedule_refresh()
        self.status_var.set(f"保存位置已切换：{selected}")

    def schedule_store_save(self) -> None:
        if not self.store:
            return
        self.pending_store_save = True
        if self.delayed_save_after_id is not None:
            self.root.after_cancel(self.delayed_save_after_id)
        self.delayed_save_after_id = self.root.after(SAVE_DELAY_MS, self.flush_pending_store_save)

    def flush_pending_store_save(self) -> None:
        if self.delayed_save_after_id is not None:
            try:
                self.root.after_cancel(self.delayed_save_after_id)
            except tk.TclError:
                pass
            self.delayed_save_after_id = None
        if self.pending_store_save and self.store:
            started = time.perf_counter()
            self.store.save()
            self.pending_store_save = False
            self._perf_log("store_flush", started)

    def clear_search(self) -> None:
        self.search_var.set("")

    def _on_selection_changed(self, item_id: str, variable: tk.BooleanVar) -> None:
        if variable.get():
            self.selected_ids.add(item_id)
        else:
            self.selected_ids.discard(item_id)
        self._update_selection_count()
        self._sync_card_selection_style(item_id)

    def _update_selection_count(self) -> None:
        self.selection_count_var.set(f"已选 {len(self.selected_ids)} 条")
        self._update_batch_bar_visibility()

    def _update_batch_bar_visibility(self) -> None:
        if not self.batch_bar_frame:
            return
        if self.selected_ids:
            self.batch_bar_frame.grid()
        else:
            self.batch_bar_frame.grid_remove()

    def select_visible_items(self) -> None:
        self.selected_ids.update(self.visible_item_ids)
        self._update_selection_count()
        self._sync_selection_widgets()

    def clear_selection(self) -> None:
        self.selected_ids.clear()
        self._update_selection_count()
        self._sync_selection_widgets()

    def _sync_selection_widgets(self) -> None:
        for item_id, variable in self.selection_vars.items():
            variable.set(item_id in self.selected_ids)
            self._sync_card_selection_style(item_id)

    def _sync_card_selection_style(self, item_id: str) -> None:
        card = self.card_frames.get(item_id)
        if not card:
            return
        item = self.store.get(item_id) if self.store else None
        if item_id in self.selected_ids:
            border_color = COLORS["primary"]
        elif item and item.pinned:
            border_color = COLORS["border_strong"]
        else:
            border_color = COLORS["border"]
        card.configure(highlightbackground=border_color)

    def _selected_items(self) -> list[HistoryItem]:
        if not self.store:
            return []
        return self.store.selected_items(self.selected_ids)

    def copy_selected_items(self) -> None:
        items = self._selected_items()
        if not items:
            self.status_var.set("请先勾选要复制的记录。")
            return
        summary = build_items_summary(items)
        if not summary:
            self.status_var.set("所选记录没有可复制的文字内容。")
            return
        try:
            copy_text(self.root, summary)
        except ClipboardAccessError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.status_var.set(f"已复制 {len(items)} 条记录的文字汇总。")

    def export_selected_items(self) -> None:
        if not self.store:
            return
        items = self._selected_items()
        if not items:
            self.status_var.set("请先勾选要导出的记录。")
            return
        selected = filedialog.askdirectory(title="选择批量导出保存位置")
        if not selected:
            return
        target = Path(selected) / f"历史粘贴板选中导出_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            count = self.store.export_items((item.id for item in items), target)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"批量导出失败：{exc}")
            return
        self.status_var.set(f"已导出 {count} 条选中记录：{target}")

    def delete_selected_items(self) -> None:
        if not self.store:
            return
        items = self._selected_items()
        if not items:
            self.status_var.set("请先勾选要删除的记录。")
            return
        if not messagebox.askyesno(APP_TITLE, f"确定删除选中的 {len(items)} 条历史记录吗？此操作不可撤销。"):
            return
        self.render_generation += 1
        for item in items:
            self._drop_thumbnail_cache(item)
        self.store.delete_many(item.id for item in items)
        self.selected_ids.difference_update(item.id for item in items)
        self._update_selection_count()
        self.refresh_items()
        self.status_var.set(f"已删除 {len(items)} 条选中记录。")

    def toggle_pause(self) -> None:
        self.config.paused = not self.is_recording_paused()
        if not self.config.paused:
            self.config.pause_until = None
        else:
            self.config.pause_until = None
        self.config_manager.save()
        self._update_pause_button()
        self.status_var.set("已暂停记录剪贴板。" if self.config.paused else "已继续记录剪贴板。")

    def pause_until_manual(self) -> None:
        self.config.paused = True
        self.config.pause_until = None
        self.config_manager.save()
        self._update_pause_button()
        self.status_var.set("已暂停记录剪贴板。")

    def resume_recording(self) -> None:
        self.config.paused = False
        self.config.pause_until = None
        self.config_manager.save()
        self._update_pause_button()
        self.status_var.set("已继续记录剪贴板。")

    def pause_for_minutes(self, minutes: int) -> None:
        self.config.paused = True
        self.config.pause_until = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self.config_manager.save()
        self._update_pause_button()
        self.status_var.set(f"已暂停记录 {minutes} 分钟，到时间会自动恢复。")

    def is_recording_paused(self) -> bool:
        active = is_pause_active(self.config.paused, self.config.pause_until)
        if self.config.paused and not active:
            self.config.paused = False
            self.config.pause_until = None
            self.config_manager.save()
            self._update_pause_button()
        return active

    def _update_pause_button(self) -> None:
        if not self.pause_button:
            return
        self.pause_button.configure(text=self._pause_button_text())

    def toggle_ocr_display(self) -> None:
        self.config.show_ocr_in_cards = not self.config.show_ocr_in_cards
        self.config_manager.save()
        if self.ocr_display_button:
            self.ocr_display_button.configure(text=self._ocr_display_button_text())
        self.status_var.set("已在卡片中显示识别结果预览。" if self.config.show_ocr_in_cards else "已隐藏卡片中的识别结果。")
        self._schedule_ocr_display_sync()

    def _ocr_display_button_text(self) -> str:
        return "隐藏识别结果" if self.config.show_ocr_in_cards else "显示识别结果"

    def _hotkey_button_text(self) -> str:
        return "快捷键：已开启" if self.config.global_hotkey_enabled else "快捷键：已关闭"

    def hotkey_status_text(self) -> str:
        if not self.config.global_hotkey_enabled:
            return "已关闭"
        return self.hotkey_status

    def _startup_button_text(self) -> str:
        return "关闭开机自启" if self.config.start_with_windows else "开启开机自启"

    def _sensitive_button_text(self) -> str:
        return "关闭敏感过滤" if self.config.sensitive_filter_enabled else "开启敏感过滤"

    def toggle_global_hotkey(self) -> None:
        self.config.global_hotkey_enabled = not self.config.global_hotkey_enabled
        self.config_manager.save()
        if self.config.global_hotkey_enabled:
            self._start_global_hotkey()
        else:
            self._stop_global_hotkey()
        if self.config.global_hotkey_enabled:
            self.status_var.set(f"快捷键 Ctrl+Alt+V 用于呼出窗口；当前状态：{self.hotkey_status_text()}。")
        else:
            self.status_var.set("已关闭全局快捷键。")

    def _start_global_hotkey(self) -> None:
        if not self.config.global_hotkey_enabled or self.hotkey_manager is not None:
            return
        manager = GlobalHotkeyManager(self.config.global_hotkey, lambda: self.root.after(0, lambda: self.show_main_window("hotkey_restore")))
        if manager.start():
            self.hotkey_manager = manager
            self.hotkey_status = "可用"
        else:
            self.hotkey_status = manager.last_error or "被占用或注册失败"
            self.status_var.set(f"快捷键 Ctrl+Alt+V 未启用：{self.hotkey_status}。可在设置中关闭或改用托盘打开。")

    def _stop_global_hotkey(self) -> None:
        manager = self.hotkey_manager
        self.hotkey_manager = None
        self.hotkey_status = "已关闭"
        if manager:
            manager.stop()

    def toggle_start_with_windows(self) -> None:
        target = not self.config.start_with_windows
        try:
            set_start_with_windows(target)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"设置开机自启失败：{exc}")
            return
        self.config.start_with_windows = target
        self.config_manager.save()
        self.status_var.set("已开启开机自启。" if target else "已关闭开机自启。")

    def toggle_sensitive_filter(self) -> None:
        self.config.sensitive_filter_enabled = not self.config.sensitive_filter_enabled
        self.config_manager.save()
        self.status_var.set("已开启疑似敏感文字过滤。" if self.config.sensitive_filter_enabled else "已关闭疑似敏感文字过滤。")

    def open_storage_folder(self) -> None:
        if not self.config.storage_dir:
            return
        path = Path(self.config.storage_dir)
        if not path.exists():
            messagebox.showwarning(APP_TITLE, "保存文件夹不存在。")
            return
        os.startfile(path)

    def clear_unpinned_items(self) -> None:
        if not self.store:
            return
        count = len([item for item in self.store.items if not item.pinned])
        if count == 0:
            self.status_var.set("没有可清理的未置顶记录。")
            return
        if not messagebox.askyesno(APP_TITLE, f"确定清理 {count} 条未置顶记录吗？置顶记录会保留。"):
            return
        self.render_generation += 1
        removed_items = [item for item in self.store.items if not item.pinned]
        for item in removed_items:
            self._drop_thumbnail_cache(item)
        deleted = self.store.clear_unpinned()
        self.selected_ids.intersection_update({item.id for item in self.store.items})
        self._update_selection_count()
        self.refresh_items()
        self.status_var.set(f"已清理 {deleted} 条未置顶记录。")

    def export_backup(self) -> None:
        if not self.config.storage_dir:
            return
        self.flush_pending_store_save()
        selected = filedialog.askdirectory(title="选择备份保存位置")
        if not selected:
            return
        source = Path(self.config.storage_dir)
        target = Path(selected) / f"历史粘贴板备份_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copytree(source, target)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"导出备份失败：{exc}")
            return
        self.status_var.set(f"已导出备份：{target}")

    def restore_backup(self) -> None:
        if not self.config.storage_dir or not self.store:
            return
        self.flush_pending_store_save()
        selected = filedialog.askdirectory(title="选择要恢复的备份文件夹")
        if not selected:
            return
        source = Path(selected)
        if not (source / "history_index.json").exists():
            messagebox.showwarning(APP_TITLE, "选择的文件夹不是有效备份，未找到 history_index.json。")
            return
        if not messagebox.askyesno(APP_TITLE, "恢复备份会替换当前历史数据。软件会先自动备份当前数据，确定继续吗？"):
            return
        current = Path(self.config.storage_dir)
        safety_backup = current.parent / f"历史粘贴板恢复前备份_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copytree(current, safety_backup)
            self.store.restore_backup(source)
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, f"恢复备份失败：{exc}")
            return
        self.render_generation += 1
        self.thumbnail_cache.clear()
        self.thumbnail_pil_cache.clear()
        self._reset_virtual_cards()
        self.clear_selection()
        self.schedule_refresh()
        self.status_var.set(f"已恢复备份，恢复前数据已备份到：{safety_backup}")

    def open_diagnostics(self) -> None:
        DiagnosticsDialog(self)

    def open_settings(self) -> None:
        SettingsDialog(self)

    def poll_clipboard(self) -> None:
        if self.is_recording_paused():
            self.root.after(POLL_MS, self.poll_clipboard)
            return
        if self.store:
            sequence = get_clipboard_sequence_number()
            if sequence is not None:
                self.clipboard_sequence_supported = True
                if self.last_clipboard_sequence == sequence:
                    self.root.after(POLL_MS, self.poll_clipboard)
                    return
                self.last_clipboard_sequence = sequence
            else:
                self.clipboard_sequence_supported = False
            started = time.perf_counter()
            text_result = read_text_clipboard(self.root)
            if text_result:
                _kind, payload = text_result
                image_path = image_path_from_clipboard_text(payload)
                if image_path:
                    if self.image_clipboard_worker_running:
                        self.last_clipboard_sequence = None
                        self.root.after(POLL_MS, self.poll_clipboard)
                        return
                    self.image_clipboard_worker_running = True
                    threading.Thread(target=self._poll_image_file_worker, args=(image_path, started), daemon=True).start()
                    self.root.after(POLL_MS, self.poll_clipboard)
                    return
                if self.config.sensitive_filter_enabled and looks_sensitive_text(payload):
                    self.status_var.set("已忽略疑似敏感文字。")
                    self.last_seen_hash = self.store.hash_text(payload)
                    self._perf_log("clipboard_sensitive_skip", started)
                    self.root.after(POLL_MS, self.poll_clipboard)
                    return
                content_hash = self.store.hash_text(payload)
                if content_hash != self.last_seen_hash:
                    item = self.store.add_text(payload)
                    self.last_seen_hash = content_hash
                    if item:
                        self.status_var.set("已记录文字")
                        self._after_store_changed(new_or_updated_id=item.id)
                        self._perf_log("clipboard_text", started)
            elif not self.image_clipboard_worker_running:
                self.image_clipboard_worker_running = True
                threading.Thread(target=self._poll_image_clipboard_worker, daemon=True).start()
        self.root.after(POLL_MS, self.poll_clipboard)

    def _poll_image_file_worker(self, path: Path, started: float | None = None) -> None:
        perf_started = started or time.perf_counter()
        try:
            image = HistoryStore.load_image_from_file(path)
            content_hash = HistoryStore.hash_image(image)
            image_mb = image_size_mb(image)
            self.root.after(
                0,
                lambda: self._handle_image_clipboard_result(image, path.name, content_hash, image_mb, perf_started),
            )
        except Exception as exc:
            message = f"读取图片文件失败：{exc}"
            self.root.after(0, lambda: self._finish_image_clipboard_worker(message, perf_started, "clipboard_image_file_error"))

    def _poll_image_clipboard_worker(self) -> None:
        started = time.perf_counter()
        try:
            result = read_image_clipboard()
            if not result:
                self.root.after(0, lambda: self._finish_image_clipboard_worker(perf_started=started, perf_label="clipboard_image_empty"))
                return
            _kind, payload = result
            image, source_name = payload
            content_hash = HistoryStore.hash_image(image)
            image_mb = image_size_mb(image)
            self.root.after(0, lambda: self._handle_image_clipboard_result(image, source_name, content_hash, image_mb, started))
        except Exception as exc:
            message = f"读取图片剪贴板失败：{exc}"
            self.root.after(0, lambda: self._finish_image_clipboard_worker(message, started, "clipboard_image_error"))

    def _handle_image_clipboard_result(self, image: Image.Image, source_name: str | None, content_hash: str, image_mb: float, perf_started: float | None = None) -> None:
        try:
            if not self.store:
                return
            if content_hash == self.last_seen_hash:
                return
            if image_mb > self.config.max_image_mb:
                self.last_seen_hash = content_hash
                self.status_var.set(f"已忽略 {image_mb:.1f}MB 图片，超过 {self.config.max_image_mb}MB 上限。")
                return
            item = self.store.add_image(image, source_name, content_hash=content_hash)
            self.last_seen_hash = content_hash
            self.status_var.set("已记录图片")
            self._after_store_changed(new_or_updated_id=item.id)
        finally:
            self._finish_image_clipboard_worker(perf_started=perf_started, perf_label="clipboard_image")

    def _finish_image_clipboard_worker(self, message: str | None = None, perf_started: float | None = None, perf_label: str | None = None) -> None:
        self.image_clipboard_worker_running = False
        if message:
            self.status_var.set(message)
        if perf_started is not None and perf_label:
            self._perf_log(perf_label, perf_started)

    def _after_store_changed(self, new_or_updated_id: str | None = None) -> None:
        if not self.store:
            return
        deleted = self.store.enforce_limit(self.config.max_history_items)
        if deleted:
            self._drop_orphaned_thumbnail_cache()
            self.status_var.set(f"已自动清理 {deleted} 条未置顶旧记录。")
            self.schedule_refresh()
        elif new_or_updated_id:
            self._sync_single_item_card(new_or_updated_id)
        else:
            self.schedule_refresh()

    def refresh_items(self) -> None:
        if not self.store:
            return
        self.render_generation += 1
        items = self.store.query(
            self.search_var.get(),
            self.type_dropdown.get(),
            self.sort_dropdown.get(),
        )
        self.visible_item_ids = [item.id for item in items]
        existing_ids = {item.id for item in self.store.items}
        self.selected_ids.intersection_update(existing_ids)
        for item_id in list(self.selection_vars):
            if item_id not in existing_ids:
                self.selection_vars.pop(item_id, None)
        self._update_selection_count()
        total = len(self.store.items)
        keyword = self.search_var.get().strip()
        suffix = f" | 搜索：{keyword}" if keyword else ""
        self.count_var.set(f"显示 {len(items)} 条 / 共 {total} 条{suffix}")
        if not items:
            self._release_all_rendered_cards()
            self.rendered_range = (0, 0)
            self._update_page_controls()
            self._render_empty_state()
            return
        self._hide_empty_state()
        self.rendered_page_start = 0
        self.canvas.yview_moveto(0)
        self._render_virtual_window("refresh")

    def _render_item_batch(self, items: list[HistoryItem], start: int, generation: int) -> None:
        self._render_virtual_window("legacy_batch")

    def _schedule_virtual_render(self, reason: str = "scheduled") -> None:
        if self.window_is_iconic:
            self._perf_log("virtual_render_skipped_iconic", time.perf_counter())
            return
        if self.virtual_render_after_id is not None:
            return
        self.virtual_render_after_id = self.root.after_idle(lambda: self._run_virtual_render(reason))

    def _run_virtual_render(self, reason: str) -> None:
        self.virtual_render_after_id = None
        self._render_virtual_window(reason)

    def _render_virtual_window(self, reason: str = "virtual") -> None:
        if self.window_is_iconic:
            self._perf_log(f"virtual_render_skipped_iconic:{reason}", time.perf_counter())
            return
        if not self.store or not self.visible_item_ids:
            return
        started = time.perf_counter()
        total = len(self.visible_item_ids)
        start, end = page_item_range(total, self.rendered_page_start, VIRTUAL_PAGE_SIZE)
        self.rendered_page_start = start
        target_ids = self.visible_item_ids[start:end]
        target_set = set(target_ids)
        for item_id in list(self.card_views):
            if item_id not in target_set:
                self._release_card_view(item_id, destroy=False)
        for item_id in target_ids:
            item = self.store.get(item_id)
            if not item:
                continue
            card = self._render_card(item)
            card.pack_forget()
            card.pack(fill="x", pady=8)
        self.rendered_range = (start, end)
        self._schedule_scrollregion_sync()
        self._update_page_controls()
        self._schedule_thumbnail_preload(start, end)
        self._perf_log(f"page_render:{reason}:{start}-{end}/{total}", started)

    def _update_count_label(self) -> None:
        if not self.store:
            return
        total = len(self.store.items)
        keyword = self.search_var.get().strip()
        suffix = f" | 搜索：{keyword}" if keyword else ""
        self.count_var.set(f"显示 {len(self.visible_item_ids)} 条 / 共 {total} 条{suffix}")

    def _query_visible_items(self) -> list[HistoryItem]:
        if not self.store:
            return []
        return self.store.query(
            self.search_var.get(),
            self.type_dropdown.get(),
            self.sort_dropdown.get(),
        )

    def _sync_single_item_card(self, item_id: str) -> None:
        if not self.store:
            return
        items = self._query_visible_items()
        item_map = {item.id: item for item in items}
        new_ids = [item.id for item in items]
        if item_id not in item_map:
            self._release_card_view(item_id, destroy=False)
            if item_id in self.visible_item_ids:
                self.visible_item_ids.remove(item_id)
            self._update_count_label()
            self._render_virtual_window("single_removed")
            return
        self.visible_item_ids = new_ids
        view = self.card_views.get(item_id)
        if view:
            view.update(item_map[item_id])
        elif item_id in self.visible_item_ids:
            index = self.visible_item_ids.index(item_id)
            if not (self.rendered_page_start <= index < self.rendered_page_start + VIRTUAL_PAGE_SIZE):
                self.rendered_page_start = max(0, min(index, max(0, len(self.visible_item_ids) - VIRTUAL_PAGE_SIZE)))
                self.canvas.yview_moveto(0)
        self._update_count_label()
        self._render_virtual_window("single_sync")

    def _pack_card_in_current_order(self, item_id: str) -> None:
        self._render_virtual_window("pack_order")

    def _remove_card_view(self, item_id: str, destroy: bool = True) -> None:
        view = self.card_views.pop(item_id, None)
        card = self.card_frames.pop(item_id, None)
        self.card_detail_frames.pop(item_id, None)
        self.selection_vars.pop(item_id, None)
        if card:
            if destroy:
                card.destroy()
            else:
                card.pack_forget()
        if view and not destroy:
            self.card_pool.append(view)
            self._trim_card_pool()
        if item_id in self.visible_item_ids:
            self.visible_item_ids.remove(item_id)

    def _release_card_view(self, item_id: str, destroy: bool = False) -> None:
        view = self.card_views.pop(item_id, None)
        self.card_frames.pop(item_id, None)
        self.card_detail_frames.pop(item_id, None)
        self.selection_vars.pop(item_id, None)
        if not view:
            return
        view.card.pack_forget()
        if destroy:
            view.card.destroy()
        else:
            self.card_pool.append(view)
            self._trim_card_pool()

    def _trim_card_pool(self) -> None:
        while len(self.card_pool) > VIRTUAL_POOL_LIMIT:
            view = self.card_pool.pop(0)
            view.card.destroy()

    def _release_all_rendered_cards(self, destroy: bool = False) -> None:
        for item_id in list(self.card_views):
            self._release_card_view(item_id, destroy=destroy)

    def _reset_virtual_cards(self) -> None:
        self._release_all_rendered_cards(destroy=True)
        for view in self.card_pool:
            view.card.destroy()
        self.card_pool = []
        self.card_frames = {}
        self.card_views = {}
        self.card_detail_frames = {}
        self.selection_vars = {}
        self.rendered_range = (0, 0)
        self.rendered_page_start = 0

    def _hide_empty_state(self) -> None:
        if self.empty_state_frame:
            self.empty_state_frame.pack_forget()

    def _render_empty_state(self) -> None:
        if self.empty_state_frame:
            for child in self.empty_state_frame.winfo_children():
                child.destroy()
            empty = self.empty_state_frame
            empty.pack(fill="x", pady=12)
        else:
            empty = tk.Frame(
                self.list_frame,
                bg=COLORS["surface"],
                highlightbackground=COLORS["border"],
                highlightthickness=1,
                padx=28,
                pady=28,
            )
            self.empty_state_frame = empty
            empty.pack(fill="x", pady=12)
        self.canvas.yview_moveto(0)
        self.root.after_idle(self._sync_canvas_scrollregion)
        tk.Label(
            empty,
            text="暂无匹配记录",
            bg=COLORS["surface"],
            fg=COLORS["primary_dark"],
            font=FONT_CARD_TITLE,
        ).pack(anchor="w")
        tk.Label(
            empty,
            text=self.empty_state_message(),
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=FONT_NORMAL,
            pady=8,
        ).pack(anchor="w")

    def _make_card_frame(self, item: HistoryItem) -> tk.Frame:
        is_selected = item.id in self.selected_ids
        border_color = COLORS["primary"] if is_selected else (COLORS["border_strong"] if item.pinned else COLORS["border"])
        empty = tk.Frame(
            self.list_frame,
            bg=COLORS["surface"],
            highlightbackground=border_color,
            highlightthickness=1,
            padx=16,
            pady=14,
        )
        return empty

    def empty_state_message(self) -> str:
        keyword = self.search_var.get().strip()
        if keyword:
            return f"没有找到包含“{keyword}”的记录。可以清空搜索，或确认类型筛选是否正确。"
        return "复制文字或图片后，这里会自动出现历史卡片。也可以调整搜索词或筛选条件。"

    def _render_card(self, item: HistoryItem) -> tk.Frame:
        view = self.card_views.get(item.id)
        if view is None:
            if self.card_pool:
                view = self.card_pool.pop()
                view.bind_item(item)
            else:
                view = CardView(self, item)
            self.card_views[item.id] = view
            self.card_frames[item.id] = view.card
            self.selection_vars[item.id] = view.select_var
            self.card_detail_frames[item.id] = view.detail_area
        else:
            view.update(item)
        return view.card

    def _get_thumbnail_photo(self, thumb_path: str) -> ImageTk.PhotoImage:
        key = self._thumbnail_cache_key(thumb_path)
        cached = self.thumbnail_cache.get(key)
        if cached is not None:
            self.thumbnail_cache.move_to_end(key)
            return cached
        pil_cached = self.thumbnail_pil_cache.get(key)
        if pil_cached is not None:
            self.thumbnail_pil_cache.move_to_end(key)
            photo = ImageTk.PhotoImage(pil_cached)
        else:
            photo = ImageTk.PhotoImage(file=thumb_path)
        self.thumbnail_cache[key] = photo
        while len(self.thumbnail_cache) > THUMBNAIL_CACHE_LIMIT:
            self.thumbnail_cache.popitem(last=False)
        return photo

    def _thumbnail_cache_key(self, thumb_path: str) -> str:
        path = Path(thumb_path)
        try:
            stat = path.stat()
            return f"{path}|{stat.st_mtime_ns}|{stat.st_size}"
        except OSError:
            return str(path)

    def _schedule_thumbnail_preload(self, start: int, end: int) -> None:
        if not self.store:
            return
        total = len(self.visible_item_ids)
        preload_start = max(0, start - THUMBNAIL_PRELOAD_AROUND)
        preload_end = min(total, end + THUMBNAIL_PRELOAD_AROUND)
        paths: list[str] = []
        for item_id in self.visible_item_ids[preload_start:preload_end]:
            item = self.store.get(item_id)
            if item and item.type == "image" and item.thumb_path:
                key = self._thumbnail_cache_key(item.thumb_path)
                if key not in self.thumbnail_cache and key not in self.thumbnail_pil_cache:
                    paths.append(item.thumb_path)
        if not paths:
            return
        self.thumbnail_preload_generation += 1
        generation = self.thumbnail_preload_generation
        self.thumbnail_preload_running = True
        started = time.perf_counter()

        def worker() -> None:
            loaded: list[tuple[str, Image.Image]] = []
            for path_text in paths[:80]:
                path = Path(path_text)
                try:
                    with Image.open(path) as image:
                        loaded.append((self._thumbnail_cache_key(path_text), image.convert("RGBA")))
                except OSError:
                    continue
            self.root.after(0, lambda: self._finish_thumbnail_preload(generation, loaded, started))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_thumbnail_preload(self, generation: int, loaded: list[tuple[str, Image.Image]], started: float) -> None:
        if generation != self.thumbnail_preload_generation:
            return
        self.thumbnail_preload_running = False
        for key, image in loaded:
            self.thumbnail_pil_cache[key] = image
            self.thumbnail_pil_cache.move_to_end(key)
        while len(self.thumbnail_pil_cache) > THUMBNAIL_PIL_CACHE_LIMIT:
            self.thumbnail_pil_cache.popitem(last=False)
        self._perf_log(f"thumbnail_preload:{len(loaded)}", started)

    def _drop_thumbnail_cache(self, item: HistoryItem | None) -> None:
        if not item or not item.thumb_path:
            return
        prefix = str(Path(item.thumb_path))
        for key in [key for key in self.thumbnail_cache if key.startswith(prefix)]:
            self.thumbnail_cache.pop(key, None)
        for key in [key for key in self.thumbnail_pil_cache if key.startswith(prefix)]:
            self.thumbnail_pil_cache.pop(key, None)

    def _drop_orphaned_thumbnail_cache(self) -> None:
        if not self.store:
            self.thumbnail_cache.clear()
            self.thumbnail_pil_cache.clear()
            return
        live_prefixes = {str(Path(item.thumb_path)) for item in self.store.items if item.thumb_path}
        for key in [key for key in self.thumbnail_cache if not any(key.startswith(prefix) for prefix in live_prefixes)]:
            self.thumbnail_cache.pop(key, None)
        for key in [key for key in self.thumbnail_pil_cache if not any(key.startswith(prefix) for prefix in live_prefixes)]:
            self.thumbnail_pil_cache.pop(key, None)

    def _render_info_section(
        self,
        parent: tk.Frame,
        title: str,
        content: str,
        item_id: str,
        muted: bool = False,
        limit: int = 220,
    ) -> None:
        section = tk.Frame(
            parent,
            bg=COLORS["surface_soft"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=12,
            pady=8,
        )
        section.pack(anchor="w", fill="x", pady=(0, 8))
        section.bind("<Double-Button-1>", lambda _event, selected_id=item_id: self.copy_item(selected_id))
        title_label = tk.Label(
            section,
            text=title,
            bg=COLORS["surface_soft"],
            fg=COLORS["primary_dark"] if not muted else COLORS["muted"],
            font=FONT_SMALL,
        )
        title_label.pack(anchor="w")
        title_label.bind("<Double-Button-1>", lambda _event, selected_id=item_id: self.copy_item(selected_id))
        text = content.strip()
        display = text[:limit] + ("..." if len(text) > limit else "")
        content_label = tk.Message(
            section,
            text=display,
            width=760,
            bg=COLORS["surface_soft"],
            fg=COLORS["muted"] if muted else COLORS["text"],
            font=FONT_NORMAL,
            justify="left",
        )
        content_label.pack(anchor="w", pady=(4, 0))
        content_label.bind("<Double-Button-1>", lambda _event, selected_id=item_id: self.copy_item(selected_id))

    def _render_image_detail_area(self, detail_area: tk.Frame, item: HistoryItem) -> None:
        has_note = bool(item.note.strip())
        has_ocr = bool(item.ocr_text.strip())
        if has_note:
            self._render_info_section(detail_area, "备注", item.note, item.id)
        if has_ocr and self.config.show_ocr_in_cards:
            self._render_info_section(detail_area, "识别结果预览", item.ocr_text, item.id, limit=160)
        if not has_note and (not has_ocr or not self.config.show_ocr_in_cards):
            placeholder = item.source_name or "图片记录。双击缩略图可放大查看，也可以添加备注或识别文字方便搜索。"
            if has_ocr and not self.config.show_ocr_in_cards:
                placeholder = "图片已完成文字识别。可点击“显示识别结果”查看预览，或点击“识别文字”查看完整结果。"
            self._render_info_section(detail_area, "图片", placeholder, item.id, muted=True)

    def _schedule_ocr_display_sync(self) -> None:
        if not self.store:
            return
        self.ocr_sync_generation += 1
        generation = self.ocr_sync_generation
        item_ids = list(self.card_views)
        started = time.perf_counter()
        self.root.after_idle(lambda: self._sync_ocr_display_batch(item_ids, 0, generation, started))

    def _sync_ocr_display_batch(self, item_ids: list[str], start: int, generation: int, perf_started: float) -> None:
        if generation != self.ocr_sync_generation or not self.store:
            return
        changed = False
        batch_size = 12
        for item_id in item_ids[start : start + batch_size]:
            item = self.store.get(item_id)
            view = self.card_views.get(item_id)
            if not item or item.type != "image" or view is None:
                continue
            view.update(item)
            changed = True
        next_start = start + batch_size
        if next_start < len(item_ids):
            self.root.after(1, lambda: self._sync_ocr_display_batch(item_ids, next_start, generation, perf_started))
        else:
            if changed:
                self._schedule_virtual_render()
            self._perf_log("ocr_display_sync", perf_started)

    def _update_single_image_detail(self, item_id: str) -> None:
        if not self.store:
            return
        item = self.store.get(item_id)
        view = self.card_views.get(item_id)
        if not item or item.type != "image" or view is None:
            return
        view.update(item)
        self._schedule_virtual_render()

    def copy_item(self, item_id: str) -> None:
        if not self.store:
            return
        started = time.perf_counter()
        item = self.store.get(item_id)
        if not item:
            return
        if item.type == "image" and item.image_path:
            self._copy_image_item_async(item, started)
            return
        try:
            if item.type == "text" and item.text is not None:
                copy_text(self.root, item.text)
            else:
                return
        except ClipboardAccessError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.last_seen_hash = item.content_hash
        self.store.touch(item.id, save=False)
        self.schedule_store_save()
        self.status_var.set("已复制到剪贴板，请到目标软件中粘贴。")
        self._update_after_touch(item.id)
        self._perf_log("copy_text_item", started)

    def _copy_image_item_async(self, item: HistoryItem, perf_started: float) -> None:
        if self.copy_image_worker_running:
            self.status_var.set("正在复制上一张图片，请稍候。")
            return
        self.copy_image_worker_running = True
        self.status_var.set("正在复制图片到剪贴板...")
        image_path = Path(item.image_path or "")
        item_id = item.id
        content_hash = item.content_hash

        def worker() -> None:
            try:
                copy_image(image_path)
            except ClipboardAccessError as exc:
                self.root.after(0, lambda: self._finish_copy_image_item(item_id, content_hash, str(exc), False, perf_started))
            except Exception as exc:
                self.root.after(0, lambda: self._finish_copy_image_item(item_id, content_hash, f"图片复制失败：{exc}", False, perf_started))
            else:
                self.root.after(0, lambda: self._finish_copy_image_item(item_id, content_hash, "", True, perf_started))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_copy_image_item(self, item_id: str, content_hash: str, message: str, ok: bool, perf_started: float) -> None:
        self.copy_image_worker_running = False
        if not ok:
            messagebox.showerror(APP_TITLE, message)
            self._perf_log("copy_image_item_error", perf_started)
            return
        if self.store:
            self.last_seen_hash = content_hash
            self.store.touch(item_id, save=False)
            self.schedule_store_save()
            self._update_after_touch(item_id)
        self.status_var.set("已复制图片到剪贴板，请到目标软件中粘贴。")
        self._perf_log("copy_image_item", perf_started)

    def _update_after_touch(self, item_id: str) -> None:
        if not self.store:
            return
        item = self.store.get(item_id)
        if not item:
            self._remove_card_view(item_id)
            return
        self._sync_single_item_card(item_id)

    def open_image_viewer(self, item_id: str) -> None:
        if not self.store:
            return
        item = self.store.get(item_id)
        if not item or not item.image_path:
            return
        path = Path(item.image_path)
        if not path.exists():
            messagebox.showwarning(APP_TITLE, "原图文件不存在，无法打开。")
            return
        ImageViewer(self.root, path, lambda: self.copy_item(item_id))

    def open_ocr_placeholder(self, item_id: str) -> None:
        if not self.store:
            return
        item = self.store.get(item_id)
        if item:
            path = Path(item.image_path or "")
            OcrDialog(
                self.root,
                item,
                path,
                lambda text, image_id=item.id: self.save_ocr_text(image_id, text),
                self.save_ocr_as_text_card,
            )

    def save_ocr_text(self, item_id: str, text: str) -> None:
        if not self.store:
            return
        self.store.update_ocr_text(item_id, text, save=False)
        self.schedule_store_save()
        self.status_var.set("已保存到当前图片记录，可直接搜索。")
        self._sync_single_item_card(item_id)

    def save_ocr_as_text_card(self, text: str) -> None:
        if not self.store:
            return
        item = self.store.add_text(text)
        if not item:
            return
        self.status_var.set("已把识别结果另存为文字卡片。")
        self._sync_single_item_card(item.id)

    def toggle_pin(self, item_id: str) -> None:
        if not self.store:
            return
        started = time.perf_counter()
        item = self.store.get(item_id)
        if item:
            self.store.update_pin(item_id, not item.pinned, save=False)
            self.schedule_store_save()
            self._sync_single_item_card(item_id)
            self._perf_log("toggle_pin_local_update", started)

    def edit_note(self, item_id: str) -> None:
        if not self.store:
            return
        item = self.store.get(item_id)
        if not item:
            return
        note = simpledialog.askstring("图片备注", "请输入图片备注：", initialvalue=item.note)
        if note is None:
            return
        self.store.update_note(item_id, note.strip(), save=False)
        self.schedule_store_save()
        self._sync_single_item_card(item_id)

    def delete_item(self, item_id: str) -> None:
        if not self.store:
            return
        if not messagebox.askyesno(APP_TITLE, "确定删除这条历史记录吗？"):
            return
        started = time.perf_counter()
        self.render_generation += 1
        item = self.store.get(item_id)
        self.store.delete(item_id)
        self._drop_thumbnail_cache(item)
        self._remove_card_view(item_id)
        self.selected_ids.discard(item_id)
        self._update_selection_count()
        self._update_count_label()
        self._render_virtual_window("delete_item")
        self._perf_log("delete_item", started)

    def _prompt_cleanup_if_needed(self, force: bool = False) -> None:
        if not self.store:
            return
        today = date.today().isoformat()
        if not force and self.config.last_cleanup_prompt_date == today:
            return
        expired = self.store.expired_items(self.config.retention_days)
        self.config.last_cleanup_prompt_date = today
        self.config_manager.save()
        if not expired:
            return
        if messagebox.askyesno(APP_TITLE, f"发现 {len(expired)} 条超过保存天数的未置顶记录，是否现在删除？"):
            self.render_generation += 1
            for item in expired:
                self._drop_thumbnail_cache(item)
            self.store.delete_many(item.id for item in expired)
            self.schedule_refresh()

    @staticmethod
    def _label_for(mapping: dict[str, str], value: str) -> str:
        return next((label for label, code in mapping.items() if code == value), next(iter(mapping)))


class DiagnosticsDialog:
    def __init__(self, app: ClipboardHistoryApp) -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("软件状态与诊断")
        self.window.geometry("760x560")
        self.window.minsize(660, 460)
        self.window.configure(bg=COLORS["bg"])
        self.window.transient(app.root)

        shell = tk.Frame(self.window, bg=COLORS["bg"], padx=18, pady=18)
        shell.pack(fill="both", expand=True)
        tk.Label(shell, text="软件状态与诊断", bg=COLORS["bg"], fg=COLORS["primary_dark"], font=FONT_CARD_TITLE).pack(anchor="w")
        tk.Label(
            shell,
            text="这里用于确认当前运行状态，也可以导出报告，方便以后排查卡顿、OCR 或保存路径问题。",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=FONT_NORMAL,
            wraplength=700,
            justify="left",
            pady=8,
        ).pack(anchor="w")

        self.text = tk.Text(
            shell,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            font=FONT_NORMAL,
            padx=12,
            pady=12,
            wrap="word",
        )
        self.text.pack(fill="both", expand=True, pady=(8, 12))
        self.refresh()

        actions = tk.Frame(shell, bg=COLORS["bg"])
        actions.pack(fill="x")
        make_button(actions, "刷新", self.refresh).pack(side="left", padx=(0, 8))
        make_button(actions, "导出诊断报告", self.export_report, primary=True).pack(side="left", padx=(0, 8))
        make_button(actions, "关闭", self.window.destroy).pack(side="right")

    def refresh(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", build_diagnostic_report(self.app))
        self.text.configure(state="disabled")

    def export_report(self) -> None:
        selected = filedialog.askdirectory(title="选择诊断报告保存位置")
        if not selected:
            return
        path = Path(selected) / f"历史粘贴板诊断_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            path.write_text(build_diagnostic_report(self.app), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"导出诊断报告失败：{exc}")
            return
        self.app.status_var.set(f"已导出诊断报告：{path}")


class SettingsDialog:
    def __init__(self, app: ClipboardHistoryApp) -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("设置")
        self.window.geometry("560x430")
        self.window.minsize(520, 380)
        self.window.configure(bg=COLORS["bg"])
        self.window.transient(app.root)

        shell = tk.Frame(self.window, bg=COLORS["bg"], padx=18, pady=18)
        shell.pack(fill="both", expand=True)
        tk.Label(shell, text="设置", bg=COLORS["bg"], fg=COLORS["primary_dark"], font=FONT_CARD_TITLE).pack(anchor="w")

        panel = tk.Frame(shell, bg=COLORS["surface"], highlightbackground=COLORS["border"], highlightthickness=1, padx=16, pady=14)
        panel.pack(fill="both", expand=True, pady=(12, 12))
        panel.columnconfigure(1, weight=1)

        tk.Label(panel, text="单张图片上限", bg=COLORS["surface"], fg=COLORS["muted"], font=FONT_NORMAL).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.max_image_dropdown = StableDropdown(
            panel,
            [(label, value) for label, value in MAX_IMAGE_LABELS.items()],
            app.config.max_image_mb,
            self.save_limits,
            width=84,
        )
        self.max_image_dropdown.grid(row=0, column=1, sticky="w", pady=(0, 6))
        tk.Label(
            panel,
            text="超过该大小的单张图片不会记录，不影响已保存历史。",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=FONT_SMALL,
        ).grid(row=1, column=1, sticky="w", pady=(0, 10))

        tk.Label(panel, text="最多记录", bg=COLORS["surface"], fg=COLORS["muted"], font=FONT_NORMAL).grid(row=2, column=0, sticky="w", pady=(0, 16))
        self.max_history_dropdown = StableDropdown(
            panel,
            [(label, value) for label, value in MAX_HISTORY_LABELS.items()],
            app.config.max_history_items,
            self.save_limits,
            width=84,
        )
        self.max_history_dropdown.grid(row=2, column=1, sticky="w", pady=(0, 16))

        self.hotkey_button = make_button(panel, app._hotkey_button_text(), self.toggle_hotkey)
        self.hotkey_button.grid(row=3, column=0, sticky="w", pady=(0, 10))
        self.hotkey_status_var = tk.StringVar(value=self._hotkey_status_text())
        tk.Label(panel, textvariable=self.hotkey_status_var, bg=COLORS["surface"], fg=COLORS["muted"], font=FONT_SMALL).grid(row=3, column=1, sticky="w", pady=(0, 10))

        self.startup_button = make_button(panel, app._startup_button_text(), self.toggle_startup)
        self.startup_button.grid(row=4, column=0, sticky="w", pady=(0, 10))
        tk.Label(panel, text="默认关闭，需要时手动开启。", bg=COLORS["surface"], fg=COLORS["muted"], font=FONT_SMALL).grid(row=4, column=1, sticky="w", pady=(0, 10))

        self.sensitive_button = make_button(panel, app._sensitive_button_text(), self.toggle_sensitive)
        self.sensitive_button.grid(row=5, column=0, sticky="w")
        tk.Label(panel, text="忽略明显密码、验证码、token 类短文本。", bg=COLORS["surface"], fg=COLORS["muted"], font=FONT_SMALL).grid(row=5, column=1, sticky="w")

        actions = tk.Frame(shell, bg=COLORS["bg"])
        actions.pack(fill="x")
        make_button(actions, "关闭", self.window.destroy).pack(side="right")

    def save_limits(self) -> None:
        self.app.update_limits(int(self.max_image_dropdown.get()), int(self.max_history_dropdown.get()))

    def toggle_hotkey(self) -> None:
        self.app.toggle_global_hotkey()
        self.hotkey_button.configure(text=self.app._hotkey_button_text())
        self.hotkey_status_var.set(self._hotkey_status_text())

    def _hotkey_status_text(self) -> str:
        shortcut = self.app.config.global_hotkey.upper().replace("+", " + ")
        return f"{shortcut} 用于呼出窗口；状态：{self.app.hotkey_status_text()}"

    def toggle_startup(self) -> None:
        self.app.toggle_start_with_windows()
        self.startup_button.configure(text=self.app._startup_button_text())

    def toggle_sensitive(self) -> None:
        self.app.toggle_sensitive_filter()
        self.sensitive_button.configure(text=self.app._sensitive_button_text())


class ImageViewer:
    def __init__(self, parent: tk.Tk, image_path: Path, copy_callback) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("查看图片")
        self.window.geometry("980x680")
        self.window.minsize(720, 480)
        self.window.configure(bg=COLORS["bg"])
        self.image_path = image_path
        self.copy_callback = copy_callback
        self.original = Image.open(image_path).convert("RGBA")
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.drag_start: tuple[int, int] | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.image_item_id: int | None = None
        self.render_after_id: str | None = None

        toolbar = tk.Frame(self.window, bg=COLORS["bg"], padx=16, pady=12)
        toolbar.pack(fill="x")
        tk.Label(
            toolbar,
            text=image_path.name,
            bg=COLORS["bg"],
            fg=COLORS["primary_dark"],
            font=FONT_CARD_TITLE,
        ).pack(side="left")
        make_button(toolbar, "复制图片", self.copy_callback, primary=True).pack(side="right", padx=(8, 0))
        make_button(toolbar, "适应窗口", self.fit_to_window).pack(side="right", padx=(8, 0))
        make_button(toolbar, "关闭", self.window.destroy).pack(side="right", padx=(8, 0))

        self.canvas = tk.Canvas(self.window, bg="#101820", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.canvas.bind("<Configure>", lambda _event: self.schedule_render(high_quality=True))
        self.canvas.bind("<MouseWheel>", self.on_zoom)
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.window.after(80, self.fit_to_window)

    def fit_to_window(self) -> None:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        img_width, img_height = self.original.size
        self.scale = min(width / img_width, height / img_height, 1.0)
        self.offset_x = 0
        self.offset_y = 0
        self.render(high_quality=True)

    def on_zoom(self, event: tk.Event) -> None:
        factor = 1.1 if event.delta > 0 else 0.9
        self.scale = min(max(self.scale * factor, 0.1), 8.0)
        self.render(high_quality=False)
        self.schedule_render(high_quality=True, delay=140)

    def on_drag_start(self, event: tk.Event) -> None:
        self.drag_start = (event.x, event.y)

    def on_drag(self, event: tk.Event) -> None:
        if not self.drag_start:
            return
        start_x, start_y = self.drag_start
        dx = event.x - start_x
        dy = event.y - start_y
        self.offset_x += dx
        self.offset_y += dy
        self.drag_start = (event.x, event.y)
        if self.image_item_id is not None:
            self.canvas.move(self.image_item_id, dx, dy)
        else:
            self.render(high_quality=False)

    def schedule_render(self, high_quality: bool = False, delay: int = 80) -> None:
        if self.render_after_id is not None:
            self.window.after_cancel(self.render_after_id)
        self.render_after_id = self.window.after(delay, lambda: self.render(high_quality=high_quality))

    def render(self, high_quality: bool = True) -> None:
        self.render_after_id = None
        if self.canvas.winfo_width() <= 1 or self.canvas.winfo_height() <= 1:
            return
        width = max(1, int(self.original.width * self.scale))
        height = max(1, int(self.original.height * self.scale))
        resample = Image.Resampling.LANCZOS if high_quality else Image.Resampling.BILINEAR
        resized = self.original.resize((width, height), resample)
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        x = self.canvas.winfo_width() // 2 + self.offset_x
        y = self.canvas.winfo_height() // 2 + self.offset_y
        self.image_item_id = self.canvas.create_image(x, y, image=self.photo)


class OcrDialog:
    def __init__(self, parent: tk.Tk, item: HistoryItem, image_path: Path, save_callback, save_as_text_callback) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("识别图片文字")
        self.window.geometry("760x480")
        self.window.minsize(640, 380)
        self.window.configure(bg=COLORS["bg"])
        self.item = item
        self.image_path = image_path
        self.save_callback = save_callback
        self.save_as_text_callback = save_as_text_callback
        self.is_running = False
        self.ocr_available = is_ocr_available()

        shell = tk.Frame(self.window, bg=COLORS["bg"], padx=18, pady=18)
        shell.pack(fill="both", expand=True)
        tk.Label(
            shell,
            text="图片文字识别",
            bg=COLORS["bg"],
            fg=COLORS["primary_dark"],
            font=FONT_CARD_TITLE,
        ).pack(anchor="w")
        tk.Label(
            shell,
            text=(
                "离线识别图片中的文字。保存到图片记录后可用于搜索；另存为文字卡片后会在主界面直接显示为一条文字历史。"
                if self.ocr_available
                else "未检测到 OCR 组件。请把 ocr_runtime 文件夹，或包含它的 OCR组件 文件夹，放到软件目录旁边，然后重新打开识别窗口。"
            ),
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=FONT_NORMAL,
            wraplength=700,
            justify="left",
            pady=8,
        ).pack(anchor="w")

        self.text = tk.Text(
            shell,
            height=12,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            font=FONT_NORMAL,
            padx=12,
            pady=12,
            wrap="word",
        )
        self.text.pack(fill="both", expand=True, pady=(8, 12))
        if item.ocr_text:
            self.text.insert("1.0", item.ocr_text)
        elif not self.ocr_available:
            self.text.insert("1.0", "OCR 组件未安装。\n\n启用方法：把 ocr_runtime 文件夹复制到 历史粘贴板.exe 所在目录旁边。\n\n如果你复制的是 OCR组件 文件夹，也可以直接放在 历史粘贴板.exe 同级目录。")
        else:
            self.text.insert("1.0", "准备识别，请稍候...")

        actions = tk.Frame(shell, bg=COLORS["bg"])
        actions.pack(fill="x")
        make_button(actions, "复制结果", self.copy_result, primary=True).pack(side="left")
        make_button(actions, "保存到图片记录", self.save_result).pack(side="left", padx=(8, 0))
        make_button(actions, "另存为文字卡片", self.save_as_text_card).pack(side="left", padx=(8, 0))
        make_button(actions, "重新识别", self.start_recognition).pack(side="left", padx=(8, 0))
        make_button(actions, "关闭", self.window.destroy).pack(side="right")
        self.status_var = tk.StringVar(value="")
        tk.Label(
            shell,
            textvariable=self.status_var,
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=FONT_SMALL,
        ).pack(anchor="w", pady=(10, 0))
        if not item.ocr_text and self.ocr_available:
            self.window.after(120, self.start_recognition)

    def start_recognition(self) -> None:
        if self.is_running:
            return
        if not is_ocr_available():
            self.text.delete("1.0", "end")
            self.text.insert("1.0", "OCR 组件未安装。\n\n请把 ocr_runtime 文件夹，或包含它的 OCR组件 文件夹，复制到 历史粘贴板.exe 所在目录旁边，然后重试。")
            return
        self.is_running = True
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "正在离线识别，请稍候...")
        thread = threading.Thread(target=self._run_ocr, daemon=True)
        thread.start()

    def _run_ocr(self) -> None:
        try:
            text = recognize_image_text(self.image_path)
        except OcrError as exc:
            message = f"识别失败：{exc}"
            self.window.after(0, lambda msg=message: self._finish_ocr(msg, False))
        except Exception as exc:
            message = f"识别失败：{exc}"
            self.window.after(0, lambda msg=message: self._finish_ocr(msg, False))
        else:
            if not text:
                text = "未识别到文字。"
            self.window.after(0, lambda: self._finish_ocr(text, True))

    def _finish_ocr(self, text: str, can_save: bool) -> None:
        self.is_running = False
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        if can_save and text != "未识别到文字。":
            self.save_callback(text)
            self.status_var.set("已自动保存到当前图片记录；可搜索识别文字。需要在列表直接看到文字时，请点“另存为文字卡片”。")

    def copy_result(self) -> None:
        content = self.text.get("1.0", "end").strip()
        self.window.clipboard_clear()
        self.window.clipboard_append(content)
        self.window.update()
        self.status_var.set("已复制识别结果，可到其他软件粘贴。")

    def save_result(self) -> None:
        content = self.text.get("1.0", "end").strip()
        if (
            content
            and not content.startswith("识别失败")
            and not content.startswith("OCR 组件未安装")
            and content != "正在离线识别，请稍候..."
        ):
            self.save_callback(content)
            self.status_var.set("已保存到当前图片历史记录。主界面隐藏识别结果时，只会显示“已识别”，但搜索可以命中。")

    def save_as_text_card(self) -> None:
        content = self.text.get("1.0", "end").strip()
        if (
            content
            and not content.startswith("识别失败")
            and not content.startswith("OCR 组件未安装")
            and content != "正在离线识别，请稍候..."
        ):
            self.save_as_text_callback(content)
            self.status_var.set("已另存为文字卡片，回到主界面可以直接看到这条文字历史。")


class StableDropdown(tk.Frame):
    def __init__(self, parent: tk.Misc, options: list[tuple[str, object]], value: object, command, width: int) -> None:
        super().__init__(parent, bg=COLORS["surface"], highlightbackground=COLORS["border_strong"], highlightthickness=1)
        self.command = command
        self.value: object | None = None
        self.label_var = tk.StringVar()
        self.button = tk.Menubutton(
            self,
            textvariable=self.label_var,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            activebackground=COLORS["primary_soft"],
            activeforeground=COLORS["primary_dark"],
            relief="flat",
            bd=0,
            width=max(4, width // 12),
            padx=10,
            pady=7,
            font=FONT_NORMAL,
            cursor="hand2",
            anchor="w",
        )
        self.menu = tk.Menu(self.button, tearoff=False, bg=COLORS["surface"], fg=COLORS["text"], activebackground=COLORS["primary_soft"])
        self.button.configure(menu=self.menu)
        self.button.pack(fill="x")
        self.set_options(options, value, run_command=False)

    def set_options(self, options: list[tuple[str, object]], value: object, run_command: bool = False) -> None:
        self.options = options
        self.menu.delete(0, "end")
        for label, option_value in options:
            self.menu.add_command(label=label, command=lambda v=option_value: self.set_value(v))
        self.set_value(value, run_command=run_command)

    def set_value(self, value: object, run_command: bool = True) -> None:
        if value in (None, "") and self.options:
            value = self.options[0][1]
        self.value = value
        label = next((label for label, option_value in self.options if option_value == value), str(value))
        self.label_var.set(label)
        if run_command:
            self.command()

    def get(self):
        return self.value


def make_button(parent: tk.Misc, text: str, command, primary: bool = False, danger: bool = False) -> tk.Button:
    bg = COLORS["primary_soft"] if primary else COLORS["surface_soft"]
    fg = COLORS["primary_dark"] if not danger else COLORS["danger"]
    hover = "#cfe8ff" if primary else ("#ffe3e6" if danger else "#eef6ff")
    if danger:
        bg = COLORS["danger_soft"]
    button = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        activebackground=hover,
        activeforeground=fg,
        relief="flat",
        bd=0,
        padx=14,
        pady=6,
        font=FONT_SMALL,
        cursor="hand2",
        highlightthickness=1,
        highlightbackground=COLORS["border"],
    )
    button.bind("<Enter>", lambda _event: button.configure(bg=hover))
    button.bind("<Leave>", lambda _event: button.configure(bg=bg))
    return button


def format_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value.replace("T", " ")


def image_size_mb(image: Image.Image) -> float:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return len(output.getvalue()) / (1024 * 1024)


def build_diagnostic_report(app: ClipboardHistoryApp) -> str:
    store = app.store
    items = store.items if store else []
    image_count = len([item for item in items if item.type == "image"])
    text_count = len([item for item in items if item.type == "text"])
    data_dir = app.config.storage_dir or "未设置"
    hotkey_status = app.hotkey_status_text() if hasattr(app, "hotkey_status_text") else getattr(app, "hotkey_status", "未知")
    lines = [
        f"{APP_TITLE} 诊断报告",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "基础信息",
        f"- 版本：{__version__}",
        f"- Python：{sys.version.split()[0]}",
        f"- 系统：{platform.platform()}",
        f"- 主程序目录：{app.app_dir}",
        f"- 配置文件：{app.config_manager.path}",
        f"- 数据目录：{data_dir}",
        "",
        "历史数据",
        f"- 总记录：{len(items)}",
        f"- 文字记录：{text_count}",
        f"- 图片记录：{image_count}",
        f"- 当前显示：{len(app.visible_item_ids)}",
        f"- 已选择：{len(app.selected_ids)}",
        f"- 延迟保存待写入：{'是' if app.pending_store_save else '否'}",
        "",
        "组件与设置",
        f"- OCR 组件：{'可用' if is_ocr_available() else '未安装'}",
        f"- 剪贴板序号检测：{sequence_status_text(app.clipboard_sequence_supported)}",
        f"- 全局快捷键：{'开启' if app.config.global_hotkey_enabled else '关闭'} ({app.config.global_hotkey})",
        f"- 快捷键注册状态：{hotkey_status}",
        f"- 开机自启：{'开启' if app.config.start_with_windows else '关闭'}",
        f"- 暂停记录：{'是' if app.is_recording_paused() else '否'}",
        f"- 敏感过滤：{'开启' if app.config.sensitive_filter_enabled else '关闭'}",
        f"- 单张图片上限：{app.config.max_image_mb}MB",
        f"- 最大记录：{app.config.max_history_items}",
        "",
        "最近性能事件",
    ]
    if app.perf_events:
        lines.extend(f"- {event}" for event in app.perf_events[-20:])
    else:
        lines.append("- 暂无性能事件。")
    return "\n".join(lines)


def sequence_status_text(value: bool | None) -> str:
    if value is True:
        return "可用"
    if value is False:
        return "不可用，已回退普通轮询"
    return "尚未检测"


def set_start_with_windows(enabled: bool) -> None:
    if platform.system() != "Windows":
        raise OSError("开机自启目前仅支持 Windows。")
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_RUN_REG_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_RUN_REG_NAME)
            except FileNotFoundError:
                pass


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(sys.argv[0]).resolve()}"'


def seed_perf_data(storage_dir: Path, count: int) -> int:
    store = HistoryStore(storage_dir)
    for index in range(count):
        item = store.add_text(f"性能测试记录 {index:04d} 学习 搜索 置顶 复制")
        if item and index % 10 == 0:
            store.update_pin(item.id, True)
    return len(store.items)


def make_menu_button(parent: tk.Misc, text: str, primary: bool = False, danger: bool = False) -> tk.Menubutton:
    bg = COLORS["primary_soft"] if primary else COLORS["surface_soft"]
    fg = COLORS["primary_dark"] if not danger else COLORS["danger"]
    hover = "#cfe8ff" if primary else ("#ffe3e6" if danger else "#eef6ff")
    if danger:
        bg = COLORS["danger_soft"]
    button = tk.Menubutton(
        parent,
        text=text,
        bg=bg,
        fg=fg,
        activebackground=hover,
        activeforeground=fg,
        relief="flat",
        bd=0,
        padx=14,
        pady=6,
        font=FONT_SMALL,
        cursor="hand2",
        highlightthickness=1,
        highlightbackground=COLORS["border"],
    )
    button.bind("<Enter>", lambda _event: button.configure(bg=hover))
    button.bind("<Leave>", lambda _event: button.configure(bg=bg))
    return button


def main() -> int:
    if "--perf-seed" in sys.argv:
        try:
            storage_arg = sys.argv[sys.argv.index("--perf-seed") + 1]
            count_arg = sys.argv[sys.argv.index("--perf-seed") + 2]
        except (ValueError, IndexError):
            return 2
        count = seed_perf_data(Path(storage_arg), int(count_arg))
        print(f"seeded {count} items")
        return 0
    if "--ocr-self-test" in sys.argv:
        try:
            image_arg = sys.argv[sys.argv.index("--ocr-self-test") + 1]
        except (ValueError, IndexError):
            return 2
        try:
            text = recognize_image_text(Path(image_arg))
        except Exception:
            error_path = app_base_dir() / "ocr_self_test_error.txt"
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
            return 1
        error_path = app_base_dir() / "ocr_self_test_error.txt"
        if error_path.exists():
            error_path.unlink()
        return 0 if text.strip() else 3
    if "--self-test" in sys.argv:
        return 0
    root = tk.Tk()
    ClipboardHistoryApp(root)
    root.mainloop()
    return 0


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", app_base_dir()))
    return base.joinpath(*parts)


if __name__ == "__main__":
    sys.exit(main())
