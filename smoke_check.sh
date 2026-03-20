#!/usr/bin/env bash
set -euo pipefail

echo "[acsm] Python syntax check..."
python3 -m py_compile \
  "tui_session_manager.py" \
  "list_claude_sessions.py" \
  "list_cursor_sessions.py" \
  "session_utils.py"

echo "[acsm] command checks..."
if command -v tmux >/dev/null 2>&1; then
  echo "  - tmux: OK"
else
  echo "  - tmux: MISSING (required)"
fi

if command -v claude >/dev/null 2>&1; then
  echo "  - claude: OK"
else
  echo "  - claude: MISSING (optional unless using Claude sessions)"
fi

if command -v agent >/dev/null 2>&1; then
  echo "  - agent: OK"
else
  echo "  - agent: MISSING (optional unless using Cursor sessions)"
fi

if command -v wl-copy >/dev/null 2>&1 || command -v xsel >/dev/null 2>&1 || command -v xclip >/dev/null 2>&1 || command -v pbcopy >/dev/null 2>&1; then
  echo "  - clipboard backend: OK"
else
  echo "  - clipboard backend: MISSING (copy actions may fail)"
fi

echo "[acsm] smoke check done."
