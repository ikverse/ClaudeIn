"""Updates from the GitHub releases of ikverse/ClaudeIn (plan 4.10).

- Checking asks GitHub's public releases page, with no login. Nothing about the owner or the PC is sent: the only
  identification is "ClaudeIn/<version>". A minute after start, then every 12 hours, and on "Check for updates".
- A newer release is only shown (a tray notice and the General page). Nothing is installed without the owner's click.
- Installing downloads the release's installer and its signature, only from this repository's release downloads, and
  checks the signature against the public key built into ClaudeIn (update_key.py). The signature covers the version and
  the installer's SHA-256, so neither a changed file nor an older signed installer under a newer name gets through. Then
  the installer runs quietly, ClaudeIn quits, and the installer starts the new ClaudeIn.
- Only the installed ClaudeIn.exe updates itself; a copy run from .venv is updated with git.
- Logs say only which version was found or installed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import autostart, config
from .claudecode import parse_version, vstr
from .update_key import PUBLIC_KEY

log = logging.getLogger("claudein")

REPO = "ikverse/ClaudeIn"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
FIRST_CHECK_S = 60
CHECK_EVERY_S = 12 * 3600
INSTALLER_ARGS = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"]


def installer_name(version: str) -> str:
    return f"ClaudeIn-Setup-{version}.exe"


def signed_message(version: str, sha256_hex: str) -> bytes:
    """What a release signature covers: the version and the installer's SHA-256."""
    return f"ClaudeIn {version}\n{sha256_hex.lower()}\n".encode()


def sign(installer: Path, version: str, private_key) -> str:
    """The signature file's text for [installer] (used by packaging/release.py with the owner's private key)."""
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    return base64.b64encode(private_key.sign(signed_message(version, digest))).decode() + "\n"


def verify(public_key_b64: str, version: str, sha256_hex: str, signature_text: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_text.strip()), signed_message(version, sha256_hex))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


@dataclass
class Release:
    version: str
    notes: str
    installer_url: str
    signature_url: str
    size: int
    page_url: str


@dataclass
class UpdateState:
    state: str = "idle"  # idle | checking | up_to_date | available | downloading | verifying | installing | failed
    release: Release | None = None
    done: int = 0
    total: int = 0
    error: str | None = None
    checked_at: float | None = None


def launch_installer(path: Path) -> None:
    """Starts the installer on its own, so it keeps going after ClaudeIn quits."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(path), *INSTALLER_ARGS], creationflags=flags, close_fds=True)


class Updater:
    def __init__(self, folder: Path, settings: config.Settings, on_change: Callable[[], None] = lambda: None,
                 api: str = API, downloads: str = DOWNLOADS, public_key: str = PUBLIC_KEY,
                 launch: Callable[[Path], None] = launch_installer, can_install: bool | None = None,
                 version: str = config.VERSION):
        self.folder, self.settings, self.on_change = folder, settings, on_change
        self.api, self.downloads, self.public_key, self.launch = api, downloads, public_key, launch
        self.can_install = autostart.FROZEN if can_install is None else can_install
        self.version = version
        self.status = UpdateState()

    def changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            log.exception("on_change failed")

    def _headers(self) -> dict:
        return {"User-Agent": f"ClaudeIn/{self.version}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    @property
    def available(self) -> Release | None:
        return self.status.release if self.status.state in ("available", "failed") and self.status.release else None

    # --- checking ------------------------------------------------------------------------------------------------------

    def _release_from(self, data: dict) -> Release | None:
        if data.get("draft") or data.get("prerelease"):
            return None
        v = parse_version(str(data.get("tag_name") or ""))
        if v is None:
            return None
        version = vstr(v)
        assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
        exe, sig = assets.get(installer_name(version)), assets.get(installer_name(version) + ".sig")
        if not exe or not sig:
            log.info("Release %s has no signed installer; ignored", version)
            return None
        notes = re.sub(r"\s+\n", "\n", str(data.get("body") or "")).strip()
        return Release(version, notes[:1500], str(exe.get("browser_download_url") or ""),
                       str(sig.get("browser_download_url") or ""), int(exe.get("size") or 0),
                       str(data.get("html_url") or ""))

    async def check(self) -> Release | None:
        """Looks for a newer release. Never raises; a problem is kept in status.error and shown, never announced."""
        import aiohttp

        if self.status.state in ("downloading", "verifying", "installing"):
            return self.available
        self.status = UpdateState("checking", release=None)
        self.changed()
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(self.api, headers=self._headers()) as r:
                    if r.status == 404:  # no releases yet
                        data = None
                    elif r.status in (403, 429):
                        raise _Quiet("GitHub is limiting checks right now. ClaudeIn will try again later.")
                    else:
                        r.raise_for_status()
                        data = await r.json(content_type=None)
        except _Quiet as e:
            return self._check_failed(str(e))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return self._check_failed("Couldn't reach GitHub. ClaudeIn will try again later.")
        release = self._release_from(data) if isinstance(data, dict) else None
        newer = release is not None and (parse_version(release.version) or (0, 0, 0)) > (
            parse_version(self.version) or (0, 0, 0))
        self.status = UpdateState("available" if newer else "up_to_date", release if newer else None,
                                  checked_at=time.time())
        if newer:
            log.info("Update available: ClaudeIn %s", release.version)
        self.changed()
        return release if newer else None

    def _check_failed(self, why: str) -> None:
        self.status = UpdateState("failed", error=why, checked_at=time.time())
        log.info("Update check failed")
        self.changed()
        return None

    async def watch(self, first_s: float | None = None, every_s: float | None = None) -> None:
        """A minute after start, then every 12 hours, while "Check for updates automatically" is on."""
        await asyncio.sleep(FIRST_CHECK_S if first_s is None else first_s)
        while True:
            if self.settings.check_updates:
                await self.check()
            await asyncio.sleep(CHECK_EVERY_S if every_s is None else every_s)

    # --- installing ----------------------------------------------------------------------------------------------------

    def _install_failed(self, why: str, release: Release) -> bool:
        self.status = UpdateState("failed", release, error=why, checked_at=self.status.checked_at)
        log.warning("Update not installed")
        self.changed()
        return False

    async def install(self) -> bool:
        """Downloads and checks the available release, then starts its installer. True means ClaudeIn should quit now."""
        import aiohttp

        release = self.available
        if release is None:
            return False
        if not self.can_install:
            return self._install_failed("This ClaudeIn runs from .venv, so it is updated with git, not here.", release)
        for url in (release.installer_url, release.signature_url):
            if not url.startswith(self.downloads):
                return self._install_failed("The release's files aren't in ClaudeIn's own GitHub releases.", release)
        folder = self.folder / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / installer_name(release.version)
        part = final.with_name(final.name + ".part")
        self.status = UpdateState("downloading", release, total=release.size, checked_at=self.status.checked_at)
        self.changed()
        scheme = self.downloads.split(":", 1)[0]
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as s:
                async with s.get(release.signature_url) as r:
                    r.raise_for_status()
                    if r.url.scheme != scheme:
                        return self._install_failed("The download wasn't secure, so it was stopped.", release)
                    signature = (await r.read()).decode("ascii", errors="replace")[:500]
                h = hashlib.sha256()
                async with s.get(release.installer_url) as r:
                    r.raise_for_status()
                    if r.url.scheme != scheme:
                        return self._install_failed("The download wasn't secure, so it was stopped.", release)
                    with part.open("wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 20):
                            f.write(chunk)
                            h.update(chunk)
                            self.status.done += len(chunk)
                            self.changed()
            self.status.state = "verifying"
            self.changed()
            if not verify(self.public_key, release.version, h.hexdigest(), signature):
                return self._install_failed("The update isn't signed with ClaudeIn's key, so it was thrown away.",
                                            release)
            os.replace(part, final)
        except asyncio.CancelledError:
            self.status = UpdateState("available", release)
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            return self._install_failed("Couldn't download the update: " + (getattr(e, "strerror", None)
                                                                             or type(e).__name__), release)
        finally:
            part.unlink(missing_ok=True)
        for old in folder.iterdir():  # keep only this installer
            if old != final:
                old.unlink(missing_ok=True)
        self.status = UpdateState("installing", release)
        self.changed()
        try:
            self.launch(final)
        except OSError:
            return self._install_failed("The installer didn't start.", release)
        log.info("Installing ClaudeIn %s", release.version)
        return True


class _Quiet(Exception):
    pass
