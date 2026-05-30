#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bulk File Editor/Renamer
- Live Template Example preview (selected file -> correct season)
- Auto season detection PER FILE (multi-season folders supported)
- Optional reset counter per season
- Folder Contents table + preview/run
- Partial episode number update and standard rename
- Dark mode fixed (ttk + ScrolledText), undo, autosave
"""

from __future__ import annotations
import os, re, json, shutil, threading, queue, subprocess, platform, webbrowser, uuid, sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Iterable, Tuple, Dict

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

# ----------------------- Utilities -----------------------

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
WINDOWS_INVALID_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

def parse_size_str(s: str | None) -> Optional[int]:
    if not s: return None
    s = s.strip().upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?", s)
    if not m: raise ValueError(f"Invalid size: {s}")
    value = float(m.group(1)); unit = m.group(2) or "B"
    return int(value * SIZE_UNITS[unit])

def parse_time_str(s: str | None, *, end_of_day: bool = False) -> Optional[float]:
    if not s: return None
    text = s.strip()
    try:
        if len(text) == 10:
            dt = datetime.fromisoformat(text)
            if end_of_day:
                dt = dt + timedelta(days=1) - timedelta(microseconds=1)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception as e:
        raise ValueError(f"Invalid date/time '{s}': {e}")

def fmt_size(n: int) -> str:
    suf = ["B","KB","MB","GB","TB"]
    x = float(n)
    for u in suf:
        if x < 1024 or u == suf[-1]:
            return f"{x:.0f}{u}" if u == "B" else f"{x:.1f}{u}"
        x /= 1024.0
    return f"{n}B"

def fmt_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def path_identity(path: Path) -> str:
    try:
        raw = str(path.resolve(strict=False))
    except Exception:
        raw = os.path.abspath(str(path))
    return os.path.normcase(raw)

def same_path(a: Path, b: Path) -> bool:
    return path_identity(a) == path_identity(b)

def display_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except Exception:
        return str(path)

def add_skip_reason(plan: "FilePlan", reason: str) -> None:
    plan.skipped_reason = f"{plan.skipped_reason}; {reason}" if plan.skipped_reason else reason

def validate_new_filename(new_name: str) -> Optional[str]:
    if not new_name:
        return "New name is empty"
    if new_name in {".", ".."}:
        return "New name cannot be '.' or '..'"
    if "/" in new_name or "\\" in new_name:
        return "New name cannot include folders"
    if any(ord(ch) < 32 for ch in new_name):
        return "New name contains a control character"
    bad = sorted({ch for ch in new_name if ch in WINDOWS_INVALID_CHARS})
    if bad:
        return f"New name contains invalid character: {bad[0]}"
    if new_name.rstrip(" .") != new_name:
        return "New name cannot end with a space or dot"
    base_name = new_name.split(".", 1)[0].upper()
    if base_name in WINDOWS_RESERVED_NAMES:
        return f"New name uses reserved Windows name: {base_name}"
    return None

def open_in_os(path: Path):
    try:
        if platform.system() == "Windows": os.startfile(path)  # type: ignore
        elif platform.system() == "Darwin": subprocess.Popen(["open", str(path)])
        else: subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        webbrowser.open(f"file://{path}")

def reveal_in_os(path: Path):
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception:
        pass

# ----------------------- Planning structs -----------------------

@dataclass
class RenamePlan:
    new_name: Optional[str] = None

@dataclass
class ContentPlan:
    changed: bool = False
    preview: Optional[str] = None

@dataclass
class FilePlan:
    path: Path
    rename: Optional[RenamePlan] = None
    content: Optional[ContentPlan] = None
    skipped_reason: Optional[str] = None

# ----------------------- Core rename/content -----------------------

def plan_rename_standard(
    path: Path,
    prefix: str, suffix: str, set_ext: Optional[str],
    to_lower: bool, to_upper: bool,
    rename_from: Optional[str], rename_to: Optional[str],
) -> Optional[RenamePlan]:
    name = path.name
    stem, ext = path.stem, path.suffix

    if rename_from and rename_to is not None:
        try:
            new = re.sub(rename_from, rename_to, name)
        except re.error as e:
            raise ValueError(f"Rename regex error: {e}")
        if new != name:
            return RenamePlan(new_name=new)
        return None

    changed = False
    if prefix:
        stem = prefix + stem
        changed = True
    if suffix:
        stem = stem + suffix
        changed = True
    if set_ext:
        if not set_ext.startswith("."):
            set_ext = "." + set_ext
        ext = set_ext
        changed = True
    if to_lower and not to_upper:
        stem, ext = stem.lower(), ext.lower()
        changed = True
    if to_upper and not to_lower:
        stem, ext = stem.upper(), ext.upper()
        changed = True

    return RenamePlan(new_name=stem + ext) if changed else None

def is_binary(path: Path, sample_bytes: int = 8192) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(sample_bytes)
        if b"\x00" in chunk:
            return True
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
    except Exception:
        return True
    return False

def safe_read_text(path: Path, encoding: str, errors: str, binary_ok: bool) -> str:
    if not binary_ok and is_binary(path):
        raise ValueError("Binary file (skipping content edit)")
    with path.open("r", encoding=encoding, errors=errors, newline="") as f:
        return f.read()

def safe_write_text(path: Path, text: str, encoding: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp-edit")
    with tmp.open("w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(tmp, path)

def make_backup(path: Path, backup_suffix: str) -> Path:
    backup = path.with_name(path.name + backup_suffix)
    if backup.exists():
        for i in range(1, 10000):
            candidate = path.with_name(f"{path.name}{backup_suffix}.{i}")
            if not candidate.exists():
                backup = candidate
                break
        else:
            raise FileExistsError(f"Could not create a unique backup for {path.name}")
    shutil.copy2(path, backup)
    return backup

def plan_content_edit(
    text: str,
    prepend: Optional[str],
    append: Optional[str],
    replace_pat: Optional[str],
    replace_with: Optional[str],
    fixed_string: bool,
    ignore_case: bool,
    multiline: bool,
    max_preview: int = 140,
) -> Tuple[bool, Optional[str], str]:
    changed = False
    out = text

    if replace_pat is not None and replace_with is not None:
        if fixed_string:
            flags = re.IGNORECASE if ignore_case else 0
            pat = re.compile(re.escape(replace_pat), flags)
        else:
            flags = (re.MULTILINE if multiline else 0) | (re.IGNORECASE if ignore_case else 0)
            pat = re.compile(replace_pat, flags)
        out, n = pat.subn(replace_with, out)
        if n > 0:
            changed = True

    if prepend:
        out = prepend + out
        changed = True
    if append:
        out = out + append
        changed = True

    preview = out[:max_preview] + ("..." if len(out) > max_preview else "") if changed else None
    return changed, preview, out

def collect_files(
    base: Path,
    recursive: bool,
    globs: List[str],
    exclude_globs: List[str],
    exts: List[str],
    name_contains: Optional[str],
    min_size: Optional[int],
    max_size: Optional[int],
    since_ts: Optional[float],
    until_ts: Optional[float],
) -> Iterable[Path]:
    candidates: List[Path] = []
    seen: set[str] = set()
    patterns = globs or (["**/*"] if recursive else ["*"])
    for pat in patterns:
        for p in base.glob(pat):
            if p.is_file():
                key = path_identity(p)
                if key not in seen:
                    seen.add(key)
                    candidates.append(p)

    excluded: set[str] = set()
    for pat in exclude_globs:
        for p in base.glob(pat):
            if p.is_file():
                excluded.add(path_identity(p))

    ok_exts = {
        (e if e.startswith(".") else "." + e).lower()
        for e in exts
    }

    for p in candidates:
        if path_identity(p) in excluded:
            continue
        if ok_exts:
            if p.suffix.lower() not in ok_exts:
                continue
        if name_contains and name_contains not in p.name:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if min_size is not None and st.st_size < min_size:
            continue
        if max_size is not None and st.st_size > max_size:
            continue
        if since_ts is not None and st.st_mtime < since_ts:
            continue
        if until_ts is not None and st.st_mtime > until_ts:
            continue
        yield p

# ----------------------- Season detection -----------------------

SEASON_PATTERNS = [
    re.compile(r"(?i)\bseason[\s\-_]*0*(\d{1,2})\b"),
    re.compile(r"(?i)\bs[\s\-_]*0*(\d{1,2})\b"),
    re.compile(r"(?i)\bseries[\s\-_]*0*(\d{1,2})\b"),
]

def detect_season_from_folder_name(folder_name: str) -> Optional[int]:
    for pat in SEASON_PATTERNS:
        m = pat.search(folder_name)
        if m:
            try:
                val = int(m.group(1))
                if 0 <= val < 100:
                    return val
            except ValueError:
                pass
    return None

def detect_season_for_file(file_path: Path, base: Path) -> Optional[int]:
    """
    Detect season for a file by scanning its parent folders up to the base folder.
    e.g. .../Season 02/episode.mkv -> 2
    """
    try:
        base = base.resolve()
        p = file_path.resolve()
    except Exception:
        base = Path(base)
        p = Path(file_path)

    cur = p.parent
    while True:
        val = detect_season_from_folder_name(cur.name)
        if val is not None:
            return val
        if cur == base:
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    return None

def season_for_path(
    file_path: Path,
    base: Path,
    manual_season: int,
    auto_season: bool,
    season_per_file: bool,
) -> Tuple[int, Optional[int]]:
    if not auto_season:
        return manual_season, None
    if season_per_file:
        detected = detect_season_for_file(file_path, base)
        return (detected if detected is not None else manual_season, detected)
    detected = detect_season_from_folder_name(base.name)
    return (detected if detected is not None else manual_season, detected)

# ----------------------- Tooltips -----------------------

class Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 2
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(self.tip, text=self.text, relief="solid", borderwidth=1, padding=4)
        label.pack()

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None

# ----------------------- App -----------------------

class BulkEditApp(ttk.Frame):
    _TRIM_CHARS = " -_[](){}.,\u3000"

    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master.title("Bulk File Editor/Renamer")
        self.grid(sticky="nsew")
        self.master.geometry("1260x980")
        self.master.grid_rowconfigure(0, weight=1)
        self.master.grid_columnconfigure(0, weight=1)

        self.style = ttk.Style()
        try:
            if "vista" in self.style.theme_names():
                self.style.theme_use("vista")
            elif "clam" in self.style.theme_names():
                self.style.theme_use("clam")
        except Exception:
            pass

        self._system_theme = self.style.theme_use()
        self._text_widgets: List[tk.Text] = []
        self._orig_text_cfg: dict = {}

        self.msg_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.last_renames: List[Tuple[Path, Path]] = []  # (new_path -> old_path)
        self._filter_refresh_job: Optional[str] = None

        self._build_ui()
        self._bind_shortcuts()
        self._load_settings()
        self._poll_log()

    # ---------- UI ----------

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.grid(row=0, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ----- Tab 1: Folder / Filters -----
        t_folder = ttk.Frame(nb, padding=8)
        nb.add(t_folder, text="Folder / Filters")
        t_folder.grid_columnconfigure(0, weight=1)
        t_folder.grid_columnconfigure(1, weight=1)

        row0 = ttk.Frame(t_folder)
        row0.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        row0.grid_columnconfigure(1, weight=1)

        self.folder_var = tk.StringVar()
        ttk.Label(row0, text="Base folder:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(row0, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(row0, text="Browse…", command=self._choose_folder).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(row0, text="Refresh List (F5)", command=self.refresh_file_list).grid(row=0, column=3, padx=(6, 0))

        self.dark_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row0, text="Dark theme", variable=self.dark_var,
                        command=lambda: self._apply_dark(self.dark_var.get())).grid(row=0, column=4, padx=(12, 0))

        self.dry_run_var = tk.BooleanVar(value=True)
        dry = ttk.Checkbutton(row0, text="Dry-run", variable=self.dry_run_var, command=self._check_dry_run)
        dry.grid(row=0, column=5, padx=(12, 0))
        Tooltip(dry, "When ON, no files are modified.\nTurn OFF to apply changes.")

        self.detected_label_var = tk.StringVar(value="")
        ttk.Label(t_folder, textvariable=self.detected_label_var, foreground="#666").grid(row=1, column=0, columnspan=2, sticky="w")

        gf = ttk.LabelFrame(t_folder, text="Filters")
        gf.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=6)
        for i in range(4):
            gf.grid_columnconfigure(i, weight=1)

        self.recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(gf, text="Recursive", variable=self.recursive_var, command=self._filters_changed).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self.glob_var = tk.StringVar(value="")
        self.exclude_var = tk.StringVar(value="")
        self.ext_var = tk.StringVar(value="")
        self.name_contains_var = tk.StringVar(value="")
        self.min_size_var = tk.StringVar(value="")
        self.max_size_var = tk.StringVar(value="")
        self.since_var = tk.StringVar(value="")
        self.until_var = tk.StringVar(value="")

        ttk.Label(gf, text="Include globs:").grid(row=1, column=0, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.glob_var).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Label(gf, text="Exclude globs:").grid(row=1, column=2, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.exclude_var).grid(row=1, column=3, sticky="ew", padx=4)
        ttk.Label(gf, text="Extensions:").grid(row=2, column=0, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.ext_var).grid(row=2, column=1, sticky="ew", padx=4)
        ttk.Label(gf, text="Name contains:").grid(row=2, column=2, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.name_contains_var).grid(row=2, column=3, sticky="ew", padx=4)
        ttk.Label(gf, text="Min size:").grid(row=3, column=0, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.min_size_var).grid(row=3, column=1, sticky="ew", padx=4)
        ttk.Label(gf, text="Max size:").grid(row=3, column=2, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.max_size_var).grid(row=3, column=3, sticky="ew", padx=4)
        ttk.Label(gf, text="Since (YYYY-MM-DD):").grid(row=4, column=0, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.since_var).grid(row=4, column=1, sticky="ew", padx=4)
        ttk.Label(gf, text="Until (YYYY-MM-DD):").grid(row=4, column=2, sticky="w", padx=4); ttk.Entry(gf, textvariable=self.until_var).grid(row=4, column=3, sticky="ew", padx=4)

        contents = ttk.LabelFrame(t_folder, text="Folder Contents (filtered)")
        contents.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        contents.grid_columnconfigure(0, weight=1)
        contents.grid_rowconfigure(1, weight=1)

        tb = ttk.Frame(contents)
        tb.grid(row=0, column=0, sticky="ew", padx=4, pady=4)

        self.list_count_var = tk.StringVar(value="0 files")
        ttk.Label(tb, textvariable=self.list_count_var).pack(side="left")
        ttk.Button(tb, text="Refresh", command=self.refresh_file_list).pack(side="right")

        self.tree = ttk.Treeview(contents, columns=("name", "size", "mtime", "relpath"), show="headings")
        for h in ("name", "size", "mtime", "relpath"):
            self.tree.heading(h, text=h.title(), command=lambda c=h: self._sort_tree(c))
        self.tree.column("name", width=320, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("mtime", width=140, anchor="center")
        self.tree.column("relpath", width=520, anchor="w")

        ysb = ttk.Scrollbar(contents, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(contents, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscroll=ysb.set, xscroll=xsb.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        ysb.grid(row=1, column=1, sticky="ns")
        xsb.grid(row=2, column=0, sticky="ew")

        self.tree.bind("<Double-1>", self._open_selected)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_template_example())
        self._build_tree_menu()

        # ----- Tab 2: Rename (Template) -----
        t_tmpl = ttk.Frame(nb, padding=8)
        nb.add(t_tmpl, text="Rename (Template)")
        t_tmpl.grid_columnconfigure(1, weight=1)

        self.inc_template_var = tk.StringVar(value="")
        self.inc_start_var = tk.StringVar(value="1")
        self.inc_step_var = tk.StringVar(value="1")
        self.inc_season_var = tk.StringVar(value="1")
        self.inc_auto_season_var = tk.BooleanVar(value=True)

        # NEW: multi-season behavior
        self.season_per_file_var = tk.BooleanVar(value=True)     # detect season per file
        self.reset_counter_per_season_var = tk.BooleanVar(value=True)  # reset episode numbers per season

        self.sort_mode_var = tk.StringVar(value="name_asc")

        ttk.Label(t_tmpl, text="Template:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ent = ttk.Entry(t_tmpl, textvariable=self.inc_template_var)
        ent.grid(row=0, column=1, columnspan=4, sticky="ew", padx=4, pady=2)
        Tooltip(ent, "Placeholders: {n} {season} {stem} {ext} {dotext} {parent}\nPadding: {n:02} {season:02}")

        ttk.Label(t_tmpl, text="Presets:").grid(row=1, column=0, sticky="e", padx=4)
        self.preset_var = tk.StringVar(value="")
        preset = ttk.Combobox(
            t_tmpl, textvariable=self.preset_var, state="readonly",
            values=[
                "S{season:02}E{n:02}{dotext}",
                "{stem} - E{n:02}{dotext}",
                "{parent} - {n:02} - {stem}{dotext}",
            ],
        )
        preset.grid(row=1, column=1, sticky="w")
        preset.bind("<<ComboboxSelected>>", lambda *_: self.inc_template_var.set(self.preset_var.get()))

        ttk.Label(t_tmpl, text="Start:").grid(row=0, column=5, sticky="e")
        ttk.Entry(t_tmpl, width=6, textvariable=self.inc_start_var).grid(row=0, column=6, sticky="w")
        ttk.Label(t_tmpl, text="Step:").grid(row=1, column=5, sticky="e")
        ttk.Entry(t_tmpl, width=6, textvariable=self.inc_step_var).grid(row=1, column=6, sticky="w")

        # Auto season + multi-season options
        ttk.Checkbutton(
            t_tmpl, text="Auto-detect season",
            variable=self.inc_auto_season_var,
            command=lambda: (self._update_detected_season_label(), self._update_template_example()),
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=2)

        ttk.Checkbutton(
            t_tmpl, text="Detect season per file (multi-season)",
            variable=self.season_per_file_var,
            command=lambda: self._update_template_example(),
        ).grid(row=2, column=2, columnspan=3, sticky="w", padx=4, pady=2)

        ttk.Checkbutton(
            t_tmpl, text="Reset episode counter per season",
            variable=self.reset_counter_per_season_var,
            command=lambda: self._update_template_example(),
        ).grid(row=3, column=2, columnspan=3, sticky="w", padx=4, pady=2)

        ttk.Label(t_tmpl, text="Season (manual):").grid(row=3, column=0, sticky="e", padx=4)
        self.season_entry = ttk.Entry(t_tmpl, width=6, textvariable=self.inc_season_var)
        self.season_entry.grid(row=3, column=1, sticky="w")

        ttk.Label(t_tmpl, text="Sort by:").grid(row=2, column=5, sticky="e")
        ttk.Combobox(
            t_tmpl, textvariable=self.sort_mode_var, state="readonly",
            values=("name_asc", "name_desc", "mtime_asc", "mtime_desc"),
        ).grid(row=2, column=6, sticky="w")

        self.template_example_var = tk.StringVar(value="Example: -")
        ttk.Label(t_tmpl, textvariable=self.template_example_var, foreground="#888").grid(
            row=4, column=0, columnspan=7, sticky="w", padx=4, pady=(6, 0)
        )

        for var in [
            self.inc_template_var, self.inc_start_var, self.inc_step_var,
            self.inc_season_var, self.inc_auto_season_var,
            self.season_per_file_var, self.reset_counter_per_season_var,
            self.sort_mode_var,
        ]:
            var.trace_add("write", lambda *_: self._update_template_example())

        # ----- Tab 3: Episode Number (Partial) -----
        t_part = ttk.Frame(nb, padding=8)
        nb.add(t_part, text="Episode Number (Partial)")
        t_part.grid_columnconfigure(1, weight=1)

        self.partial_enable_var = tk.BooleanVar(value=False)
        self.partial_mode_var = tk.StringVar(value="update_in_place")  # or drop_prefix_keep_post
        self.partial_pad_width_var = tk.StringVar(value="")
        self.partial_trim_title_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(t_part, text="Enable partial number update", variable=self.partial_enable_var).grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Radiobutton(t_part, text="Update number in place (keep prefix + title)", value="update_in_place", variable=self.partial_mode_var).grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(t_part, text="Remove everything before the number (keep title)", value="drop_prefix_keep_post", variable=self.partial_mode_var).grid(row=2, column=0, sticky="w", pady=(0, 6))
        ttk.Label(t_part, text="Pad width (blank = auto):").grid(row=1, column=1, sticky="e")
        ttk.Entry(t_part, width=8, textvariable=self.partial_pad_width_var).grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(t_part, text="Trim separators before title ( - _ [ ] ( ) . and spaces )", variable=self.partial_trim_title_var).grid(row=2, column=1, columnspan=2, sticky="w")

        # ----- Tab 4: Standard Rename / Content -----
        t_std = ttk.Frame(nb, padding=8)
        nb.add(t_std, text="Standard Rename / Content")
        for i in range(4):
            t_std.grid_columnconfigure(i, weight=1)

        self.prefix_var = tk.StringVar(value="")
        self.suffix_var = tk.StringVar(value="")
        self.set_ext_var = tk.StringVar(value="")
        self.lower_var = tk.BooleanVar(value=False)
        self.upper_var = tk.BooleanVar(value=False)
        self.rename_from_var = tk.StringVar(value="")
        self.rename_to_var = tk.StringVar(value="")

        self.prepend_var = tk.StringVar(value="")
        self.append_var = tk.StringVar(value="")
        self.replace_pat_var = tk.StringVar(value="")
        self.replace_with_var = tk.StringVar(value="")
        self.fixed_string_var = tk.BooleanVar(value=False)
        self.ignore_case_var = tk.BooleanVar(value=False)
        self.multiline_var = tk.BooleanVar(value=False)

        rn = ttk.LabelFrame(t_std, text="Standard Rename")
        rn.grid(row=0, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Label(rn, text="Prefix:").grid(row=0, column=0, sticky="e"); ttk.Entry(rn, textvariable=self.prefix_var).grid(row=0, column=1, sticky="ew")
        ttk.Label(rn, text="Suffix:").grid(row=0, column=2, sticky="e"); ttk.Entry(rn, textvariable=self.suffix_var).grid(row=0, column=3, sticky="ew")
        ttk.Label(rn, text="Set extension (.log):").grid(row=1, column=0, sticky="e"); ttk.Entry(rn, textvariable=self.set_ext_var).grid(row=1, column=1, sticky="ew")
        ttk.Checkbutton(rn, text="lowercase", variable=self.lower_var).grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(rn, text="UPPERCASE", variable=self.upper_var).grid(row=1, column=3, sticky="w")
        ttk.Label(rn, text="Regex From:").grid(row=2, column=0, sticky="e"); ttk.Entry(rn, textvariable=self.rename_from_var).grid(row=2, column=1, sticky="ew")
        ttk.Label(rn, text="To:").grid(row=2, column=2, sticky="e"); ttk.Entry(rn, textvariable=self.rename_to_var).grid(row=2, column=3, sticky="ew")

        ct = ttk.LabelFrame(t_std, text="Content Edits")
        ct.grid(row=1, column=0, columnspan=4, sticky="ew", pady=4)
        ttk.Label(ct, text="Prepend:").grid(row=0, column=0, sticky="e"); ttk.Entry(ct, textvariable=self.prepend_var).grid(row=0, column=1, columnspan=3, sticky="ew")
        ttk.Label(ct, text="Append:").grid(row=1, column=0, sticky="e"); ttk.Entry(ct, textvariable=self.append_var).grid(row=1, column=1, columnspan=3, sticky="ew")
        ttk.Label(ct, text="Find:").grid(row=2, column=0, sticky="e"); ttk.Entry(ct, textvariable=self.replace_pat_var).grid(row=2, column=1, sticky="ew")
        ttk.Label(ct, text="Replace:").grid(row=2, column=2, sticky="e"); ttk.Entry(ct, textvariable=self.replace_with_var).grid(row=2, column=3, sticky="ew")
        ttk.Checkbutton(ct, text="Fixed string", variable=self.fixed_string_var).grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(ct, text="Ignore case", variable=self.ignore_case_var).grid(row=3, column=2, sticky="w")
        ttk.Checkbutton(ct, text="Regex multiline (^ / $ per line)", variable=self.multiline_var).grid(row=3, column=3, sticky="w")

        # ----- Tab 5: Run / Preview / Log -----
        t_ops = ttk.Frame(nb, padding=8)
        nb.add(t_ops, text="Run / Preview / Log")
        for i in range(4):
            t_ops.grid_columnconfigure(i, weight=1)

        options = ttk.LabelFrame(t_ops, text="Options")
        options.grid(row=0, column=0, columnspan=4, sticky="ew")

        self.backup_var = tk.StringVar(value=".bak")
        self.encoding_var = tk.StringVar(value="utf-8")
        self.errors_var = tk.StringVar(value="strict")
        self.binary_ok_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(options, text="Allow binary edits (unsafe)", variable=self.binary_ok_var).grid(row=0, column=0, sticky="w", padx=4)
        ttk.Label(options, text="Backup suffix:").grid(row=0, column=1, sticky="e"); ttk.Entry(options, width=10, textvariable=self.backup_var).grid(row=0, column=2, sticky="w")
        ttk.Label(options, text="Encoding:").grid(row=1, column=1, sticky="e"); ttk.Entry(options, width=12, textvariable=self.encoding_var).grid(row=1, column=2, sticky="w")
        ttk.Label(options, text="Decode errors:").grid(row=1, column=3, sticky="e"); ttk.Combobox(options, width=12, textvariable=self.errors_var, values=("strict", "ignore", "replace")).grid(row=1, column=4, sticky="w")

        actions = ttk.Frame(t_ops)
        actions.grid(row=1, column=0, columnspan=4, sticky="ew", pady=6)

        ttk.Button(actions, text="Preview Plan (Ctrl+P)", command=self.preview_plan).grid(row=0, column=0, padx=4)
        self.run_btn = ttk.Button(actions, text="Run (Ctrl+R)", command=self.run_plan)
        self.run_btn.grid(row=0, column=1, padx=4)
        self.stop_btn = ttk.Button(actions, text="Stop", command=self._stop_run, state="disabled")
        self.stop_btn.grid(row=0, column=2, padx=4)
        self.undo_btn = ttk.Button(actions, text="Undo Last Rename", command=self._undo_last, state="disabled")
        self.undo_btn.grid(row=0, column=3, padx=4)

        self.prog = ttk.Progressbar(actions, mode="determinate", maximum=100)
        self.prog.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=(6, 0))

        panes = ttk.PanedWindow(t_ops, orient=tk.HORIZONTAL)
        panes.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=4)
        t_ops.grid_rowconfigure(2, weight=1)

        preview_frame = ttk.LabelFrame(panes, text="Preview")
        log_frame = ttk.LabelFrame(panes, text="Log")
        panes.add(preview_frame, weight=1)
        panes.add(log_frame, weight=1)

        self.preview_list = ScrolledText(preview_frame, height=12, wrap="none")
        self.preview_list.pack(fill="both", expand=True, padx=4, pady=4)
        self._register_text_widget(self.preview_list)

        self.log_text = ScrolledText(log_frame, height=12, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)
        self._register_text_widget(self.log_text)

        # reactive updates
        for var in [self.folder_var, self.glob_var, self.exclude_var, self.ext_var,
                    self.name_contains_var, self.min_size_var, self.max_size_var,
                    self.since_var, self.until_var, self.recursive_var]:
            var.trace_add("write", lambda *_: self._filters_changed())

        self.folder_var.trace_add("write", lambda *_: self._update_detected_season_label())

        self._update_detected_season_label()
        self._update_template_example()

    # ---------- Menu / Tree ----------

    def _build_tree_menu(self):
        self.menu = tk.Menu(self.tree, tearoff=0)
        self.menu.add_command(label="Open", command=self._open_selected)
        self.menu.add_command(label="Reveal in File Manager", command=self._reveal_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Copy Name", command=lambda: self._copy_selected("name"))
        self.menu.add_command(label="Copy Path", command=lambda: self._copy_selected("relpath"))
        self.tree.bind("<Button-3>", self._popup_menu)

    def _popup_menu(self, event):
        try:
            self.tree.selection_set(self.tree.identify_row(event.y))
            self.menu.post(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _selected_path(self) -> Optional[Path]:
        sel = self.tree.selection()
        if not sel:
            return None
        rel = self.tree.item(sel[0], "values")[3]
        base = Path(self.folder_var.get().strip())
        return base / rel

    def _open_selected(self, *_):
        p = self._selected_path()
        if p and p.exists():
            open_in_os(p)

    def _reveal_selected(self, *_):
        p = self._selected_path()
        if p and p.exists():
            reveal_in_os(p)

    def _copy_selected(self, which: str):
        sel = self.tree.selection()
        if not sel:
            return
        idx = {"name": 0, "relpath": 3}[which]
        txt = self.tree.item(sel[0], "values")[idx]
        self.master.clipboard_clear()
        self.master.clipboard_append(txt)

    def _sort_tree(self, col: str):
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        reverse = getattr(self, "_sort_rev_" + col, False)

        if col == "size":
            def key(x):
                try:
                    return parse_size_str(x[0]) or 0
                except Exception:
                    return 0
        else:
            key = lambda x: x[0].lower()

        items.sort(key=key, reverse=reverse)
        for i, (_, k) in enumerate(items):
            self.tree.move(k, "", i)

        setattr(self, "_sort_rev_" + col, not reverse)

    # ---------- Shortcuts / Settings ----------

    def _bind_shortcuts(self):
        self.master.bind("<Control-o>", lambda e: self._choose_folder())
        self.master.bind("<F5>", lambda e: self.refresh_file_list())
        self.master.bind("<Control-p>", lambda e: self.preview_plan())
        self.master.bind("<Control-r>", lambda e: self.run_plan())
        self.master.bind("<Control-d>", lambda e: self._toggle_dry())

    def _settings_path(self) -> Path:
        return Path.home() / ".bulk_edit_gui.json"

    def _load_settings(self):
        p = self._settings_path()
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(self, k):
                    var = getattr(self, k)
                    if isinstance(var, tk.Variable):
                        var.set(v)
            self.refresh_file_list()
            self._apply_dark(bool(data.get("dark_var", False)))
        except Exception:
            pass

    def _save_settings(self):
        keys = [k for k in self.__dict__ if isinstance(getattr(self, k), tk.Variable)]
        data = {k: getattr(self, k).get() for k in keys}
        try:
            self._settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---------- Dark mode (fixed) ----------

    def _register_text_widget(self, w: tk.Text):
        if w not in self._text_widgets:
            self._text_widgets.append(w)
            self._orig_text_cfg[w] = dict(
                bg=w.cget("bg"),
                fg=w.cget("fg"),
                insertbackground=w.cget("insertbackground") if "insertbackground" in w.keys() else "black",
                selectbackground=w.cget("selectbackground") if "selectbackground" in w.keys() else "#cce8ff",
                selectforeground=w.cget("selectforeground") if "selectforeground" in w.keys() else "black",
            )

    def _apply_dark(self, on: bool):
        s = self.style
        if on:
            try:
                s.theme_use("clam")
            except Exception:
                pass

            bg   = "#1f2328"
            acc  = "#2b3138"
            fg   = "#e6e6e6"
            sub  = "#9aa4ad"
            sel  = "#3b82f6"
            bghl = "#242a31"

            try:
                self.master.configure(bg=bg)
            except Exception:
                pass

            for cls in ("TFrame","TLabelframe","TLabelframe.Label","TLabel","TButton","TCheckbutton","TRadiobutton"):
                s.configure(cls, background=bg, foreground=fg)

            for cls in ("TEntry","TCombobox","TSpinbox"):
                s.configure(cls, fieldbackground=acc, background=acc, foreground=fg)
                s.map(cls,
                      foreground=[("disabled", sub), ("!disabled", fg)],
                      fieldbackground=[("readonly", acc), ("focus", acc)],
                      background=[("active", bghl), ("!active", acc)])

            s.configure("TNotebook", background=bg, borderwidth=0)
            s.configure("TNotebook.Tab", background=acc, foreground=fg)
            s.map("TNotebook.Tab",
                  background=[("selected", bghl), ("active", bghl), ("!selected", acc)],
                  foreground=[("selected", fg), ("!selected", sub)])

            s.configure("Treeview", background=acc, fieldbackground=acc, foreground=fg, bordercolor=bg)
            s.configure("Treeview.Heading", background=bghl, foreground=fg)
            s.map("Treeview", background=[("selected", sel)], foreground=[("selected", "#ffffff")])

            for w in self._text_widgets:
                w.configure(bg=acc, fg=fg, insertbackground=fg, selectbackground=sel, selectforeground=bg)

        else:
            try:
                s.theme_use(self._system_theme)
            except Exception:
                for cand in ("vista","xpnative","aqua","clam","default"):
                    if cand in s.theme_names():
                        s.theme_use(cand)
                        break

            def _reset_style(cls):
                try:
                    s.configure(cls, background="", foreground="", fieldbackground="", bordercolor="")
                    s.map(cls, background=[], foreground=[], fieldbackground=[])
                except Exception:
                    pass

            for cls in ("TFrame","TLabelframe","TLabelframe.Label","TLabel","TButton","TCheckbutton","TRadiobutton",
                        "TEntry","TCombobox","TSpinbox","TNotebook","TNotebook.Tab","Treeview","Treeview.Heading"):
                _reset_style(cls)

            try:
                self.master.configure(bg="")
            except Exception:
                pass

            for w in self._text_widgets:
                orig = self._orig_text_cfg.get(w)
                if not orig:
                    continue
                w.configure(bg=orig["bg"], fg=orig["fg"],
                            insertbackground=orig["insertbackground"],
                            selectbackground=orig["selectbackground"],
                            selectforeground=orig["selectforeground"])

    # ---------- Season logic (single vs multi-season) ----------

    def _season_for_path(self, file_path: Path) -> Tuple[int, Optional[int]]:
        """
        Returns (season_used, detected_season_or_None).
        - If auto-season off: manual
        - If auto-season on and per-file enabled: detect from the file's parent folders
        - Else (auto-season on but per-file disabled): detect from base folder name (legacy behavior)
        """
        base = Path(self.folder_var.get().strip()) if self.folder_var.get().strip() else Path(".")
        try:
            manual = int(self.inc_season_var.get().strip() or "1")
        except ValueError:
            manual = 1
        return season_for_path(
            file_path,
            base,
            manual,
            self.inc_auto_season_var.get(),
            self.season_per_file_var.get(),
        )

    def _update_detected_season_label(self):
        base_txt = self.folder_var.get().strip()
        if not base_txt:
            self.detected_label_var.set("Choose a base folder.")
            return

        base = Path(base_txt)
        if not self.inc_auto_season_var.get():
            self.detected_label_var.set("Auto-detect off. Using manual Season.")
            try: self.season_entry.state(["!disabled"])
            except Exception: pass
            self._update_template_example()
            return

        # show a helpful status line:
        if self.season_per_file_var.get():
            self.detected_label_var.set("Auto season: per-file (multi-season) - uses each file's season folder (Season 01/S02/etc).")
        else:
            det = detect_season_from_folder_name(base.name)
            if det is None:
                self.detected_label_var.set(f"No season found in base folder \"{base.name}\". Using manual Season.")
            else:
                self.detected_label_var.set(f"Detected season {det} from base folder \"{base.name}\"")

        # Manual season still allowed as fallback; don't lock it out
        try: self.season_entry.state(["!disabled"])
        except Exception: pass

        self._update_template_example()

    # ---------- Template Example ----------

    def _update_template_example(self):
        try:
            tmpl = (self.inc_template_var.get().strip() or "")
            if not tmpl:
                self.template_example_var.set("Example: (set a Template to see a preview)")
                return

            base_txt = self.folder_var.get().strip()
            n0 = int(self.inc_start_var.get().strip() or "1")

            # choose sample file (selected, else first matched)
            sample_path: Optional[Path] = None
            if base_txt:
                base = Path(base_txt)
                sel = self.tree.selection()
                if sel:
                    try:
                        rel = self.tree.item(sel[0], "values")[3]
                        sample_path = base / rel
                    except Exception:
                        sample_path = None
                if sample_path is None or not sample_path.exists():
                    _, files = self._collect_for_list()
                    sample_path = files[0] if files else None

            if sample_path is None:
                # generic placeholder example
                season = int(self.inc_season_var.get().strip() or "1")
                stem = "Example.File"
                ext = "mkv"
                dotext = ".mkv"
                parent = "Season 01"
            else:
                season, _det = self._season_for_path(sample_path)
                stem = sample_path.stem
                ext = sample_path.suffix[1:] if sample_path.suffix.startswith(".") else sample_path.suffix
                dotext = sample_path.suffix if sample_path.suffix else ""
                parent = sample_path.parent.name

            try:
                preview = tmpl.format(
                    n=n0, n0=n0 - 1, idx=0,
                    stem=stem, ext=ext, dotext=dotext, parent=parent,
                    season=season, season0=season - 1
                )
                self.template_example_var.set(f"Example: {preview}")
            except KeyError as e:
                self.template_example_var.set(f"Example: (template missing field: {e})")
            except Exception as e:
                self.template_example_var.set(f"Example error: {e}")
        except Exception:
            self.template_example_var.set("Example: (unable to render)")

    # ---------- Filters / listing ----------

    def _filters_changed(self):
        if self._filter_refresh_job:
            try:
                self.after_cancel(self._filter_refresh_job)
            except Exception:
                pass
        self._filter_refresh_job = self.after(250, self._refresh_after_filters_changed)

    def _refresh_after_filters_changed(self):
        self._filter_refresh_job = None
        self.refresh_file_list()

    def _choose_folder(self):
        path = filedialog.askdirectory(title="Select base folder")
        if path:
            self.folder_var.set(path)

    def _collect_for_list(self) -> Tuple[Path, List[Path]]:
        base_txt = self.folder_var.get().strip()
        if not base_txt:
            return Path("."), []
        base = Path(base_txt)
        if not base.exists() or not base.is_dir():
            return base, []
        try:
            files = list(collect_files(
                base=base,
                recursive=self.recursive_var.get(),
                globs=[g.strip() for g in self.glob_var.get().split(",") if g.strip()],
                exclude_globs=[g.strip() for g in self.exclude_var.get().split(",") if g.strip()],
                exts=[e.strip() for e in self.ext_var.get().split(",") if e.strip()],
                name_contains=(self.name_contains_var.get().strip() or None),
                min_size=parse_size_str(self.min_size_var.get().strip() or None),
                max_size=parse_size_str(self.max_size_var.get().strip() or None),
                since_ts=parse_time_str(self.since_var.get().strip() or None),
                until_ts=parse_time_str(self.until_var.get().strip() or None, end_of_day=True),
            ))
            files = self._sort_files(files, self.sort_mode_var.get())
            return base, files
        except Exception:
            return base, []

    def refresh_file_list(self):
        base, files = self._collect_for_list()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for p in files:
            try:
                st = p.stat()
                self.tree.insert("", "end", values=(p.name, fmt_size(st.st_size), fmt_mtime(st.st_mtime), str(p.relative_to(base))))
            except Exception:
                self.tree.insert("", "end", values=(p.name, "", "", str(p)))
        self.list_count_var.set(f"{len(files)} file(s)")
        self._update_template_example()

    # ---------- Sorting and numbering helpers ----------

    def _sort_files(self, files: List[Path], mode: str) -> List[Path]:
        if mode == "name_desc":
            return sorted(files, key=lambda p: p.name, reverse=True)
        if mode == "mtime_asc":
            return sorted(files, key=lambda p: p.stat().st_mtime)
        if mode == "mtime_desc":
            return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
        return sorted(files, key=lambda p: p.name)

    def _find_first_number_span(self, stem: str) -> Optional[Tuple[int, int]]:
        m = re.search(r"\d+", stem)
        return (m.start(), m.end()) if m else None

    def _format_episode_number(self, n: int, original_width: int, override_width: Optional[int]) -> str:
        width = override_width if (override_width and override_width > 0) else original_width
        return f"{n:0{width}d}" if width > 0 else str(n)

    # ---------- Config / Plan ----------

    def _get_config(self):
        def parse_int_field(raw: str, label: str, default: int, *, min_value: Optional[int] = None, disallow_zero: bool = False) -> int:
            text = raw.strip()
            if not text:
                value = default
            else:
                try:
                    value = int(text)
                except ValueError:
                    raise ValueError(f"{label} must be a whole number.")
            if disallow_zero and value == 0:
                raise ValueError(f"{label} cannot be 0.")
            if min_value is not None and value < min_value:
                raise ValueError(f"{label} must be at least {min_value}.")
            return value

        base = Path(self.folder_var.get().strip())
        if not base.exists() or not base.is_dir():
            raise ValueError("Please choose a valid base folder.")

        recursive = self.recursive_var.get()
        glob_list = [g.strip() for g in self.glob_var.get().split(",") if g.strip()]
        exclude_list = [g.strip() for g in self.exclude_var.get().split(",") if g.strip()]
        exts = [e.strip() for e in self.ext_var.get().split(",") if e.strip()]
        name_contains = self.name_contains_var.get().strip() or None
        min_size = parse_size_str(self.min_size_var.get().strip() or None)
        max_size = parse_size_str(self.max_size_var.get().strip() or None)
        since_ts = parse_time_str(self.since_var.get().strip() or None)
        until_ts = parse_time_str(self.until_var.get().strip() or None, end_of_day=True)
        if min_size is not None and max_size is not None and min_size > max_size:
            raise ValueError("Min size cannot be greater than max size.")
        if since_ts is not None and until_ts is not None and since_ts > until_ts:
            raise ValueError("Since date cannot be later than Until date.")

        prefix = self.prefix_var.get()
        suffix = self.suffix_var.get()
        set_ext = self.set_ext_var.get().strip() or None
        to_lower = self.lower_var.get()
        to_upper = self.upper_var.get()
        rename_from = self.rename_from_var.get().strip() or None
        rename_to = self.rename_to_var.get()
        if to_lower and to_upper:
            raise ValueError("Choose either lowercase or UPPERCASE, not both.")

        prepend = self.prepend_var.get() or None
        append = self.append_var.get() or None
        replace_pat = self.replace_pat_var.get().strip() or None
        replace_with = self.replace_with_var.get()
        fixed_string = self.fixed_string_var.get()
        ignore_case = self.ignore_case_var.get()
        multiline = self.multiline_var.get()

        inc_template = self.inc_template_var.get().strip() or ""
        inc_start = parse_int_field(self.inc_start_var.get(), "Start", 1)
        inc_step = parse_int_field(self.inc_step_var.get(), "Step", 1, disallow_zero=True)
        inc_season = parse_int_field(self.inc_season_var.get(), "Season", 1, min_value=0)

        partial_enable = self.partial_enable_var.get()
        partial_mode = self.partial_mode_var.get()
        ppw = self.partial_pad_width_var.get().strip()
        partial_pad_width = None
        if ppw:
            partial_pad_width = parse_int_field(ppw, "Pad width", 0, min_value=1)
        partial_trim_title = self.partial_trim_title_var.get()

        dry_run = self.dry_run_var.get()
        backup_suffix = (self.backup_var.get().strip() or "")
        encoding = self.encoding_var.get().strip() or "utf-8"
        errors = self.errors_var.get().strip() or "strict"
        binary_ok = self.binary_ok_var.get()

        any_rename = any([prefix, suffix, set_ext, to_lower, to_upper, (rename_from is not None)])
        any_content = any([prepend, append, (replace_pat is not None)])
        any_incremental = bool(inc_template)
        any_partial = bool(partial_enable)

        if not (any_incremental or any_partial or any_rename or any_content):
            raise ValueError("No operations selected.")

        return dict(
            base=base, recursive=recursive, globs=glob_list, exclude_globs=exclude_list,
            exts=exts, name_contains=name_contains, min_size=min_size, max_size=max_size,
            since_ts=since_ts, until_ts=until_ts,
            prefix=prefix, suffix=suffix, set_ext=set_ext, to_lower=to_lower, to_upper=to_upper,
            rename_from=rename_from, rename_to=rename_to,
            prepend=prepend, append=append, replace_pat=replace_pat, replace_with=replace_with,
            fixed_string=fixed_string, ignore_case=ignore_case, multiline=multiline,
            dry_run=dry_run, backup_suffix=backup_suffix, encoding=encoding, errors=errors,
            binary_ok=binary_ok,
            inc_template=inc_template, inc_start=inc_start, inc_step=inc_step,
            inc_season=inc_season, sort_mode=self.sort_mode_var.get(),
            partial_enable=partial_enable, partial_mode=partial_mode,
            partial_pad_width=partial_pad_width, partial_trim_title=partial_trim_title,
            # multi-season flags
            auto_season=self.inc_auto_season_var.get(),
            season_per_file=self.season_per_file_var.get(),
            reset_per_season=self.reset_counter_per_season_var.get(),
        )

    def _plan(self, cfg) -> Tuple[List[FilePlan], int, int, int]:
        files = list(collect_files(
            base=cfg["base"], recursive=cfg["recursive"],
            globs=cfg["globs"], exclude_globs=cfg["exclude_globs"],
            exts=cfg["exts"], name_contains=cfg["name_contains"],
            min_size=cfg["min_size"], max_size=cfg["max_size"],
            since_ts=cfg["since_ts"], until_ts=cfg["until_ts"]
        ))

        # If resetting per season, group sort by season then name/mtime (helps sane episode order)
        if cfg["reset_per_season"] and cfg["auto_season"] and cfg["season_per_file"]:
            def season_key(p: Path) -> Tuple[int, str]:
                det = detect_season_for_file(p, cfg["base"])
                s = det if det is not None else cfg["inc_season"]
                return (s, p.name.lower())
            files = sorted(files, key=season_key)
        else:
            files = self._sort_files(files, cfg["sort_mode"])

        plans: List[FilePlan] = []

        any_standard_rename = any([cfg["prefix"], cfg["suffix"], cfg["set_ext"], cfg["to_lower"], cfg["to_upper"], cfg["rename_from"] is not None])
        use_template = bool(cfg["inc_template"])
        use_partial = bool(cfg["partial_enable"])

        # Counters:
        # - global counter if not reset_per_season
        # - per-season counters if reset_per_season
        next_global = cfg["inc_start"]
        season_counters: Dict[int, int] = {}

        for idx, p in enumerate(files):
            fp = FilePlan(path=p)

            # Determine season for THIS file (if auto enabled)
            season_used, _detected = season_for_path(
                p,
                cfg["base"],
                cfg["inc_season"],
                cfg["auto_season"],
                cfg["season_per_file"],
            )

            # Episode number allocation
            if cfg["reset_per_season"] and cfg["auto_season"] and cfg["season_per_file"]:
                if season_used not in season_counters:
                    season_counters[season_used] = cfg["inc_start"]
                n_for_file = season_counters[season_used]
                season_counters[season_used] += cfg["inc_step"]
            else:
                n_for_file = next_global
                next_global += cfg["inc_step"]

            # 1) Template rename
            if use_template:
                try:
                    stem = p.stem
                    ext = p.suffix[1:] if p.suffix.startswith(".") else p.suffix
                    dotext = p.suffix if p.suffix else ""
                    parent = p.parent.name

                    fmt_vars = dict(
                        n=n_for_file, n0=(n_for_file - 1), idx=idx,
                        stem=stem, ext=ext, dotext=dotext, parent=parent,
                        season=season_used, season0=season_used - 1
                    )
                    new_name = cfg["inc_template"].format(**fmt_vars)
                    if not new_name:
                        raise ValueError("Empty result from template.")
                    if new_name != p.name:
                        fp.rename = RenamePlan(new_name=new_name)
                except KeyError as e:
                    fp.skipped_reason = f"Template missing field: {e}"
                except Exception as e:
                    fp.skipped_reason = f"Template error: {e}"

            # 2) Partial number update
            elif use_partial:
                try:
                    stem = p.stem
                    span = self._find_first_number_span(stem)
                    if not span:
                        fp.skipped_reason = "No number found in name"
                    else:
                        s, e = span
                        pre = stem[:s]
                        num = stem[s:e]
                        post = stem[e:]

                        formatted = self._format_episode_number(
                            n_for_file,
                            original_width=len(num),
                            override_width=cfg["partial_pad_width"]
                        )

                        if cfg["partial_mode"] == "update_in_place":
                            new_stem = pre + formatted + post
                        else:
                            new_post = post
                            if cfg["partial_trim_title"]:
                                new_post = new_post.lstrip(self._TRIM_CHARS)
                            if new_post:
                                new_stem = f"{formatted} - {new_post}" if not new_post.startswith(" ") else f"{formatted}{new_post}"
                            else:
                                new_stem = formatted

                        new_name = new_stem + p.suffix
                        if new_name != p.name:
                            fp.rename = RenamePlan(new_name=new_name)
                except Exception as e:
                    fp.skipped_reason = f"Partial rename error: {e}"

            # 3) Standard rename
            elif any_standard_rename:
                try:
                    rp = plan_rename_standard(
                        p, cfg["prefix"], cfg["suffix"], cfg["set_ext"],
                        cfg["to_lower"], cfg["to_upper"], cfg["rename_from"], cfg["rename_to"]
                    )
                    if rp:
                        fp.rename = rp
                except Exception as e:
                    fp.skipped_reason = f"Rename error: {e}"

            # Content edits (independent)
            try:
                if any([cfg["prepend"], cfg["append"], (cfg["replace_pat"] is not None)]):
                    text = safe_read_text(p, cfg["encoding"], cfg["errors"], cfg["binary_ok"])
                    changed, preview, _ = plan_content_edit(
                        text, cfg["prepend"], cfg["append"], cfg["replace_pat"], cfg["replace_with"],
                        cfg["fixed_string"], cfg["ignore_case"], cfg["multiline"]
                    )
                    if changed:
                        fp.content = ContentPlan(True, preview)
            except Exception as e:
                add_skip_reason(fp, str(e))

            plans.append(fp)

        self._annotate_rename_conflicts(plans)
        total = len(plans)
        will_rename = sum(1 for pl in plans if pl.rename and not pl.skipped_reason)
        will_edit = sum(1 for pl in plans if pl.content and pl.content.changed and not pl.skipped_reason)
        skipped = sum(1 for pl in plans if pl.skipped_reason)
        return plans, will_rename, will_edit, skipped

    def _annotate_rename_conflicts(self, plans: List[FilePlan]) -> None:
        candidates: List[Tuple[FilePlan, Path, str, str]] = []
        for pl in plans:
            if not pl.rename or not pl.rename.new_name or pl.skipped_reason:
                continue

            reason = validate_new_filename(pl.rename.new_name)
            if reason:
                add_skip_reason(pl, reason)
                continue

            try:
                target = pl.path.with_name(pl.rename.new_name)
            except ValueError as e:
                add_skip_reason(pl, f"Invalid target name: {e}")
                continue

            if same_path(pl.path, target) and pl.path.name == target.name:
                pl.rename = None
                continue

            candidates.append((pl, target, path_identity(target), path_identity(pl.path)))

        by_target: Dict[str, List[Tuple[FilePlan, Path, str, str]]] = {}
        for item in candidates:
            by_target.setdefault(item[2], []).append(item)

        for hits in by_target.values():
            if len(hits) < 2:
                continue
            target_name = hits[0][1].name
            for pl, *_ in hits:
                add_skip_reason(pl, f"Duplicate target name: {target_name}")

        active = [item for item in candidates if not item[0].skipped_reason]
        active_sources = {source_key for _, _, _, source_key in active}
        for pl, target, target_key, source_key in active:
            if target.exists() and target_key != source_key and target_key not in active_sources:
                add_skip_reason(pl, f"Target already exists: {target.name}")

    # ---------- Preview / Run / Undo ----------

    def preview_plan(self):
        self.preview_list.delete("1.0", tk.END)
        self.log_text.delete("1.0", tk.END)
        self.refresh_file_list()

        try:
            cfg = self._get_config()
        except Exception as e:
            messagebox.showerror("Invalid configuration", str(e))
            return

        plans, will_rename, will_edit, skipped = self._plan(cfg)
        base = cfg["base"]

        season_mode = "per-file" if (cfg["auto_season"] and cfg["season_per_file"]) else ("base" if cfg["auto_season"] else "manual")
        reset_note = "reset per season" if (cfg["reset_per_season"] and cfg["auto_season"] and cfg["season_per_file"]) else "continuous"
        mode_note = "template" if cfg["inc_template"] else ("partial" if cfg["partial_enable"] else "standard")

        header = [
            f"Matched files: {len(plans)} | rename mode: {mode_note}",
            f"Season mode: {season_mode} | episode numbering: {reset_note}",
            f"Would rename: {will_rename}",
            f"Would edit:   {will_edit}",
            *( [f"Skipped:     {skipped}"] if skipped else [] ),
            "",
        ]
        self.preview_list.insert(tk.END, "\n".join(header))

        for pl in plans:
            rel = display_path(pl.path, base)
            line = f"- {rel}"
            if pl.rename:
                line += f"  [rename -> {pl.rename.new_name}]"
            if pl.content and pl.content.changed:
                line += "  [edit]"
            if pl.skipped_reason:
                line += f"  [skip: {pl.skipped_reason}]"
            if pl.content and pl.content.preview:
                line += f"\n    preview: {pl.content.preview}"
            self.preview_list.insert(tk.END, line + "\n")

    def _check_dry_run(self):
        if not self.dry_run_var.get():
            if not messagebox.askyesno("Apply changes?", "Dry-run is OFF. The program will modify files. Continue?"):
                self.dry_run_var.set(True)

    def _toggle_dry(self):
        self.dry_run_var.set(not self.dry_run_var.get())
        self._check_dry_run()

    def _set_running(self, running: bool):
        self.run_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.undo_btn.config(state="disabled" if running else ("normal" if self.last_renames else "disabled"))

    def _stop_run(self):
        self.stop_flag.set()

    def run_plan(self):
        try:
            cfg = self._get_config()
        except Exception as e:
            messagebox.showerror("Invalid configuration", str(e))
            return

        if cfg["dry_run"] is False and not messagebox.askyesno("Confirm run", "Apply changes to files now?"):
            return

        self.stop_flag.clear()
        self.log_text.delete("1.0", tk.END)
        self._set_running(True)
        self.prog["value"] = 0

        self.worker = threading.Thread(target=self._do_run, args=(cfg,), daemon=True)
        self.worker.start()

    def _log(self, msg: str):
        self.msg_q.put(("log", msg))

    def _progress(self, value: int):
        self.msg_q.put(("progress", max(0, min(100, value))))

    def _run_finished(self):
        self.msg_q.put(("done", None))

    def _poll_log(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self.log_text.insert(tk.END, str(payload) + "\n")
                    self.log_text.see(tk.END)
                elif kind == "progress":
                    self.prog["value"] = int(payload)
                elif kind == "done":
                    self._set_running(False)
                    self.refresh_file_list()
                    self._save_settings()
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _unique_temp_rename_path(self, path: Path) -> Path:
        token = uuid.uuid4().hex[:10]
        for i in range(10000):
            suffix = f".~bulk-rename-{token}"
            if i:
                suffix += f"-{i}"
            candidate = path.with_name(path.name + suffix + ".tmp")
            if not candidate.exists():
                return candidate
        raise FileExistsError(f"Could not create temporary rename path for {path.name}")

    def _perform_rename_batch(
        self,
        rename_items: List[Tuple[Path, Path]],
        base: Path,
        *,
        record_undo: bool,
        action: str,
    ) -> List[Tuple[Path, Path]]:
        if not rename_items:
            return []

        source_keys = {path_identity(src) for src, _ in rename_items}
        staged: List[Tuple[Path, Path, Path]] = []
        completed: List[Tuple[Path, Path]] = []

        for src, dst in rename_items:
            try:
                if not src.exists():
                    self._log(f"[SKIP {action}] Missing source: {display_path(src, base)}")
                    continue
                src_key = path_identity(src)
                dst_key = path_identity(dst)
                if same_path(src, dst) and src.name == dst.name:
                    continue
                if dst.exists() and dst_key != src_key and dst_key not in source_keys:
                    self._log(f"[SKIP {action}] Target exists: {display_path(dst, base)}")
                    continue

                temp = self._unique_temp_rename_path(src)
                src.rename(temp)
                staged.append((temp, src, dst))
            except Exception as e:
                self._log(f"[SKIP {action}] {display_path(src, base)}: {e}")

        for temp, src, dst in staged:
            try:
                temp.rename(dst)
                completed.append((src, dst))
                if record_undo:
                    self.last_renames.append((dst, src))
                self._log(f"[{action}] {display_path(src, base)} -> {dst.name}")
            except Exception as e:
                self._log(f"[{action} ERROR] {display_path(src, base)} -> {dst.name}: {e}")
                try:
                    if temp.exists() and not src.exists():
                        temp.rename(src)
                except Exception as rollback_error:
                    self._log(f"[{action} ROLLBACK ERROR] {display_path(src, base)}: {rollback_error}")

        return completed

    def _do_run(self, cfg):
        try:
            plans, *_ = self._plan(cfg)
            base = cfg["base"]
            total = max(1, len(plans))
            done = 0
            self.last_renames.clear()
            deferred_renames: List[Tuple[Path, Path]] = []

            if cfg["dry_run"]:
                self._log("[DRY-RUN] Planning only. No changes will be written.")

            for pl in plans:
                if self.stop_flag.is_set():
                    self._log("Stopped by user.")
                    break

                # Content first
                if pl.content and pl.content.changed and not pl.skipped_reason:
                    try:
                        if not cfg["dry_run"]:
                            if cfg["backup_suffix"]:
                                make_backup(pl.path, cfg["backup_suffix"])
                            original = safe_read_text(pl.path, cfg["encoding"], cfg["errors"], cfg["binary_ok"])
                            _, _, final_text = plan_content_edit(
                                original, cfg["prepend"], cfg["append"],
                                cfg["replace_pat"], cfg["replace_with"],
                                cfg["fixed_string"], cfg["ignore_case"], cfg["multiline"]
                            )
                            safe_write_text(pl.path, final_text, cfg["encoding"])
                        self._log(f"[EDIT] {display_path(pl.path, base)}")
                    except Exception as e:
                        self._log(f"[SKIP EDIT] {pl.path.name}: {e}")

                if pl.skipped_reason:
                    self._log(f"[SKIP] {display_path(pl.path, base)} - {pl.skipped_reason}")
                elif pl.rename and pl.rename.new_name:
                    new_path = pl.path.with_name(pl.rename.new_name)
                    if cfg["dry_run"]:
                        self._log(f"[RENAME] {display_path(pl.path, base)} -> {pl.rename.new_name}")
                    else:
                        deferred_renames.append((pl.path, new_path))

                done += 1
                self._progress(int(done * 80 / total))

            if not cfg["dry_run"] and not self.stop_flag.is_set():
                self._perform_rename_batch(
                    deferred_renames,
                    base,
                    record_undo=True,
                    action="RENAME",
                )

            if not self.stop_flag.is_set():
                self._progress(100)

            self._log("Done.")
        except Exception as e:
            self._log(f"[ERROR] {e}")
        finally:
            self._run_finished()

    def _undo_last(self):
        if not self.last_renames:
            messagebox.showinfo("Undo", "Nothing to undo.")
            return

        base_txt = self.folder_var.get().strip()
        base = Path(base_txt) if base_txt else self.last_renames[0][0].parent
        undo_items = [(new_path, old_path) for new_path, old_path in reversed(self.last_renames)]
        completed = self._perform_rename_batch(
            undo_items,
            base,
            record_undo=False,
            action="UNDO",
        )
        completed_keys = {(path_identity(src), path_identity(dst)) for src, dst in completed}
        self.last_renames = [
            pair for pair in self.last_renames
            if (path_identity(pair[0]), path_identity(pair[1])) not in completed_keys
        ]
        self.undo_btn.config(state="normal" if self.last_renames else "disabled")
        self.refresh_file_list()

# ----------------------- Main -----------------------

def main():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        raise SystemExit(
            "Unable to start the Tkinter window.\n\n"
            "Your current Python install cannot find a usable Tcl/Tk runtime. "
            "Try Launch Bulk File Editor.bat, or repair/reinstall Python with the Tcl/Tk option enabled.\n\n"
            f"Original Tk error:\n{e}"
        ) from e

    app = BulkEditApp(root)
    if "--startup-check" in sys.argv:
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        return
    root.protocol("WM_DELETE_WINDOW", lambda: (app._save_settings(), root.destroy()))
    root.mainloop()

if __name__ == "__main__":
    main()
