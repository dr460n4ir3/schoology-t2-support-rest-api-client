"""Start/Stop Ollama commands per platform (nothing is actually started or stopped)."""

from pathlib import Path

from restclient import ollama_service as svc


def test_mac_uses_the_ollama_app(tmp_path, monkeypatch):
    app = tmp_path / "Ollama.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Resources" / "ollama").write_text("")
    monkeypatch.setattr(svc, "MAC_APPS", [app])
    monkeypatch.setattr(svc.sys, "platform", "darwin")
    monkeypatch.setattr(svc.shutil, "which", lambda name: None)
    assert svc.is_installed()
    assert svc.start_command() == (["open", "-a", str(app)], {})
    assert svc.cli_path() == str(app / "Contents" / "Resources" / "ollama")
    stops = svc.stop_commands()
    assert stops[0][0] == "osascript" and 'quit' in stops[0][-1] and stops[-1] == ["pkill", "-x", "ollama"]


def test_windows_uses_the_tray_app(tmp_path, monkeypatch):
    folder = tmp_path / "Programs" / "Ollama"
    folder.mkdir(parents=True)
    (folder / "ollama app.exe").write_text("")
    monkeypatch.setattr(svc, "_windows_dir", lambda: folder)
    monkeypatch.setattr(svc.sys, "platform", "win32")
    cmd, opts = svc.start_command()
    assert cmd == [str(folder / "ollama app.exe")] and "creationflags" in opts
    assert ["taskkill", "/F", "/T", "/IM", "ollama.exe"] in svc.stop_commands()


def test_not_installed(monkeypatch):
    monkeypatch.setattr(svc, "MAC_APPS", [Path("/nope/Ollama.app")])
    monkeypatch.setattr(svc, "_windows_dir", lambda: Path("/nope"))
    monkeypatch.setattr(svc.shutil, "which", lambda name: None)
    monkeypatch.setattr(svc.Path, "is_file", lambda self: False)
    assert not svc.is_installed() and svc.start_command() is None
    try:
        svc.start()
    except FileNotFoundError as e:
        assert "Install Local AI" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")
