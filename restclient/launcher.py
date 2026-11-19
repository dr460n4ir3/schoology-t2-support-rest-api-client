"""Run a team's own sign-in commands and open the connection files they produce.

Built for workflows like "sign in to AWS, run the company's access script, then double-click a connection
file in a folder to open it in a database app (e.g. Sequel Ace)". Nothing here knows about any specific
company tool: the commands and folders are typed in by the user and stored in their local settings.json.

The app never reads the connection files; it only lists them and asks the operating system to open them,
exactly like double-clicking them in Finder / Explorer.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")
MAX_DEPTH = 3


# ----------------------------------------------------------------------------- commands
def parse_commands(text: str) -> list[str]:
    """One command per line; blank lines and lines starting with # are ignored."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]


def command_env(cwd: Path | None) -> dict[str, str]:
    """The user's normal environment, minus this app's own Python (so their scripts use their Python),
    plus the tool folder (and its bin/) on PATH so its commands are found."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    for key in ("PYTHONHOME", "PYTHONPATH", "RESTCLIENT_HOME"):
        env.pop(key, None)
    for key in [k for k in env if k.endswith("_ORIG")]:  # PyInstaller saves the user's originals as X_ORIG
        env[key[:-5]] = env.pop(key)
    parts = env.get("PATH", "").split(os.pathsep)
    own = {str(Path(sys.executable).parent)} | ({str(Path(venv) / ("Scripts" if os.name == "nt" else "bin"))}
                                                 if venv else set())
    parts = [p for p in parts if p and p not in own]
    if cwd:
        parts = [str(d) for d in (cwd / "bin", cwd) if d.is_dir()] + parts
    env["PATH"] = os.pathsep.join(parts)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def popen_args(command: str) -> dict:
    """How to start one command in the user's own shell (login + interactive, so aliases and PATH from
    ~/.zshrc / ~/.bash_profile work like they do in Terminal)."""
    if os.name == "nt":
        return {"args": command, "shell": True}
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    return {"args": [shell, "-l", "-i", "-c", command], "start_new_session": True}


class StepRunner:
    """Runs commands one after another in the background, keeping their output for the UI."""

    def __init__(self, max_lines: int = 3000):
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.count = 0  # lines logged since start (the UI uses it to append only new lines)
        self.steps: list[dict] = []
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.finished_at: float | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, commands: list[str], cwd: str | Path | None = None) -> None:
        if self.running:
            raise RuntimeError("Already running")
        if not commands:
            raise ValueError("No commands to run")
        folder = Path(cwd).expanduser() if cwd and str(cwd).strip() else None
        if folder and not folder.is_dir():
            raise FileNotFoundError(f"Folder not found: {folder}")
        self.lines.clear()
        self.count = 0
        self.steps = [{"command": c, "status": "waiting", "code": None} for c in commands]
        self._stop.clear()
        self.finished_at = None
        self._thread = threading.Thread(target=self._run_all, args=(folder,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)  # noqa: S603,S607
                else:
                    os.killpg(proc.pid, 15)
            except OSError:
                proc.terminate()

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread:
            self._thread.join(timeout)
        return not self.running

    def new_lines(self, seen: int) -> list[str]:
        """Lines logged after the first `seen` ones (as many as are still kept)."""
        missing = self.count - seen
        return list(self.lines)[-missing:] if missing > 0 else []

    def _log(self, text: str) -> None:
        for line in ANSI.sub("", text).splitlines() or [""]:
            self.lines.append(line)
            self.count += 1

    def _run_all(self, cwd: Path | None) -> None:
        env = command_env(cwd)
        try:
            for step in self.steps:
                if self._stop.is_set():
                    step["status"] = "skipped"
                    continue
                step["status"] = "running"
                self._log(f"$ {step['command']}")
                try:
                    self.proc = subprocess.Popen(  # noqa: S603 - the user's own commands from their settings
                        **popen_args(step["command"]), cwd=str(cwd) if cwd else None, env=env,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                except OSError as e:
                    step.update(status="failed", code=None)
                    self._log(f"Could not start: {e}")
                    self._stop.set()
                    continue
                # Read output on its own thread: a command that starts a background process (e.g. a tunnel with
                # -d) can exit while the background process keeps the output pipe open forever.
                threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
                code = self.proc.wait()
                time.sleep(0.2)  # let the last lines arrive
                step["code"] = code
                if self._stop.is_set():
                    step["status"] = "stopped"
                elif code == 0:
                    step["status"] = "ok"
                else:
                    step["status"] = "failed"
                    self._log(f"Stopped: that command failed (exit code {code}).")
                    self._stop.set()
        finally:
            self.proc = None
            self.finished_at = time.time()

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            self._log(raw.decode("utf-8", "replace").rstrip("\n"))


def terminal_launch(commands: list[str], cwd: str | Path | None = None) -> list[str] | dict:
    """Open the commands in a real Terminal window instead (for steps that ask questions)."""
    folder = str(Path(cwd).expanduser()) if cwd and str(cwd).strip() else ""
    if os.name == "nt":
        chain = " && ".join(commands)
        return {"args": ["cmd", "/k", f'cd /d "{folder}" && {chain}' if folder else chain],
                "creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE", 0)}
    script = " && ".join(([f"cd {shlex.quote(folder)}"] if folder else []) + commands)
    if sys.platform == "darwin":
        quoted = script.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", 'tell application "Terminal" to activate',
                "-e", f'tell application "Terminal" to do script "{quoted}"']
    return ["x-terminal-emulator", "-e", "bash", "-lc", f"{script}; exec bash"]


def open_in_terminal(commands: list[str], cwd: str | Path | None = None) -> None:
    spec = terminal_launch(commands, cwd)
    if isinstance(spec, dict):
        subprocess.Popen(**spec)  # noqa: S603
    else:
        subprocess.Popen(spec)  # noqa: S603


# --------------------------------------------------------------------- connection files
def list_connections(folder: str | Path) -> list[dict]:
    """Files in the connections folder (and its subfolders). Folders with an extension, like Sequel Ace's
    .spfs bundles or .app, count as one item. Hidden files are skipped."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []
    items: list[dict] = []

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entries:
            if p.name.startswith((".", "~$")):
                continue
            if p.is_dir() and not p.suffix:
                if depth < MAX_DEPTH:
                    walk(p, depth + 1)
                continue
            try:
                modified = p.stat().st_mtime
            except OSError:
                continue
            group = str(p.parent.relative_to(root)).replace("\\", "/")
            items.append({"name": p.stem if p.suffix else p.name, "file": p.name, "path": str(p),
                          "group": "" if group == "." else group, "modified": modified})

    walk(root, 0)
    return items


def open_command(path: str | Path, app: str = "") -> list[str] | None:
    """The command that opens a file like a double-click (None on Windows: os.startfile is used)."""
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return ["open", "-a", app, str(path)] if app.strip() else ["open", str(path)]
    return ["xdg-open", str(path)]


def open_path(path: str | Path, app: str = "") -> None:
    """Open like a double-click. Raises OSError with the reason (e.g. 'Unable to find application named ...')."""
    cmd = open_command(path, app)
    if cmd is None:
        os.startfile(str(path))  # noqa: S606 - the user's own file, like a double-click
        return
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
    except subprocess.TimeoutExpired:
        return  # still opening; nothing to report
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout).strip() or f"exit code {result.returncode}")


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400 * 2:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"
