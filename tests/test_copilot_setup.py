"""Tests for writing the Copilot global MCP file (uses a temp file, never the real ~/.copilot)."""

import json

from restclient import copilot_setup as cs


def test_entry_has_paths_filled_in():
    data = json.loads(cs.copilot_config_text())
    entry = data["mcpServers"]["schoology-api-client"]
    assert entry["type"] == "local" and entry["args"] == ["-m", "restclient.mcp_server"]
    assert entry["command"].lower().endswith(("python", "python.exe"))
    assert (cs.app_folder() / "restclient").is_dir()
    assert entry["env"]["PYTHONPATH"] == str(cs.app_folder())
    assert json.loads(cs.vscode_config_text())["servers"]["schoology-api-client"]["cwd"] == str(cs.app_folder())


def test_install_creates_the_file_and_folder(tmp_path):
    path = tmp_path / ".copilot" / "mcp-config.json"
    written, backup = cs.install_copilot_config(path)
    assert written == path and backup is None
    assert "schoology-api-client" in json.loads(path.read_text())["mcpServers"]


def test_install_keeps_other_servers_and_backs_up(tmp_path):
    path = tmp_path / "mcp-config.json"
    other = {"type": "local", "command": "npx", "args": ["something"], "tools": ["*"]}
    path.write_text(json.dumps({"mcpServers": {"other": other}}))
    _, backup = cs.install_copilot_config(path)
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == other and "schoology-api-client" in data["mcpServers"]
    assert json.loads(backup.read_text()) == {"mcpServers": {"other": other}}
    cs.install_copilot_config(path)  # running it again just updates our entry
    assert len(json.loads(path.read_text())["mcpServers"]) == 2


def test_install_refuses_to_overwrite_a_broken_file(tmp_path):
    path = tmp_path / "mcp-config.json"
    path.write_text('{"mcpServers": {"other": ')
    try:
        cs.install_copilot_config(path)
    except ValueError as e:
        assert "isn't valid JSON" in str(e)
    else:
        raise AssertionError("expected ValueError")
    assert path.read_text() == '{"mcpServers": {"other": '
