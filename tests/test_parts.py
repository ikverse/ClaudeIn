"""The pieces on their own: address filter, codes, wrong-code guard, prompt, backup, job object."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from claudein import backup, netguard, tls, winproc
from claudein.pairing import LocalToken, Pairings, WrongCodeGuard, app_id, hash_code, normalize, shown
from claudein.runner import prompt_text
from claudein.runs import BadRequest, RunSpec


def test_address_filter():
    for ok in ("127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "172.20.1.1", "100.101.102.103",
               "fe80::1%12", "fd12:3456::1", "fd7a:115c:a1e0::1", "::ffff:192.168.1.2"):
        assert netguard.allowed(ok), ok
    for bad in ("8.8.8.8", "2001:4860:4860::8888", "172.32.0.1", "100.128.0.1", "", None, "nonsense"):
        assert not netguard.allowed(bad), bad
    assert netguard.is_loopback("127.0.0.1") and netguard.is_loopback("::1")
    assert not netguard.is_loopback("192.168.1.2")


def test_codes():
    assert normalize("abcde-fgh23 ") == "ABCDEFGH23"
    assert shown("ABCDEFGH23") == "ABCDE-FGH23"
    assert hash_code("abcde-fgh23") == hash_code("ABCDEFGH23")
    assert app_id("Signal Lab EGX") == "signal-lab-egx"


def test_pairings_store_only_hashes(tmp_path):
    p = Pairings(tmp_path)
    dev, code = p.pair("Signal Lab", "Phone")
    text = (tmp_path / "devices.json").read_text()
    assert code not in text and shown(code) not in text
    assert p.check(shown(code).lower()).id == dev.id
    assert p.check("WRONG00000") is None
    assert Pairings(tmp_path).devices[dev.id].first_used is not None


def test_unused_code_expires(tmp_path):
    p = Pairings(tmp_path)
    dev, code = p.pair("Signal Lab", "Phone")
    p.devices[dev.id].created -= 31 * 60
    assert p.check(code) is None
    p.prune()
    assert dev.id not in p.devices


def test_used_code_does_not_expire(tmp_path):
    p = Pairings(tmp_path)
    dev, code = p.pair("Signal Lab", "Phone")
    assert p.check(code) is not None
    p.devices[dev.id].created -= 31 * 60
    assert p.check(code) is not None


def test_wrong_code_guard():
    now = [0.0]
    g = WrongCodeGuard(clock=lambda: now[0])
    delays = [g.failed("1.2.3.4") for _ in range(19)]
    assert delays[0] == 0.25 and delays[-1] == 4.75 and delays == sorted(delays)
    assert not g.blocked("1.2.3.4")
    assert g.failed("1.2.3.4") == 5.0
    assert g.blocked("1.2.3.4") and not g.blocked("5.6.7.8")
    now[0] += 3601
    assert not g.blocked("1.2.3.4")
    g.failed("9.9.9.9")
    g.succeeded("9.9.9.9")
    assert g.failed("9.9.9.9") == 0.25


def test_local_token(tmp_path):
    t = LocalToken(tmp_path, 8791)
    data = json.loads((tmp_path / "local.json").read_text())
    assert data == {"token": t.token, "port": 8791}
    old = t.token
    t.renew()
    assert t.token != old and not t.matches(old) and t.matches(t.token)
    assert LocalToken(tmp_path, 8791).token == t.token


def test_prompt_with_history_as_kotlin_host():
    s = RunSpec.parse({"model": "claude-opus-5-5", "system": "s", "prompt": "And now?",
                       "history": [{"role": "user", "text": " Hi "}, {"role": "assistant", "text": "Hello"}]})
    assert prompt_text(s) == ("Earlier in this conversation:\n\nThe owner: Hi\n\nYou: Hello\n\n"
                              "The owner now asks:\nAnd now?")


def test_run_spec_rules():
    base = {"model": "claude-opus-5-5", "system": "s", "prompt": "p"}
    s = RunSpec.parse(base)
    assert s.max_budget_usd == 5.0 and s.builtin_tools == ["WebSearch", "WebFetch"] and s.tool_timeout_s == 60
    assert RunSpec.parse(dict(base, max_budget_usd=500)).max_budget_usd == 50
    assert RunSpec.parse(dict(base, builtin_tools=[])).builtin_tools == []
    for bad in (dict(base, model="Bad Model"), {"model": "claude-x", "system": "s"},
                dict(base, builtin_tools=["Bash"]), dict(base, tools=[{"name": "no spaces"}])):
        with pytest.raises(BadRequest):
            RunSpec.parse(bad)


def test_backup_then_restore(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    tls.ensure_certificate(a)
    dev, code = Pairings(a).pair("Signal Lab", "Phone")
    backup.export(a, tmp_path / "pairings.backup")
    tls.ensure_certificate(b)
    assert tls.fingerprint(a) != tls.fingerprint(b)
    backup.restore(b, tmp_path / "pairings.backup")
    assert tls.fingerprint(b) == tls.fingerprint(a)
    assert Pairings(b).check(code).id == dev.id
    with pytest.raises(ValueError):
        (tmp_path / "junk").write_text("{}")
        backup.restore(b, tmp_path / "junk")


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows only")
async def test_job_object_stops_child_programs(tmp_path):
    """A program started for a run, and anything it starts, all end when the run's job is closed."""
    import anyio

    winproc.patch_sdk()
    from claude_agent_sdk._internal.transport import subprocess_cli

    pidfile = tmp_path / "child.pid"
    code = (f"import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"open(r'{pidfile}','w').write(str(c.pid)); time.sleep(60)")
    job = winproc.Job()
    token = winproc.current_job.set(job)
    try:
        proc = await subprocess_cli.anyio.open_process([sys.executable, "-c", code])
    finally:
        winproc.current_job.reset(token)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert _alive(proc.pid) and _alive(child)
    job.close()
    with anyio.fail_after(5):
        await proc.wait()
    time.sleep(0.3)
    assert not _alive(proc.pid) and not _alive(child)
