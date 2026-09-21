"""
Audit Master launcher — the entry point for a double-clicked build.

Running a Flask app from a packaged executable differs from running it in a
terminal in three ways, and all three are silent failures if missed:

  * the **reloader must be off**. It restarts the process by re-running the
    interpreter with the original argv, which a frozen executable cannot do;
  * the **port may be taken** — on macOS, port 5000 is AirPlay Receiver — so a
    fixed port produces "address already in use" and nothing else;
  * nobody sees a URL printed in a console window they did not ask for, so the
    browser has to be opened for them.

Run directly for a normal launch::

    python launch.py

``--no-browser`` and ``--port N`` are available for scripted use.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

#: Tried in order. 5000 is first for familiarity, but it is AirPlay Receiver on
#: macOS and often taken on developer machines, so several fallbacks follow.
CANDIDATE_PORTS = (5000, 5001, 5002, 5057, 8000, 8080)

BANNER = r"""
   _              _ _ _     __  __          _
  /_\  _  _ __ _(_) |_)   |  \/  |__ _ ___| |_ ___ _ _
 / _ \| || / _` | | __|   | |\/| / _` (_-<|  _/ -_) '_|
/_/ \_\\_,_\__,_|_|\__|   |_|  |_\__,_/__/ \__\___|_|
"""


def find_free_port(preferred: int | None = None) -> int:
    """A port nothing else is listening on.

    Checked by binding rather than by asking the OS for a list, so it works
    identically on Windows, macOS and Linux with no external commands.
    """
    candidates = (preferred,) + CANDIDATE_PORTS if preferred else CANDIDATE_PORTS
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port

    # Everything preferred was busy; let the OS choose any free port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def open_browser_when_ready(host: str, port: int, timeout: float = 15.0) -> None:
    """Open the browser once the server is actually accepting connections.

    Opening it immediately races the server startup and shows the user a
    connection error on a cold start.
    """
    import time

    url = f"http://{host}:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.15)
    else:
        return                      # never came up; the error is on the console
    try:
        webbrowser.open(url)
    except Exception:               # pragma: no cover - browserless machine
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Audit Master locally.")
    parser.add_argument("--port", type=int, default=None,
                        help="port to serve on (default: first free one)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (use 0.0.0.0 to share on your network)")
    args = parser.parse_args(argv)

    from app import app                       # imported late so --help stays fast

    port = find_free_port(args.port)
    url = f"http://127.0.0.1:{port}"

    print(BANNER)
    print(f"  Audit Master is running at   {url}")
    if args.host != "127.0.0.1":
        print(f"  Others on your network:      http://<your-ip>:{port}")
    print("  Everything runs on this machine. No API key, nothing uploaded.")
    print()
    print("  Close this window (or press Ctrl+C) to stop.")
    print()

    if not args.no_browser:
        threading.Thread(
            target=open_browser_when_ready, args=("127.0.0.1", port), daemon=True
        ).start()

    try:
        # debug/reloader must stay off: the reloader re-executes the interpreter,
        # which a packaged build cannot do.
        app.run(host=args.host, port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\n  Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
