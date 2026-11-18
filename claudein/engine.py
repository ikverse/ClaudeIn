"""Runs ClaudeIn's engine without the tray, for testing before the tray exists (Part B).

    .venv\\Scripts\\python.exe -m claudein.engine                      start it (Ctrl+C stops it)
    .venv\\Scripts\\python.exe -m claudein.engine pair "Signal Lab" Phone
                                                                     pair a device and show its code once
"""

from __future__ import annotations

import asyncio
import sys

from . import config, tls
from .pairing import shown
from .service import Service, lan_addresses


def _pair(app: str, device: str) -> None:
    svc_folder = config.home()
    tls.ensure_certificate(svc_folder)
    from .pairing import Pairings

    settings = config.Settings.load(svc_folder)
    dev, code = Pairings(svc_folder).pair(app, device)
    print(f"Paired {dev.label}.")
    print("Address to type on the phone:", ", ".join(f"{a}:{settings.phone_port}" for a in lan_addresses()) or "?")
    print("Code (shown only now, use it within 30 minutes):", shown(code))
    print("Certificate ends with:", tls.short_fingerprint(svc_folder))


async def _run() -> None:
    svc = Service()
    await svc.start()
    print(f"ClaudeIn engine running. Phones: port {svc.phone_port}. Programs: 127.0.0.1:{svc.local_port}.")
    print("Addresses:", ", ".join(lan_addresses()) or "?", "· certificate ends with", tls.short_fingerprint(svc.folder))
    print("Claude Code:", svc.claude_version, f"({svc.claude.state})", "· Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await svc.stop()


def main(argv: list[str]) -> None:
    if len(argv) >= 1 and argv[0] == "pair":
        if len(argv) != 3:
            print('Usage: python -m claudein.engine pair "<app name>" "<device name>"')
            sys.exit(2)
        _pair(argv[1], argv[2])
        return
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
