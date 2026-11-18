"""Settings and file locations.

Everything a user creates lives in the "app home" folder (default: ~/SchoologyAPIClient).
Override it with the RESTCLIENT_HOME environment variable. Individual folders
(e.g. collections on a shared drive) can be changed in settings.json or the Settings dialog.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def app_home() -> Path:
    home = os.environ.get("RESTCLIENT_HOME")
    path = Path(home).expanduser() if home else Path.home() / "SchoologyAPIClient"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_dir() -> Path:
    """Folder holding bundled files (examples/). Works both from source and a PyInstaller build."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    collections_dir: str = ""  # blank = <app home>/collections
    environments_dir: str = ""  # blank = <app home>/environments
    results_dir: str = ""  # blank = <app home>/results
    timeout_seconds: float = 30
    verify_ssl: bool = True
    use_system_certificates: bool = True
    port: int = 8765
    # AI / MCP safety switches
    mcp_allow_write_methods: bool = False  # allow POST/PUT/PATCH/DELETE from the AI
    mcp_allow_adhoc_requests: bool = False  # allow the AI to call URLs that are not saved in a collection
    mcp_max_response_chars: int = 20000

    def _dir(self, value: str, default: str) -> Path:
        path = Path(value).expanduser() if value.strip() else app_home() / default
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def collections_path(self) -> Path:
        return self._dir(self.collections_dir, "collections")

    @property
    def environments_path(self) -> Path:
        return self._dir(self.environments_dir, "environments")

    @property
    def results_path(self) -> Path:
        return self._dir(self.results_dir, "results")

    @property
    def logs_path(self) -> Path:
        return self._dir("", "logs")


def settings_file() -> Path:
    return app_home() / "settings.json"


def load_settings() -> Settings:
    path = settings_file()
    if not path.exists():
        settings = Settings()
        save_settings(settings)
        return settings
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in data.items() if k in known})


def save_settings(settings: Settings) -> None:
    settings_file().write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
