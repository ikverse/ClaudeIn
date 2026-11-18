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

    async def stop_run(self, request: web.Request) -> web.Response:
        c = await self._caller(request)
        if isinstance(c, web.Response):
            return c
        run = self.service.runs.get(request.match_info["id"])
        if run is None or not c.owns(run):
            return _json(404, {"ok": False})
        run.close()
        return _json(200, {"ok": True})

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

        run = Run(spec, aid, app_name, c.device.id if c.device else None, label, c.is_phone)
        svc.runs[run.id] = run

        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        try:
            await resp.prepare(request)
            await self._send(resp, {"type": "run", "id": run.id})
            if not svc.pairings.model_allowed(aid, spec.model):
                run.finish(runs.done(f"failed: {app_name} isn't allowed to use {spec.model}.",
                                     error_kind="not_allowed"))
                svc.run_finished(run)
            else:
                svc.scheduler.submit(run)
            while True:
                try:
                    e = await asyncio.wait_for(run.events.get(), KEEPALIVE_S)
                except asyncio.TimeoutError:
                    await resp.write(b": still working\n\n")
                    continue
                await self._send(resp, e)
                if e.get("type") == "done":
                    break
        except (ConnectionError, RuntimeError):
            log.info("A caller went away; its run is stopped.")
        finally:
            if run.result is None:  # the caller went away (or was cancelled) before the end: stop the run
                run.close()
            svc.runs.pop(run.id, None)
        return resp

    @staticmethod
    async def _send(resp: web.StreamResponse, event: dict) -> None:
        line = "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        await resp.write(line.encode("utf-8"))
