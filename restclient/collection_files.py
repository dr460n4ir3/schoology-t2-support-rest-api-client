"""Collections on disk as a folder tree: one small file per request, so teammates can edit and review them in git.

    collections/
      Schoology API/                    <- one folder per collection
        _collection.json                <- name, description, auth, variables, order of the items
        Users/                          <- one folder per folder in the app
          _folder.json                  <- name, description, order of the items
          Get user.json                 <- one file per request
          Who am I (app-user-info).json

Hand edits are welcome: a request file only needs "name", "method" and "url". A file added without being listed
in "order" still shows up (after the listed ones, A-Z), and an "order" entry whose file is gone is ignored, so a
git merge can't lose a request. Missing ids are derived from the file's path, so loading never changes a file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .models import Auth, Collection, Folder, Item, KV, Request, kv_list

COLLECTION_META = "_collection.json"
FOLDER_META = "_folder.json"
FORMAT = "restclient-collection-folder/1"
_RESERVED = {COLLECTION_META.lower(), FOLDER_META.lower()}


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return cleaned[:80] or "untitled"


def is_collection_dir(path: Path) -> bool:
    return path.is_dir() and (path / COLLECTION_META).is_file()


def dumps(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _stable_id(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix().lower()
    return uuid.uuid5(uuid.NAMESPACE_URL, "restclient:" + rel).hex[:12]


# ------------------------------------------------------------------ compact request files
def _kv_out(kv: KV) -> dict:
    d = {"key": kv.key, "value": kv.value}
    if not kv.enabled:
        d["enabled"] = False
    if kv.secret:
        d["secret"] = True
    return d


def request_to_file(r: Request) -> dict:
    """The request as a short, readable file: empty fields are left out."""
    d: dict = {"name": r.name, "method": r.method, "url": r.url}
    if r.description:
        d["description"] = r.description
    if r.params:
        d["params"] = [_kv_out(p) for p in r.params]
    if r.headers:
        d["headers"] = [_kv_out(h) for h in r.headers]
    if r.body_mode != "none":
        d["body_mode"] = r.body_mode
    if r.body:
        d["body"] = r.body
    if r.form:
        d["form"] = [_kv_out(f) for f in r.form]
    if r.auth.type != "inherit":
        d["auth"] = r.auth.to_dict()
    if r.fetch_all_pages:
        d["fetch_all_pages"] = True
    d["id"] = r.id
    return d


def _meta(kind: str, obj, order: list[str]) -> dict:
    if kind == "collection":
        c: Collection = obj
        return {"format": FORMAT, "id": c.id, "name": c.name, "description": c.description,
                "variables": [_kv_out(v) for v in c.variables], "auth": c.auth.to_dict(), "order": order}
    f: Folder = obj
    d = {"id": f.id, "name": f.name, "order": order}
    if f.description:
        d["description"] = f.description
    return d


def collection_files(c: Collection) -> dict[str, str]:
    """Every file of the collection: {relative posix path: text}."""
    out: dict[str, str] = {}

    def add_items(items: list[Item], prefix: str) -> list[str]:
        used: set[str] = set(_RESERVED)
        order = []
        for item in items:
            base = safe_name(item.name)
            for n in range(1, 1000):
                name = base if n == 1 else f"{base} ({n})"
                entry = name if item.kind == "folder" else name + ".json"
                if entry.lower() not in used:
                    break
            used.add(entry.lower())
            order.append(entry)
            if item.kind == "folder":
                child_order = add_items(item.items, f"{prefix}{entry}/")
                out[f"{prefix}{entry}/{FOLDER_META}"] = dumps(_meta("folder", item, child_order))
            else:
                out[f"{prefix}{entry}"] = dumps(request_to_file(item))
        return order

    order = add_items(c.items, "")
    out[COLLECTION_META] = dumps(_meta("collection", c, order))
    return out


# ------------------------------------------------------------------ reading
class CollectionReadError(ValueError):
    pass


def _read_json(path: Path, problems: list[str], root: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{path.relative_to(root.parent).as_posix()}: {_short(exc)}")
        return None
    if not isinstance(data, dict):
        problems.append(f"{path.relative_to(root.parent).as_posix()}: not a JSON object")
        return None
    return data


def _short(exc: Exception) -> str:
    text = str(exc)
    if "<<<<<<<" in text or "Expecting" in text:
        return "not valid JSON (" + text.split(":")[0] + ")"
    return text


def _ordered_children(folder: Path, order: list) -> list[Path]:
    kids = {p.name.lower(): p for p in folder.iterdir()
            if not p.name.startswith((".", "~")) and p.name.lower() not in _RESERVED
            and (p.is_dir() or p.suffix.lower() == ".json")}
    out = []
    for entry in order if isinstance(order, list) else []:
        p = kids.pop(str(entry).lower(), None)
        if p is not None:
            out.append(p)
    out += sorted(kids.values(), key=lambda p: (p.is_dir(), p.name.lower()))
    return out


def _read_items(folder: Path, order: list, root: Path, problems: list[str]) -> list[Item]:
    items: list[Item] = []
    for path in _ordered_children(folder, order):
        if path.is_dir():
            meta = (_read_json(path / FOLDER_META, problems, root) if (path / FOLDER_META).exists() else {}) or {}
            f = Folder(name=meta.get("name") or path.name, description=meta.get("description", ""),
                       id=meta.get("id") or _stable_id(root, path))
            f.items = _read_items(path, meta.get("order") or [], root, problems)
            items.append(f)
            continue
        data = _read_json(path, problems, root)
        if data is None:
            continue
        data.setdefault("name", path.stem)
        data.setdefault("id", _stable_id(root, path))
        items.append(Request.from_dict(data))
    return items


def read_collection(folder: Path) -> tuple[Collection, list[str]]:
    """Load one collection folder. Returns the collection and a list of files that couldn't be read."""
    problems: list[str] = []
    meta = _read_json(folder / COLLECTION_META, problems, folder)
    if meta is None:
        raise CollectionReadError(problems[0] if problems else f"{folder.name}: {COLLECTION_META} is missing")
    auth = Auth.from_dict(meta.get("auth"), "none")
    if auth.type == "inherit":
        auth.type = "none"
    c = Collection(id=meta.get("id") or _stable_id(folder.parent, folder), name=meta.get("name") or folder.name,
                   description=meta.get("description", ""), variables=kv_list(meta.get("variables")), auth=auth,
                   path=folder)
    c.items = _read_items(folder, meta.get("order") or [], folder, problems)
    return c, problems


# ------------------------------------------------------------------ writing
def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _effective_order(listed, children: list[str]) -> list[str]:
    """The order the loader would show: listed entries that exist, then the rest (files A-Z, then folders A-Z)."""
    by_lower = {ch.lower(): ch for ch in children}
    out = []
    for entry in listed if isinstance(listed, list) else []:
        ch = by_lower.pop(str(entry).lower(), None)
        if ch is not None:
            out.append(ch)
    rest = sorted(by_lower.values(), key=lambda n: (not n.lower().endswith(".json"), n.lower()))
    return [n.lower() for n in out + rest]


def _same_meaning(folder: Path, rel: str, new_text: str) -> bool:
    """True when the file on disk already says the same thing in a different form (e.g. a teammate's hand-written
    file without an id, or an "order" list missing a file the loader appends anyway). Leaving such files alone
    keeps someone else's edit from showing up as your change."""
    path = folder / rel
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        new = json.loads(new_text)
    except (OSError, ValueError):
        return False
    if not isinstance(old, dict):
        return False
    name = path.name.lower()
    if name in _RESERVED:
        old.setdefault("id", new.get("id"))
        old.setdefault("name", new.get("name"))
        children = new.get("order") or []
        if _effective_order(old.get("order"), children) != [n.lower() for n in children]:
            return False
        keys = (set(old) | set(new)) - {"order", "format"}
        if name == COLLECTION_META.lower():
            old_auth = Auth.from_dict(old.get("auth"), "none").to_dict()
            if old_auth != new.get("auth") or kv_list(old.get("variables")) != kv_list(new.get("variables")):
                return False
            keys -= {"auth", "variables"}
        return all((old.get(k) or "") == (new.get(k) or "") for k in keys)
    old.setdefault("name", path.stem)
    old.setdefault("id", _stable_id(folder, path))
    return request_to_file(Request.from_dict(old)) == new


def write_collection(c: Collection, folder: Path, trash: Path | None = None) -> list[str]:
    """Write the collection into `folder`, touching only files that changed (so git shows just real edits).
    Files of requests/folders that were removed are moved to `trash`. Returns the changed relative paths."""
    wanted = collection_files(c)
    changed = []
    for rel, text in wanted.items():
        path = folder / rel
        try:
            if path.read_text(encoding="utf-8") == text or _same_meaning(folder, rel, text):
                continue
        except OSError:
            pass
        _write_text(path, text)
        changed.append(rel)
    wanted_l = {rel.lower() for rel in wanted}
    wanted_dirs = {rel.lower().rsplit("/", 1)[0] for rel in wanted if "/" in rel}
    wanted_dirs |= {"/".join(d.split("/")[:i]) for d in wanted_dirs for i in range(1, d.count("/") + 1)}
    for path in sorted(folder.rglob("*"), key=lambda p: -len(p.parts)):
        rel = path.relative_to(folder).as_posix()
        if any(part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        if path.is_file() and path.suffix.lower() == ".json" and rel.lower() not in wanted_l:
            if trash is not None:
                dest = trash / folder.name / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            path.unlink()
            changed.append(rel)
        elif path.is_dir() and rel.lower() not in wanted_dirs and not any(path.iterdir()):
            path.rmdir()
    return changed


def remove_collection(folder: Path, trash: Path | None = None) -> None:
    if not folder.exists():
        return
    if trash is not None:
        dest = trash / f"{folder.name} {time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(folder, dest, ignore=shutil.ignore_patterns(".*"))
    shutil.rmtree(folder)
