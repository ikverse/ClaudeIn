"""Phones and programs talking to ClaudeIn, with a fake Claude Code (plan section 7)."""

from __future__ import annotations

import asyncio
import ssl
import time

import aiohttp

from claudein import tls
from claudein.config import HELLO_APP

from .conftest import Phone, rate_limit, recorded, result, spec, stream_text, until


async def test_hello_stays_compatible(svc, phone):
    status, body = await phone.get("/hello", auth=None)
    assert status == 200
    assert body["app"] == HELLO_APP == "Signal Lab analyst host"
    assert body["protocol"] == 2  # no phone app reads this number
    assert body["service"] == "ClaudeIn"


async def test_recorded_reply_matches_kotlin_host(svc, phone, fake):
    """Same recorded stream as the Kotlin host's test; same events and done fields."""
    msgs = recorded("claude-plain-reply.jsonl")

    async def script(prompt, options, tools):
        for x in msgs:
            yield x

    fake.script = script
    events = await phone.ask(spec())
    assert [e["type"] for e in events] == ["run", "text", "done"]
    assert events[1]["delta"] == "ready"
    done = events[-1]
    assert done["ending"] == "done"
    assert done["answer"] == "ready"
    assert done["usage"]["output"] == 4
    assert done["usage"]["cache_write"] == 5143
    assert abs(done["limits"]["five_hour"] - 0.63) < 1e-9
    assert "error_kind" not in done
    assert done["queued_ms"] >= 0


async def test_tool_call_round_trip(svc, phone, fake):
    async def script(prompt, options, tools):
        answer = await tools["echo"]({"x": "hi"})
        said = "The tool said: " + answer["content"][0]["text"]
        for x in stream_text(said):
            yield x
        yield result(said)

    fake.script = script
    tool = {"name": "echo", "description": "Echoes", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}
    events = await phone.ask(spec(tools=[tool]), on_tool=lambda e: {"text": "echo: " + e["input"]["x"], "is_error": False})
    t = next(e for e in events if e["type"] == "tool")
    assert t["name"] == "echo" and t["input"] == {"x": "hi"}
    assert events[-1]["answer"] == "The tool said: echo: hi"
    # the options Claude Code got: locked down as the Kotlin host
    _, opts = fake.calls[-1]
    assert opts.tools == ["WebSearch", "WebFetch"]
    assert opts.allowed_tools == ["WebSearch", "WebFetch", "mcp__signallab__echo"]
    assert list(opts.mcp_servers) == ["signallab"]
    assert opts.permission_mode == "dontAsk" and opts.strict_mcp_config and opts.setting_sources == []
    assert opts.env["ANTHROPIC_API_KEY"] == ""
    assert "restricted" in opts.extra_args and "no-session-persistence" in opts.extra_args


async def test_image_tool_answer(svc, phone, fake):
    seen = {}

    async def script(prompt, options, tools):
        seen["a"] = await tools["chart"]({})
        yield result("ok")

    fake.script = script
    tool = {"name": "chart", "description": "", "input_schema": {"type": "object"}}
    await phone.ask(spec(tools=[tool]), on_tool=lambda e: {
        "content": [{"type": "text", "text": "chart"}, {"type": "image", "data": "AAAA", "mime_type": "image/png"}]})
    assert seen["a"]["content"][1] == {"type": "image", "data": "AAAA", "mimeType": "image/png"}


async def test_tool_time_out(svc, phone, fake):
    seen = {}

    async def script(prompt, options, tools):
        seen["a"] = await tools["slow"]({})
        yield result("ok")

    fake.script = script
    tool = {"name": "slow", "description": "", "input_schema": {"type": "object"}}
    t0 = time.monotonic()
    events = await phone.ask(spec(tools=[tool], tool_timeout_s=5), on_tool=lambda e: None)
    assert events[-1]["ending"] == "done"
    assert seen["a"]["is_error"] and "did not answer within 5 seconds" in seen["a"]["content"][0]["text"]
    assert time.monotonic() - t0 >= 4.9


async def test_wrong_code_refused(svc, phone):
    status, body = await phone.ask(spec(), auth="WRONG00000")
    assert status == 401 and body["error"] == "Wrong pairing code."
    status, _ = await phone.ask(spec(), auth=None)
    assert status == 401


async def test_switched_off_device_refused(svc, phone):
    dev = next(iter(svc.pairings.devices.values()))
    svc.pairings.set_enabled(dev.id, False)
    status, body = await phone.ask(spec())
    assert status == 403 and "switched off" in body["error"]
    svc.pairings.set_enabled(dev.id, True)
    assert (await phone.ask(spec()))[-1]["ending"] == "done"


async def test_forget_stops_running_run(svc, phone, fake):
    fake.gate = asyncio.Event()
    dev = next(iter(svc.pairings.devices.values()))
    task = asyncio.create_task(phone.ask(spec()))
    await until(lambda: len(svc.scheduler.running) == 1)
    svc.forget(dev.id)
    events = await asyncio.wait_for(task, 5)
    assert events[-1]["ending"] == "stopped"
    await until(lambda: not svc.scheduler.running)
    status, _ = await phone.ask(spec())
    assert status == 401


async def test_app_comes_from_pairing(svc, phone):
    await phone.ask(spec())
    assert svc.usage.recent("signal-lab")[0]["device"] == "Signal Lab — Phone"
    status, body = await phone.ask(spec(app="village"))
    assert status == 403 and "Signal Lab" in body["error"]
    assert (await phone.ask(spec(app="Signal Lab")))[-1]["ending"] == "done"


async def test_two_at_once_third_waits(svc, phone, fake):
    fake.gate = asyncio.Event()
    waits: list[str] = []

    def note(e):
        if e["type"] == "working":
            waits.append(e["what"])

    tasks = [asyncio.create_task(phone.ask(spec(prompt=f"q{i}"))) for i in range(2)]
    await until(lambda: len(svc.scheduler.running) == 2)
    third = asyncio.create_task(phone.ask(spec(prompt="q3"), on_event=note))
    await until(lambda: len(svc.scheduler.waiting) == 1)
    await until(lambda: bool(waits))
    assert waits[0] == "Waiting for the PC (0 ahead)…"
    status, check = await phone.get("/check")
    assert check == {"ok": True, "busy": False, "queue": 1, "features": ["images", "output_schema"]}
    fake.gate.set()
    results = await asyncio.gather(*tasks, third)
    assert all(r[-1]["ending"] == "done" for r in results)
    assert results[2][-1]["queued_ms"] > 0


async def test_phone_goes_before_waiting_program(svc, phone, program, fake):
    svc.scheduler.set_workers(1)
    fake.gate = asyncio.Event()
    first = asyncio.create_task(program.ask(spec(app="village", prompt="p1")))
    await until(lambda: len(svc.scheduler.running) == 1)
    prog = asyncio.create_task(program.ask(spec(app="village", prompt="p2")))
    await until(lambda: len(svc.scheduler.waiting) == 1)
    ph = asyncio.create_task(phone.ask(spec(prompt="phone")))
    await until(lambda: len(svc.scheduler.waiting) == 2)
    fake.gate.set()
    await asyncio.gather(first, prog, ph)
    assert fake.started == ["p1", "phone", "p2"]


async def test_queue_full(svc, phone, fake):
    svc.settings.max_waiting = 0
    svc.scheduler.set_workers(1)
    fake.gate = asyncio.Event()
    first = asyncio.create_task(phone.ask(spec()))
    await until(lambda: len(svc.scheduler.running) == 1)
    events = await phone.ask(spec())
    assert events[-1]["ending"] == "failed: The PC has too many questions waiting."
    assert events[-1]["error_kind"] == "unavailable"
    fake.gate.set()
    await first


async def test_waiting_too_long(svc, phone, fake):
    svc.scheduler.set_workers(1)
    svc.settings.wait_limit_min = 0.01  # 0.6 s
    fake.gate = asyncio.Event()
    first = asyncio.create_task(phone.ask(spec()))
    await until(lambda: len(svc.scheduler.running) == 1)
    events = await phone.ask(spec())
    assert events[-1]["ending"] == "failed: The PC was busy for too long. Try again."
    fake.gate.set()
    await first


async def test_run_time_limit(svc, phone, fake):
    svc.settings.run_limit_min = 0.01
    fake.gate = asyncio.Event()
    events = await phone.ask(spec())
    assert events[-1]["ending"].startswith("failed: The question took longer than")
    assert events[-1]["error_kind"] == "limit"
    await until(lambda: fake.cancelled == 1)


async def test_stop_route(svc, phone, fake):
    fake.gate = asyncio.Event()
    run_id = {}

    async def note(e):
        if e["type"] == "run":
            run_id["id"] = e["id"]

    task = asyncio.create_task(phone.ask(spec(), on_event=note))
    await until(lambda: len(svc.scheduler.running) == 1 and "id" in run_id)
    assert (await phone.post(f"/runs/{run_id['id']}/stop", {}))[0] == 200
    events = await task
    assert events[-1]["ending"] == "stopped"
    await until(lambda: fake.cancelled == 1 and not svc.scheduler.running)


async def test_dropped_connection_stops_run_and_frees_worker(svc, phone, fake):
    fake.gate = asyncio.Event()
    async with aiohttp.ClientSession() as s:
        r = await s.post(phone.base + "/runs", json=spec(), headers={"Authorization": f"Bearer {phone.code}"})
        await r.content.readline()
        await until(lambda: len(svc.scheduler.running) == 1)
        r.close()  # the phone goes away mid-run
    await until(lambda: fake.cancelled == 1 and not svc.scheduler.running, timeout=5)
    assert svc.usage.recent("signal-lab")[0]["ending"] == "stopped"


async def test_pause(svc, phone):
    svc.scheduler.set_paused(True)
    events = await phone.ask(spec())
    assert events[-1]["ending"] == "failed: ClaudeIn is paused on the PC."
    assert events[-1]["error_kind"] == "paused"
    assert (await phone.get("/check"))[1]["busy"] is True
    svc.scheduler.set_paused(False)
    assert (await phone.ask(spec()))[-1]["ending"] == "done"


async def test_model_allow_list(svc, phone):
    svc.pairings.set_models("signal-lab", ["claude-haiku-5-5"])
    events = await phone.ask(spec(model="claude-opus-5-5"))
    assert events[-1]["ending"] == "failed: Signal Lab isn't allowed to use claude-opus-5-5."
    assert (await phone.ask(spec(model="claude-haiku-5-5")))[-1]["ending"] == "done"
    svc.pairings.set_models("signal-lab", [])
    assert (await phone.ask(spec(model="claude-opus-5-5")))[-1]["ending"] == "done"


async def test_plan_limit_reached(svc, phone, fake):
    at = int(time.time()) + 3600

    async def script(prompt, options, tools):
        yield rate_limit(status="rejected", five=1.0, resets_at=at)
        yield result("Claude AI usage limit reached", is_error=True, api_error_status=429)

    fake.script = script
    done = (await phone.ask(spec()))[-1]
    assert done["ending"] == f"failed: Claude plan limit reached; it resets at {time.strftime('%H:%M', time.localtime(at))}."
    assert done["error_kind"] == "rate_limited"


async def test_endings(svc, phone, fake):
    cases = [
        (dict(subtype="error_max_budget_usd", is_error=True), "rounds"),
        (dict(subtype="error_max_turns", is_error=True), "rounds"),
        (dict(stop_reason="refusal"), "declined"),
        (dict(stop_reason="max_tokens"), "cut short"),
    ]
    for kwargs, ending in cases:
        async def script(prompt, options, tools, kw=kwargs):
            yield result("x", **kw)

        fake.script = script
        assert (await phone.ask(spec()))[-1]["ending"] == ending

    async def crash(prompt, options, tools):
        raise RuntimeError("boom")
        yield

    fake.script = crash
    done = (await phone.ask(spec()))[-1]
    assert done["ending"].startswith("failed: Claude Code stopped without an answer.")


async def test_usage_rows_written(svc, phone, program):
    await phone.ask(spec())
    await program.ask(spec(app="Village"))
    totals = svc.usage.totals(0)
    assert totals["signal-lab"]["questions"] == 1
    assert totals["signal-lab"]["tokens"] == 3 + 5 + 7 + 11
    assert totals["village"]["questions"] == 1
    assert svc.usage.recent("village")[0]["device"] == "Village (this PC)"
    assert svc.usage.latest_limits()["five_hour"] == 0.34


async def test_program_must_name_app(svc, program):
    status, body = await program.ask(spec())
    assert status == 400 and body["error"] == 'Missing "app".'


async def test_local_token_refused_on_phone_port(svc):
    p = Phone(f"http://127.0.0.1:{svc.phone_port}", svc.token.token)
    try:
        status, _ = await p.ask(spec(app="village"))
        assert status == 401
    finally:
        await p.close()


async def test_pairing_code_refused_on_local_port(svc, phone):
    p = Phone(f"http://127.0.0.1:{svc.local_port}", phone.code)
    try:
        status, _ = await p.ask(spec(app="village"))
        assert status == 401
    finally:
        await p.close()


async def test_nothing_secret_in_logs_or_usage(svc, phone, program, fake):
    secrets_ = ["SECRET-PROMPT-71", "SECRET-SYSTEM-72", "SECRET-ANSWER-73", "SECRET-TOOL-74", "SECRET-HISTORY-75"]

    async def script(prompt, options, tools):
        await tools["t"]({"q": "x"})
        for x in stream_text("SECRET-ANSWER-73"):
            yield x
        yield result("SECRET-ANSWER-73")

    fake.script = script
    tool = {"name": "t", "description": "", "input_schema": {"type": "object"}}
    body = spec(prompt="SECRET-PROMPT-71", system="SECRET-SYSTEM-72", tools=[tool],
                history=[{"role": "user", "text": "SECRET-HISTORY-75"}])
    await phone.ask(body, on_tool=lambda e: {"text": "SECRET-TOOL-74"})
    await program.ask(dict(body, app="village"), on_tool=lambda e: {"text": "SECRET-TOOL-74"})

    async def failing(prompt, options, tools):
        yield result("failed with SECRET-ANSWER-73", is_error=True, api_error_status=500)

    fake.script = failing
    await phone.ask(body)
    await svc.stop()  # flush and close usage.db and logs
    svc.stop = _noop  # the fixture stops it again
    blobs = b""
    for f in svc.folder.rglob("*"):
        if f.is_file() and f.suffix in (".db", ".log", "") or f.name.startswith("claudein.log"):
            blobs += f.read_bytes()
    for s in secrets_ + [phone.code, svc.token.token]:
        assert s.encode() not in blobs, s
    assert b"signal-lab" in blobs  # the files were really read


async def _noop():
    pass


async def test_tls_certificate_is_the_pinned_one(home, fake):
    from claudein.service import Service

    from .conftest import make_claude

    s = Service(home, query_fn=fake.query, claude=make_claude(home)[0])
    await s.start(use_tls=True, phone_port=0, local_port=0)
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", s.phone_port, ssl=ctx)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.close()
        import hashlib

        assert hashlib.sha256(der).hexdigest().upper() == tls.fingerprint(home)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://127.0.0.1:{s.phone_port}/hello", ssl=ctx) as r:
                assert (await r.json())["app"] == HELLO_APP
    finally:
        await s.stop()
