"""Phase 4: finding Tailscale, the firewall rule ClaudeIn builds, and the "Phone access" check. Never touches the real
firewall."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from claudein import firewall, tailscale
from claudein.service import PhoneAccess

RUNNING = json.dumps({
    "BackendState": "Running",
    "Self": {"HostName": "My-PC", "DNSName": "my-pc.example.ts.net.",
             "TailscaleIPs": ["100.100.1.2", "fd7a:115c:a1e0::1:2"]},
})


def test_tailscale_on():
    ts = tailscale.parse(RUNNING)
    assert ts.on and ts.state == "on"
    assert ts.name == "my-pc" and ts.full_name == "my-pc.example.ts.net"
    assert ts.addresses == ["100.100.1.2"]  # IPv4 only: what a person types


def test_tailscale_off_or_unreadable():
    for text in (json.dumps({"BackendState": "Stopped", "Self": {"TailscaleIPs": ["100.1.2.3"]}}),
                 json.dumps({"BackendState": "NeedsLogin"}), "", "not json", "[]"):
        ts = tailscale.parse(text)
        assert not ts.on and ts.state == "off", text
    # an address outside Tailscale's range is never offered
    assert tailscale.parse(json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": ["8.8.8.8"]}})).addresses == []


def test_tailscale_missing(monkeypatch):
    monkeypatch.setattr(tailscale, "find_exe", lambda: None)
    assert tailscale.status().state == "missing"


def test_is_tailscale():
    assert tailscale.is_tailscale("100.100.1.2")
    assert not tailscale.is_tailscale("192.168.1.20") and not tailscale.is_tailscale("100.128.0.1")
    assert not tailscale.is_tailscale("nonsense")


def test_rule_is_narrow():
    prog = Path(r"C:\Users\Me\AppData\Local\Programs\ClaudeIn\ClaudeIn.exe")
    s = firewall.setup_script(prog, 8790)
    new = next(line for line in s.splitlines() if line.startswith("New-NetFirewallRule"))
    assert "-Direction Inbound" in new and "-Action Allow" in new
    assert "-Protocol TCP" in new and "-LocalPort 8790" in new
    assert f"-Program '{prog}'" in new
    assert "-RemoteAddress 'LocalSubnet','100.64.0.0/10','fd7a:115c:a1e0::/48'" in new
    assert "8791" not in s and "8765" not in s  # loopback needs no rule; Village's port is never touched
    # only Windows' own wide "claudein.exe" rules are removed, never the "Python" ones
    assert "DisplayName -eq 'claudein.exe'" in s and "python" not in s.lower()


def test_rule_quotes_paths():
    s = firewall.setup_script(Path(r"C:\Users\O'Neil\ClaudeIn.exe"), 8790)
    assert r"-Program 'C:\Users\O''Neil\ClaudeIn.exe'" in s


def test_setup_asks_windows_and_never_runs_hidden_as_admin_otherwise(monkeypatch):
    seen = {}

    def fake(script, timeout):
        seen["script"] = script
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(firewall, "_powershell", fake)
    assert firewall.set_up(8790) == "done"
    assert "-Verb RunAs" in seen["script"]  # the UAC question
    inner = seen["script"].split("'-EncodedCommand','", 1)[1].split("'", 1)[0]
    assert "New-NetFirewallRule" in base64.b64decode(inner).decode("utf-16-le")

    monkeypatch.setattr(firewall, "_powershell", lambda s, timeout: SimpleNamespace(returncode=1223, stdout=""))
    assert firewall.set_up(8790) == "cancelled"
    monkeypatch.setattr(firewall, "_powershell", lambda s, timeout: SimpleNamespace(returncode=1, stdout=""))
    assert firewall.set_up(8790) == "failed"


def test_judge_rule():
    prog = Path(r"C:\Python314\pythonw.exe")
    good = json.dumps({"enabled": "True", "program": r"c:\python314\pythonw.exe", "port": "8790"})
    assert firewall.judge(good, prog, 8790).state == "ok"
    assert firewall.judge("missing", prog, 8790).state == "missing"
    assert firewall.judge("", prog, 8790).state == "missing"
    assert firewall.judge(json.dumps({"enabled": "False", "program": str(prog), "port": "8790"}),
                          prog, 8790).state == "off"
    assert firewall.judge(json.dumps({"enabled": "True", "program": r"C:\Other\ClaudeIn.exe", "port": "8790"}),
                          prog, 8790).state == "different"
    assert firewall.judge(json.dumps({"enabled": "True", "program": str(prog), "port": "9000"}),
                          prog, 8790).state == "different"


def test_program_is_a_real_file():
    assert firewall.program().is_file()


def test_phone_access_wording():
    on = tailscale.parse(RUNNING)
    pa = PhoneAccess(on, firewall.RuleState("ok"), 8790, listening=True)
    assert pa.all_ok and not pa.needs_rule
    assert "my-pc" in pa.lines[0][1] and "100.100.1.2" in pa.lines[0][1]

    pa = PhoneAccess(tailscale.Tailscale("off"), firewall.RuleState("missing"), 8790, listening=False)
    assert [ok for ok, _ in pa.lines] == [False, False, False] and pa.needs_rule
    assert "Tailscale is off" in pa.lines[0][1] and "isn't set up" in pa.lines[1][1]
    assert "Tailscale isn't installed" in PhoneAccess(tailscale.Tailscale("missing"), firewall.RuleState("ok"),
                                                      8790, True).lines[0][1]


def test_pairing_addresses_put_tailscale_first():
    from claudein.windows import address_text, qr_payload

    text = address_text(tailscale.parse(RUNNING), ["192.168.1.20"], 8790)
    first, second = text.splitlines()
    assert first.startswith("100.100.1.2:8790") and "home and away" in first
    assert second.startswith("192.168.1.20:8790") and "home only" in second
    off = address_text(tailscale.Tailscale("off"), ["192.168.1.20"], 8790)
    assert "Tailscale is off" in off and "100." not in off

    data = json.loads(qr_payload(["100.100.1.2", "192.168.1.20"], 8790, "ABCDEFGH23", "AB" * 32,
                                 name="my-pc.example.ts.net"))
    assert data["addresses"][0] == "100.100.1.2" and data["name"] == "my-pc.example.ts.net"
