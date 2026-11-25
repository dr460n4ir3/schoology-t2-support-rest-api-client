"""Load and save collections (a folder per collection, a file per request: see collection_files.py) and
environments (one JSON file each, encrypted: see vault.py).

Secret environment values (the lock icon) and auth secrets are kept in the Mac Keychain / Windows Credential
Manager when one is available (see secret_store.py); the environment file then only says "keychain": true.
The same goes for collections: a key or secret typed on a request's Auth tab is never written into the request
file (those files go to GitHub).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from . import collection_files as cf
from . import redact, secret_store, vault
from .config import Settings, app_home, resource_dir
from .models import Collection, Environment
from .variables import is_unset

log = logging.getLogger(__name__)


_SECRET_NAMES = ("secret", "password", "token", "api_key", "apikey")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return cleaned[:80] or "untitled"


def _atomic_write(path: Path, data: dict, encrypt: bool = True) -> None:
    vault.write_text(path, json.dumps(data, indent=2, ensure_ascii=False), encrypt_file=encrypt)


def _target_path(folder: Path, name: str, current: Path | None, obj_id: str) -> Path:
    """Pick '<name>.json', avoiding clobbering a different item that has the same name."""
    base = safe_filename(name)
    for n in range(0, 100):
        candidate = folder / (f"{base}.json" if n == 0 else f"{base} ({n}).json")
        if current and candidate.resolve() == current.resolve():
            return candidate
        if not candidate.exists():
            return candidate
        try:
            if json.loads(vault.read_text(candidate)).get("id") == obj_id:
                return candidate
        except (OSError, ValueError, vault.VaultError):
            pass
    return folder / f"{base}_{obj_id}.json"


def _storable(value) -> bool:
    """A real secret value worth moving to the keychain (not blank, a PASTE-... placeholder or a {{variable}})."""
    return isinstance(value, str) and not is_unset(value) and "{{" not in value


def _collection_secret_slots(c: Collection):
    """(keychain account, object, attribute) for every place in a collection that can hold a secret."""
    def auth_slots(owner: str, auth):
        if auth.type in ("inherit", "none"):
            return
        relevant = auth.to_dict()
        for f in secret_store.AUTH_SECRET_FIELDS:
            if f in relevant:
                yield secret_store.ref(owner, f"auth:{f}"), auth, f

    def kv_slots(owner: str, kind: str, kvs):
        for kv in kvs:
            if kv.secret and kv.key:
                yield secret_store.ref(owner, f"{kind}:{kv.key}"), kv, "value"

    base = f"collection:{c.id}"
    yield from auth_slots(base, c.auth)
    yield from kv_slots(base, "var", c.variables)
    for _trail, r in c.walk():
        owner = f"{base}/{r.id}"
        yield from auth_slots(owner, r.auth)
        for kind, kvs in (("param", r.params), ("header", r.headers), ("form", r.form)):
            yield from kv_slots(owner, kind, kvs)


def _has_plain_secrets(data: dict) -> bool:
    if any(v.get("secret") and _storable(v.get("value")) for v in data.get("variables") or []):
        return True
    auth = data.get("auth") or {}
    return any(_storable(auth.get(f)) for f in secret_store.AUTH_SECRET_FIELDS)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.load_errors: list[str] = []
        self._kept: dict[str, str] = {}  # collection secrets already in the keychain (account -> value)

    def use_keychain(self) -> bool:
        return bool(getattr(self.settings, "use_keychain", True)) and secret_store.available()

    # ------------------------------------------------------------- collections
    def trash_path(self) -> Path:
        """Deleted collections/requests are copied here (outside any git folder) so a mis-click is recoverable."""
        return app_home() / "trash" / "collections"

    def load_collections(self) -> list[Collection]:
        folder = self.settings.collections_path
        out, seen = [], set()
        for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if not cf.is_collection_dir(path):
                continue
            try:
                c, problems = cf.read_collection(path)
            except Exception as exc:  # a broken file should never stop the app
                self.load_errors.append(f"{path.name}: {exc}")
                log.warning("Could not load collection %s: %s", path, exc)
                continue
            for problem in problems:
                self.load_errors.append(f"{problem} (skipped; fix or delete that file)")
            if c.id in seen:
                self.load_errors.append(f"{path.name}: same id as another collection, skipped")
                continue
            seen.add(c.id)
            if self._fill_collection_secrets(c):  # an older file had a secret in it: move it to the keychain
                try:
                    self.save_collection(c)
                    log.info("Moved the secrets of collection '%s' to the %s", c.name, secret_store.store_name())
                except OSError as exc:
                    log.warning("Could not re-save collection %s: %s", c.name, exc)
            out.append(c)
        for path in sorted(folder.glob("*.json")):  # older one-file collections: convert them once
            if path.name.startswith(".tmp_"):
                continue
            try:
                c = Collection.from_dict(json.loads(path.read_text(encoding="utf-8")), path)
            except Exception as exc:
                self.load_errors.append(f"{path.name}: {exc}")
                log.warning("Could not load collection %s: %s", path, exc)
                continue
            if c.id in seen:  # already converted (e.g. the file came back from a backup)
                continue
            try:
                self.save_collection(c)
                log.info("Converted collection %s to one file per request in %s", path.name, c.path)
            except OSError as exc:
                log.warning("Could not convert %s: %s", path, exc)
                c.path = path
            seen.add(c.id)
            out.append(c)
        return sorted(out, key=lambda c: c.name.lower())

    def _fill_collection_secrets(self, c: Collection) -> bool:
        """Put the collection's secrets back from the keychain. Returns True when a file still had one in it."""
        if not self.use_keychain():
            return False
        plain = False
        for account, obj, attr in _collection_secret_slots(c):
            value = getattr(obj, attr)
            if _storable(value):
                plain = True
                redact.register(value)
                continue
            if value:  # a {{variable}} or PASTE-... placeholder
                continue
            try:
                stored = secret_store.get(account)
            except secret_store.SecretStoreError as exc:
                self.load_errors.append(f"{c.name}: {exc}")
                return False
            if stored:
                setattr(obj, attr, stored)
                self._kept[account] = stored
                redact.register(stored)
        return plain

    def _collection_secrets_to_keychain(self, c: Collection) -> bool:
        """Keep the collection's secrets in the keychain. Returns False when the keychain couldn't be used."""
        if not self.use_keychain():
            return False
        try:
            for account, obj, attr in _collection_secret_slots(c):
                value = getattr(obj, attr)
                if _storable(value):
                    if self._kept.get(account) != value:
                        secret_store.put(account, value)
                        self._kept[account] = value
                    redact.register(value)
                elif not value and self._kept.pop(account, None) is not None:
                    secret_store.delete(account)  # cleared: don't leave the old value behind
        except secret_store.SecretStoreError as exc:
            log.warning("%s - the secrets of '%s' are kept for this session only", exc, c.name)
        return True

    def _collection_dir(self, c: Collection) -> Path:
        """'<collections>/<name>', avoiding a different collection that has the same name."""
        folder = self.settings.collections_path
        base = cf.safe_name(c.name)
        for n in range(1, 100):
            candidate = folder / (base if n == 1 else f"{base} ({n})")
            if c.path and c.path.is_dir() and candidate.resolve() == c.path.resolve():
                return candidate
            if not candidate.exists():
                return candidate
            try:
                meta = json.loads((candidate / cf.COLLECTION_META).read_text(encoding="utf-8"))
                if meta.get("id") == c.id:
                    return candidate
            except (OSError, ValueError):
                pass
        return folder / f"{base}_{c.id}"

    def save_collection(self, c: Collection) -> Path:
        target = self._collection_dir(c)
        old = c.path
        if old and old.is_dir() and old.resolve() != target.resolve() and not target.exists():
            old.rename(target)  # renamed: move the folder so git sees a rename, not delete + add
        # with no keychain at all (rare) the secrets stay in the files, as before
        strip = self._collection_secrets_to_keychain(c)
        cf.write_collection(c, target, self.trash_path(), strip_secrets=strip)
        if old and old.is_file() and old.suffix == ".json":  # converted from the older one-file format
            self._backup(old, self.trash_path())
            old.unlink()
        c.path = target
        return target

    def delete_collection(self, c: Collection) -> None:
        if c.path and c.path.is_dir():
            cf.remove_collection(c.path, self.trash_path())
        elif c.path and c.path.exists():
            self._backup(c.path, self.trash_path())
            c.path.unlink()

    # ------------------------------------------------------------ environments
    def load_environments(self) -> list[Environment]:
        out, to_move = [], []
        for path in sorted(self.settings.environments_path.glob("*.json")):
            if path.name.startswith(".tmp_"):
                continue
            try:
                raw = path.read_bytes()
                data = json.loads(vault.decrypt(raw))
                # re-save older files: secrets into the keychain, the rest encrypted
                plain = _has_plain_secrets(data) or (not vault.is_encrypted(raw) and vault.available())
                self._fill_from_keychain(data, path.name)
                env = Environment.from_dict(data, path)
            except Exception as exc:
                self.load_errors.append(f"{path.name}: {exc}")
                log.warning("Could not load environment %s: %s", path, exc)
                continue
            redact.register(*env.secret_values(),
                            *[v.value for v in env.variables if any(h in v.key.lower() for h in _SECRET_NAMES)])
            out.append(env)
            if plain:
                to_move.append(env)
        if to_move and self.use_keychain():
            for env in to_move:  # older files kept secrets in plain text: move them into the keychain now
                try:
                    self.save_environment(env)
                    log.info("Moved the secrets of environment '%s' to the %s", env.name, secret_store.store_name())
                except OSError as exc:
                    log.warning("Could not re-save environment %s: %s", env.name, exc)
        return sorted(out, key=lambda e: e.name.lower())

    def _fill_from_keychain(self, data: dict, file_name: str) -> None:
        """Put the secret values recorded as "keychain" back into the environment data."""
        env_id = data.get("id") or ""
        wanted = [(v, "value", f"var:{v.get('key', '')}") for v in data.get("variables") or [] if v.get("keychain")]
        auth = data.get("auth") or {}
        wanted += [(auth, f, f"auth:{f}") for f in auth.get("keychain") or [] if f in secret_store.AUTH_SECRET_FIELDS]
        if not wanted:
            return
        if not secret_store.available():
            self.load_errors.append(f"{file_name}: its secrets are in a keychain this computer can't open. "
                                    "Type them in again on the Environments tab.")
            return
        lost = []
        for holder, key, field in wanted:
            try:
                value = secret_store.get(secret_store.ref(env_id, field))
            except secret_store.SecretStoreError as exc:
                self.load_errors.append(f"{file_name}: {exc}")
                return
            if value is None:
                lost.append(field.split(":", 1)[1])
            holder[key] = value or ""
        if lost:
            self.load_errors.append(f"{data.get('name', file_name)}: {', '.join(lost)} not found in the "
                                    f"{secret_store.store_name()}. Type them in again on the Environments tab.")

    def _move_to_keychain(self, env_id: str, data: dict) -> dict:
        """Store secret values in the keychain and blank them in the data written to the file."""
        for var in data.get("variables") or []:
            account = secret_store.ref(env_id, f"var:{var.get('key', '')}")
            if var.get("secret") and _storable(var.get("value")):
                secret_store.put(account, var["value"])
                var["value"], var["keychain"] = "", True
            elif var.get("secret"):
                secret_store.delete(account)  # cleared: don't leave the old value behind
        auth = data.get("auth") or {}
        moved = []
        for field in secret_store.AUTH_SECRET_FIELDS:
            if _storable(auth.get(field)):
                secret_store.put(secret_store.ref(env_id, f"auth:{field}"), auth[field])
                auth[field] = ""
                moved.append(field)
        if moved:
            auth["keychain"] = moved
        return data

    def save_environment(self, e: Environment) -> Path:
        folder = self.settings.environments_path
        target = _target_path(folder, e.name, e.path, e.id)
        data = e.to_dict()
        if self.use_keychain():
            try:
                data = self._move_to_keychain(e.id, data)
            except secret_store.SecretStoreError as exc:  # keep working: write the file as before
                log.warning("%s - keeping the secrets of '%s' in its file", exc, e.name)
                data = e.to_dict()
        redact.register(*e.secret_values())
        _atomic_write(target, data, encrypt=self.use_keychain())
        if e.path and e.path.resolve() != target.resolve() and e.path.exists():
            e.path.unlink()
        e.path = target
        return target

    def delete_environment(self, e: Environment) -> None:
        if e.path and e.path.exists():
            self._backup(e.path)
            e.path.unlink()

    # ------------------------------------------------------------------- misc
    def _backup(self, path: Path, trash: Path | None = None) -> None:
        """Deleted files go to <folder>/.trash so a mis-click is recoverable."""
        trash = trash or path.parent / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, trash / path.name)

    def seed_examples(self) -> list[str]:
        """On first run, copy the bundled example collections/environments into place."""
        added = []
        bundled = resource_dir() / "collections"
        dest = self.settings.collections_path
        if bundled.is_dir() and dest.resolve() != bundled.resolve() and not any(
                cf.is_collection_dir(p) or p.suffix == ".json" for p in dest.iterdir()):
            for src in sorted(p for p in bundled.iterdir() if cf.is_collection_dir(p)):
                shutil.copytree(src, dest / src.name)
                added.append(src.name)
        dest = self.settings.environments_path
        if not any(dest.glob("*.json")):
            for src in sorted((resource_dir() / "examples" / "environments").glob("*.json")):
                shutil.copy2(src, dest / src.name)
                added.append(src.name)
        return added
