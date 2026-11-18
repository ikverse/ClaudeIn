"""ClaudeIn's engine: settings, certificate, pairings, queue, runner, usage and the two servers, together.

The tray (Part B) drives this; for now `python -m claudein.engine` runs it on its own.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Callable

from aiohttp import web

from . import config, tls
from .claudecode import ClaudeCode
from .pairing import Device, LocalToken, Pairings, WrongCodeGuard
from .runner import QueryFn, Runner
from .runs import Run
from .scheduler import Scheduler
from .server import Server
from .usage import Usage, log_run, setup_logs

CLAUDE_RECHECK_S = 15 * 60  # how often Claude Code and the account are looked at again (free, no question)


class Service:
    def __init__(self, folder: Path | None = None, query_fn: QueryFn | None = None,
                 on_change: Callable[[], None] | None = None, claude: ClaudeCode | None = None):
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
        self.claude = claude or ClaudeCode(self.folder)
        self.claude.attach(self.settings, self.changed)
        self.runner = Runner(query_fn, claude=self.claude)
        self._claude_watch: asyncio.Task | None = None
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

    @property
    def claude_version(self) -> str | None:
        return self.claude.version

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
        await self.claude.refresh()  # free: no question is asked; first, so a busy port can't skip it
        if self._claude_watch is None:
            self._claude_watch = asyncio.create_task(self._watch_claude())
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
        self.log.info("ClaudeIn %s started: phones on %s, programs on 127.0.0.1:%s, Claude Code %s (%s)",
                      config.VERSION, self.phone_port, self.local_port, self.claude_version, self.claude.state)

    async def _watch_claude(self) -> None:
        """Looks at Claude Code and the account again now and then, so an update or an expired login shows up."""
        while True:
            await asyncio.sleep(CLAUDE_RECHECK_S)
            await self.claude.refresh()

    async def stop(self) -> None:
        if self._claude_watch is not None:
            self._claude_watch.cancel()
        self.claude.cancel_sign_in()
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
            "claude": self.claude_summary(),
        }

    def signed_in(self) -> bool | None:
        """From Claude Code's own `auth status` (free), and corrected by runs: True after one that worked, False when
        Claude Code said it is signed out, None when it can't be told."""
        return self.claude.signed_in

    def claude_summary(self) -> dict:
        c = self.claude
        return {"state": c.state, "version": c.version, "path": str(c.path) if c.path else None,
                "email": c.account.email if c.account else None, "plan": c.account.plan if c.account else None,
                "own_login": self.settings.own_login, "copy": self.settings.claude_copy, "suspect": c.suspect,
                "needs_setup": c.needs_setup(), "login": c.login.state, "install": c.install.state}

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
        return list(self.start_problems) + self.claude.problems()

    # --- phone access (phase 4) ----------------------------------------------------------------------------------------

    async def phone_access(self) -> "PhoneAccess":
        """The "Phone access" check: Tailscale, the firewall rule and port 8790. Reads only; changes nothing."""
        from . import firewall, tailscale

        port = self.phone_port or self.settings.phone_port
        ts, rule = await asyncio.gather(asyncio.to_thread(tailscale.status), asyncio.to_thread(firewall.check, port))
        return PhoneAccess(ts, rule, port, listening=self.phone_port is not None)

    async def set_up_phone_access(self) -> str:
        """Adds the firewall rule. Windows asks the owner first. Returns "done", "cancelled" or "failed"."""
        from . import firewall

        result = await asyncio.to_thread(firewall.set_up, self.phone_port or self.settings.phone_port)
        self.log.info("Phone access set-up: %s", result)
        return result

    # --- Claude Code and the account (the Claude page and the panel call these) ----------------------------------------

    async def recheck_claude(self) -> None:
        await self.claude.refresh()

    async def sign_in(self) -> bool:
        return await self.claude.sign_in()

    def cancel_sign_in(self) -> None:
        self.claude.cancel_sign_in()

    async def sign_out(self) -> None:
        await self.claude.sign_out()

    async def set_own_login(self, on: bool) -> None:
        """Own login: ClaudeIn keeps its Claude login apart from the terminal's. Off: it uses this PC's Claude Code login."""
        self.settings.own_login = on
        self.settings.save(self.folder)
        await self.claude.refresh()

    async def install_claude_copy(self) -> bool:
        return await self.claude.install_own_copy()

    async def use_installed_claude(self) -> None:
        await self.claude.use_installed_copy()

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


class PhoneAccess:
    """What the "Phone access" check found, as plain lines."""

    def __init__(self, ts, rule, port: int, listening: bool):
        self.ts, self.rule, self.port, self.listening = ts, rule, port, listening

    @property
    def lines(self) -> list[tuple[bool, str]]:
        ts, rule = self.ts, self.rule
        if ts.on:
            t = (True, f"Tailscale is on. This PC is {ts.name or 'on Tailscale'} at {ts.addresses[0]}.")
        elif ts.state == "missing":
            t = (False, "Tailscale isn't installed. Phones can reach ClaudeIn only at home.")
        else:
            t = (False, "Tailscale is off. Phones can reach ClaudeIn only at home. Turn Tailscale on.")
        if rule.state == "ok":
            f = (True, f"The firewall rule is there (port {self.port}, Tailscale and home network only).")
        elif rule.state == "off":
            f = (False, "The firewall rule is turned off.")
        elif rule.state == "different":
            f = (False, f"The firewall rule needs setting up again: {rule.detail}.")
        else:
            f = (False, "The firewall rule isn't set up yet." + (f" ({rule.detail})" if rule.detail else ""))
        p = ((True, f"ClaudeIn is listening for phones on port {self.port}.") if self.listening else
             (False, f"ClaudeIn isn't listening on port {self.port}. Another program may be using it."))
        return [t, f, p]

    @property
    def needs_rule(self) -> bool:
        return self.rule.state != "ok"

    @property
    def all_ok(self) -> bool:
        return all(ok for ok, _ in self.lines)


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
