"""Connect GitHub Copilot to this app's MCP server.

Newer VS Code versions read MCP servers from the "Copilot global" file ~/.copilot/mcp-config.json
(shared with the Copilot CLI). Older ones use VS Code's own user mcp.json, which has a different format.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

SERVER_NAME = "schoology-api-client"


def python_path() -> Path:
    py = Path(sys.executable)
    return py.with_name("python.exe") if py.name.lower() == "pythonw.exe" else py


def app_folder() -> Path:
    return Path(__file__).resolve().parent.parent


def copilot_config_path() -> Path:
    return Path.home() / ".copilot" / "mcp-config.json"


def copilot_entry() -> dict:
    """Format of ~/.copilot/mcp-config.json. PYTHONPATH (not cwd) lets Python find the app from any folder."""
    return {"type": "local", "command": str(python_path()), "args": ["-m", "restclient.mcp_server"],
            "env": {"PYTHONPATH": str(app_folder())}, "tools": ["*"]}


def copilot_config_text() -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: copilot_entry()}}, indent=2)


def vscode_config_text() -> str:
    """Format of VS Code's own user mcp.json (older VS Code versions)."""
    return json.dumps({"servers": {SERVER_NAME: {
        "type": "stdio", "command": str(python_path()), "args": ["-m", "restclient.mcp_server"],
        "cwd": str(app_folder())}}}, indent=2)


def install_copilot_config(path: Path | None = None) -> tuple[Path, Path | None]:
    """Add (or update) this app in the Copilot global file, keeping any other servers in it.
    Returns (file written, backup of the old file or None). Raises ValueError if the existing file
    can't be read, so a hand-edited file is never overwritten."""
    path = path or copilot_config_path()
    data: dict = {}
    backup = None
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            try:
                data = json.loads(text)
            except ValueError as e:
                raise ValueError(f"{path} isn't valid JSON ({e}). Fix or delete it, then try again.") from e
            if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
                raise ValueError(f"{path} doesn't look like a Copilot MCP file. Fix or delete it, then try again.")
            backup = path.with_name(path.name + ".bak")
            shutil.copy2(path, backup)
    data.setdefault("mcpServers", {})[SERVER_NAME] = copilot_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path, backup
