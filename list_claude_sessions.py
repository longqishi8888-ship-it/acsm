#!/usr/bin/env python3
"""List all Claude Code sessions with ID, title, project, date, and prompt previews."""

import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from session_utils import (
    build_session_cache_key,
    filter_sessions,
    get_cached_session,
    load_session_cache,
    parse_args,
    print_sessions,
    put_cached_session,
    resolve_dash_path,
    save_session_cache,
    session_source_stat,
    truncate,
)

CLAUDE_CODE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_CODE_DIR / "projects"
CLAUDE_HISTORY_FILE = CLAUDE_CODE_DIR / "history.jsonl"


def get_history_metadata() -> dict[str, dict]:
    """Read ~/.claude/history.jsonl to build per-session metadata."""
    sessions: dict[str, dict] = {}
    if not CLAUDE_HISTORY_FILE.exists():
        return sessions

    try:
        with open(CLAUDE_HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = obj.get("sessionId")
                if not sid:
                    continue
                ts = obj.get("timestamp")
                display = obj.get("display", "")
                project = obj.get("project", "")

                if sid not in sessions:
                    sessions[sid] = {
                        "project": project,
                        "first_timestamp": ts,
                        "last_timestamp": ts,
                        "history_prompts": [],
                    }
                else:
                    if ts:
                        sessions[sid]["last_timestamp"] = max(
                            sessions[sid].get("last_timestamp") or 0, ts
                        )

                if display and not display.startswith("/"):
                    sessions[sid]["history_prompts"].append(display)
    except Exception:
        pass
    return sessions


def find_transcripts() -> dict[str, dict]:
    """Scan ~/.claude/projects/*/<uuid>.jsonl for transcript files."""
    transcripts: dict[str, dict] = {}
    if not CLAUDE_PROJECTS_DIR.exists():
        return transcripts

    for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        project_name = proj_dir.name
        for entry in proj_dir.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                session_id = entry.stem
                transcripts[session_id] = {
                    "project": project_name,
                    "path": str(entry),
                }
    return transcripts


def extract_first_prompt_and_session_info(path: str) -> tuple[str | None, dict]:
    """Extract first valid prompt and first user metadata in one pass."""
    info: dict = {}
    first_prompt: str | None = None
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
                if obj.get("type") != "user":
                    continue

                if not info:
                    info["cwd"] = obj.get("cwd", "")
                    info["version"] = obj.get("version", "")
                    info["entrypoint"] = obj.get("entrypoint", "")
                    info["gitBranch"] = obj.get("gitBranch", "")
                    ts = obj.get("timestamp")
                    if ts:
                        try:
                            info["created_at"] = int(
                                datetime.fromisoformat(
                                    ts.replace("Z", "+00:00")
                                ).timestamp() * 1000
                            )
                        except Exception:
                            pass

                msg = obj.get("message", {})
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not content:
                    continue

                text = ""
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text = part.get("text", "").strip()
                            break

                if (
                    text
                    and len(text) > 2
                    and not text.startswith("[Request interrupted")
                    and not text.startswith("<local-command-")
                    and not text.startswith("<command-name>/")
                    and not text.startswith("<local-command-stdout>")
                ):
                    first_prompt = text
                    break
    except Exception:
        pass
    return first_prompt, info


def _worker_count(total_to_load: int) -> int:
    if total_to_load > 64:
        return 8
    if total_to_load > 32:
        return 4
    return 2


def _split_batches(items: list[str], batch_count: int) -> list[list[str]]:
    if batch_count <= 1 or not items:
        return [items]
    batches: list[list[str]] = []
    n = len(items)
    for i in range(batch_count):
        start = i * n // batch_count
        end = (i + 1) * n // batch_count
        part = items[start:end]
        if part:
            batches.append(part)
    return batches or [items]


def _build_claude_entries_batch(
    sids: list[str],
    history_meta: dict[str, dict],
    transcripts: dict[str, dict],
) -> list[dict]:
    entries: list[dict] = []
    for sid in sids:
        hmeta = history_meta.get(sid, {})
        trans = transcripts.get(sid)

        all_prompts: list[str] = []
        session_info: dict = {}
        if trans:
            first_prompt, session_info = extract_first_prompt_and_session_info(trans["path"])
            if first_prompt:
                all_prompts = [first_prompt]

        project_dir = trans["project"] if trans else ""
        workspace = (
            session_info.get("cwd")
            or hmeta.get("project")
            or (resolve_dash_path(project_dir, strip_leading_dash=True) if project_dir else "")
        )

        if not all_prompts and hmeta.get("history_prompts"):
            first_hist = hmeta["history_prompts"][0] if hmeta["history_prompts"] else ""
            if first_hist:
                all_prompts = [first_hist]

        name = ""
        if all_prompts:
            name = truncate(all_prompts[0], 40)

        created_at = session_info.get("created_at") or hmeta.get("first_timestamp")

        file_mtime = None
        if trans:
            try:
                file_mtime = os.path.getmtime(trans["path"])
            except Exception:
                pass

        last_active = int(file_mtime * 1000) if file_mtime else hmeta.get("last_timestamp")
        sort_key = created_at or last_active or 0

        entrypoint = session_info.get("entrypoint", "")
        git_branch = session_info.get("gitBranch", "")

        session_path = trans["path"] if trans else ""

        entries.append({
            "id": sid,
            "name": name,
            "workspace": workspace,
            "workspace_exists": os.path.isdir(workspace) if workspace else False,
            "session_path": session_path,
            "created_at": created_at,
            "last_active": last_active,
            "all_prompts": all_prompts,
            "sort_key": sort_key,
            "entrypoint": entrypoint,
            "git_branch": git_branch,
        })
    return entries


def collect_sessions(filter_keyword: str | None = None) -> list[dict]:
    t0 = time.monotonic()
    history_meta = get_history_metadata()
    transcripts = find_transcripts()
    cache = load_session_cache("claude_sessions")

    all_session_ids = list(set(history_meta.keys()) | set(transcripts.keys()))
    sessions: list[dict] = []
    misses: list[str] = []
    miss_keys: dict[str, str] = {}

    for sid in all_session_ids:
        trans = transcripts.get(sid)
        source_path = trans["path"] if trans else ""
        mtime_ns, size, mtime_ms = session_source_stat(source_path)
        cache_key = build_session_cache_key(sid, source_path, mtime_ns, size)
        cached = get_cached_session(cache, cache_key, mtime_ms)
        if cached:
            entry = dict(cached)
            ws = entry.get("workspace", "")
            entry["workspace_exists"] = os.path.isdir(ws) if ws else False
            sessions.append(entry)
            continue
        misses.append(sid)
        miss_keys[sid] = cache_key

    worker_count = _worker_count(len(misses))
    if not misses:
        total = len(all_session_ids)
        hit = len(sessions)
        miss = len(misses)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        hit_rate = (hit / total * 100.0) if total else 100.0
        print(
            (
                f"[acsm][claude] cache total={total} hit={hit} miss={miss} "
                f"hit_rate={hit_rate:.1f}% workers={worker_count} time={elapsed_ms}ms"
            ),
            file=sys.stderr,
        )
        sessions.sort(key=lambda s: s["sort_key"], reverse=True)
        return filter_sessions(sessions, filter_keyword)
    if worker_count <= 1:
        fresh_entries = _build_claude_entries_batch(misses, history_meta, transcripts)
    else:
        fresh_entries = []
        batches = _split_batches(misses, worker_count)
        try:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(
                        _build_claude_entries_batch, batch, history_meta, transcripts
                    )
                    for batch in batches
                ]
                for future in as_completed(futures):
                    try:
                        fresh_entries.extend(future.result())
                    except Exception:
                        continue
        except Exception:
            # Fallback path if multiprocessing is unavailable in current runtime.
            fresh_entries = _build_claude_entries_batch(misses, history_meta, transcripts)

    for entry in fresh_entries:
        sid = entry["id"]
        sessions.append(entry)
        key = miss_keys.get(sid)
        if key:
            put_cached_session(cache, key, entry)

    save_session_cache("claude_sessions", cache)
    total = len(all_session_ids)
    hit = len(sessions) - len(fresh_entries)
    miss = len(fresh_entries)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    hit_rate = (hit / total * 100.0) if total else 100.0
    print(
        (
            f"[acsm][claude] cache total={total} hit={hit} miss={miss} "
            f"hit_rate={hit_rate:.1f}% workers={worker_count} time={elapsed_ms}ms"
        ),
        file=sys.stderr,
    )

    sessions.sort(key=lambda s: s["sort_key"], reverse=True)
    return filter_sessions(sessions, filter_keyword)


def main():
    args = parse_args("List all Claude Code sessions")
    keyword = args.filter.lower() if args.filter else None
    sessions = collect_sessions(keyword)
    print_sessions(sessions, "Claude Code Sessions", limit=args.limit)


if __name__ == "__main__":
    main()
