#!/usr/bin/env python3
"""TUI session manager for Claude Code and Cursor Agent.

Architecture:
  - ``acsm`` tmux session: two panes (left=Textual picker, right=attached to agents session)
  - ``acsm-agents`` tmux session: one window per opened agent (acts as tabs)
  - Open  → new-window in acsm-agents
  - Attach → select-window in acsm-agents
  - Kill  → kill-window in acsm-agents
  All agent windows stay alive simultaneously; switching is instant.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.notifications import Notify
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from list_claude_sessions import collect_sessions as collect_claude_sessions
from list_cursor_sessions import collect_sessions as collect_cursor_sessions
from session_utils import (
    extract_user_query,
    format_timestamp,
    load_session_cache,
    save_session_cache,
)

TMUX_MAIN = "acsm"
TMUX_AGENTS = "acsm-agents"


def _runtime_state_dir() -> Path:
    """Return a per-user writable runtime directory for state files."""
    # Prefer XDG runtime dir; fallback to a uid-scoped temp dir.
    # We actively probe writability because switched-user shells may carry
    # an XDG_RUNTIME_DIR that points to another account.
    candidates: list[Path] = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(Path(runtime) / "acsm")
    candidates.append(Path(tempfile.gettempdir()) / f"acsm-{os.getuid()}")

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue

    # Last resort path; caller-side writes may still fail but we tried
    # all safer candidates first.
    return candidates[-1]


def _command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def _preflight_check(tool: str, *, skip: bool = False) -> bool:
    """Validate runtime dependencies before launching the TUI.

    Returns True when startup may continue, False when a fatal requirement is
    missing.
    """
    if skip:
        return True

    fatal_errors: list[str] = []
    warnings: list[str] = []

    if not _command_exists("tmux"):
        fatal_errors.append("Missing required command: tmux")

    if tool in ("claude", "all") and not _command_exists("claude"):
        warnings.append("`claude` CLI not found; Claude session open/resume will fail.")
    if tool in ("cursor", "all") and not _command_exists("agent"):
        warnings.append("`agent` CLI not found; Cursor session open/resume will fail.")

    if sys.platform == "win32":
        warnings.append(
            "Windows native shell detected; prefer WSL2 or SSH to Linux/macOS host."
        )

    clipboard_cmds = ("wl-copy", "xsel", "xclip", "pbcopy")
    if not any(_command_exists(cmd) for cmd in clipboard_cmds):
        warnings.append(
            "No clipboard backend found (wl-copy/xsel/xclip/pbcopy); copy actions may fail."
        )

    for warning in warnings:
        print(f"[acsm][warn] {warning}", file=sys.stderr)

    if fatal_errors:
        for err in fatal_errors:
            print(f"[acsm][error] {err}", file=sys.stderr)
        print("[acsm][error] Startup aborted. Install missing dependencies.", file=sys.stderr)
        return False
    return True


def _process_state_dir() -> Path:
    """Return a stable state directory shared by this app instance.

    We bootstrap tmux, then launch the picker in another process inside tmux.
    If state is keyed by PID, the child process cannot read the right-pane ID
    created during bootstrap and will keep creating duplicate right panes.
    """
    return _runtime_state_dir() / "panes" / TMUX_MAIN


def _right_pane_id_path() -> Path:
    return _process_state_dir() / "right-pane-id"


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

def _tmux(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux"] + list(args), capture_output=capture, text=True)


def _tmux_out(*args: str) -> str:
    return _tmux(*args, capture=True).stdout.strip()


def _session_alive(name: str) -> bool:
    return _tmux("has-session", "-t", name, capture=True).returncode == 0


def _resume_command(record: "SessionRecord") -> list[str]:
    if record.source == "claude":
        return ["claude", "--resume", record.session_id]
    return ["agent", "--resume", record.session_id]


def _window_name(source: str, session_id: str) -> str:
    # Use full session id as window name for stable one-to-one mapping.
    return session_id


def in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _save_right_pane_id(pane_id: str) -> None:
    try:
        path = _right_pane_id_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pane_id)
    except OSError:
        # Non-fatal: pane id persistence is an optimization.
        return


def get_right_pane_id() -> str | None:
    try:
        return _right_pane_id_path().read_text().strip() or None
    except (FileNotFoundError, PermissionError):
        return None


def _pane_exists(pane_id: str) -> bool:
    if not pane_id:
        return False
    r = _tmux("display-message", "-p", "-t", pane_id, "#{pane_id}", capture=True)
    return r.returncode == 0


def _infer_right_pane_id() -> str | None:
    """Best-effort discovery of an existing right pane in TMUX_MAIN."""
    out = _tmux_out("list-panes", "-t", TMUX_MAIN, "-F", "#{pane_id}")
    pane_ids = [p for p in out.splitlines() if p]
    if len(pane_ids) < 2:
        return None

    current = _tmux_out("display-message", "-p", "#{pane_id}") if in_tmux() else None
    for pane_id in pane_ids:
        if pane_id != current and _pane_exists(pane_id):
            return pane_id
    return None


def _ensure_right_pane() -> None:
    """Recreate the right attach pane if it was closed."""
    if not _session_alive(TMUX_MAIN):
        return
    _ensure_agents_session()

    pane_id = get_right_pane_id()
    if pane_id and _pane_exists(pane_id):
        return
    inferred = _infer_right_pane_id()
    if inferred:
        _save_right_pane_id(inferred)
        return

    right_pane = _tmux_out(
        "split-window", "-d", "-t", TMUX_MAIN, "-h", "-l", "55%",
        "-P", "-F", "#{pane_id}",
        f"unset TMUX; exec tmux attach-session -t {TMUX_AGENTS}",
    )
    if right_pane:
        _save_right_pane_id(right_pane)


def _configure_tmux_session(name: str) -> None:
    """Apply shared tmux settings (mouse, clipboard, scrollback)."""
    _tmux("set-option", "-t", name, "mouse", "on")
    _tmux("set-option", "-t", name, "history-limit", "50000")
    # Older tmux versions may not support this option.
    _tmux("set-option", "-t", name, "allow-passthrough", "on", capture=True)
    _tmux("set-option", "-t", name, "set-clipboard", "on")
    _tmux("set-option", "-t", name, "mode-keys", "vi")
    copy_cmd = "xclip -selection clipboard 2>/dev/null || xsel --clipboard 2>/dev/null || true"
    _tmux("bind-key", "-T", "copy-mode-vi",
          "MouseDragEnd1Pane", "send-keys", "-X", "copy-pipe-no-clear", copy_cmd)
    _tmux("bind-key", "-T", "copy-mode-vi",
          "MouseDown1Pane", "select-pane", r"\;", "send-keys", "-X", "clear-selection")


def _ensure_agents_session() -> None:
    """Create the agents session if it doesn't exist yet."""
    if not _session_alive(TMUX_AGENTS):
        _tmux(
            "new-session", "-d", "-s", TMUX_AGENTS,
            "-x", "200", "-y", "50",
            "echo 'Select a session from the left panel and click [Open]'; read",
        )
        _tmux("rename-window", "-t", f"{TMUX_AGENTS}:0", "placeholder")
        _configure_tmux_session(TMUX_AGENTS)
    _tmux("set-option", "-t", TMUX_AGENTS, "status", "on")
    _tmux("set-option", "-t", TMUX_AGENTS, "status-position", "top")
    _tmux("set-option", "-t", TMUX_AGENTS, "status-style", "bg=#1a1a2e,fg=#aaaaaa")
    _tmux("set-option", "-t", TMUX_AGENTS, "window-status-current-style", "bg=#44bb44,fg=#000000,bold")
    _tmux("set-option", "-t", TMUX_AGENTS, "window-status-style", "bg=#333333,fg=#aaaaaa")
    _tmux("set-option", "-t", TMUX_AGENTS, "window-status-format", " #W ")
    _tmux("set-option", "-t", TMUX_AGENTS, "window-status-current-format", " #W ")


def bootstrap_tmux() -> None:
    """If not inside our tmux session, create it with left/right split and exec into it."""
    if in_tmux():
        return

    if _session_alive(TMUX_MAIN):
        _tmux("kill-session", "-t", TMUX_MAIN)
    if _session_alive(TMUX_AGENTS):
        _tmux("kill-session", "-t", TMUX_AGENTS)

    _ensure_agents_session()

    if getattr(sys, "frozen", False):
        # PyInstaller binary: execute the binary itself.
        cmd_parts = [sys.executable, *sys.argv[1:]]
    else:
        cmd_parts = [sys.executable, *sys.argv]
    picker_cmd = " ".join(shlex.quote(a) for a in cmd_parts)

    _tmux(
        "new-session", "-d", "-s", TMUX_MAIN,
        "-x", str(os.get_terminal_size().columns),
        "-y", str(os.get_terminal_size().lines),
        picker_cmd,
    )
    _tmux("set-option", "-t", TMUX_MAIN, "mouse", "on")
    _tmux("set-option", "-t", TMUX_MAIN, "status", "off")

    right_pane = _tmux_out(
        "split-window", "-t", TMUX_MAIN, "-h", "-l", "55%",
        "-P", "-F", "#{pane_id}",
        f"unset TMUX; exec tmux attach-session -t {TMUX_AGENTS}",
    )
    _save_right_pane_id(right_pane)
    _tmux("select-pane", "-t", f"{TMUX_MAIN}.0")

    os.execvp("tmux", ["tmux", "attach-session", "-t", TMUX_MAIN])


def _agents_window_exists(win_name: str) -> bool:
    return bool(_agents_window_ids_by_name(win_name))


def _agents_windows() -> list[tuple[str, str]]:
    """List windows as (window_id, window_name) in agents session."""
    out = _tmux_out("list-windows", "-t", TMUX_AGENTS, "-F", "#{window_id}\t#{window_name}")
    if not out:
        return []
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        try:
            win_id, win_name = line.split("\t", 1)
        except ValueError:
            continue
        rows.append((win_id, win_name))
    return rows


def _agents_window_ids_by_name(win_name: str) -> list[str]:
    return [win_id for win_id, name in _agents_windows() if name == win_name]


def _agents_list_windows() -> list[str]:
    return [name for _, name in _agents_windows()]


def _agents_active_window() -> str | None:
    return _tmux_out(
        "display-message", "-t", TMUX_AGENTS, "-p", "#{window_name}",
    ) or None


def _agents_active_pane_id() -> str | None:
    return _tmux_out(
        "display-message", "-t", TMUX_AGENTS, "-p", "#{pane_id}",
    ) or None


def agents_create_window(win_name: str, cmd: str, cwd: str) -> None:
    """Create a new window in the agents session and switch to it."""
    _ensure_agents_session()
    _ensure_right_pane()
    _tmux(
        "new-window", "-t", TMUX_AGENTS,
        "-n", win_name, "-c", cwd, cmd,
    )
    placeholder_wins = [
        w for w in _agents_list_windows() if w == "placeholder"
    ]
    for _ in placeholder_wins:
        _tmux("kill-window", "-t", f"{TMUX_AGENTS}:placeholder")


def agents_select_window(win_name: str) -> None:
    """Switch the agents session to the given window."""
    _ensure_right_pane()
    ids = _agents_window_ids_by_name(win_name)
    if not ids:
        return
    # Use explicit window id to avoid ambiguous target when duplicate names exist.
    _tmux("select-window", "-t", ids[-1])


def agents_kill_window(win_name: str) -> None:
    """Kill a window in the agents session."""
    ids = _agents_window_ids_by_name(win_name)
    if not ids:
        return

    # Keep at least one window alive so `tmux attach-session -t acsm-agents`
    # in the right pane never exits.
    windows = _agents_list_windows()
    non_placeholder = [w for w in windows if w != "placeholder"]
    target_non_placeholder = [w for w in windows if w == win_name and w != "placeholder"]
    if len(non_placeholder) == len(target_non_placeholder) and non_placeholder:
        _tmux(
            "new-window", "-t", TMUX_AGENTS, "-n", "placeholder",
            "echo 'All sessions closed'; read",
        )

    for win_id in ids:
        _tmux("kill-window", "-t", win_id)


def agents_kill_all() -> None:
    """Kill all agent windows but keep the session alive with a placeholder."""
    if not _session_alive(TMUX_AGENTS):
        return
    windows = _agents_list_windows()
    if not windows:
        return
    _tmux(
        "new-window", "-t", TMUX_AGENTS, "-n", "placeholder",
        "echo 'All sessions closed'; read",
    )
    for w in windows:
        if w != "placeholder":
            _tmux("kill-window", "-t", f"{TMUX_AGENTS}:{w}")


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    source: str
    session_id: str
    name: str
    workspace: str
    session_path: str
    workspace_exists: bool
    created_at: int | None
    last_active: int | None
    match_preview: str = ""

    @property
    def display_name(self) -> str:
        return self.name if self.name else "(untitled)"


@dataclass
class OpenedSession:
    record: SessionRecord
    win_name: str


class SessionManager:
    def __init__(self) -> None:
        self.opened: dict[str, OpenedSession] = {}
        self.active_key: str | None = None

    @staticmethod
    def key(record: SessionRecord) -> str:
        return f"{record.source}:{record.session_id}"

    def is_running(self, record: SessionRecord) -> bool:
        k = self.key(record)
        o = self.opened.get(k)
        if not o:
            return False
        if _agents_window_exists(o.win_name):
            return True
        self.opened.pop(k, None)
        return False

    def is_attached(self, record: SessionRecord) -> bool:
        k = self.key(record)
        o = self.opened.get(k)
        if not o:
            return False
        active = _agents_active_window()
        return active == o.win_name

    def open_session(self, record: SessionRecord) -> str:
        k = self.key(record)
        o = self.opened.get(k)
        if o and _agents_window_exists(o.win_name):
            agents_select_window(o.win_name)
            self.active_key = k
            return o.win_name

        if o:
            self.opened.pop(k, None)

        win_name = _window_name(record.source, record.session_id)
        cmd = _resume_command(record)
        shell_cmd = " ".join(shlex.quote(c) for c in cmd)
        cwd = record.workspace if record.workspace_exists else str(Path.home())

        if _agents_window_exists(win_name):
            agents_kill_window(win_name)

        agents_create_window(win_name, shell_cmd, cwd)
        self.opened[k] = OpenedSession(record=record, win_name=win_name)
        self.active_key = k
        return win_name

    def attach_session(self, record: SessionRecord) -> bool:
        k = self.key(record)
        o = self.opened.get(k)
        if not o or not _agents_window_exists(o.win_name):
            self.opened.pop(k, None)
            return False
        agents_select_window(o.win_name)
        self.active_key = k
        return True

    def kill_session(self, record: SessionRecord) -> None:
        k = self.key(record)
        o = self.opened.pop(k, None)
        if o:
            agents_kill_window(o.win_name)
        if self.active_key == k:
            self.active_key = None

    def kill_all(self) -> None:
        agents_kill_all()
        self.opened.clear()
        self.active_key = None

    def running_count(self) -> int:
        self._gc()
        return len(self.opened)

    def _gc(self) -> None:
        dead = [k for k, o in self.opened.items()
                if not _agents_window_exists(o.win_name)]
        for k in dead:
            self.opened.pop(k, None)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class SessionListItem(Horizontal):
    DEFAULT_CSS = """
    SessionListItem {
        padding: 0 1;
        height: auto;
        border-bottom: solid #333333;
    }
    SessionListItem:hover { background: #1a2a3a; }
    SessionListItem.-selected { background: #1a3a5a; }
    SessionListItem.-running { border-left: thick #44bb44; }
    SessionListItem .item-info { height: auto; }
    SessionListItem .item-title { height: auto; }
    SessionListItem .item-detail { height: auto; color: #999999; }
    SessionListItem > Button { min-width: 10; height: 3; margin: 0 0 0 1; }
    """

    class Selected(Message):
        def __init__(self, item: "SessionListItem") -> None:
            self.item = item
            super().__init__()

    selected = reactive(False)

    def __init__(self, record: SessionRecord, running: bool = False, attached: bool = False) -> None:
        super().__init__()
        self.record = record
        self.session_running = running
        self.session_attached = attached

    def compose(self) -> ComposeResult:
        tag = "[b cyan]Claude[/]" if self.record.source == "claude" else "[b magenta]Cursor[/]"
        ws = self.record.workspace or "-"
        if not self.record.workspace_exists and ws != "-":
            ws += " [!]"
        ts = format_timestamp(self.record.last_active or self.record.created_at)

        if self.session_attached:
            status = "[b green]● RUNNING...[/]"
        elif self.session_running:
            status = "[b yellow]● RUNNING[/]"
        elif self.record.workspace_exists:
            status = "[b dodger_blue]◆ SUSPEND[/]"
        else:
            status = "[dim red]✕ INVALID[/]"

        with Vertical(classes="item-info"):
            hit = f"\n  ↳ {self.record.match_preview}" if self.record.match_preview else ""
            yield Static(f"{tag} {self.record.display_name}  {status}", classes="item-title")
            yield Static(
                f"  {self.record.session_id[:16]}… {ts}\n"
                f"  {ws}{hit}",
                classes="item-detail",
            )

        if self.session_attached:
            yield Button("Kill", id=f"kill_{id(self)}", variant="error")
        elif self.session_running:
            yield Button("Attach", id=f"attach_{id(self)}", variant="success")
            yield Button("Kill", id=f"kill_{id(self)}", variant="error")
        elif self.record.workspace_exists:
            yield Button("Open", id=f"open_{id(self)}", variant="primary")

    def on_mount(self) -> None:
        if self.session_running:
            self.add_class("-running")

    def watch_selected(self, val: bool) -> None:
        self.set_class(val, "-selected")

    async def on_click(self, event: events.Click) -> None:
        if event.button != 1:
            return
        self.post_message(self.Selected(self))


class CopyPathMenuScreen(ModalScreen[str | None]):
    CSS = """
    CopyPathMenuScreen {
        align: left top;
    }
    #copy_path_modal {
        width: 24;
        height: auto;
        padding: 0 1;
        border: round #666666;
        background: #1c1c1c;
    }
    #copy_path_title {
        height: 1;
        color: #cccccc;
    }
    CopyPathMenuScreen Button {
        height: 1;
        margin: 0;
        border: none;
    }
    """

    def __init__(self, filename: str, screen_x: int = 0, screen_y: int = 0) -> None:
        super().__init__()
        self.filename = filename
        self.screen_x = screen_x
        self.screen_y = screen_y

    def compose(self) -> ComposeResult:
        with Vertical(id="copy_path_modal"):
            title = self.filename
            if len(title) > 20:
                title = title[:17] + "..."
            yield Label(title, id="copy_path_title")
            yield Button("Name", id="copy_name", variant="primary")
            yield Button("Relative", id="copy_rel")
            yield Button("Absolute", id="copy_abs")
            yield Button("Cancel", id="copy_cancel", variant="error")

    def on_mount(self) -> None:
        menu = self.query_one("#copy_path_modal", Vertical)
        # Place near right-click position while staying on-screen.
        x = max(0, int(self.screen_x) - 1)
        y = max(0, int(self.screen_y) - 1)
        menu.styles.offset = (x, y)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "copy_name": "name",
            "copy_rel": "relative",
            "copy_abs": "absolute",
        }
        action = mapping.get(event.button.id or "")
        self.dismiss(action)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class SessionManagerApp(App):
    TITLE = "ACSM"
    CSS = """
    #top_controls { height: 7; }
    #top_left_controls { width: 1fr; height: 7; }
    #top_row1 { height: 1; }
    #row1_spacer { width: 1fr; }
    #ops_panel { height: 6; margin: 0 1; }
    #left_ops, #right_ops {
        width: 1fr;
        height: 6;
        border: round #555555;
        padding: 0 1;
    }
    #ops_header { height: 1; }
    #ops_buttons { height: 1; width: auto; }
    #ops_title { height: 1; color: #aaaaaa; width: 1fr; }
    #left_search, #right_search {
        height: 2;
        width: 1fr;
        color: #ffffff;
        background: #2a2a2a;
        border: none;
    }
    #session_title { padding: 0 1; height: 1; color: #aaaaaa; width: auto; }
    #page_prev_btn, #page_next_btn, #page_go_btn, #layout_btn, #jump_top_btn, #jump_bottom_btn, #right_top_btn, #right_bottom_btn {
        min-width: 8;
        height: 1;
        border: none;
        margin: 0 1 0 0;
    }
    #page_info { height: 1; color: #aaaaaa; width: 14; content-align: center middle; }
    #page_jump {
        width: 8;
        min-width: 8;
        height: 1;
        margin: 0 1 0 0;
        color: #ffffff;
        background: #2a2a2a;
        border: none;
    }
    #quit_btn { min-width: 10; height: 7; border: none; margin: 0 1 0 0; }
    #main_content { height: 1fr; layout: vertical; }
    #session_panel { height: 1fr; }
    #session_list { height: 1fr; }
    #files_panel { height: 1fr; border-top: solid #555555; }
    .layout-lmr #main_content { layout: horizontal; }
    .layout-lmr #session_panel { width: 1fr; height: 1fr; }
    .layout-lmr #files_panel { width: 1fr; height: 1fr; border-top: none; border-left: solid #555555; }
    #detail_title { height: auto; padding: 0 1; }
    #detail_info { height: auto; color: #999999; padding: 0 1; }
    #file_tabs { height: 1fr; }
    #file_copy_bar { height: 1; margin: 0 1; }
    #file_copy_bar Button { min-width: 10; height: 1; margin: 0 1 0 0; border: none; }
    #preview_copy_bar, #csv_copy_bar { height: 1; margin: 0 1; }
    #preview_copy_bar Button, #csv_copy_bar Button { min-width: 14; height: 1; border: none; margin: 0 1 0 0; }
    #file_preview { height: 1fr; }
    #csv_preview { height: 1fr; }
    #status_bar { height: 1; padding: 0 1; color: #aaaaaa; background: #1a1a1a; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, tool: str, keyword: str | None, page_size: int = 10) -> None:
        super().__init__()
        self.tool = tool
        self.keyword = keyword
        self.records: list[SessionRecord] = []
        self.filtered_records: list[SessionRecord] = []
        self.session_items: list[SessionListItem] = []
        self.selected_index: int = 0
        self.mgr = SessionManager()
        self._current_workspace: str | None = None
        self._open_inflight: set[str] = set()
        self._last_open_key: str | None = None
        self._last_open_ts: float = 0.0
        self.page_size: int = max(1, page_size)
        self.current_page: int = 0
        self._last_selected_file_path: Path | None = None
        self._context_file_path: Path | None = None
        self._suppress_preview_until: float = 0.0
        self._ignore_next_file_selected: bool = False
        self._ignore_file_selected_until: float = 0.0
        self._right_search_last_key: tuple[str, str] | None = None
        self._right_search_index: int = 0
        self._right_search_total: int = 0
        self._prompt_search_cache: dict[str, str | None] = {}
        self._prompt_search_cache_lock = threading.Lock()
        self._prompt_cache_dirty: bool = False
        self._claude_history_prompts: dict[str, list[str]] | None = None
        self._claude_history_stat: tuple[int, int] | None = None
        self._preview_language: str | None = None
        self._preview_wrap: bool = False
        self._csv_wrap: bool = False
        self.layout_mode: str = "upr"
        self._left_search_text: str = keyword or ""
        self._right_search_text: str = ""
        self._load_prompt_search_cache()
        self._load_ui_prefs()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top_controls"):
            with Vertical(id="top_left_controls"):
                with Horizontal(id="top_row1"):
                    yield Label("Sessions", id="session_title")
                    yield Button("◀ Prev", id="page_prev_btn")
                    yield Label("Page 1/1", id="page_info")
                    yield Button("Next ▶", id="page_next_btn")
                    yield Input(value="", placeholder="Page", id="page_jump")
                    yield Button("Go", id="page_go_btn")
                    yield Label("", id="row1_spacer")
                    yield Button("Layout: UPR", id="layout_btn")
                with Horizontal(id="ops_panel"):
                    with Vertical(id="left_ops"):
                        with Horizontal(id="ops_header"):
                            yield Label("Session List", id="ops_title")
                            with Horizontal(id="ops_buttons"):
                                yield Button("Top", id="jump_top_btn")
                                yield Button("Bottom", id="jump_bottom_btn")
                        yield Input(
                            value=self._left_search_text,
                            placeholder="Search sessions (press Enter)...",
                            id="left_search",
                        )
                    with Vertical(id="right_ops"):
                        with Horizontal(id="ops_header"):
                            yield Label("Session Detail", id="ops_title")
                            with Horizontal(id="ops_buttons"):
                                yield Button("Top", id="right_top_btn")
                                yield Button("Bottom", id="right_bottom_btn")
                        yield Input(
                            value=self._right_search_text,
                            placeholder="Search right detail (press Enter)...",
                            id="right_search",
                        )
            yield Button("✕ EXIT", id="quit_btn", variant="error")
        with Vertical(id="main_content"):
            with Vertical(id="session_panel"):
                yield VerticalScroll(id="session_list")
            with Vertical(id="files_panel"):
                yield Label("", id="detail_title")
                yield Label("", id="detail_info")
                with TabbedContent(id="file_tabs"):
                    with TabPane("Files", id="tab_files"):
                        with Vertical():
                            with Horizontal(id="file_copy_bar"):
                                yield Button("File Name", id="file_copy_name_btn")
                                yield Button("Rel Path", id="file_copy_rel_btn")
                                yield Button("Abs Path", id="file_copy_abs_btn")
                            yield DirectoryTree(str(Path.home()), id="dir_tree")
                    with TabPane("Preview", id="tab_preview"):
                        with Vertical():
                            with Horizontal(id="preview_copy_bar"):
                                yield Button("Copy Selection", id="preview_copy_btn")
                                yield Button("Wrap: Off", id="preview_wrap_btn")
                            yield TextArea(
                                "Select a file from the Files tab",
                                id="file_preview",
                                theme="monokai",
                                soft_wrap=False,
                                read_only=True,
                                show_line_numbers=True,
                            )
                    with TabPane("CSV", id="tab_csv"):
                        with Vertical():
                            with Horizontal(id="csv_copy_bar"):
                                yield Button("Copy Selection", id="csv_copy_btn")
                                yield Button("Wrap: Off", id="csv_wrap_btn")
                            yield TextArea(
                                "Select a CSV file from the Files tab",
                                id="csv_preview",
                                soft_wrap=False,
                                read_only=True,
                                show_line_numbers=True,
                            )
        yield Label("", id="status_bar")

    def on_mount(self) -> None:
        self._apply_layout_mode()
        self.records = self._collect_records()
        self.filtered_records = self._filter_records(self._left_search_text)
        self.current_page = 0
        self._render_list()
        if self.session_items:
            self._set_selected(0)
        self._update_status()

    def _on_notify(self, event: Notify) -> None:
        """Keep at most three toast notifications visible."""
        self._notifications.add(event.notification)
        while len(self._notifications) > 3:
            oldest = next(iter(self._notifications), None)
            if oldest is None:
                break
            del self._notifications[oldest]
        self._refresh_notifications()

    def on_unmount(self) -> None:
        self._save_ui_prefs()
        self._flush_prompt_search_cache()
        self.mgr.kill_all()
        if _session_alive(TMUX_AGENTS):
            _tmux("kill-session", "-t", TMUX_AGENTS)

    # --- data ---

    def _collect_records(self) -> list[SessionRecord]:
        rows: list[SessionRecord] = []
        kw = self.keyword.lower() if self.keyword else None
        collectors: list[tuple[str, object]] = []
        if self.tool in ("claude", "all"):
            collectors.append(("claude", collect_claude_sessions))
        if self.tool in ("cursor", "all"):
            collectors.append(("cursor", collect_cursor_sessions))

        if len(collectors) == 1:
            source, collector = collectors[0]
            for s in collector(kw):
                rows.append(SessionRecord(
                    source=source, session_id=s["id"],
                    name=s["name"] or "", workspace=s["workspace"] or "",
                    session_path=s.get("session_path", "") or "",
                    workspace_exists=s.get("workspace_exists", False),
                    created_at=s.get("created_at"), last_active=s.get("last_active"),
                ))
        elif collectors:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(collectors)) as executor:
                future_to_source = {
                    executor.submit(collector, kw): source for source, collector in collectors
                }
                for future in concurrent.futures.as_completed(future_to_source):
                    source = future_to_source[future]
                    try:
                        sessions = future.result()
                    except Exception:
                        sessions = []
                    for s in sessions:
                        rows.append(SessionRecord(
                            source=source, session_id=s["id"],
                            name=s["name"] or "", workspace=s["workspace"] or "",
                            session_path=s.get("session_path", "") or "",
                            workspace_exists=s.get("workspace_exists", False),
                            created_at=s.get("created_at"), last_active=s.get("last_active"),
                        ))
        rows.sort(key=lambda r: r.last_active or r.created_at or 0, reverse=True)
        return rows

    def _filter_records(self, text: str) -> list[SessionRecord]:
        kw = text.strip().lower()
        if not kw:
            for rec in self.records:
                rec.match_preview = ""
            return list(self.records)
        matched: list[SessionRecord] = []
        need_prompt_scan: list[SessionRecord] = []
        for r in self.records:
            r.match_preview = ""
            if (
                kw in r.source.lower()
                or kw in r.name.lower()
                or kw in r.workspace.lower()
                or kw in r.session_id.lower()
            ):
                matched.append(r)
                continue
            if len(kw) < 2:
                continue
            need_prompt_scan.append(r)

        if need_prompt_scan:
            if len(need_prompt_scan) < 12:
                for r in need_prompt_scan:
                    snippet = self._first_prompt_hit_preview(r, kw)
                    if snippet:
                        r.match_preview = snippet
                        matched.append(r)
            else:
                max_workers = min(8, max(2, (os.cpu_count() or 2) // 2))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(self._first_prompt_hit_preview, r, kw): r
                        for r in need_prompt_scan
                    }
                    for future in concurrent.futures.as_completed(futures):
                        r = futures[future]
                        try:
                            snippet = future.result()
                        except Exception:
                            snippet = None
                        if snippet:
                            r.match_preview = snippet
                            matched.append(r)
        return matched

    def _first_prompt_hit_preview(self, record: SessionRecord, keyword: str) -> str | None:
        if record.source == "claude":
            return self._first_prompt_hit_from_claude_history(record.session_id, keyword)

        path = record.session_path
        if not path or not os.path.isfile(path):
            return None
        try:
            st = os.stat(path)
            key = self._prompt_cache_key(path, int(st.st_mtime_ns), int(st.st_size), keyword)
        except OSError:
            return None
        with self._prompt_search_cache_lock:
            if key in self._prompt_search_cache:
                return self._prompt_search_cache[key]

        ext = Path(path).suffix.lower()
        if ext == ".jsonl":
            snippet = self._first_prompt_hit_jsonl(path, keyword)
        elif ext == ".txt":
            snippet = self._first_prompt_hit_txt(path, keyword)
        else:
            snippet = None
        with self._prompt_search_cache_lock:
            self._prompt_search_cache[key] = snippet
            self._prompt_cache_dirty = True
        return snippet

    def _first_prompt_hit_from_claude_history(self, session_id: str, keyword: str) -> str | None:
        history_path = Path.home() / ".claude" / "history.jsonl"
        if not history_path.exists():
            return None
        try:
            st = history_path.stat()
            stat_sig = (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

        if self._claude_history_prompts is None or self._claude_history_stat != stat_sig:
            self._claude_history_prompts = self._load_claude_history_prompts(history_path)
            self._claude_history_stat = stat_sig

        cache_key = self._prompt_cache_key(
            f"claude-history:{session_id}",
            stat_sig[0],
            stat_sig[1],
            keyword,
        )
        with self._prompt_search_cache_lock:
            if cache_key in self._prompt_search_cache:
                return self._prompt_search_cache[cache_key]

        prompts = self._claude_history_prompts.get(session_id, []) if self._claude_history_prompts else []
        snippet = None
        for text in prompts:
            snippet = self._snippet_around_keyword(text, keyword)
            if snippet:
                break

        with self._prompt_search_cache_lock:
            self._prompt_search_cache[cache_key] = snippet
            self._prompt_cache_dirty = True
        return snippet

    @staticmethod
    def _load_claude_history_prompts(path: Path) -> dict[str, list[str]]:
        prompts_by_sid: dict[str, list[str]] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = obj.get("sessionId")
                    display = (obj.get("display") or "").strip()
                    if not sid or not display:
                        continue
                    if display.startswith("/"):
                        continue
                    prompts_by_sid.setdefault(sid, []).append(display)
        except OSError:
            return {}
        return prompts_by_sid

    @staticmethod
    def _prompt_cache_key(path: str, mtime_ns: int, size: int, keyword: str) -> str:
        return json.dumps([path, mtime_ns, size, keyword], ensure_ascii=False)

    def _load_prompt_search_cache(self) -> None:
        cache = load_session_cache("prompt_search_cache")
        entries = cache.get("entries", {})
        if not isinstance(entries, dict):
            return
        # Keep a bounded cache in memory.
        if len(entries) > 30000:
            items = list(entries.items())[-30000:]
            entries = dict(items)
        self._prompt_search_cache = {
            str(k): (v if isinstance(v, str) or v is None else None)
            for k, v in entries.items()
        }

    def _flush_prompt_search_cache(self) -> None:
        with self._prompt_search_cache_lock:
            if not self._prompt_cache_dirty:
                return
            entries = self._prompt_search_cache
            if len(entries) > 30000:
                items = list(entries.items())[-30000:]
                entries = dict(items)
                self._prompt_search_cache = entries
            save_session_cache("prompt_search_cache", {"version": 1, "entries": entries})
            self._prompt_cache_dirty = False

    @staticmethod
    def _snippet_around_keyword(text: str, keyword: str, radius: int = 36) -> str | None:
        plain = " ".join(text.split())
        if not plain:
            return None
        lower_plain = plain.casefold()
        lower_kw = keyword.casefold()
        idx = lower_plain.find(lower_kw)
        if idx < 0:
            return None
        start = max(0, idx - radius)
        end = min(len(plain), idx + len(keyword) + radius)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(plain) else ""
        return f"{prefix}{plain[start:end]}{suffix}"

    def _first_prompt_hit_jsonl(self, path: str, keyword: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    text = ""
                    if obj.get("type") == "user":
                        msg = obj.get("message", {})
                        if msg.get("role") == "user":
                            content = msg.get("content")
                            if isinstance(content, str):
                                text = content
                            elif isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        text = part.get("text", "")
                                        break
                    elif obj.get("role") == "user":
                        content_parts = obj.get("message", {}).get("content", [])
                        for part in content_parts:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = extract_user_query(part.get("text", ""))
                                break

                    if not text:
                        continue
                    snippet = self._snippet_around_keyword(text, keyword)
                    if snippet:
                        return snippet
        except OSError:
            return None
        return None

    def _first_prompt_hit_txt(self, path: str, keyword: str) -> str | None:
        speaker_re = re.compile(r"^\s*[A-Z]:\s*$")
        in_user = False
        block_lines: list[str] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    s = raw.strip()
                    if not in_user:
                        if s.lower() == "user:":
                            in_user = True
                            block_lines = []
                        continue
                    if speaker_re.match(raw) or s.startswith("[Tool call]"):
                        text = extract_user_query("".join(block_lines))
                        snippet = self._snippet_around_keyword(text, keyword) if text else None
                        if snippet:
                            return snippet
                        in_user = s.lower() == "user:"
                        block_lines = []
                        continue
                    block_lines.append(raw)
            if in_user and block_lines:
                text = extract_user_query("".join(block_lines))
                snippet = self._snippet_around_keyword(text, keyword) if text else None
                if snippet:
                    return snippet
        except OSError:
            return None
        return None

    def _sorted_records(self) -> list[SessionRecord]:
        def sort_key(rec: SessionRecord) -> tuple[int, int]:
            running = 0 if self.mgr.is_running(rec) else 1
            ts = rec.last_active or rec.created_at or 0
            return (running, -ts)
        return sorted(self.filtered_records, key=sort_key)

    # --- list ---

    def _render_list(self) -> None:
        container = self.query_one("#session_list", VerticalScroll)
        container.remove_children()
        self.session_items.clear()
        sorted_records = self._sorted_records()
        total = len(sorted_records)
        total_pages = self._total_pages(total)
        self.current_page = max(0, min(self.current_page, total_pages - 1))
        start = self.current_page * self.page_size
        end = start + self.page_size
        page_records = sorted_records[start:end]

        for rec in page_records:
            running = self.mgr.is_running(rec)
            attached = self.mgr.is_attached(rec) if running else False
            item = SessionListItem(rec, running=running, attached=attached)
            self.session_items.append(item)
            container.mount(item)
        self._update_pagination(total, total_pages)

    def _set_selected(self, index: int) -> None:
        if not self.session_items:
            self.selected_index = 0
            self._update_detail(None)
            return
        self.selected_index = max(0, min(index, len(self.session_items) - 1))
        for i, item in enumerate(self.session_items):
            item.selected = (i == self.selected_index)
        self.session_items[self.selected_index].scroll_visible()
        rec = self.session_items[self.selected_index].record
        self._update_detail(rec)
        self._update_file_tree(rec)

    def _update_detail(self, record: SessionRecord | None) -> None:
        title = self.query_one("#detail_title", Label)
        info = self.query_one("#detail_info", Label)

        if not record:
            title.update("")
            info.update("")
            return

        tag = "Claude" if record.source == "claude" else "Cursor"
        running = self.mgr.is_running(record)
        attached = self.mgr.is_attached(record) if running else False
        if attached:
            status = "RUNNING..."
        elif running:
            status = "RUNNING"
        elif record.workspace_exists:
            status = "SUSPEND"
        else:
            status = "INVALID"
        title.update(f"{tag}: {record.display_name}")
        ws = record.workspace or "-"
        if not record.workspace_exists and ws != "-":
            ws += " [NOT FOUND]"
        info.update(f"{ws} | {status} | {format_timestamp(record.last_active)}")

    def _update_file_tree(self, record: SessionRecord) -> None:
        ws = record.workspace
        if not record.workspace_exists or not ws:
            return
        if ws == self._current_workspace:
            return
        self._current_workspace = ws
        tree = self.query_one("#dir_tree", DirectoryTree)
        tree.path = ws
        tree.reload()

    def _update_status(self) -> None:
        bar = self.query_one("#status_bar", Label)
        n = len(self.filtered_records)
        nr = self.mgr.running_count()
        total_pages = self._total_pages(n)
        parts = [f"{n} sessions"]
        if nr:
            parts.append(f"{nr} running")
        parts.append(f"page {self.current_page + 1}/{total_pages}")
        parts.append(f"size {self.page_size}")
        parts.append(f"layout {self.layout_mode.upper()}")
        parts.append(f"preview {self._preview_language or 'plain'}")
        parts.append("click to interact")
        bar.update(" | ".join(parts))

    def _apply_layout_mode(self) -> None:
        self.remove_class("layout-lmr")
        if self.layout_mode == "lmr":
            self.add_class("layout-lmr")
        btn = self.query_one("#layout_btn", Button)
        btn.label = f"Layout: {self.layout_mode.upper()}"

    def _toggle_layout_mode(self) -> None:
        self.layout_mode = "lmr" if self.layout_mode == "upr" else "upr"
        self._apply_layout_mode()
        self._save_ui_prefs()
        self._update_status()

    def _load_ui_prefs(self) -> None:
        cache = load_session_cache("ui_prefs")
        entries = cache.get("entries", {})
        if not isinstance(entries, dict):
            return

        layout = entries.get("layout_mode")
        if layout in {"upr", "lmr"}:
            self.layout_mode = layout

        # Command line keyword has higher priority than saved left-search text.
        if self.keyword is None:
            left = entries.get("left_search_text")
            if isinstance(left, str):
                self._left_search_text = left

        right = entries.get("right_search_text")
        if isinstance(right, str):
            self._right_search_text = right

    def _save_ui_prefs(self) -> None:
        left_search = self._left_search_text
        right_search = self._right_search_text
        try:
            left_input = self.query_one("#left_search", Input)
            right_input = self.query_one("#right_search", Input)
            left_search = left_input.value
            right_search = right_input.value
        except Exception:
            pass

        save_session_cache(
            "ui_prefs",
            {
                "version": 1,
                "entries": {
                    "layout_mode": self.layout_mode,
                    "left_search_text": left_search,
                    "right_search_text": right_search,
                },
            },
        )

    def _update_pagination(self, total: int, total_pages: int) -> None:
        page_info = self.query_one("#page_info", Label)
        page_info.update(f"Page {self.current_page + 1}/{total_pages}")
        prev_btn = self.query_one("#page_prev_btn", Button)
        next_btn = self.query_one("#page_next_btn", Button)
        prev_btn.disabled = self.current_page <= 0
        next_btn.disabled = self.current_page >= (total_pages - 1) or total == 0

    def _change_page(self, delta: int) -> None:
        total = len(self.filtered_records)
        total_pages = self._total_pages(total)
        new_page = max(0, min(self.current_page + delta, total_pages - 1))
        if new_page == self.current_page:
            return
        self.current_page = new_page
        self._render_list()
        if self.session_items:
            self._set_selected(0)
        else:
            self._update_detail(None)
        self._update_status()

    def _total_pages(self, total_records: int | None = None) -> int:
        total = len(self.filtered_records) if total_records is None else total_records
        return max(1, (total + self.page_size - 1) // self.page_size)

    def _goto_page(self, page_number: int) -> None:
        total_pages = self._total_pages()
        target = max(1, min(page_number, total_pages))
        if target - 1 == self.current_page:
            return
        self.current_page = target - 1
        self._render_list()
        if self.session_items:
            self._set_selected(0)
        else:
            self._update_detail(None)
        self._update_status()

    def _go_to_page_input(self) -> None:
        page_jump = self.query_one("#page_jump", Input)
        raw = page_jump.value.strip()
        if not raw:
            return
        try:
            page_num = int(raw)
        except ValueError:
            self.notify("Invalid page number", severity="warning", timeout=2)
            return
        self._goto_page(page_num)

    def _jump_to_top(self) -> None:
        self.current_page = 0
        self._render_list()
        if self.session_items:
            self._set_selected(0)
        else:
            self._update_detail(None)
        self._update_status()

    def _jump_to_bottom(self) -> None:
        total_pages = self._total_pages()
        self.current_page = max(0, total_pages - 1)
        self._render_list()
        if self.session_items:
            self._set_selected(len(self.session_items) - 1)
        else:
            self._update_detail(None)
        self._update_status()

    def _selected_record(self) -> SessionRecord | None:
        if not self.session_items:
            return None
        idx = max(0, min(self.selected_index, len(self.session_items) - 1))
        return self.session_items[idx].record

    # --- events ---

    async def on_session_list_item_selected(self, msg: SessionListItem.Selected) -> None:
        if msg.item in self.session_items:
            self._set_selected(self.session_items.index(msg.item))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "quit_btn":
            self._do_quit()
            return
        if btn_id == "page_prev_btn":
            self._change_page(-1)
            return
        if btn_id == "page_next_btn":
            self._change_page(1)
            return
        if btn_id == "page_go_btn":
            self._go_to_page_input()
            return
        if btn_id == "layout_btn":
            self._toggle_layout_mode()
            return
        if btn_id == "jump_top_btn":
            self._jump_to_top()
            return
        if btn_id == "jump_bottom_btn":
            self._jump_to_bottom()
            return
        if btn_id == "file_copy_name_btn":
            self._copy_current_file_path("name")
            return
        if btn_id == "file_copy_rel_btn":
            self._copy_current_file_path("relative")
            return
        if btn_id == "file_copy_abs_btn":
            self._copy_current_file_path("absolute")
            return
        if btn_id == "preview_copy_btn":
            self._copy_preview_selection()
            return
        if btn_id == "preview_wrap_btn":
            self._toggle_preview_wrap()
            return
        if btn_id == "csv_copy_btn":
            self._copy_csv_selection()
            return
        if btn_id == "csv_wrap_btn":
            self._toggle_csv_wrap()
            return
        if btn_id == "right_top_btn":
            self._right_jump_top()
            return
        if btn_id == "right_bottom_btn":
            self._right_jump_bottom()
            return

        item: SessionListItem | None = None
        widget = event.button
        while widget is not None:
            if isinstance(widget, SessionListItem):
                item = widget
                break
            widget = widget.parent

        if not item:
            return

        rec = item.record
        if btn_id.startswith("open_"):
            self._do_open(rec)
        elif btn_id.startswith("attach_"):
            self._do_attach(rec)
        elif btn_id.startswith("kill_"):
            self._do_kill(rec)

    def _do_quit(self) -> None:
        self.mgr.kill_all()
        if _session_alive(TMUX_AGENTS):
            _tmux("kill-session", "-t", TMUX_AGENTS)
        if _session_alive(TMUX_MAIN):
            _tmux("kill-session", "-t", TMUX_MAIN)
        self.exit()

    def on_input_changed(self, event: Input.Changed) -> None:
        # Searches are applied only on Enter to avoid expensive filtering
        # on every keystroke when session count is large.
        if event.input.id not in {"left_search", "right_search"}:
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "page_jump":
            self._go_to_page_input()
        elif event.input.id == "right_search":
            self._right_search_text = event.value
            self._right_find(event.value)
            self._save_ui_prefs()
        elif event.input.id == "left_search":
            self._left_search_text = event.value
            self.filtered_records = self._filter_records(event.value)
            self.current_page = 0
            self._render_list()
            if self.session_items:
                self._set_selected(0)
            self._update_status()
            self._save_ui_prefs()

    def on_key(self, event: events.Key) -> None:
        if event.key != "ctrl+c":
            return
        focused = self.focused
        if focused is self.query_one("#file_preview", TextArea):
            self._copy_preview_selection()
            event.stop()
            return
        if focused is self.query_one("#csv_preview", TextArea):
            self._copy_csv_selection()
            event.stop()

    @on(DirectoryTree.FileSelected)
    def on_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        try:
            now = time.monotonic()
            if self._ignore_next_file_selected and now < self._ignore_file_selected_until:
                self._ignore_next_file_selected = False
                return
            self._last_selected_file_path = Path(event.path)
            if time.monotonic() < self._suppress_preview_until:
                return
            self._preview_file(str(event.path))
        except Exception as e:
            self.notify(f"Preview error: {e}", severity="error", timeout=3)

    @on(DirectoryTree.NodeHighlighted)
    def on_dir_node_highlighted(self, event: DirectoryTree.NodeHighlighted) -> None:
        path = self._path_from_tree_node(event.node)
        if path and path.exists():
            self._last_selected_file_path = path

    def _event_under_tree(self, widget) -> bool:
        tree = self.query_one("#dir_tree", DirectoryTree)
        while widget is not None:
            if widget is tree:
                return True
            widget = widget.parent
        return False

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3 or not self._event_under_tree(event.widget):
            return
        self._ignore_next_file_selected = True
        self._ignore_file_selected_until = time.monotonic() + 1.0
        self._suppress_preview_until = time.monotonic() + 0.8
        event.stop()
        try:
            event.prevent_default()
        except Exception:
            pass

    def on_click(self, event: events.Click) -> None:
        if event.button != 3 or not self._event_under_tree(event.widget):
            return
        path = self._current_tree_path()
        if not path:
            self.notify("No path selected", severity="warning", timeout=2)
            return
        self._context_file_path = path
        sx = int(event.screen_x if event.screen_x is not None else event.x)
        sy = int(event.screen_y if event.screen_y is not None else event.y)
        self.push_screen(CopyPathMenuScreen(path.name, sx, sy), self._on_copy_path_action)
        event.stop()
        try:
            event.prevent_default()
        except Exception:
            pass

    # --- file preview ---

    def _preview_file(self, path: str) -> None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return

        if size > 2 * 1024 * 1024:
            self._show_text_preview(f"File too large ({size // 1024}KB)")
            return

        if path.lower().endswith(".csv"):
            try:
                self._show_csv(path)
            except Exception:
                self._show_text_preview("Cannot preview this CSV file")
            return

        try:
            with open(path, "rb") as fb:
                head = fb.read(8192)
            if b"\x00" in head:
                self._show_text_preview(f"[Binary file – {size} bytes]")
                return
        except OSError:
            return

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(200_000)
        except OSError as e:
            self._show_text_preview(f"Cannot read: {e}")
            return

        try:
            lang = self._detect_language(path)
            self._show_text_preview(content, lang)
        except Exception:
            self._show_text_preview(content)

    def _show_text_preview(self, text: str, language: str | None = None) -> None:
        preview = self.query_one("#file_preview", TextArea)
        preview.soft_wrap = self._preview_wrap
        # TextArea may reset syntax parser after loading new content in some
        # versions; set language both before and after load for reliability.
        preview.language = language
        preview.load_text(text)
        preview.language = language
        self._preview_language = language
        tabs = self.query_one("#file_tabs", TabbedContent)
        tabs.active = "tab_preview"
        self._update_status()

    @staticmethod
    def _detect_language(path: str) -> str | None:
        ext_map = {
            ".py": "python", ".pyw": "python",
            ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
            ".jsx": "jsx", ".tsx": "tsx",
            ".ts": "typescript",
            ".rs": "rust",
            ".go": "go",
            ".c": "c", ".h": "c",
            ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp", ".hpp": "cpp",
            ".java": "java",
            ".rb": "ruby",
            ".sh": "bash", ".bash": "bash", ".zsh": "bash",
            ".json": "json",
            ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml",
            ".md": "markdown",
            ".html": "html", ".htm": "html",
            ".css": "css",
            ".sql": "sql",
            ".xml": "xml",
            ".lua": "lua",
            ".kt": "kotlin",
            ".swift": "swift",
            ".r": "r",
            ".dockerfile": "dockerfile",
        }
        ext = Path(path).suffix.lower()
        name = Path(path).name.lower()
        if name == "dockerfile":
            return "dockerfile"
        if name == "makefile":
            return "bash"
        return ext_map.get(ext)

    def _show_csv(self, path: str) -> None:
        csv_preview = self.query_one("#csv_preview", TextArea)
        rows: list[list[str]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                headers = next(reader, None)
                if not headers:
                    csv_preview.load_text("Empty CSV")
                    return
                rows.append(headers)
                for i, row in enumerate(reader):
                    if i >= 5000:
                        break
                    rows.append(row)
        except Exception as e:
            self._show_text_preview(f"CSV error: {e}")
            return
        rendered = "\n".join("\t".join(col for col in row) for row in rows)
        csv_preview.language = None
        csv_preview.soft_wrap = self._csv_wrap
        csv_preview.load_text(rendered)
        self._preview_language = "csv"
        tabs = self.query_one("#file_tabs", TabbedContent)
        tabs.active = "tab_csv"
        self._update_status()

    @staticmethod
    def _path_from_tree_node(node) -> Path | None:
        data = getattr(node, "data", None)
        path = getattr(data, "path", None)
        if path is None:
            return None
        try:
            return Path(path)
        except Exception:
            return None

    def _current_tree_path(self) -> Path | None:
        tree = self.query_one("#dir_tree", DirectoryTree)
        node = tree.cursor_node
        if node is not None:
            path = self._path_from_tree_node(node)
            if path and path.exists():
                return path
        if self._last_selected_file_path and self._last_selected_file_path.exists():
            return self._last_selected_file_path
        return None

    def _copy_text(self, text: str, ok_message: str) -> None:
        candidates = [
            ["wl-copy"],
            ["xsel", "--clipboard", "--input"],
            ["xclip", "-selection", "clipboard", "-in"],
            ["pbcopy"],
        ]
        for cmd in candidates:
            if shutil.which(cmd[0]) is None:
                continue
            try:
                proc = subprocess.run(
                    cmd,
                    input=text,
                    text=True,
                    capture_output=True,
                    timeout=0.8,
                )
                if proc.returncode == 0:
                    self.notify(ok_message, timeout=2)
                    return
            except subprocess.TimeoutExpired:
                # Some clipboard tools (notably xclip) may wait indefinitely.
                # Skip blocking backends so the UI remains responsive.
                continue
            except Exception:
                continue
        try:
            self.copy_to_clipboard(text)
            self.notify(ok_message, timeout=2)
        except Exception:
            self.notify("Clipboard unavailable", severity="error", timeout=3)

    def _copy_current_file_path(self, mode: str) -> None:
        path = self._current_tree_path()
        if not path:
            self.notify("No path selected", severity="warning", timeout=2)
            return
        if mode == "name":
            text = path.name
        elif mode == "absolute":
            text = str(path.resolve())
        else:
            rec = self._selected_record()
            ws = rec.workspace if rec else ""
            if ws:
                try:
                    text = os.path.relpath(str(path), ws)
                except Exception:
                    text = str(path.resolve())
            else:
                text = str(path.resolve())
        self._copy_text(text, "Path copied")

    def _copy_preview_selection(self) -> None:
        area = self.query_one("#file_preview", TextArea)
        selected = (area.selected_text or "").strip("\n")
        if not selected:
            self.notify("No preview text selected", severity="warning", timeout=2)
            return
        self._copy_text(selected, "Preview selection copied")

    def _toggle_preview_wrap(self) -> None:
        self._preview_wrap = not self._preview_wrap
        area = self.query_one("#file_preview", TextArea)
        area.soft_wrap = self._preview_wrap
        btn = self.query_one("#preview_wrap_btn", Button)
        btn.label = "Wrap: On" if self._preview_wrap else "Wrap: Off"
        self.notify("Preview wrap enabled" if self._preview_wrap else "Preview wrap disabled", timeout=1.5)

    def _copy_csv_selection(self) -> None:
        area = self.query_one("#csv_preview", TextArea)
        selected = (area.selected_text or "").strip("\n")
        if not selected:
            self.notify("No CSV text selected", severity="warning", timeout=2)
            return
        self._copy_text(selected, "CSV selection copied")

    def _toggle_csv_wrap(self) -> None:
        self._csv_wrap = not self._csv_wrap
        area = self.query_one("#csv_preview", TextArea)
        area.soft_wrap = self._csv_wrap
        btn = self.query_one("#csv_wrap_btn", Button)
        btn.label = "Wrap: On" if self._csv_wrap else "Wrap: Off"
        self.notify("CSV wrap enabled" if self._csv_wrap else "CSV wrap disabled", timeout=1.5)

    def _on_copy_path_action(self, action: str | None) -> None:
        if not action:
            return
        if self._context_file_path and self._context_file_path.exists():
            self._last_selected_file_path = self._context_file_path
        self._copy_current_file_path(action)

    def _right_pane_target(self) -> str | None:
        _ensure_right_pane()
        pane_id = _agents_active_pane_id()
        if not pane_id:
            self.notify("No active session pane on right", severity="warning", timeout=2)
            return None
        if not _pane_exists(pane_id):
            self.notify("Right session pane unavailable", severity="warning", timeout=2)
            return None
        return pane_id

    def _right_jump_top(self) -> None:
        pane_id = self._right_pane_target()
        if not pane_id:
            return
        _tmux("copy-mode", "-t", pane_id)
        _tmux("send-keys", "-t", pane_id, "-X", "history-top")
        self.notify("Right pane: top", timeout=1)

    def _right_jump_bottom(self) -> None:
        pane_id = self._right_pane_target()
        if not pane_id:
            return
        _tmux("send-keys", "-t", pane_id, "C-c")
        _tmux("send-keys", "-t", pane_id, "C-c")
        self.notify("Right pane: bottom", timeout=1)

    def _right_find(self, query: str) -> None:
        q = query.strip()
        if not q:
            self.notify("Enter search text for right pane", severity="warning", timeout=2)
            return
        pane_id = self._right_pane_target()
        if not pane_id:
            return
        total = self._right_match_count(pane_id, q)
        key = (pane_id, q)
        if key != self._right_search_last_key or total != self._right_search_total:
            self._right_search_last_key = key
            self._right_search_total = total
            self._right_search_index = 1 if total > 0 else 0
        else:
            if total > 0:
                self._right_search_index = (self._right_search_index % total) + 1
            else:
                self._right_search_index = 0

        _tmux("copy-mode", "-t", pane_id)
        result = _tmux("send-keys", "-t", pane_id, "-X", "search-forward", q, capture=True)
        if result.returncode != 0:
            _tmux("send-keys", "-t", pane_id, "-X", "search-backward", q, capture=True)
        self.notify(f"{q} ({self._right_search_index}/{total})", timeout=2)

    def _right_match_count(self, pane_id: str, keyword: str) -> int:
        # Capture full pane history, not just a fixed recent window.
        out = _tmux_out("capture-pane", "-p", "-t", pane_id, "-S", "-", "-E", "-", "-J")
        if not out:
            # Fallback for tmux variants where full-range markers are not supported.
            out = _tmux_out("capture-pane", "-p", "-t", pane_id, "-S", "-32768", "-E", "-1", "-J")
        if not out or not keyword:
            return 0
        # Count all non-overlapping occurrences across full history buffer.
        kw = keyword.casefold()
        raw = out.casefold()
        return len(list(re.finditer(re.escape(kw), raw)))

    # --- session operations ---

    def _do_open(self, record: SessionRecord) -> None:
        if not record.workspace_exists:
            self.notify("Cannot resume: workspace not found", severity="error", timeout=4)
            return
        key = self.mgr.key(record)
        now = time.monotonic()
        if key in self._open_inflight:
            return
        # Guard against duplicated click events in very short intervals.
        if self._last_open_key == key and (now - self._last_open_ts) < 0.8:
            return

        self._open_inflight.add(key)
        self._last_open_key = key
        self._last_open_ts = now
        try:
            self.mgr.open_session(record)
            self._refresh()
        finally:
            self._open_inflight.discard(key)

    def _do_attach(self, record: SessionRecord) -> None:
        if not self.mgr.attach_session(record):
            self.notify("Session is no longer running", severity="warning", timeout=3)
        self._refresh()

    def _do_kill(self, record: SessionRecord) -> None:
        self.mgr.kill_session(record)
        self.notify(f"Killed {record.session_id[:8]}", timeout=3)
        self._refresh()

    def _refresh(self) -> None:
        self._render_list()
        if self.session_items:
            self._set_selected(self.selected_index)
        else:
            self._update_detail(None)
        self._update_status()

    def action_prev_page(self) -> None:
        self._change_page(-1)

    def action_next_page(self) -> None:
        self._change_page(1)

    def action_copy_file_name(self) -> None:
        self._copy_current_file_path("name")

    def action_copy_file_relative(self) -> None:
        self._copy_current_file_path("relative")

    def action_copy_file_absolute(self) -> None:
        self._copy_current_file_path("absolute")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TUI session manager")
    parser.add_argument(
        "--tool", choices=["claude", "cursor", "all"], default="all",
        help="Which tool's sessions to list",
    )
    parser.add_argument(
        "filter", nargs="?", default=None,
        help="Filter by session name, workspace, id, or prompt text",
    )
    parser.add_argument(
        "--page-size", type=int, default=10,
        help="Sessions per page (default: 10)",
    )
    parser.add_argument(
        "--skip-preflight", action="store_true",
        help="Skip startup dependency checks",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not _preflight_check(args.tool, skip=args.skip_preflight):
        sys.exit(1)
    bootstrap_tmux()
    app = SessionManagerApp(
        tool=args.tool,
        keyword=args.filter,
        page_size=max(1, args.page_size),
    )
    app.run()


if __name__ == "__main__":
    main()
