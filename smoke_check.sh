#!/usr/bin/env bash
set -euo pipefail

echo "[acsm] Python syntax check..."
python3 -m py_compile \
  "tui_session_manager.py" \
  "list_claude_sessions.py" \
  "list_cursor_sessions.py" \
  "session_utils.py"

echo "[acsm] Python module checks..."
for mod in textual sqlite3 multiprocessing concurrent.futures; do
  if python3 -c "import $mod" 2>/dev/null; then
    echo "  - $mod: OK"
  else
    echo "  - $mod: MISSING"
  fi
done

echo "[acsm] Locale / encoding check..."
enc=$(python3 -c "import locale; print(locale.getpreferredencoding(False))" 2>/dev/null || echo "unknown")
lang="${LANG:-<unset>}"
term="${TERM:-<unset>}"
echo "  - LANG=$lang  TERM=$term  encoding=$enc"
if echo "$enc" | grep -qi utf; then
  echo "  - UTF-8: OK"
else
  echo "  - UTF-8: WARNING (box-drawing characters may not render)"
  echo "    Alpine: apk add musl-locales  Debian: apt install locales"
fi

echo "[acsm] Command checks..."
if command -v tmux >/dev/null 2>&1; then
  echo "  - tmux: OK ($(tmux -V 2>/dev/null || echo '?'))"
else
  echo "  - tmux: MISSING (required)"
  echo "    Alpine: apk add tmux  Debian: apt install tmux"
fi

if command -v claude >/dev/null 2>&1; then
  echo "  - claude: OK"
else
  echo "  - claude: MISSING (optional unless using Claude sessions)"
fi

if command -v cursor-agent >/dev/null 2>&1; then
  echo "  - cursor-agent: OK"
elif command -v agent >/dev/null 2>&1; then
  echo "  - agent: OK"
else
  echo "  - cursor-agent/agent: MISSING (optional unless using Cursor sessions)"
fi

if command -v wl-copy >/dev/null 2>&1 || command -v xsel >/dev/null 2>&1 || command -v xclip >/dev/null 2>&1 || command -v pbcopy >/dev/null 2>&1; then
  echo "  - clipboard backend: OK"
else
  echo "  - clipboard backend: MISSING (copy actions may fail)"
fi

echo "[acsm] Data directories..."
for d in ~/.claude ~/.cursor; do
  if [ -d "$d" ]; then
    echo "  - $d: exists"
  else
    echo "  - $d: not found"
  fi
done

echo "[acsm] smoke check done."
