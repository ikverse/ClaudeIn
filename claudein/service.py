"""ClaudeIn's engine: settings, certificate, pairings, queue, runner, usage and the two servers, together.

The tray (Part B) drives this; for now `python -m claudein.engine` runs it on its own.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import Path
from typing import Callable

from aiohttp import web

from . import config, tls
from .pairing import Device, LocalToken, Pairings, WrongCodeGuard
from .runner import QueryFn, Runner, bundled_claude_version
from .runs import Run
from .scheduler import Scheduler
from .server import Server
from .usage import Usage, log_run, setup_logs


class Service:
    def __init__(self, folder: Path | None = None, query_fn: QueryFn | None = None,
                 on_change: Callable[[], None] | None = None):
        self.folder = folder or config.home()
        self.settings = config.Settings.load(self.folder)
        self.log = setup_logs(self.folder)
        tls.ensure_certificate(self.folder)
        self.on_change = on_change or (lambda: None)
        self.pairings = Pairings(self.folder, on_change=self.changed)
        self.pairings.prune()
        self.token = LocalToken(self.folder, self.settings.local_port)
        self.usage = Usage(self.folder / "usage.db")
        self.guard = WrongCodeGuard()
        self.runner = Runner(query_fn)
        self.claude_version = bundled_claude_version()
        self.runs: dict[str, Run] = {}
        self.scheduler = Scheduler(self.settings, self.runner.execute, on_finished=self.run_finished,
                                   on_change=self.changed, over_cap=self.over_cap)
        self.server = Server(self)
        self._runners: list[web.AppRunner] = []
        self.phone_port: int | None = None
        self.local_port: int | None = None
        self.start_problems: list[str] = []  # e.g. a port in use, found when starting

    async def start_safely(self) -> None:
        """start(), but a port in use becomes a problem the tray shows instead of a crash."""
        try:
            await self.start()
        except OSError as e:
            self.log.error("Could not start listening: %s", e.strerror or type(e).__name__)
            self.start_problems.append(f"A port ClaudeIn needs ({self.settings.phone_port} or "
                                       f"{self.settings.local_port}) is used by another program.")
            self.changed()

    def changed(self) -> None:
        try:
            self.on_change()
        except Exception:  # a window's problem must never stop a run
            logging.getLogger("claudein").exception("on_change failed")

    def over_cap(self, run: Run) -> bool:
        """Phase 2: has the run's app used up today's cap? 'Check Claude Code' is never capped."""
        if run.app_id == "claudein":
            return False
        cap = self.pairings.cap(run.app_id)
        return cap is not None and self.usage.points_today(run.app_id) >= cap

    def run_finished(self, run: Run) -> None:
        self.usage.record(run)
        log_run(self.log, run)
        self.changed()

    # --- starting and stopping ----------------------------------------------------------------------------------------

    async def start(self, use_tls: bool = True, phone_port: int | None = None, local_port: int | None = None) -> None:
        """Starts listening. Port 0 picks a free port (tests). Raises OSError if a port is in use."""
        pport = self.settings.phone_port if phone_port is None else phone_port
        lport = self.settings.local_port if local_port is None else local_port
        ctx = tls.server_context(self.folder) if use_tls else None

        phone = web.AppRunner(self.server.phone_app(), access_log=None, handle_signals=False)
        await phone.setup()
        self._runners.append(phone)
        site = web.TCPSite(phone, "0.0.0.0", pport, ssl_context=ctx)
        await site.start()
        self.phone_port = site._server.sockets[0].getsockname()[1]
        try:  # IPv6 too, for names like my-pc.local; fine if this PC has no IPv6
            await web.TCPSite(phone, "::", self.phone_port, ssl_context=ctx).start()
        except OSError:
            pass

        local = web.AppRunner(self.server.local_app(), access_log=None, handle_signals=False)
        await local.setup()
        self._runners.append(local)
        lsite = web.TCPSite(local, "127.0.0.1", lport)
        await lsite.start()
        self.local_port = lsite._server.sockets[0].getsockname()[1]
        if self.local_port != self.token.port:
            self.token.port = self.local_port
            self.token.write(self.token.token)
        self.log.info("ClaudeIn %s started: phones on %s, programs on 127.0.0.1:%s, Claude Code %s",
                      config.VERSION, self.phone_port, self.local_port, self.claude_version)

    async def stop(self) -> None:
        await self.scheduler.shutdown()
        for r in self._runners:
            await r.cleanup()
        self._runners.clear()
        self.usage.close()
        self.log.info("ClaudeIn stopped.")

    # --- owner's actions (the tray calls these) ------------------------------------------------------------------------

    def pair(self, app_name: str, device: str) -> tuple[Device, str]:
        return self.pairings.pair(app_name, device)

    def forget(self, device_id: str) -> None:
        """Deletes the device's code for good; its runs stop at once."""
        self.pairings.forget(device_id)
        self.scheduler.stop_device(device_id)

    def status(self) -> dict:
        s = self.scheduler
        return {
            "paused": self.settings.paused,
            "working": len(s.running),
            "waiting": len(s.waiting),
            "workers": self.settings.workers,
            "claude_version": self.claude_version,
            "signed_in": self.signed_in(),
            "plan": self.plan_use(),
            "problems": self.problems(),
        }

    def signed_in(self) -> bool | None:
        """True after a run that worked (this session or before), False when Claude Code said it is signed out,
        None when nothing has run yet. ClaudeIn never asks a question on its own to find out."""
        if self.runner.signed_in is not None:
            return self.runner.signed_in
        row = self.usage.db.execute(
            "SELECT ending, error_kind FROM runs WHERE ending != 'stopped' AND (error_kind IS NULL OR error_kind"
            " NOT IN ('paused','cap','not_allowed')) ORDER BY asked_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return row[1] != "unavailable" if row[0] == "failed" else True

    def plan_use(self) -> dict | None:
        """The plan's 5-hour and 7-day use (0-1) Claude Code last reported, for the whole plan."""
        raw = self.runner.last_limits
        if raw:
            w = raw.get("unifiedWindows") or {}
            five = (w.get("five_hour") or {}).get("utilization")
            week = (w.get("seven_day") or {}).get("utilization")
            if isinstance(five, (int, float)):
                return {"five_hour": five, "seven_day": week, "resets_at": raw.get("resetsAt")}
        last = self.usage.latest_limits()
        return {"five_hour": last["five_hour"], "seven_day": last["seven_day"], "resets_at": None} if last else None

    def problems(self) -> list[str]:
        out = list(self.start_problems)
        if not self.claude_version:
            out.append("Claude Code is missing. Run Setup-ClaudeIn.bat again.")
        if self.signed_in() is False:
            out.append("Claude Code is signed out. Open a terminal, run claude, and sign in.")
        return out

    async def check_claude(self) -> dict:
        """'Check Claude Code': one tiny Haiku question, through the queue like any other. Only on the owner's click."""
        from .runs import RunSpec

        spec = RunSpec(model="haiku", system="Reply with one word.", prompt="Reply with the word OK.",
                       builtin_tools=[], max_turns=1, max_budget_usd=0.2)
        self.pairings.ensure_app("claudein", "ClaudeIn")
        run = Run(spec, "claudein", "ClaudeIn", None, "ClaudeIn (this PC)", is_phone=False)
        self.scheduler.submit(run)
        await run.finished.wait()
        return run.result or {}

    def restore(self, path: Path) -> None:
        """Puts back a pairings backup. ClaudeIn must restart afterwards to use the restored certificate."""
        from . import backup

        backup.restore(self.folder, path)
        self.pairings.load()


def lan_addresses() -> list[str]:
    """This PC's addresses on home networks and Tailscale, for typing into a phone."""
    found = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        a = info[4][0]
        ip = ipaddress.ip_address(a)
        if (ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")) and not ip.is_loopback and a not in found:
            found.append(a)
    return found
