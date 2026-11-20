"""Start the app:  python -m restclient   (the .bat/.command launchers and the built app all run this).

If the app is already running, this just opens a new browser tab to it.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import urllib.request
import webbrowser


def _is_our_app(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as resp:
            return json.loads(resp.read()).get("app") == "schoology-api-client"
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> None:
    from restclient.config import load_settings  # importing restclient enables OS certificates

    settings = load_settings()
    log_file = settings.logs_path / "app.log"
    # pythonw.exe / windowed builds have no console: send output to a log file instead
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_file, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(log_file, encoding="utf-8")])
    from restclient.redact import install_log_filter

    install_log_filter()  # keys, secrets and tokens never reach the log file

    show = "--no-browser" not in sys.argv
    port = settings.port
    for candidate in range(settings.port, settings.port + 20):
        if _is_our_app(candidate):
            if show:
                webbrowser.open(f"http://127.0.0.1:{candidate}")
            return
        if _port_free(candidate):
            port = candidate
            break

    logging.info("Starting on port %s", port)
    from restclient.ui import start

    start(port, show=show)


if __name__ in {"__main__", "__mp_main__"}:
    main()
