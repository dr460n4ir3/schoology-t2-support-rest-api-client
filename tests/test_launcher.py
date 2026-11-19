"""Tests for the Database tab's command runner and connection-file list (no real tools needed)."""

import os
import sys
import time

from restclient import launcher

PY = f'"{sys.executable}"'


def py(code: str) -> str:
    """A shell command that runs a bit of Python (works in cmd.exe and zsh/bash)."""
    return f'{PY} -c "{code}"'


def test_parse_commands_skips_blanks_and_comments():
    text = "\n  aws sso login --profile x  \n# a note\n\nrun-tool --flag\n"
    assert launcher.parse_commands(text) == ["aws sso login --profile x", "run-tool --flag"]


def test_steps_run_in_order_in_the_tool_folder(tmp_path):
    r = launcher.StepRunner()
    r.start([py("print('first')"), py("import os; print('cwd=' + os.getcwd())")], tmp_path)
    assert r.wait(30)
    assert [s["status"] for s in r.steps] == ["ok", "ok"]
    out = "\n".join(r.lines)
    assert "first" in out and f"cwd={tmp_path}" in out
    assert out.index("first") < out.index("cwd=")


def test_failed_step_stops_the_rest(tmp_path):
    r = launcher.StepRunner()
    r.start([py("import sys; sys.exit(3)"), py("print('should not run')")], tmp_path)
    assert r.wait(30)
    assert [s["status"] for s in r.steps] == ["failed", "skipped"]
    assert r.steps[0]["code"] == 3
    assert "should not run" not in "\n".join(r.lines)


def test_command_that_leaves_a_background_process_does_not_hang(tmp_path):
    # like a tunnel started with -d: the command exits but its child keeps the output pipe open
    child = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']); print('started')"
    r = launcher.StepRunner()
    t = time.time()
    r.start([py(child), py("print('next step')")], tmp_path)
    assert r.wait(15) and time.time() - t < 15
    assert [s["status"] for s in r.steps] == ["ok", "ok"]
    assert "next step" in "\n".join(r.lines)


def test_stop(tmp_path):
    r = launcher.StepRunner()
    r.start([py("import time; time.sleep(30)"), py("print('x')")], tmp_path)
    time.sleep(1)
    r.stop()
    assert r.wait(15)
    assert [s["status"] for s in r.steps] == ["stopped", "skipped"]


def test_new_lines_only_returns_unseen_output():
    r = launcher.StepRunner()
    r._log("a\nb")
    assert r.new_lines(0) == ["a", "b"]
    r._log("\x1b[32mc\x1b[0m")  # colour codes are removed
    assert r.new_lines(2) == ["c"] and r.new_lines(3) == []


def test_missing_folder_is_reported(tmp_path):
    r = launcher.StepRunner()
    try:
        r.start(["echo hi"], tmp_path / "nope")
    except FileNotFoundError as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_command_env_drops_this_apps_python_and_adds_tool_folder(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    venv = tmp_path / "venv"
    venv_bin = venv / ("Scripts" if os.name == "nt" else "bin")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), "/usr/bin"]))
    monkeypatch.setenv("RESTCLIENT_HOME", "x")
    env = launcher.command_env(tmp_path)
    parts = env["PATH"].split(os.pathsep)
    assert parts[:2] == [str(tmp_path / "bin"), str(tmp_path)]
    assert str(venv_bin) not in parts and "/usr/bin" in parts
    assert "VIRTUAL_ENV" not in env and "RESTCLIENT_HOME" not in env


def test_list_connections(tmp_path):
    (tmp_path / "district-a.spf").write_text("x")
    (tmp_path / ".DS_Store").write_text("x")
    (tmp_path / "region 2").mkdir()
    (tmp_path / "region 2" / "district-b.spf").write_text("x")
    (tmp_path / "saved.spfs").mkdir()  # a Sequel Ace bundle: one item, not a folder to look inside
    (tmp_path / "saved.spfs" / "inner.plist").write_text("x")
    items = launcher.list_connections(tmp_path)
    assert [(i["group"], i["name"]) for i in items] == [("", "district-a"), ("region 2", "district-b"), ("", "saved")]
    assert launcher.list_connections(tmp_path / "missing") == []


def test_open_and_terminal_commands(monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    assert launcher.open_command("/x/a.spf", "Sequel Ace") == ["open", "-a", "Sequel Ace", "/x/a.spf"]
    assert launcher.open_command("/x/a.spf") == ["open", "/x/a.spf"]
    cmd = launcher.terminal_launch(['echo "hi"', "tool prod"], "/Users/me/My Tools")
    assert cmd[0] == "osascript"
    assert cmd[-1] == 'tell application "Terminal" to do script "cd \'/Users/me/My Tools\' && echo \\"hi\\" && tool prod"'


def test_age_text():
    assert launcher.age_text(10) == "just now"
    assert launcher.age_text(600) == "10 min ago"
    assert launcher.age_text(7200) == "2 h ago"


def test_open_path_reports_failures(monkeypatch):
    monkeypatch.setattr(launcher, "open_command", lambda path, app="": [sys.executable, "-c",
                        "import sys; sys.stderr.write('Unable to find application named Sequel Acee'); sys.exit(1)"])
    try:
        launcher.open_path("/x/a.spf", "Sequel Acee")
    except OSError as e:
        assert "Unable to find application" in str(e)
    else:
        raise AssertionError("expected OSError")
