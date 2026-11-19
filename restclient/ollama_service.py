"""Start and stop the Ollama server from the app (Assistant tab), so nobody needs a terminal.

Mac: the Ollama app (menu-bar llama) runs the server; quitting it stops the server and frees the memory.
Windows: "ollama app.exe" (tray icon) does the same. Without the app, "ollama serve" is started instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

MAC_APPS = [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"]


def _windows_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "Ollama"


def mac_app() -> Path | None:
    return next((p for p in MAC_APPS if p.is_dir()), None)


def cli_path() -> str | None:
    """The ollama command, including the copy inside the Mac app and the Windows install folder."""
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [Path("/usr/local/bin/ollama"), Path("/opt/homebrew/bin/ollama")]
    if mac_app():
        candidates.insert(0, mac_app() / "Contents" / "Resources" / "ollama")
    candidates.append(_windows_dir() / "ollama.exe")
    return next((str(p) for p in candidates if p.is_file()), None)


def is_installed() -> bool:
    if sys.platform == "darwin" and mac_app():
        return True
    if sys.platform == "win32" and (_windows_dir() / "ollama app.exe").is_file():
        return True
    return cli_path() is not None


def start_command() -> tuple[list[str], dict] | None:
    """(command, Popen options) that starts the server, or None if Ollama isn't installed."""
    if sys.platform == "darwin" and mac_app():
        return ["open", "-a", str(mac_app())], {}
    if sys.platform == "win32":
        tray = _windows_dir() / "ollama app.exe"
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if tray.is_file():
            return [str(tray)], {"creationflags": flags}
        cli = cli_path()
        return ([cli, "serve"], {"creationflags": flags}) if cli else None
    cli = cli_path()
    return ([cli, "serve"], {"start_new_session": True}) if cli else None


def stop_commands() -> list[list[str]]:
    """Commands that stop the Ollama app and server (the models are unloaded, freeing their memory)."""
    if sys.platform == "win32":
        return [["taskkill", "/F", "/T", "/IM", "ollama app.exe"], ["taskkill", "/F", "/T", "/IM", "ollama.exe"]]
    cmds = [["pkill", "-x", "ollama"]]
    if sys.platform == "darwin":
        cmds.insert(0, ["osascript", "-e", 'tell application "Ollama" to quit'])
    return cmds


def start() -> None:
    spec = start_command()
    if spec is None:
        raise FileNotFoundError("Ollama isn't installed. Use 'Install Local AI (optional)' first.")
    cmd, opts = spec
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: S603
                     stderr=subprocess.DEVNULL, **opts)


def stop() -> None:
    for cmd in stop_commands():
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)  # noqa: S603 - "not running" errors are fine
        except (OSError, subprocess.TimeoutExpired):
            pass
