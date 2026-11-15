"""Pairings and access (plan 4.3).

- One code per pairing (one app on one device). Codes are kept only as SHA-256 hashes, so ClaudeIn can check a code but
  never show it again. A code never used expires after 30 minutes.
- Each device can be switched off, or forgotten for good.
- Each app has a list of allowed models; empty means any model.
- Wrong codes are slowed down, and 20 in a row from one address block it for an hour.
- Programs on this PC use one token from local.json instead of pairing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import write_atomic

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read and type
CODE_LENGTH = 10
UNUSED_CODE_LIFETIME_S = 30 * 60
DEVICES = "devices.json"
LOCAL = "local.json"


def random_code(n: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def normalize(code: str) -> str:
    """A code as a person types it: case, spaces and dashes don't matter."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def shown(code: str) -> str:
    """Two groups of five, as the phones show it."""
    return "-".join(code[i : i + 5] for i in range(0, len(code), 5))


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize(code).encode()).hexdigest()


def app_id(name: str) -> str:
    """"Signal Lab EGX" -> "signal-lab-egx"."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@dataclass
class Device:
    id: str
    app_id: str
    app_name: str
    device: str
    code_hash: str
    created: float
    first_used: float | None = None
    last_used: float | None = None
    enabled: bool = True

    @property
    def label(self) -> str:
        return f"{self.app_name} — {self.device}"


@dataclass
class App:
    name: str
    models: list[str] = field(default_factory=list)  # empty = any model
    cap_points: float | None = None  # daily cap in plan points (phase 2); None = no cap


class Pairings:
    """devices.json: the paired devices and the per-app settings."""

    def __init__(self, folder: Path, on_change: Callable[[], None] | None = None):
        self.path = folder / DEVICES
        self.on_change = on_change or (lambda: None)
        self.devices: dict[str, Device] = {}
        self.apps: dict[str, App] = {}
        self.load()

    # --- storage -----------------------------------------------------------------------------------------------------

    def load(self) -> None:
        self.devices, self.apps = {}, {}
        if not self.path.exists():
            return
        self._mtime = self.path.stat().st_mtime
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for d in data.get("devices", []):
            dev = Device(**d)
            self.devices[dev.id] = dev
        for aid, a in data.get("apps", {}).items():
            self.apps[aid] = App(**a)

    def save(self) -> None:
        data = {
            "devices": [asdict(d) for d in self.devices.values()],
            "apps": {aid: asdict(a) for aid, a in self.apps.items()},
        }
        write_atomic(self.path, json.dumps(data, indent=2))
        self._mtime = self.path.stat().st_mtime
        self.on_change()

    # --- pairing -----------------------------------------------------------------------------------------------------

    def pair(self, app_name: str, device: str) -> tuple[Device, str]:
        """A new pairing. Returns it and its code, which is shown once and never kept."""
        app_name, device = app_name.strip(), device.strip()
        aid = app_id(app_name)
        if not aid or not device:
            raise ValueError("An app name and a device name are needed.")
        code = random_code()
        dev = Device(id=uuid.uuid4().hex[:12], app_id=aid, app_name=app_name, device=device,
                     code_hash=hash_code(code), created=time.time())
        self.devices[dev.id] = dev
        self.ensure_app(aid, app_name, save=False)
        self.save()
        return dev, code

    def ensure_app(self, aid: str, name: str | None = None, save: bool = True) -> App:
        a = self.apps.get(aid)
        if a is None:
            a = self.apps[aid] = App(name=name or aid)
            if save:
                self.save()
        return a

    def _expired(self, dev: Device, now: float) -> bool:
        return dev.first_used is None and now - dev.created > UNUSED_CODE_LIFETIME_S

    def prune(self) -> None:
        now = time.time()
        gone = [d.id for d in self.devices.values() if self._expired(d, now)]
        for i in gone:
            del self.devices[i]
        if gone:
            self.save()

    def reload_if_changed(self) -> None:
        """Picks up a pairing made by another ClaudeIn process (the test starter's `pair` command)."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime != getattr(self, "_mtime", None):
            self.load()

    def check(self, code: str) -> Device | None:
        """The device whose code this is (on or off), or None. Marks it used."""
        self.reload_if_changed()
        h = hash_code(code)
        now = time.time()
        found = None
        for dev in self.devices.values():  # compare with every one, in constant time each
            if hmac.compare_digest(dev.code_hash, h) and not self._expired(dev, now):
                found = dev
        if found is not None:
            first = found.first_used is None
            if first:
                found.first_used = now
            # last_used is saved at most once a minute, so a busy phone doesn't rewrite the file all the time
            if first or found.last_used is None or now - found.last_used > 60:
                found.last_used = now
                self.save()
        return found

    def set_enabled(self, device_id: str, on: bool) -> None:
        self.devices[device_id].enabled = on
        self.save()

    def forget(self, device_id: str) -> Device | None:
        dev = self.devices.pop(device_id, None)
        if dev is not None:
            self.save()
        return dev

    def set_models(self, aid: str, models: list[str]) -> None:
        self.ensure_app(aid, save=False).models = [m.strip() for m in models if m.strip()]
        self.save()

    def set_cap(self, aid: str, points: float | None) -> None:
        """A daily cap in plan points for the app, or None for no cap."""
        if points is not None and points < 0:
            raise ValueError("A cap can't be below 0.")
        self.ensure_app(aid, save=False).cap_points = points
        self.save()

    def cap(self, aid: str) -> float | None:
        a = self.apps.get(aid)
        return a.cap_points if a else None

    def model_allowed(self, aid: str, model: str) -> bool:
        a = self.apps.get(aid)
        return a is None or not a.models or model in a.models

    def app_name(self, aid: str) -> str:
        a = self.apps.get(aid)
        return a.name if a else aid


class WrongCodeGuard:
    """Slows down wrong codes per address, and blocks an address for an hour after 20 wrong in a row."""

    BLOCK_AFTER = 20
    BLOCK_S = 3600

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.failures: dict[str, int] = {}
        self.blocked_until: dict[str, float] = {}

    def blocked(self, address: str) -> bool:
        until = self.blocked_until.get(address)
        if until is None:
            return False
        if self.clock() >= until:
            del self.blocked_until[address]
            self.failures.pop(address, None)
            return False
        return True

    def failed(self, address: str) -> float:
        """Counts a wrong code; returns how long to wait before answering (up to 5 s, as the Kotlin host)."""
        n = self.failures.get(address, 0) + 1
        self.failures[address] = n
        if n >= self.BLOCK_AFTER:
            self.blocked_until[address] = self.clock() + self.BLOCK_S
        return min(5.0, 0.25 * n)

    def succeeded(self, address: str) -> None:
        self.failures.pop(address, None)


class LocalToken:
    """local.json: the token programs on this PC send to 127.0.0.1:<local port>. Readable only by the owner."""

    def __init__(self, folder: Path, port: int):
        self.path = folder / LOCAL
        self.port = port
        self.token = ""
        data: dict = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.token = data.get("token", "")
            except ValueError:
                pass
        if not self.token or data.get("port") != port:
            self.write(self.token or None)

    def write(self, token: str | None = None) -> None:
        self.token = token or secrets.token_urlsafe(32)
        write_atomic(self.path, json.dumps({"token": self.token, "port": self.port}, indent=2))
        _owner_only(self.path)

    def renew(self) -> None:
        """'New local token' in the tray: the old one stops working at once."""
        self.write()

    def matches(self, sent: str) -> bool:
        return bool(self.token) and hmac.compare_digest(sent.encode(), self.token.encode())


def _owner_only(path: Path) -> None:
    """Lets only the signed-in Windows account read the file."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
