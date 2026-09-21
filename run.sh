#!/usr/bin/env bash
# Start Audit Master. Creates the virtualenv and installs Flask on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi

# launch.py picks a free port, opens the browser, and runs with the debugger
# OFF. The debugger must stay off: it offers an interactive Python console on
# any unhandled exception, to anyone who can reach the port.
exec .venv/bin/python launch.py "$@"
