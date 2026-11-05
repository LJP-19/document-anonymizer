#!/bin/bash
# Double-click this file on macOS to publish a release.
# It commits the current project, pushes it to GitHub, and GitHub Actions
# builds the Windows .exe and macOS .dmg on native runners.
set -u
cd "$(dirname "$0")/.."

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: Git is not installed. Run: xcode-select --install"
  read -r -p "Press Enter to close..."
  exit 1
fi

PY=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python 3.11+ is not installed. Get it from https://www.python.org/downloads/"
  read -r -p "Press Enter to close..."
  exit 1
fi

if ! "$PY" -m pytest --version >/dev/null 2>&1; then
  echo "Installing development dependencies, one moment..."
  "$PY" -m pip install -r requirements-dev.txt
fi

"$PY" release/release.py
