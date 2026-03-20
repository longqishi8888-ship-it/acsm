#!/usr/bin/env python3
"""List all Cursor AI sessions with ID, title, project, date, and prompt previews."""

import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from session_utils import (
    build_session_cache_key,
    extract_user_query,
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

CURSOR_DIR = Path.home() / ".cursor"
CHATS_DIR = CURSOR_DIR / "chats"
PROJECTS_DIR = CURSOR_DIR / "projects"


def decode_hex_meta(raw: str) -> dict | None:
    try:
        _, hex_str = raw.split("|", 1)
        return json.loads(bytes.fromhex(hex_str).decode("utf-8"))
    except Exception:
        return None


def get_all_chat_metadata() -> dict[str, dict]:
    """Scan all store.db files under ~/.cursor/chats/ for session metadata."""
    sessions = {}
    if not CHATS_DIR.exists():
        return sessions

    for db_path in CHATS_DIR.rglob("store.db"):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = conn.execute("SELECT * FROM meta").fetchone()
            conn.close()
            if not row:
                continue
            raw = "|".join(str(c) for c in row)
            meta = decode_hex_meta(raw)
            if meta and "agentId" in meta:
                workspace_hash = db_path.parent.parent.name
                meta["_workspace_hash"] = workspace_hash
                meta["_db_path"] = str(db_path)
                sessions[meta["agentId"]] = meta
        except Exception:
            continue
    return sessions


def find_all_transcripts() -> dict[str, dict]:
    """Scan ~/.cursor/projects/*/agent-transcripts/ for transcript files."""
    transcripts = {}
    if not PROJECTS_DIR.exists():
        return transcripts

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        at_dir = proj_dir / "agent-transcripts"
        if not at_dir.exists():
            continue

        project_name = proj_dir.name

        for entry in at_dir.iterdir():
            if entry.is_file() and entry.suffix in (".jsonl", ".txt"):
                session_id = entry.stem
                transcripts[session_id] = {
                    "project": project_name,
                    "path": str(entry),
                    "format": entry.suffix,
                }
            elif entry.is_dir():
                session_id = entry.name
                for f in entry.iterdir():
                    if f.is_file() and f.name == f"{session_id}.jsonl":
                        transcripts[session_id] = {
                            "project": project_name,
                            "path": str(f),
                            "format": ".jsonl",
                        }
                        break
    return transcripts


def extract_first_prompt_jsonl(path: str) -> str | None:
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
                if obj.get("role") != "user":
                    continue
                content_parts = obj.get("message", {}).get("content", [])
                for part in content_parts:
                    if part.get("type") == "text":
                        text = extract_user_query(part["text"])
                        if text and len(text) > 2:
                            return text
    except Exception:
        pass
    return None


def extract_first_prompt_txt(path: str) -> str | None:
    speaker_re = re.compile(r"^\s*[A-Z]:\s*$")
    in_user = False
    block_lines: list[str] = []

    def _flush_block() -> str | None:
        text = extract_user_query("".join(block_lines))
        if text and len(text) > 2:
            return text
        return None

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not in_user:
                    if stripped.lower() == "user:":
                        in_user = True
                        block_lines = []
                    continue

                if speaker_re.match(line) or stripped.startswith("[Tool call]"):
                    found = _flush_block()
                    if found:
                        return found
                    in_user = False
                    block_lines = []
                    if stripped.lower() == "user:":
                        in_user = True
                    continue

                block_lines.append(line)

        if in_user and block_lines:
            found = _flush_block()
            if found:
                return found
    except Exception:
        pass
    return None


def extract_first_prompt(path: str, fmt: str) -> str | None:
    if fmt == ".jsonl":
        return extract_first_prompt_jsonl(path)
    return extract_first_prompt_txt(path)


def _build_workspace_hash_map(
    chat_meta: dict[str, dict], transcripts: dict[str, dict]
) -> dict[str, str]:
    """Build chats workspace_hash -> resolved path from sessions present in both sources."""
    hash_map: dict[str, str] = {}
    for sid, meta in chat_meta.items():
        ws_hash = meta.get("_workspace_hash", "")
        if not ws_hash or ws_hash in hash_map:
            continue
        trans = transcripts.get(sid)
        if trans and trans["project"]:
            resolved = resolve_dash_path(trans["project"])
            if resolved:
                hash_map[ws_hash] = resolved
    return hash_map


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


def _build_cursor_entries_batch(
    sids: list[str],
    chat_meta: dict[str, dict],
    transcripts: dict[str, dict],
    ws_hash_map: dict[str, str],
) -> list[dict]:
    entries: list[dict] = []
    for sid in sids:
        meta = chat_meta.get(sid, {})
        trans = transcripts.get(sid)

        name = meta.get("name", "")
        created_at = meta.get("createdAt")
        project = trans["project"] if trans else ""

        workspace = resolve_dash_path(project) if project else ""
        if not workspace:
            workspace = ws_hash_map.get(meta.get("_workspace_hash", ""), "")

        all_prompts: list[str] = []
        if trans:
            first_prompt = extract_first_prompt(trans["path"], trans["format"])
            if first_prompt:
                all_prompts = [first_prompt]

        if not name and all_prompts:
            name = truncate(all_prompts[0], 40)

        file_mtime = None
        if trans:
            try:
                file_mtime = os.path.getmtime(trans["path"])
            except Exception:
                pass

        last_active = int(file_mtime * 1000) if file_mtime else None
        sort_key = created_at or last_active or 0

        session_path = trans["path"] if trans else meta.get("_db_path", "")

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
        })
    return entries


def collect_sessions(filter_keyword: str | None = None) -> list[dict]:
    t0 = time.monotonic()
    chat_meta = get_all_chat_metadata()
    transcripts = find_all_transcripts()
    ws_hash_map = _build_workspace_hash_map(chat_meta, transcripts)
    cache = load_session_cache("cursor_sessions")

    all_session_ids = list(set(chat_meta.keys()) | set(transcripts.keys()))
    sessions: list[dict] = []
    misses: list[str] = []
    miss_keys: dict[str, str] = {}

    for sid in all_session_ids:
        trans = transcripts.get(sid)
        meta = chat_meta.get(sid, {})
        source_path = trans["path"] if trans else meta.get("_db_path", "")
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
                f"[acsm][cursor] cache total={total} hit={hit} miss={miss} "
                f"hit_rate={hit_rate:.1f}% workers={worker_count} time={elapsed_ms}ms"
            ),
            file=sys.stderr,
        )
        sessions.sort(key=lambda s: s["sort_key"], reverse=True)
        return filter_sessions(sessions, filter_keyword)
    if worker_count <= 1:
        fresh_entries = _build_cursor_entries_batch(
            misses, chat_meta, transcripts, ws_hash_map
        )
    else:
        fresh_entries = []
        batches = _split_batches(misses, worker_count)
        try:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(
                        _build_cursor_entries_batch,
                        batch,
                        chat_meta,
                        transcripts,
                        ws_hash_map,
                    )
                    for batch in batches
                ]
                for future in as_completed(futures):
                    try:
                        fresh_entries.extend(future.result())
                    except Exception:
                        continue
        except Exception:
            fresh_entries = _build_cursor_entries_batch(
                misses, chat_meta, transcripts, ws_hash_map
            )

    for entry in fresh_entries:
        sid = entry["id"]
        sessions.append(entry)
        key = miss_keys.get(sid)
        if key:
            put_cached_session(cache, key, entry)

    save_session_cache("cursor_sessions", cache)
    total = len(all_session_ids)
    hit = len(sessions) - len(fresh_entries)
    miss = len(fresh_entries)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    hit_rate = (hit / total * 100.0) if total else 100.0
    print(
        (
            f"[acsm][cursor] cache total={total} hit={hit} miss={miss} "
            f"hit_rate={hit_rate:.1f}% workers={worker_count} time={elapsed_ms}ms"
        ),
        file=sys.stderr,
    )

    sessions.sort(key=lambda s: s["sort_key"], reverse=True)
    return filter_sessions(sessions, filter_keyword)


def main():
    args = parse_args("List all Cursor AI sessions")
    keyword = args.filter.lower() if args.filter else None
    sessions = collect_sessions(keyword)
    print_sessions(sessions, "Cursor Sessions", limit=args.limit)


if __name__ == "__main__":
    main()
