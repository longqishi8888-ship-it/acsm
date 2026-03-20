#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv-build}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT_DIR"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: Python not found: $PYTHON_BIN" >&2
  exit 1
fi

echo "[1/5] Prepare virtualenv: $VENV_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

echo "[2/5] Upgrade build tooling"
python -m pip install --upgrade pip setuptools wheel

echo "[3/5] Install project dependencies"
python -m pip install -r requirements.txt pyinstaller

echo "[4/5] Clean previous build artifacts"
rm -rf build dist

echo "[5/5] Build executable with PyInstaller"
pyinstaller --noconfirm --clean acsm.spec

OUTPUT_BIN="$ROOT_DIR/dist/acsm"
if [[ -x "$OUTPUT_BIN" ]]; then
  echo
  echo "Build succeeded."
  echo "Binary: $OUTPUT_BIN"
else
  echo "Build finished, but binary not found: $OUTPUT_BIN" >&2
  exit 1
fi
