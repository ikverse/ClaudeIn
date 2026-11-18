"""The Windows Firewall rule for phones (plan phase 4 and 4.7).

One rule, named RULE_NAME, lets in only TCP port 8790, only for the program that really runs ClaudeIn, and only from
Tailscale and the home network. Adding it needs administrator rights, so Windows asks once (UAC) when the owner clicks
"Set up phone access". The same step removes the wide "claudein.exe" rules Windows made from its first question.

From .venv, the program Windows sees is the shared C:\\Python314\\pythonw.exe (the .venv hands off to it), which Village
uses too. So the broad "Python" rules are left alone here: removing them would cut off Village's phone page.

Reading whether the rule is there needs no administrator rights.
"""

from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RULE_NAME = "ClaudeIn phones"
GROUP = "ClaudeIn"
REMOTE = ("LocalSubnet", "100.64.0.0/10", "fd7a:115c:a1e0::/48")  # home network, Tailscale IPv4 and IPv6
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CANCELLED = 1223  # Windows' "the operation was cancelled by the user": No on the UAC question


def program() -> Path:
    """The program Windows sees listening: ClaudeIn.exe when installed, else the real pythonw.exe behind .venv."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    buf = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
        exe = Path(buf.value)
    else:
        exe = Path(getattr(sys, "_base_executable", sys.executable))
    w = exe.with_name("pythonw.exe")
    return w if w.is_file() else exe


def _q(text: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(text).replace("'", "''") + "'"


def setup_script(prog: Path, port: int) -> str:
    """The PowerShell run as administrator: remove the old wide rules, then add the one narrow rule."""
    remote = ",".join(_q(r) for r in REMOTE)
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"Get-NetFirewallRule -DisplayName {_q(RULE_NAME)} -ErrorAction SilentlyContinue | Remove-NetFirewallRule",
        # Windows' own rules from its first question, named after the program: "claudein.exe", every network.
        "Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*\\claudein.exe' } | Get-NetFirewallRule |"
        " Where-Object { $_.DisplayName -eq 'claudein.exe' } | Remove-NetFirewallRule",
        f"New-NetFirewallRule -DisplayName {_q(RULE_NAME)} -Group {_q(GROUP)} -Direction Inbound -Action Allow"
        f" -Protocol TCP -LocalPort {int(port)} -Program {_q(prog)} -RemoteAddress {remote} -Profile Any"
        f" -Description {_q('Lets paired phones reach ClaudeIn, only from Tailscale and the home network.')}"
        " | Out-Null",
    ])


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _elevated_launcher(script: str) -> str:
    """Starts the setup script as administrator (Windows asks the owner) and passes on how it ended."""
    args = ",".join(_q(a) for a in ("-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                                    "-EncodedCommand", _encoded(script)))
    return ("try { $p = Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -Wait -PassThru"
            f" -ArgumentList {args}; exit $p.ExitCode }} catch {{ exit {CANCELLED} }}")


def _powershell(script: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(script)],
                          capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                          encoding="utf-8", errors="replace")


def set_up(port: int) -> str:
    """Adds the rule. Blocks until Windows' question is answered. Returns "done", "cancelled" or "failed"."""
    try:
        code = _powershell(_elevated_launcher(setup_script(program(), port)), timeout=300).returncode
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "done" if code == 0 else "cancelled" if code == CANCELLED else "failed"


@dataclass
class RuleState:
    state: str  # "ok", "missing", "off" (turned off) or "different" (another program or port)
    detail: str = ""


def read_script() -> str:
    return "\n".join([
        f"$r = Get-NetFirewallRule -DisplayName {_q(RULE_NAME)} -ErrorAction SilentlyContinue | Select-Object -First 1",
        "if (-not $r) { 'missing'; exit 0 }",
        "@{ enabled = [string]$r.Enabled; program = [string]($r | Get-NetFirewallApplicationFilter).Program;"
        " port = [string]($r | Get-NetFirewallPortFilter).LocalPort } | ConvertTo-Json -Compress",
    ])


def judge(text: str, prog: Path, port: int) -> RuleState:
    """Compares what Windows has with what ClaudeIn needs."""
    text = (text or "").strip()
    if text == "missing" or not text:
        return RuleState("missing")
    try:
        r = json.loads(text)
    except ValueError:
        return RuleState("missing")
    if str(r.get("enabled")) != "True":
        return RuleState("off")
    if str(r.get("program", "")).lower() != str(prog).lower():
        return RuleState("different", "it is for another copy of ClaudeIn")
    if str(r.get("port")) != str(port):
        return RuleState("different", f"it is for port {r.get('port')}")
    return RuleState("ok")


def check(port: int) -> RuleState:
    try:
        out = _powershell(read_script(), timeout=30)
    except (OSError, subprocess.SubprocessError):
        return RuleState("missing", "Windows Firewall couldn't be read")
    return judge(out.stdout, program(), port)
