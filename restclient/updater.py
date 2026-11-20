"""Check for updates. git copies update themselves with `git pull`; the stand-alone app checks GitHub
Releases and links to the download. Only public information is fetched; nothing is sent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .config import resource_dir

REPO = "dr460n4ir3/schoology-t2-support-rest-api-client"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateStatus:
    kind: str  # "git" (update in place), "release" (download a new build) or "none" (can't tell)
    available: bool = False
    current: str = __version__
    latest: str = ""
    changes: list[str] = field(default_factory=list)
    message: str = ""
    url: str = ""
    can_apply: bool = False


def source_folder() -> Path | None:
    """The app folder when this copy was installed with `git clone` (not the stand-alone build)."""
    if getattr(sys, "frozen", False):
        return None
    root = resource_dir()
    return root if (root / ".git").exists() else None


def git_exe() -> str | None:
    return shutil.which("git") or next((p for p in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git")
                                        if Path(p).exists()), None)


def _git(*args: str, cwd: Path, timeout: int = 90) -> subprocess.CompletedProcess:
    exe = git_exe()
    if not exe:
        raise UpdateError("git isn't installed on this computer.")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "LC_ALL": "C"}  # never wait for a password
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    try:
        return subprocess.run([exe, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                              **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"git {args[0]} took too long. Check your internet connection / VPN.") from exc


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def check() -> UpdateStatus:
    folder = source_folder()
    if folder is not None and git_exe():
        return _check_git(folder)
    return _check_release()


def _check_git(folder: Path) -> UpdateStatus:
    st = UpdateStatus(kind="git", url=RELEASES_URL)
    fetch = _git("fetch", "--quiet", cwd=folder)
    if fetch.returncode != 0:
        st.message = "Couldn't reach GitHub: " + (_first_line(fetch.stderr) or "unknown error")
        return st
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=folder)
    if upstream.returncode != 0:
        st.message = "This copy isn't following a GitHub branch, so it can't update itself."
        return st
    behind = int(_git("rev-list", "--count", "HEAD..@{u}", cwd=folder).stdout.strip() or 0)
    ahead = int(_git("rev-list", "--count", "@{u}..HEAD", cwd=folder).stdout.strip() or 0)
    st.changes = [line for line in _git("log", "--format=%s", "HEAD..@{u}", cwd=folder).stdout.splitlines()
                  if line.strip()][:20]
    st.latest = _git("rev-parse", "--short", "@{u}", cwd=folder).stdout.strip()
    st.available = behind > 0
    if not st.available:
        st.message = "You have the latest version."
        return st
    edited = [line[3:] for line in _git("status", "--porcelain", "--untracked-files=no", cwd=folder).stdout
              .splitlines() if line.strip()]
    if ahead:
        st.message = (f"{behind} update(s) available, but this copy has its own commits, so it can't update "
                      "automatically. Ask the app admin.")
    elif edited:
        st.message = (f"{behind} update(s) available, but these app files were edited on this computer: "
                      f"{', '.join(edited[:5])}. Ask the app admin, or undo those edits first.")
    else:
        st.message = f"{behind} update(s) available."
        st.can_apply = True
    return st


def _version_tuple(v: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", v or "")[:3]) or (0,)


def _check_release() -> UpdateStatus:
    import requests

    st = UpdateStatus(kind="release", url=RELEASES_URL)
    try:
        r = requests.get(API_LATEST, timeout=10, headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as exc:
        st.message = f"Couldn't reach GitHub: {exc}"
        return st
    if r.status_code == 404:
        st.kind, st.message = "none", "No published builds yet. Ask the app admin for the latest version."
        return st
    if r.status_code >= 400:
        st.message = f"GitHub answered {r.status_code}. Try again later."
        return st
    data = r.json()
    st.latest = str(data.get("tag_name") or "").lstrip("v")
    st.url = data.get("html_url") or RELEASES_URL
    st.changes = [line.strip("-* ").strip() for line in (data.get("body") or "").splitlines()
                  if line.strip().startswith(("-", "*"))][:20]
    st.available = _version_tuple(st.latest) > _version_tuple(__version__)
    st.message = (f"Version {st.latest} is available (you have {__version__}). Download it, unzip it and "
                  "replace this app." if st.available else "You have the latest version.")
    return st


def apply() -> str:
    """git pull, and install new components if requirements.txt changed. Returns a short summary."""
    folder = source_folder()
    if folder is None:
        raise UpdateError("This copy can't update itself. Download the latest version instead.")
    req = folder / "requirements.txt"
    before = req.read_text(encoding="utf-8") if req.exists() else ""
    pull = _git("pull", "--ff-only", "--quiet", cwd=folder, timeout=180)
    if pull.returncode != 0:
        raise UpdateError("Update failed: " + (_first_line(pull.stderr) or _first_line(pull.stdout) or "git error"))
    after = req.read_text(encoding="utf-8") if req.exists() else ""
    if after != before:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        pip = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                              str(req)], cwd=folder, capture_output=True, text=True, timeout=900, **kwargs)
        if pip.returncode != 0:
            raise UpdateError("The update downloaded, but installing new components failed: "
                              + (_first_line(pip.stderr) or "pip error") + ". Close the app and double-click "
                              "the launcher; it retries automatically.")
        marker = folder / ".venv" / "installed-requirements.txt"
        if marker.parent.exists():
            shutil.copyfile(req, marker)  # so the launcher doesn't reinstall again
        return "Updated, including new components."
    return "Updated."


RESTART_CODE = (
    "import runpy, socket, sys, time\n"
    "port = int(sys.argv[1])\n"
    "for _ in range(80):\n"  # wait (up to 40 s) for the old app to let go of its port
    "    s = socket.socket(); busy = s.connect_ex(('127.0.0.1', port)) == 0; s.close()\n"
    "    if not busy: break\n"
    "    time.sleep(0.5)\n"
    "sys.argv = ['restclient', '--no-browser']\n"
    "runpy.run_module('restclient', run_name='__main__', alter_sys=True)\n"
)


def restart_command(port: int) -> list[str]:
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"  # no console window
    return [str(exe), "-c", RESTART_CODE, str(port)]


def spawn_restart(port: int) -> None:
    """Start a fresh copy of the app that waits for this one to close, then takes over the same port."""
    folder = source_folder() or resource_dir()
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(restart_command(port), cwd=folder, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(restart_command(port), cwd=folder, start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
