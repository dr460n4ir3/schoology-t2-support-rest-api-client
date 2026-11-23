"""Team collections: share request changes with teammates through GitHub, without typing git commands.

The collections/ folder of the repo holds one file per request (collection_files.py). This module shows what
changed on this computer, gets teammates' changes (git pull) and shares yours (git commit + push). Only files
in collections/ are ever committed. Nothing here asks for a password: git uses the GitHub sign-in already
saved on the computer (Git Credential Manager / Keychain).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import collection_files as cf
from .config import team_collections_dir
from .secret_store import AUTH_SECRET_FIELDS
from .updater import UpdateError, _first_line, _git, git_exe
from .variables import is_unset

PREFIX = "collections/"


class TeamError(RuntimeError):
    pass


class NeedIdentity(TeamError):
    """git doesn't know this person's name/email yet (needed once, to label their changes)."""


class Conflict(TeamError):
    """This person and a teammate changed the same request."""

    def __init__(self, files: list[str]):
        self.files = files
        super().__init__("You and a teammate both changed: " + ", ".join(label(f) for f in files))


@dataclass
class Change:
    path: str  # repo-relative, e.g. collections/Schoology API/Users/Get user.json
    kind: str  # added | edited | deleted

    @property
    def label(self) -> str:
        return label(self.path)


@dataclass
class TeamStatus:
    changes: list[Change] = field(default_factory=list)  # edited here, not shared yet
    unsent: int = 0  # shared changes saved on this computer but not on GitHub yet (e.g. offline)
    incoming: list[str] = field(default_factory=list)  # teammates' changes waiting on GitHub
    message: str = ""  # e.g. why GitHub couldn't be reached

    @property
    def behind(self) -> int:
        return len(self.incoming)


def repo() -> Path | None:
    """The git folder when the app is using the repo's shared collections/ folder."""
    folder = team_collections_dir()
    return folder.parent if folder is not None and git_exe() else None


def _need_repo() -> Path:
    root = repo()
    if root is None:
        raise TeamError("Team sharing works when the app runs from the GitHub copy (git clone) of the project.")
    return root


def _run(root: Path, *args: str, timeout: int = 120):
    try:
        return _git(*args, cwd=root, timeout=timeout)
    except UpdateError as exc:
        raise TeamError(str(exc)) from exc


def label(path: str) -> str:
    """'collections/Schoology API/Users/Get user.json' -> 'Schoology API › Users › Get user'."""
    parts = path[len(PREFIX):].split("/") if path.startswith(PREFIX) else path.split("/")
    last = parts[-1]
    if last == cf.COLLECTION_META:
        return " › ".join(parts[:-1]) + " (collection settings)"
    if last == cf.FOLDER_META:
        return " › ".join(parts[:-1]) + " (folder settings)"
    return " › ".join(parts[:-1] + [last[:-5] if last.endswith(".json") else last])


# ------------------------------------------------------------------ status
def local_changes(root: Path | None = None) -> list[Change]:
    root = root or _need_repo()
    out = _run(root, "-c", "core.quotepath=off", "status", "--porcelain=v1", "-z", "--untracked-files=all",
               "--", PREFIX).stdout
    changes = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # the old name follows
        if not path.endswith(".json"):
            continue
        kind = "added" if xy in ("??", "A ") or "A" in xy else "deleted" if "D" in xy else "edited"
        changes.append(Change(path, kind))
    return sorted(changes, key=lambda c: c.path.lower())


def status(fetch: bool = True) -> TeamStatus:
    root = _need_repo()
    st = TeamStatus(changes=local_changes(root))
    if fetch:
        res = _run(root, "fetch", "--quiet", timeout=60)
        if res.returncode != 0:
            st.message = "Couldn't reach GitHub: " + (_first_line(res.stderr) or "unknown error")
    if _run(root, "rev-parse", "--abbrev-ref", "@{u}").returncode != 0:
        st.message = st.message or "This copy isn't following a GitHub branch."
        return st
    st.unsent = int(_run(root, "rev-list", "--count", "@{u}..HEAD").stdout.strip() or 0)
    st.incoming = [line for line in _run(root, "log", "--format=%s (%an)", "HEAD..@{u}").stdout.splitlines()
                   if line.strip()]
    return st


def _incoming_files(root: Path) -> set[str]:
    return {line for line in _run(root, "-c", "core.quotepath=off", "diff", "--name-only", "HEAD...@{u}")
            .stdout.splitlines() if line.strip()}


# ------------------------------------------------------------------ checks
_SECRET_HEADER = re.compile(r"^(authorization|x-api-key|api-key|cookie)$", re.I)


def find_secrets(root: Path, changes: list[Change]) -> list[str]:
    """Request files must never carry real keys: they go to GitHub. {{variables}} are fine."""
    found = []
    for ch in changes:
        if ch.kind == "deleted":
            continue
        try:
            data = json.loads((root / ch.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        bad = secrets_in(data)
        if bad:
            found.append(f"{ch.label}: {', '.join(str(b) for b in bad)}")
    return found


_BODY_PAIR = re.compile(r"""["']?([A-Za-z_\-]+)["']?\s*[:=]\s*["']?([^"'&\s,}]+)""")
_SECRET_WORDS = ("secret", "password", "passwd", "pwd", "token", "apikey", "api_key", "access_key", "private_key")


def _secret_name(name) -> bool:
    n = str(name or "").lower().replace("-", "_")
    return any(w in n for w in _SECRET_WORDS) and n not in ("token_url", "token_type", "grant_type")


def _json_secrets(value, out: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, str) and _secret_name(k) and _real(v):
                out.append(f"body {k}")
            else:
                _json_secrets(v, out)
    elif isinstance(value, list):
        for v in value:
            _json_secrets(v, out)


def secrets_in(data: dict) -> list[str]:
    """Names of the places in one request/collection file that hold a real key, password or token."""
    auth = data.get("auth") or {}
    bad = [f for f in AUTH_SECRET_FIELDS if _real(auth.get(f))]
    bad += [h.get("key") for h in data.get("headers") or [] if isinstance(h, dict)
            and _SECRET_HEADER.match(str(h.get("key", ""))) and _real(h.get("value"))]
    bad += [v.get("key") for v in data.get("variables") or [] if isinstance(v, dict) and _real(v.get("value"))
            and any(w in str(v.get("key", "")).lower() for w in ("secret", "password", "token", "key"))]
    for where in ("params", "form"):
        bad += [f"{where} {v.get('key')}" for v in data.get(where) or [] if isinstance(v, dict)
                and _secret_name(v.get("key")) and _real(v.get("value"))]
    body = data.get("body")
    if isinstance(body, str) and body.strip():
        try:
            _json_secrets(json.loads(body), bad)
        except ValueError:  # text / urlencoded body: look for password=... or "password": "..."
            for m in _BODY_PAIR.finditer(body):
                if _secret_name(m.group(1)) and _real(m.group(2)):
                    bad.append(f"body {m.group(1)}")
    return bad


def _real(value) -> bool:
    return isinstance(value, str) and not is_unset(value) and "{{" not in value


def broken_files(root: Path, paths) -> list[str]:
    out = []
    for p in paths:
        if p.startswith(PREFIX) and p.endswith(".json") and (root / p).exists():
            try:
                json.loads((root / p).read_text(encoding="utf-8"))
            except ValueError:
                out.append(p)
    return out


def identity(root: Path | None = None) -> tuple[str, str]:
    root = root or _need_repo()
    name = _run(root, "config", "user.name").stdout.strip()
    email = _run(root, "config", "user.email").stdout.strip()
    return name, email


def set_identity(name: str, email: str) -> None:
    root = _need_repo()
    if not name.strip() or "@" not in email:
        raise TeamError("Type your name and an email address.")
    _run(root, "config", "user.name", name.strip())
    _run(root, "config", "user.email", email.strip())


# ------------------------------------------------------------------ actions
def pull(root: Path, prefer: str = "") -> set[str]:
    """Bring in teammates' changes (and app updates). Local unshared edits are kept. Returns the changed files.

    prefer: "" = stop on a clash, "mine" / "theirs" = settle clashes in favour of that side."""
    incoming = _incoming_files(root)
    if not incoming:
        return set()
    clash = sorted({c.path for c in local_changes(root)} & incoming)
    if clash and not prefer:
        raise Conflict(clash)
    if clash and prefer == "theirs":
        for path in clash:  # drop my unshared edit of those files, take the teammate's
            undo(path, root)
    elif clash and prefer == "mine":
        raise TeamError("Share your changes first (they are newer), then get the team's changes.")
    args = ["pull", "--rebase", "--autostash", "--quiet"]
    if prefer == "mine":
        args.insert(2, "--strategy-option=theirs")  # in a rebase, "theirs" = the commits being replayed = mine
    elif prefer == "theirs":
        args.insert(2, "--strategy-option=ours")
    res = _run(root, *args, timeout=180)
    if res.returncode != 0:
        conflicted = [f for f in _run(root, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if f]
        _run(root, "rebase", "--abort")
        if conflicted and not prefer:
            raise Conflict(conflicted)
        raise TeamError("Getting the team's changes failed: "
                        + (_first_line(res.stderr) or _first_line(res.stdout) or "git error"))
    broken = broken_files(root, incoming)
    if broken:
        raise TeamError("These files came in damaged and were skipped: " + ", ".join(label(b) for b in broken)
                        + ". Ask the teammate who changed them to fix them.")
    return incoming


def get_latest(prefer: str = "") -> set[str]:
    root = _need_repo()
    res = _run(root, "fetch", "--quiet", timeout=60)
    if res.returncode != 0:
        raise TeamError("Couldn't reach GitHub: " + (_first_line(res.stderr) or "unknown error"))
    return pull(root, prefer)


def suggest_message(changes: list[Change]) -> str:
    words = {"added": "Add", "edited": "Update", "deleted": "Remove"}
    reqs = [c for c in changes if not c.path.endswith((cf.COLLECTION_META, cf.FOLDER_META))] or changes
    parts = [f"{words[c.kind]} {label(c.path).split(' › ')[-1]}" for c in reqs[:3]]
    more = f" and {len(reqs) - 3} more" if len(reqs) > 3 else ""
    return "; ".join(parts) + more if parts else "Update collections"


def share(message: str, prefer: str = "") -> str:
    """Commit the collections/ changes, merge in teammates' changes, and push. Returns a short summary."""
    root = _need_repo()
    changes = local_changes(root)
    secrets = find_secrets(root, changes)
    if secrets:
        raise TeamError("Not shared: these requests contain real keys or secrets, which must not go to GitHub. "
                        "Put them in your environment and use {{variables}} in the request instead. "
                        + "; ".join(secrets))
    broken = broken_files(root, [c.path for c in changes])
    if broken:
        raise TeamError("Not shared: these files aren't valid JSON: " + ", ".join(label(b) for b in broken))
    if changes:
        name, email = identity(root)
        if not name or not email:
            raise NeedIdentity("Tell the app your name once, so teammates can see who changed what.")
        _run(root, "add", "-A", "--", PREFIX)
        res = _run(root, "commit", "--quiet", "-m", message.strip() or suggest_message(changes), "--", PREFIX)
        if res.returncode != 0:
            raise TeamError("Couldn't save your changes: " + (_first_line(res.stderr) or _first_line(res.stdout)))
    ahead = int(_run(root, "rev-list", "--count", "@{u}..HEAD").stdout.strip() or 0)
    if not ahead:
        return "Nothing to share."
    for attempt in range(2):
        fetch = _run(root, "fetch", "--quiet", timeout=60)
        if fetch.returncode != 0:
            raise TeamError("Saved on this computer, but couldn't reach GitHub: " + _first_line(fetch.stderr)
                            + ". Click Share again when you're back online.")
        pull(root, prefer)
        push = _run(root, "push", "--quiet", timeout=180)
        if push.returncode == 0:
            return f"Shared {len(changes) or ahead} change(s) with the team."
        err = push.stderr or push.stdout
        if attempt == 0 and re.search(r"rejected|fetch first|non-fast-forward", err, re.I):
            continue  # a teammate pushed a moment ago: merge and try once more
        raise TeamError(_push_help(err))
    raise TeamError("A teammate keeps sharing at the same moment. Wait a minute and click Share again.")


def _push_help(err: str) -> str:
    first = _first_line(err)
    if re.search(r"403|permission|denied to", err, re.I):
        return ("GitHub said you don't have permission to change this repo. Ask the repo owner to add your GitHub "
                "account (repo Settings → Collaborators). Your changes are saved and will go up then. "
                f"({first})")
    if re.search(r"authentication|could not read username|terminal prompts disabled|credential", err, re.I):
        return ("GitHub needs you to sign in once on this computer. Open a terminal in the app folder, type "
                "`git push`, and sign in when asked (or sign in with GitHub Desktop). Then click Share again. "
                "Your changes are saved in the meantime.")
    return "Couldn't send your changes to GitHub: " + first


def undo(path: str, root: Path | None = None) -> None:
    """Throw away this computer's unshared change to one file (back to the last shared version)."""
    root = root or _need_repo()
    if not path.startswith(PREFIX) or ".." in path.split("/"):
        raise TeamError("Only files in collections/ can be undone here.")
    tracked = _run(root, "ls-files", "--error-unmatch", "--", path).returncode == 0
    if tracked:
        _run(root, "checkout", "HEAD", "--", path)
    else:
        target = root / path
        target.unlink(missing_ok=True)
        parent = target.parent
        while parent != root / "collections" and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
