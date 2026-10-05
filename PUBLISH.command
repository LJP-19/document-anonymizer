#!/bin/bash
# Document Anonymizer - update and publish. Double-click to run.
set -u
cd "$(dirname "$0")"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: Git is not installed. Run: xcode-select --install"
  read -r -p "Press Enter to close..."
  exit 1
fi

# The pinned dependencies ship wheels for Python 3.11 and 3.12 only.
PY=""
for candidate in python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done

if [ -z "$PY" ]; then
  echo
  echo "Python 3.12 was not found. Publishing still works, but the tests"
  echo "cannot run here. GitHub runs them on all three platforms before"
  echo "building anything, so this is safe to skip."
  echo
  if command -v brew >/dev/null 2>&1; then
    read -r -p "Install Python 3.12 with Homebrew now? [y/N] " reply
    case "$reply" in
      [Yy]*) brew install python@3.12 && PY="$(command -v python3.12 || true)" ;;
    esac
  else
    echo "To install it yourself: https://www.python.org/downloads/"
  fi
fi

[ -z "$PY" ] && PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "ERROR: No Python found. Install Python 3.12 from https://www.python.org/downloads/"
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Using $("$PY" -c 'import sys;print(f"Python {sys.version_info.major}.{sys.version_info.minor}")')"
echo
"$PY" release/update.py "$@"
