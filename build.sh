#!/usr/bin/env bash
# Build a standalone Audit Master app for THIS platform.
# PyInstaller does not cross-compile: run build.bat on Windows for a .exe.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv-build/bin/python ]; then
  echo "Creating build environment…"
  python3 -m venv .venv-build
  .venv-build/bin/python -m pip install --quiet --upgrade pip
  .venv-build/bin/python -m pip install --quiet -r requirements.txt pyinstaller
fi

echo "Building…"
# ":" separates source from destination on macOS/Linux (";" on Windows).
.venv-build/bin/pyinstaller \
  --noconfirm --clean --onefile --console \
  --name AuditMaster \
  --add-data "templates:templates" \
  --add-data "static:static" \
  --hidden-import auditmaster \
  --collect-submodules auditmaster \
  launch.py

echo
echo "Done: dist/AuditMaster"
