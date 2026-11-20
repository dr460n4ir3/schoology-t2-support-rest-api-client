"""Load and save collections/environments as JSON files (one file each).

Secret environment values (the lock icon) and auth secrets are kept in the Mac Keychain / Windows Credential
Manager when one is available (see secret_store.py); the environment file then only says "keychain": true.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import redact, secret_store
from .config import Settings, resource_dir
from .models import Collection, Environment
from .variables import is_unset

log = logging.getLogger(__name__)


_SECRET_NAMES = ("secret", "password", "token", "api_key", "apikey")


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return cleaned[:80] or "untitled"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
            if json.loads(candidate.read_text(encoding="utf-8")).get("id") == obj_id:
                return candidate
        except (OSError, ValueError):
            pass
    return folder / f"{base}_{obj_id}.json"


def _storable(value) -> bool:
    """A real secret value worth moving to the keychain (not blank, a PASTE-... placeholder or a {{variable}})."""
    return isinstance(value, str) and not is_unset(value) and "{{" not in value


def _has_plain_secrets(data: dict) -> bool:
    if any(v.get("secret") and _storable(v.get("value")) for v in data.get("variables") or []):
        return True
    auth = data.get("auth") or {}
    return any(_storable(auth.get(f)) for f in secret_store.AUTH_SECRET_FIELDS)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.load_errors: list[str] = []

    def use_keychain(self) -> bool:
        return bool(getattr(self.settings, "use_keychain", True)) and secret_store.available()

    # ------------------------------------------------------------- collections
    def load_collections(self) -> list[Collection]:
        out = []
        for path in sorted(self.settings.collections_path.glob("*.json")):
            if path.name.startswith(".tmp_"):
                continue
            try:
                out.append(Collection.from_dict(json.loads(path.read_text(encoding="utf-8")), path))
            except Exception as exc:  # a broken file should never stop the app
                self.load_errors.append(f"{path.name}: {exc}")
                log.warning("Could not load collection %s: %s", path, exc)
        return sorted(out, key=lambda c: c.name.lower())

    def save_collection(self, c: Collection) -> Path:
        folder = self.settings.collections_path
        target = _target_path(folder, c.name, c.path, c.id)
        _atomic_write(target, c.to_dict())
        if c.path and c.path.resolve() != target.resolve() and c.path.exists():
            c.path.unlink()  # renamed
        c.path = target
        return target

    def delete_collection(self, c: Collection) -> None:
        if c.path and c.path.exists():
            self._backup(c.path)
            c.path.unlink()

    # ------------------------------------------------------------ environments
    def load_environments(self) -> list[Environment]:
        out, to_move = [], []
        for path in sorted(self.settings.environments_path.glob("*.json")):
            if path.name.startswith(".tmp_"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                plain = _has_plain_secrets(data)
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
        _atomic_write(target, data)
        if e.path and e.path.resolve() != target.resolve() and e.path.exists():
            e.path.unlink()
        e.path = target
        return target

    def delete_environment(self, e: Environment) -> None:
        if e.path and e.path.exists():
            self._backup(e.path)
            e.path.unlink()

    # ------------------------------------------------------------------- misc
    def _backup(self, path: Path) -> None:
        """Deleted files go to <folder>/.trash so a mis-click is recoverable."""
        trash = path.parent / ".trash"
        trash.mkdir(exist_ok=True)
        shutil.copy2(path, trash / path.name)

    def seed_examples(self) -> list[str]:
        """On first run, copy the bundled example collections/environments into place."""
        added = []
        examples = resource_dir() / "examples"
        for sub, dest in (("collections", self.settings.collections_path),
                          ("environments", self.settings.environments_path)):
            if any(dest.glob("*.json")):
                continue
            for src in sorted((examples / sub).glob("*.json")):
                shutil.copy2(src, dest / src.name)
                added.append(src.name)
        return added
