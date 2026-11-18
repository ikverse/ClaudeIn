"""The two ways in (plan 4.1 and 4.2).

- Phones: HTTPS on port 8790, on every network address, but only from this PC, home networks and Tailscale. Each request
  but /hello carries the device's own pairing code.
- Programs on this PC: plain HTTP on 127.0.0.1:8791, with the token from local.json.

Routes, events and endings are the Kotlin analyst host's, so today's phones work unchanged apart from the address.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from . import netguard, runs
from .config import HELLO_APP, PROTOCOL, VERSION
from .pairing import Device, app_id
from .runs import BadRequest, Run, RunSpec

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger("claudein")

KEEPALIVE_S = 5.0
LOCAL = web.AppKey("local", bool)  # True on the program port, False on the phone port


def _json(status: int, body: dict) -> web.Response:
    return web.Response(status=status, text=json.dumps(body, ensure_ascii=False), content_type="application/json")


def _bearer(request: web.Request) -> str:
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h.startswith("Bearer ") else ""


class Caller:
    """Who is asking: a paired device, or a program on this PC."""

    def __init__(self, device: Device | None):
        self.device = device

    @property
    def is_phone(self) -> bool:
        return self.device is not None

    def owns(self, run: Run) -> bool:
        return run.device_id == (self.device.id if self.device else None) and run.is_phone == self.is_phone


class Server:
    def __init__(self, service: "Service"):
        self.service = service

    # --- the two apps ------------------------------------------------------------------------------------------------

    def phone_app(self) -> web.Application:
        return self._app(local=False)

    def local_app(self) -> web.Application:
        return self._app(local=True)

    def _app(self, local: bool) -> web.Application:
        @web.middleware
        async def guard(request: web.Request, handler):
            remote = request.remote
            ok = netguard.is_loopback(remote) if local else netguard.allowed(remote)
            if not ok:
                if request.transport is not None:
                    request.transport.close()
                return _json(403, {"error": "Not allowed."})
            return await handler(request)

        app = web.Application(middlewares=[guard], client_max_size=32 * 1024 * 1024)
        app[LOCAL] = local
        app.router.add_get("/hello", self.hello)
        app.router.add_get("/check", self.check)
        app.router.add_post("/runs", self.start_run)
        app.router.add_get("/runs/{id}", self.get_run)
        app.router.add_get("/runs/{id}/events", self.run_events)
        app.router.add_post("/runs/{id}/stop", self.stop_run)
        app.router.add_post("/runs/{id}/tools/{call}", self.tool_answer)
        return app

    # --- access ------------------------------------------------------------------------------------------------------

    async def _caller(self, request: web.Request) -> Caller | web.Response:
        svc = self.service
        address = request.remote or "?"
        key = "local" if request.app[LOCAL] else address
        if svc.guard.blocked(key):
            return _json(429, {"error": "Too many wrong codes. Try again in an hour."})
        sent = _bearer(request)
        if request.app[LOCAL]:
            if svc.token.matches(sent):
                svc.guard.succeeded(key)
                return Caller(None)
            await asyncio.sleep(svc.guard.failed(key))
            return _json(401, {"error": "Wrong local token."})
        device = svc.pairings.check(sent) if sent else None
        if device is None:
            await asyncio.sleep(svc.guard.failed(key))
            return _json(401, {"error": "Wrong pairing code."})
        svc.guard.succeeded(key)
        if not device.enabled:
            return _json(403, {"error": "This device is switched off in ClaudeIn on the PC."})
        svc.changed()
        return Caller(device)

    # --- routes ------------------------------------------------------------------------------------------------------

    async def hello(self, request: web.Request) -> web.Response:
        body = {"app": HELLO_APP, "protocol": PROTOCOL}
        if self.service.claude_version:
            body["claude"] = self.service.claude_version
        body["service"] = "ClaudeIn"
        body["version"] = VERSION
        body["features"] = self.service.claude.features()
        return _json(200, body)

    async def check(self, request: web.Request) -> web.Response:
        c = await self._caller(request)
        if isinstance(c, web.Response):
            return c
        s = self.service.scheduler
        return _json(200, {"ok": True, "busy": self.service.settings.paused or s.full, "queue": len(s.waiting),
                           "features": self.service.claude.features()})

    def _own_run(self, c: Caller, request: web.Request) -> Run | None:
        """The run named in the address, if this caller started it (someone else's looks the same as none)."""
        self.service.sweep()
        run = self.service.runs.get(request.match_info["id"])
        return run if run is not None and c.owns(run) else None

    async def stop_run(self, request: web.Request) -> web.Response:
        c = await self._caller(request)
        if isinstance(c, web.Response):
            return c
        run = self._own_run(c, request)
        if run is None:
            return _json(404, {"ok": False})
        if run.spec.detach and run.result is not None:
            self.service.runs.pop(run.id, None)  # a kept answer the app has already read: free its place
        else:
            run.close()
        return _json(200, {"ok": True})

    async def get_run(self, request: web.Request) -> web.Response:
        c = await self._caller(request)
        if isinstance(c, web.Response):
            return c
        run = self._own_run(c, request)
        if run is None:
            return _json(404, {"error": "No such run (it ended over an hour ago, or ClaudeIn restarted)."})
        return _json(200, {"id": run.id, "state": run.state, "done": run.result, "queued_ms": run.queued_ms,
                           "last_seq": run.last_seq})

    async def run_events(self, request: web.Request) -> web.StreamResponse:
        c = await self._caller(request)
        if isinstance(c, web.Response):
            return c
        raw = request.query.get("after", "0")
        if not raw.isdigit():
            return _json(400, {"error": '"after" must be a whole number of 0 or more.'})
        run = self._own_run(c, request)
        if run is None:
            return _json(404, {"error": "No such run (it ended over an hour ago, or ClaudeIn restarted)."})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        try:
            await resp.prepare(request)
            await self._stream(resp, run, int(raw))
        except (ConnectionError, RuntimeError):
            pass  # a reader going away never stops the run
        return resp

    async def tool_answer(self, request: web.Request) -> web.Response:
        c = await self._caller(request)
        if isinstance(c, web.Response):
            return c
        run = self.service.runs.get(request.match_info["id"])
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return _json(400, {"error": "The answer is not JSON."})
        ok = run is not None and c.owns(run) and run.answer(request.match_info["call"], body)
        return _json(200 if ok else 404, {"ok": ok})

    async def start_run(self, request: web.Request) -> web.StreamResponse:
        c = await self._caller(request)
        if isinstance(c, web.Response):
            return c
        svc = self.service
        try:
            spec = RunSpec.parse(await request.json())
        except (ValueError, UnicodeDecodeError) as e:
            msg = str(e) if isinstance(e, BadRequest) else "The request is not JSON."
            return _json(400, {"error": msg})

        if c.device is not None:
            dev = c.device
            if spec.app is not None and app_id(spec.app) != dev.app_id:
                return _json(403, {"error": f"This pairing is for {dev.app_name}."})
            aid, app_name, label = dev.app_id, dev.app_name, dev.label
        else:
            if spec.app is None:
                return _json(400, {"error": 'Missing "app".'})
            aid = app_id(spec.app)
            if not aid:
                return _json(400, {"error": '"app" must be a name.'})
            app_name = svc.pairings.ensure_app(aid, spec.app).name
            label = f"{app_name} (this PC)"

        if spec.detach and svc.detached_count(aid) >= runs.MAX_DETACHED_PER_APP:
            return _json(429, {"error": f"{app_name} already has {runs.MAX_DETACHED_PER_APP} long runs kept on the "
                                        "PC. Wait for one to finish, or stop one you have read."})

        run = Run(spec, aid, app_name, c.device.id if c.device else None, label, c.is_phone)
        svc.runs[run.id] = run

        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        try:
            await resp.prepare(request)
            if not svc.pairings.model_allowed(aid, spec.model):
                run.finish(runs.done(f"failed: {app_name} isn't allowed to use {spec.model}.",
                                     error_kind="not_allowed"))
                svc.run_finished(run)
            else:
                svc.scheduler.submit(run)
            await self._stream(resp, run, 0)
        except (ConnectionError, RuntimeError):
            if spec.detach:
                log.info("A caller went away; its run carries on (detached).")
            else:
                log.info("A caller went away; its run is stopped.")
        finally:
            if not spec.detach:
                if run.result is None:  # the caller went away (or was cancelled) before the end: stop the run
                    run.close()
                svc.runs.pop(run.id, None)
            # a detached run stays in svc.runs until an hour after it ends (Service.sweep)
        return resp

    async def _stream(self, resp: web.StreamResponse, run: Run, after: int) -> None:
        """Sends [run]'s events after [after], then new ones as they come, until its done event."""
        sent = after
        while True:
            while sent < len(run.log):
                e = run.log[sent]
                sent += 1
                await self._send(resp, e)
                if e.get("type") == "done":
                    return
            if run.result is not None and sent >= len(run.log):
                return  # asked for events after the done event: nothing more will come
            if not await run.wait_past(sent, KEEPALIVE_S):
                await resp.write(b": still working\n\n")

    @staticmethod
    async def _send(resp: web.StreamResponse, event: dict) -> None:
        line = "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        await resp.write(line.encode("utf-8"))
