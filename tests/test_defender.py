"""Phase 7: fewer false Windows Defender alarms. The restart task is its own switch (off, every 5 minutes), ClaudeIn
closes politely with --quit, updates start the normal Windows way, the program carries version details, and test
installers never touch the real ClaudeIn."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from claudein import __main__ as entry
from claudein import autostart, config, updates

ROOT = Path(__file__).resolve().parent.parent
ISS = (ROOT / "packaging" / "ClaudeIn.iss").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def task(tmp_path, monkeypatch):
    """A private Startup folder and a uniquely named restart task, removed afterwards."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    name = "ClaudeIn test " + uuid.uuid4().hex[:8]
    monkeypatch.setattr(autostart, "TASK_NAME", name)
    try:
        yield name
    finally:
        autostart.remove_task(name)


# --- the restart task ---------------------------------------------------------------------------------------------------------

def test_the_restart_task_is_off_by_default_and_every_5_minutes():
    assert config.Settings().restart_if_stopped is False
    xml = autostart.task_xml()
    assert "<Interval>PT5M</Interval>" in xml and "within 5 minutes" in xml and "PT1M" not in xml


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler")
def test_start_with_windows_no_longer_adds_the_restart_task(task):
    assert autostart.set_enabled(True)
    assert autostart.startup_shortcut().exists() and not autostart.task_exists(task)
    assert autostart.set_restart(True) and autostart.task_exists(task)
    assert autostart.set_restart(False) and not autostart.task_exists(task)
    autostart.set_enabled(False)
    assert not autostart.startup_shortcut().exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler")
def test_an_older_every_minute_task_is_cleared_when_the_switch_is_off(task):
    assert autostart.add_task(task)  # what older versions left behind
    autostart.sync_restart(False)
    assert not autostart.task_exists(task)
    autostart.sync_restart(True)
    assert autostart.task_exists(task)


async def test_the_general_page_switch_sets_the_task_and_the_setting(qapp, svc, monkeypatch):
    from claudein.windows import GeneralPage

    calls = []
    monkeypatch.setattr(autostart, "set_restart", lambda on: calls.append(on) or True)
    page = GeneralPage(svc, lambda: None)
    assert page.restart_sw.isChecked() is False
    await page._restart_switch(True)
    assert calls == [True] and svc.settings.restart_if_stopped is True
    assert config.Settings.load(svc.folder).restart_if_stopped is True
    monkeypatch.setattr(autostart, "set_restart", lambda on: False)  # Windows refuses
    await page._restart_switch(True)
    assert svc.settings.restart_if_stopped is False


# --- closing politely -------------------------------------------------------------------------------------------------------

QUIT_CODE = ("import sys; from PySide6.QtCore import QCoreApplication; app = QCoreApplication([]); "
             "import claudein.__main__ as e; e.instance_name = lambda: sys.argv[1]; "
             "sys.exit(e.quit_running_copy(float(sys.argv[2])))")


def ask_to_quit(name: str, wait_s: float, until_done) -> int:
    p = subprocess.Popen([sys.executable, "-c", QUIT_CODE, name, str(wait_s)], cwd=str(ROOT))
    end = time.monotonic() + 20
    try:
        while p.poll() is None and time.monotonic() < end:
            QCoreApplication.processEvents()
            until_done()
            time.sleep(0.01)
        return p.returncode
    finally:
        if p.poll() is None:
            p.kill()


def test_quit_asks_the_running_copy_and_waits_for_it(qapp, monkeypatch):
    name = "ClaudeIn-test-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(entry, "instance_name", lambda: name)
    heard = []
    one = entry.OneCopy(heard.append)
    closing = {"at": None}

    def close_when_asked():  # what the real ClaudeIn does on "quit": it closes (here, a moment later)
        if heard and closing["at"] is None:
            closing["at"] = time.monotonic()
        if closing["at"] and time.monotonic() - closing["at"] > 0.7 and one.server.isListening():
            one.server.close()

    assert ask_to_quit(name, 10, close_when_asked) == 0
    assert heard == ["quit"]


def test_quit_says_so_when_the_copy_does_not_close(qapp, monkeypatch):
    name = "ClaudeIn-test-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(entry, "instance_name", lambda: name)
    heard = []
    one = entry.OneCopy(heard.append)
    try:
        assert ask_to_quit(name, 1.0, lambda: None) == 1  # so the installer knows to stop it by force
        assert heard == ["quit"]
    finally:
        one.server.close()


def test_quit_with_nothing_running_returns_at_once(qapp, monkeypatch):
    monkeypatch.setattr(entry, "instance_name", lambda: "ClaudeIn-test-" + uuid.uuid4().hex[:8])
    start = time.monotonic()
    assert entry.main(["--quit"]) == 0
    assert time.monotonic() - start < 3


def test_the_installer_closes_claudein_politely_and_forces_only_as_a_last_resort():
    assert "'--quit'" in ISS and "procedure StopClaudeIn" in ISS
    stop = ISS[ISS.index("procedure StopClaudeIn"):]
    stop = stop[:stop.index("end;\n\n")]
    assert stop.index("'--quit'") < stop.index("taskkill")  # polite first
    assert stop.count("if ClaudeInRunning then") == 1  # force only if still running
    assert "[UninstallRun]" not in ISS  # no unconditional stop by name any more
    assert "if CurUninstallStep = usUninstall then" in ISS


def test_test_installers_have_their_own_id_and_leave_the_real_claudein_alone():
    start = ISS.index("#ifdef TestBuild")
    middle = ISS.index("#else", start)
    test_part = ISS[start:middle]
    real_part = ISS[middle:ISS.index("#endif", middle)]
    assert "AppId=" in test_part and "AppId=" in real_part
    assert test_part.split("AppId=")[1].split("\n")[0] != real_part.split("AppId=")[1].split("\n")[0]
    assert "ClaudeIn-Test" in test_part
    # tasks, shortcuts, --setup and every stopping of ClaudeIn are only in the real installer
    for marker in ("[Tasks]", "[Icons]", "procedure StopClaudeIn", "function PrepareToInstall"):
        assert ISS[:ISS.index(marker)].rstrip().endswith("#ifndef TestBuild") or \
            ISS.rfind("#ifndef TestBuild", 0, ISS.index(marker)) > ISS.rfind("#endif", 0, ISS.index(marker))


def test_the_installer_names_its_publisher():
    for line in ("AppPublisher={#Publisher}", "VersionInfoCompany={#Publisher}", "VersionInfoVersion={#AppVersion}",
                 '#define Publisher "ikverse"', "AppPublisherURL={#Site}"):
        assert line in ISS


# --- updates start the normal way -------------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows ShellExecute")
def test_an_update_starts_like_a_double_click(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(updates.os, "startfile", lambda *a, **k: seen.append((a, k)))
    updates.launch_installer(tmp_path / "ClaudeIn-Setup-9.9.9.exe")
    (args, kwargs), = seen
    assert args == (str(tmp_path / "ClaudeIn-Setup-9.9.9.exe"),)
    assert kwargs["arguments"] == "/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE=1" and kwargs["show_cmd"] == 1


# --- version details --------------------------------------------------------------------------------------------------------

def load_build():
    spec = importlib.util.spec_from_file_location("claudein_build", ROOT / "packaging" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_program_gets_its_name_version_and_publisher():
    from PyInstaller.utils.win32 import versioninfo

    text = load_build().version_info_text("1.2.3")
    info = eval(text, vars(versioninfo))  # the same reading PyInstaller does
    assert info.ffi.fileVersionMS == (1 << 16) | 2 and info.ffi.fileVersionLS == 3 << 16
    strings = {s.name: s.val for s in info.kids[0].kids[0].kids}
    assert strings["CompanyName"] == "ikverse" and strings["ProductName"] == "ClaudeIn"
    assert strings["FileVersion"] == "1.2.3" and strings["OriginalFilename"] == "ClaudeIn.exe"
    assert strings["Comments"] == "https://github.com/ikverse/ClaudeIn"


def test_the_spec_uses_the_version_details():
    spec = (ROOT / "packaging" / "ClaudeIn.spec").read_text(encoding="utf-8")
    assert "version=VERSION_FILE" in spec and "version_info.txt" in spec
