"""Part B pieces that don't need a window: one copy only, Start with Windows, status for the tray, QR contents,
no question at start, and Check Claude Code."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import uuid

import pytest
from PySide6.QtCore import QCoreApplication

from claudein import __main__ as entry
from claudein import autostart
from claudein.service import Service

from .conftest import make_claude, rate_limit, result, spec


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_one_copy_only(qapp, monkeypatch):
    name = "ClaudeIn-test-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(entry, "instance_name", lambda: name)
    assert not entry.other_copy_running()
    heard = []
    one = entry.OneCopy(heard.append)
    # a second start, as its own program, as when the owner starts ClaudeIn twice
    code = ("import sys; from PySide6.QtCore import QCoreApplication; app = QCoreApplication([]); "
            "import claudein.__main__ as e; e.instance_name = lambda: sys.argv[1]; "
            "sys.exit(0 if e.other_copy_running(b'show') else 3)")
    second = subprocess.Popen([sys.executable, "-c", code, name], cwd=str(autostart.PROJECT))
    try:
        end = time.monotonic() + 15
        while second.poll() is None and time.monotonic() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        assert second.returncode == 0  # it saw the running copy
        assert heard == ["show"]  # and the running copy heard it (to show the balloon)
    finally:
        if second.poll() is None:
            second.kill()
        one.server.close()
    assert not entry.other_copy_running()


def test_watchdog_does_nothing_after_quit(home):
    autostart.set_quit_flag()
    assert autostart.quit_flag_set()
    assert entry.main(["--watchdog"]) == 0  # returns at once: no window, no engine
    autostart.clear_quit_flag()
    assert not autostart.quit_flag_set()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_shortcut(tmp_path):
    import win32com.client

    path = tmp_path / "ClaudeIn.lnk"
    autostart.make_shortcut(path)
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
    assert sc.TargetPath.lower().endswith("pythonw.exe")
    assert sc.Arguments == "-m claudein"
    assert sc.WorkingDirectory.lower() == str(autostart.PROJECT).lower()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Task Scheduler")
def test_restart_task_added_and_removed():
    name = "ClaudeIn test " + uuid.uuid4().hex[:8]
    try:
        assert autostart.add_task(name)
        assert autostart.task_exists(name)
    finally:
        autostart.remove_task(name)
    assert not autostart.task_exists(name)


def test_task_runs_the_watchdog():
    xml = autostart.task_xml()
    assert "<Arguments>-m claudein --watchdog</Arguments>" in xml
    assert "<Interval>PT5M</Interval>" in xml  # every 5 minutes since phase 7 (every minute looked like malware)
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml  # never cut ClaudeIn off after 72 hours
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml  # no admin rights


def test_qr_payload():
    from claudein.windows import qr_payload

    data = json.loads(qr_payload(["192.168.1.20", "100.64.1.2"], 8790, "ABCDEFGH23", "AB" * 32))
    assert data == {"claudein": 1, "addresses": ["192.168.1.20", "100.64.1.2"], "port": 8790,
                    "code": "ABCDE-FGH23", "fingerprint": "AB" * 32}


def test_qr_picture(qapp):
    from claudein.windows import qr_pixmap

    assert not qr_pixmap('{"claudein":1}').isNull()


def test_tray_states(qapp):
    from claudein.tray import icon_image, state_of

    base = {"problems": [], "paused": False, "working": 0, "waiting": 0}
    assert state_of(base) == "normal"
    assert state_of(dict(base, working=1)) == "working"
    assert state_of(dict(base, paused=True)) == "paused"
    assert state_of(dict(base, problems=["x"], paused=True)) == "problem"
    for s in ("normal", "working", "paused", "problem"):
        assert not icon_image(s, 32).isNull()


async def test_no_question_at_start(svc, fake):
    st = svc.status()
    assert fake.calls == []  # starting (and showing the status) never asks Claude anything
    assert st["signed_in"] is True and st["plan"] is None  # signed in comes from `auth status`, which is free
    assert st["claude"]["state"] == "ok" and st["claude"]["email"] == "owner@example.com"
    assert st["problems"] == []


async def test_check_claude_code(svc, fake):
    d = await svc.check_claude()
    assert d["ending"] == "done"
    prompt, opts = fake.calls[-1]
    assert opts.model == "haiku" and opts.tools == [] and opts.max_turns == 1
    assert svc.status()["signed_in"] is True
    assert svc.usage.recent("claudein")[0]["device"] == "ClaudeIn (this PC)"


async def test_status_after_runs(svc, phone, fake):
    async def script(prompt, options, tools):
        yield rate_limit(five=0.5, week=0.2)
        yield result("ok")

    fake.script = script
    await phone.ask(spec())
    st = svc.status()
    assert st["plan"]["five_hour"] == 0.5 and st["plan"]["seven_day"] == 0.2
    assert st["signed_in"] is True


async def test_signed_out_is_a_problem(svc, phone, fake):
    async def script(prompt, options, tools):
        yield result("Not logged in · Please run /login", is_error=True)

    fake.script = script
    done = (await phone.ask(spec()))[-1]
    assert done["error_kind"] == "unavailable"
    assert any("signed out" in p for p in svc.status()["problems"])


async def test_plan_use_remembered_from_usage(home, fake):
    s = Service(home, query_fn=fake.query, claude=make_claude(home)[0])
    await s.start(use_tls=False, phone_port=0, local_port=0)
    await s.check_claude()
    await s.stop()
    s2 = Service(home, query_fn=fake.query, claude=make_claude(home)[0])
    try:
        assert s2.plan_use()["five_hour"] == 0.34  # from the last run in usage.db, without asking again
    finally:
        s2.usage.close()


async def test_port_in_use_is_a_problem(home, fake):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    s = Service(home, query_fn=fake.query, claude=make_claude(home)[0])
    s.settings.local_port = port
    try:
        await s.start_safely()
        assert any("port" in p for p in s.status()["problems"])
    finally:
        blocker.close()
        await s.stop()
