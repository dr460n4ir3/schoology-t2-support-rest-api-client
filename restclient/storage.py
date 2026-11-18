"""Load and save collections/environments as JSON files (one file each)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from .config import Settings, resource_dir
from .models import Collection, Environment

log = logging.getLogger(__name__)


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


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.load_errors: list[str] = []

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
        out = []
        for path in sorted(self.settings.environments_path.glob("*.json")):
            if path.name.startswith(".tmp_"):
                continue
            try:
                out.append(Environment.from_dict(json.loads(path.read_text(encoding="utf-8")), path))
            except Exception as exc:
                self.load_errors.append(f"{path.name}: {exc}")
                log.warning("Could not load environment %s: %s", path, exc)
        return sorted(out, key=lambda e: e.name.lower())

    def save_environment(self, e: Environment) -> Path:
        folder = self.settings.environments_path
        target = _target_path(folder, e.name, e.path, e.id)
        _atomic_write(target, e.to_dict())
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
