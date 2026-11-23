"""Check for updates. git copies update themselves with `git pull`; the stand-alone app checks GitHub
Releases and links to the download. Only public information is fetched; nothing is sent.

Nothing here names a particular GitHub account: a git copy uses the address it was cloned from (`origin`), and
the stand-alone build carries the address of the repo it was built from (build_repo.txt, written by
`python -m restclient.updater stamp` in the build scripts). Moving the project to another repo needs no code
change.
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

TEAM_FOLDER = "collections/"  # the team's shared requests: edited in the app, never block an update
STAMP_FILE = "build_repo.txt"  # bundled with the stand-alone build: the repo's web address


@dataclass
class RepoInfo:
    web: str  # e.g. https://github.com/owner/repo
    api: str  # e.g. https://api.github.com/repos/owner/repo

    @property
    def releases_url(self) -> str:
        return self.web + "/releases/latest"


_REMOTE = re.compile(r"^(?:(?:https?|ssh|git)://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+?)/([^/]+?)(?:\.git)?/?$")


def parse_remote(url: str) -> RepoInfo | None:
    """'https://github.com/o/r.git', 'git@github.com:o/r.git', an enterprise host... -> RepoInfo.
    Any user name or token in the address is dropped."""
    m = _REMOTE.match((url or "").strip())
    if not m:
        return None
    host, owner, name = m.group(1).lower(), m.group(2), m.group(3)
    if host == "ssh.github.com":
        host = "github.com"
    api = (f"https://api.github.com/repos/{owner}/{name}" if host == "github.com"
           else f"https://{host}/api/v3/repos/{owner}/{name}")  # GitHub Enterprise Server
    return RepoInfo(web=f"https://{host}/{owner}/{name}", api=api)


def repo_info() -> RepoInfo | None:
    folder = source_folder()
    if folder is not None and git_exe():
        try:
            url = _git("remote", "get-url", "origin", cwd=folder, timeout=15).stdout.strip()
        except UpdateError:
            url = ""
        return parse_remote(url)
    stamp = resource_dir() / STAMP_FILE
    try:
        return parse_remote(stamp.read_text(encoding="utf-8").strip())
    except OSError:
        return None


def stamp(folder: Path | None = None) -> str:
    """Write build_repo.txt (the repo's web address, no credentials) for the stand-alone build to carry."""
    folder = folder or Path(__file__).resolve().parent.parent
    info = None
    if git_exe():
        try:
            info = parse_remote(_git("remote", "get-url", "origin", cwd=folder, timeout=15).stdout.strip())
        except UpdateError:
            info = None
    text = info.web if info else ""
    (folder / STAMP_FILE).write_text(text + "\n", encoding="utf-8")
    return text


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
    info = repo_info()
    st = UpdateStatus(kind="git", url=info.releases_url if info else "")
    fetch = _git("fetch", "--quiet", cwd=folder)
    if fetch.returncode != 0:
        st.message = "Couldn't reach GitHub: " + (_first_line(fetch.stderr) or "unknown error")
        return st
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=folder)
    if upstream.returncode != 0:
        st.message = "This copy isn't following a GitHub branch, so it can't update itself."
        return st
    app_only = ["--", ".", ":(exclude)" + TEAM_FOLDER]  # teammates' request changes come in via Team, not here
    behind = int(_git("rev-list", "--count", "HEAD..@{u}", *app_only, cwd=folder).stdout.strip() or 0)
    ahead = int(_git("rev-list", "--count", "@{u}..HEAD", cwd=folder).stdout.strip() or 0)
    st.changes = [line for line in _git("log", "--format=%s", "HEAD..@{u}", *app_only, cwd=folder).stdout.splitlines()
                  if line.strip()][:20]
    st.latest = _git("rev-parse", "--short", "@{u}", cwd=folder).stdout.strip()
    st.available = behind > 0
    if not st.available:
        st.message = "You have the latest version."
        return st
    edited = [line[3:] for line in _git("status", "--porcelain", "--untracked-files=no", cwd=folder).stdout
              .splitlines() if line.strip() and not line[3:].strip('"').startswith(TEAM_FOLDER)]
    if ahead:  # commits of team collection changes (not sent yet) are fine: they are replayed on top
        own = [f for f in _git("diff", "--name-only", "@{u}...HEAD", cwd=folder).stdout.splitlines() if f.strip()]
        ahead = sum(1 for f in own if not f.startswith(TEAM_FOLDER))
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

    info = repo_info()
    if info is None:
        return UpdateStatus(kind="none", message="This copy doesn't know which GitHub repo it came from. Ask the "
                                                 "app admin for the latest version.")
    st = UpdateStatus(kind="release", url=info.releases_url)
    try:
        r = requests.get(info.api + "/releases/latest", timeout=10,
                         headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as exc:
        st.message = f"Couldn't reach GitHub: {exc}"
        return st
    if r.status_code == 404:
        st.kind, st.message = "none", ("No published builds found (none yet, or the repo is private). Ask the "
                                       "app admin for the latest version.")
        return st
    if r.status_code >= 400:
        st.message = f"GitHub answered {r.status_code}. Try again later."
        return st
    data = r.json()
    st.latest = str(data.get("tag_name") or "").lstrip("v")
    st.url = data.get("html_url") or info.releases_url
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
    from . import team  # late import: team uses this module

    try:
        team.pull(folder)  # keeps this computer's unshared request edits
    except team.Conflict as exc:
        raise UpdateError(f"{exc}. Open Team (sidebar) to choose whose version to keep, then update.") from exc
    except team.TeamError as exc:
        raise UpdateError(f"Update failed: {exc}") from exc
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


if __name__ == "__main__" and sys.argv[1:] == ["stamp"]:
    print("build_repo.txt:", stamp() or "(no GitHub address found: the build won't check for updates)")
