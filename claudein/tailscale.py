"""Finding this PC on Tailscale (plan phase 4): its Tailscale address and name, and whether Tailscale is on.

It only asks the Tailscale program installed on this PC (`tailscale status --json`). Nothing goes to the internet, and
nothing is changed.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Tailscale:
    state: str  # "on", "off" (installed but not connected) or "missing" (not installed)
    name: str = ""  # the PC's Tailscale name, such as my-pc
    full_name: str = ""  # with the tailnet, such as my-pc.example.ts.net
    addresses: list[str] = field(default_factory=list)  # IPv4 only: what a person types into a phone

    @property
    def on(self) -> bool:
        return self.state == "on" and bool(self.addresses)


def find_exe() -> Path | None:
    found = shutil.which("tailscale")
    if found:
        return Path(found)
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base and (p := Path(base) / "Tailscale" / "tailscale.exe").is_file():
            return p
    return None


def parse(text: str) -> Tailscale:
    """Reads the answer of `tailscale status --json`."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return Tailscale("off")
    if not isinstance(data, dict) or data.get("BackendState") != "Running":
        return Tailscale("off")
    me = data.get("Self") or {}
    full = str(me.get("DNSName") or "").rstrip(".")
    name = full.split(".", 1)[0] if full else str(me.get("HostName") or "")
    addresses = []
    for a in me.get("TailscaleIPs") or []:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version == 4 and ip in TAILSCALE_V4:
            addresses.append(str(ip))
    return Tailscale("on" if addresses else "off", name, full, addresses)


def status(timeout: float = 5.0) -> Tailscale:
    exe = find_exe()
    if exe is None:
        return Tailscale("missing")
    try:
        out = subprocess.run([str(exe), "status", "--json"], capture_output=True, text=True, timeout=timeout,
                             creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return Tailscale("off")
    return parse(out.stdout)


def is_tailscale(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in TAILSCALE_V4
    except ValueError:
        return False
