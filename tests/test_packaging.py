"""Phase 5b: ClaudeIn installed as ClaudeIn.exe (no .venv, no `-m claudein`), and the uninstaller's clean-up."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from claudein import __main__ as entry
from claudein import autostart


@pytest.fixture
def installed(tmp_path, monkeypatch) -> Path:
    """Pretends ClaudeIn is running as the installed ClaudeIn.exe, in a folder with a space in its name."""
    exe = tmp_path / "Program Files" / "ClaudeIn" / "ClaudeIn.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    monkeypatch.setattr(autostart, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(autostart, "PROJECT", exe.parent)
    return exe


def test_run_from_venv_still_starts_with_dash_m():
    assert not autostart.FROZEN
    assert autostart.launch_args() == "-m claudein"
    assert autostart.launch_args("--watchdog") == "-m claudein --watchdog"


def test_the_installed_program_starts_without_dash_m(installed):
    assert autostart.pythonw() == installed
    assert autostart.launch_args() == "" and autostart.launch_args("--watchdog") == "--watchdog"
    assert autostart.launch_args("--after-restart").split() == ["--after-restart"]
    xml = autostart.task_xml()
    assert f"<Command>{installed}</Command>" in xml
    assert "<Arguments>--watchdog</Arguments>" in xml
    assert f"<WorkingDirectory>{installed.parent}</WorkingDirectory>" in xml


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_the_installed_shortcut_points_at_the_program(installed, tmp_path):
    import win32com.client

    path = tmp_path / "ClaudeIn.lnk"
    autostart.make_shortcut(path)
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
    assert Path(sc.TargetPath) == installed and sc.Arguments == ""
    assert Path(sc.WorkingDirectory) == installed.parent


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler")
def test_the_uninstaller_removes_only_what_starts_this_program(installed, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    name = "ClaudeIn test " + uuid.uuid4().hex[:8]
    monkeypatch.setattr(autostart, "TASK_NAME", name)
    try:
        autostart.make_shortcut(autostart.startup_shortcut())
        assert autostart.add_task(name)
        # another copy of ClaudeIn (for example one run from .venv) asks to clean up: nothing of ours is touched
        other = tmp_path / "elsewhere" / "pythonw.exe"
        other.parent.mkdir()
        other.write_bytes(b"x")
        monkeypatch.setattr(sys, "executable", str(other))
        autostart.remove_if_ours()
        assert autostart.startup_shortcut().exists() and autostart.task_exists(name)
        # the installed program asks: its shortcut and task go
        monkeypatch.setattr(sys, "executable", str(installed))
        autostart.remove_if_ours()
        assert not autostart.startup_shortcut().exists() and not autostart.task_exists(name)
    finally:
        autostart.remove_task(name)


def test_the_uninstaller_command_does_not_start_claudein(monkeypatch):
    called = []
    monkeypatch.setattr(autostart, "remove_if_ours", lambda: called.append(1))
    assert entry.main(["--remove-startup"]) == 0  # returns at once: no window, no engine
    assert called == [1]
