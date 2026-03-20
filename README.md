# AI Session Manager (acsm)

Terminal UI for managing Claude/Cursor sessions with tmux.

## Platform Support

- Linux: supported (recommended)
- macOS: supported
- Windows native shell (PowerShell/CMD/Git Bash): not recommended
- Windows via WSL2: supported (recommended on Windows)
- SSH from any client to Linux/macOS host: supported

## Requirements

- Python 3.10+
- `tmux` (required)
- `claude` CLI (for Claude session open/resume)
- `agent` CLI (for Cursor session open/resume)
- Clipboard backend (optional, for copy actions):
  - Linux Wayland: `wl-copy`
  - Linux X11: `xsel` or `xclip`
  - macOS: `pbcopy` (built-in)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python3 tui_session_manager.py
```

Options:

- `--tool all|claude|cursor`
- `--page-size 10`
- `--skip-preflight` (skip startup dependency checks)

Example:

```bash
python3 tui_session_manager.py --tool all --page-size 10
```

## Windows Usage

### Recommended: WSL2

Run this project inside Ubuntu (or other Linux distro) in WSL2.

### Also recommended: SSH to remote Linux/macOS

Using Git Bash as SSH client is fine:

```bash
ssh user@host
cd /path/to/AiCodeSessionManager
python3 tui_session_manager.py
```

## Build Executable (PyInstaller)

`acsm.spec` is included.

Build on each target OS separately:

```bash
pip install pyinstaller
pyinstaller acsm.spec
```

Output binary:

- `dist/acsm`

## Quick Smoke Check

```bash
bash smoke_check.sh
```

This checks Python syntax and key runtime commands (`tmux`, optional CLIs).

## Notes

- Runtime caches are stored under `~/.config/acsm`.
- Search uses memory + disk cache for prompt hit snippets.
- Claude prompt search is based on `~/.claude/history.jsonl`.
