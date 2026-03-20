"""Shared utilities for AI session listing scripts."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

MAX_PROMPT_PREVIEW_LEN = 120

_workspace_path_cache: dict[str, str] = {}


def resolve_dash_path(project_dir_name: str, strip_leading_dash: bool = False) -> str:
    """Resolve a dash-encoded directory name back to a real filesystem path.

    Cursor uses 'home-sgf-Projs-foo', Claude Code uses '-home-sgf-Projs-foo'.
    We do a DFS trying both dash-as-separator and dash-as-literal at each
    boundary, returning the first path that exists on disk.
    """
    if project_dir_name in _workspace_path_cache:
        return _workspace_path_cache[project_dir_name]

    name = project_dir_name.lstrip("-") if strip_leading_dash else project_dir_name
    parts = [p for p in name.split("-") if p]

    def _search(idx: int, current_path: str) -> str | None:
        if idx == len(parts):
            if os.path.isdir(current_path):
                return current_path
            return None
        for end in range(len(parts), idx, -1):
            segment = "-".join(parts[idx:end])
            candidate = os.path.join(current_path, segment)
            if os.path.isdir(candidate):
                result = _search(end, candidate)
                if result:
                    return result
        return None

    resolved = _search(0, "/")
    if not resolved:
        resolved = "/" + "/".join(parts)

    _workspace_path_cache[project_dir_name] = resolved
    return resolved


def extract_user_query(text: str) -> str:
    """Extract content between <user_query> tags, or return raw text."""
    m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def truncate(text: str, length: int = MAX_PROMPT_PREVIEW_LEN) -> str:
    text = text.replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > length:
        return text[:length] + "..."
    return text


def format_timestamp(ts_ms: int | None) -> str:
    if not ts_ms:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"


def filter_sessions(sessions: list[dict], keyword: str | None) -> list[dict]:
    if not keyword:
        return sessions
    return [
        s for s in sessions
        if keyword in s["name"].lower()
        or keyword in s["workspace"].lower()
        or keyword in s["id"].lower()
        or any(keyword in p.lower() for p in s["all_prompts"])
    ]


def print_sessions(sessions: list[dict], title: str, limit: int | None = None):
    total = len(sessions)
    display = sessions[:limit] if limit else sessions

    separator = "=" * 110
    thin_sep = "-" * 110

    showing = f"Showing: {len(display)}/{total}" if limit and limit < total else f"Total: {total}"
    print(f"\n{title:^110}")
    print(f"{showing:^110}")
    print(separator)

    for i, s in enumerate(display):
        created_str = format_timestamp(s["created_at"])
        active_str = format_timestamp(s["last_active"])
        prompts = s["all_prompts"]
        count = len(prompts)

        extra = ""
        if s.get("entrypoint"):
            extra += f"  [{s['entrypoint']}]"
        if s.get("git_branch"):
            extra += f"  branch:{s['git_branch']}"

        ws = s.get("workspace") or "-"
        ws_exists = s.get("workspace_exists")
        if ws_exists is False:
            ws += "  [NOT FOUND - cannot resume]"

        print(f"  {s['id']}  {s['name'] or '(untitled)'}{extra}")
        print(f"  Workspace : {ws}")
        if s.get("session_path"):
            print(f"  Session   : {s['session_path']}")
        print(f"  Prompts: {count}    Created: {created_str}    LastActive: {active_str}")

        if prompts:
            print(f"    [1] {truncate(prompts[0])}")
            if count >= 2:
                print(f"    [2] {truncate(prompts[1])}")
            if count >= 3:
                print(f"    [{count}] {truncate(prompts[-1])}")
        else:
            print("    (no transcript found)")

        if i < len(display) - 1:
            print(thin_sep)

    print(separator)


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("filter", nargs="?", default=None,
                        help="filter by session name, workspace, ID, or prompt content")
    parser.add_argument("--limit", type=int, default=None,
                        help="only show the first N matching sessions")
    return parser.parse_args()


def _acsm_config_dir() -> Path:
    cfg = Path.home() / ".config" / "acsm"
    cfg.mkdir(parents=True, exist_ok=True)
    return cfg


def _cache_path(cache_name: str) -> Path:
    return _acsm_config_dir() / f"{cache_name}.json"


def load_session_cache(cache_name: str) -> dict:
    path = _cache_path(cache_name)
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return {"version": 1, "entries": {}}
        entries = obj.get("entries")
        if not isinstance(entries, dict):
            return {"version": 1, "entries": {}}
        return {"version": 1, "entries": entries}
    except Exception:
        return {"version": 1, "entries": {}}


def save_session_cache(cache_name: str, cache_obj: dict) -> None:
    path = _cache_path(cache_name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache_obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def session_source_stat(path: str) -> tuple[int, int, int]:
    """Return (mtime_ns, size, mtime_ms) for cache key and staleness checks."""
    if not path:
        return (0, 0, 0)
    try:
        st = os.stat(path)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        size = int(st.st_size)
        mtime_ms = int(st.st_mtime * 1000)
        return (mtime_ns, size, mtime_ms)
    except Exception:
        return (0, 0, 0)


def build_session_cache_key(session_id: str, path: str, mtime_ns: int, size: int) -> str:
    return f"{session_id}|{path}|{mtime_ns}|{size}"


def get_cached_session(cache_obj: dict, key: str, source_mtime_ms: int) -> dict | None:
    entries = cache_obj.get("entries", {})
    item = entries.get(key)
    if not isinstance(item, dict):
        return None
    cached_at_ms = int(item.get("cached_at_ms", 0) or 0)
    # Explicit guard: if source updated after this cache was written, reload it.
    if source_mtime_ms and cached_at_ms and source_mtime_ms > cached_at_ms:
        return None
    data = item.get("data")
    if not isinstance(data, dict):
        return None
    return data


def put_cached_session(cache_obj: dict, key: str, data: dict) -> None:
    entries = cache_obj.setdefault("entries", {})
    entries[key] = {
        "cached_at_ms": int(time.time() * 1000),
        "data": data,
    }
