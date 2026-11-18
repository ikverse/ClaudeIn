"""Phase 8b: runs that survive a dropped connection (detach, seq, GET /runs/<id>, replay) and background priority."""

from __future__ import annotations

import asyncio
import json

import pytest

import claudein_client as cc
from claudein import runs
from claudein.scheduler import FULL, TOO_LONG_IN_LINE
from claudein_client.testing import FakeClaudeIn

from .conftest import Phone, result, spec, stream_text, until


async def start_and_drop(caller: Phone, body: dict, after_type: str = "run") -> tuple[str, list[dict]]:
    """Asks, reads events up to the first one of [after_type], then hangs up. Returns the run id and what was read."""
    seen: list[dict] = []
    async with caller.session.post(caller.base + "/runs", json=body, headers=caller._h()) as r:
        assert r.status == 200, await r.text()
        async for raw in r.content:
            line = raw.decode().rstrip("\n")
            if line.startswith("data: "):
                seen.append(json.loads(line[6:]))
                if seen[-1]["type"] == after_type:
                    break
        r.close()
    return seen[0]["id"], seen


async def read_events(caller: Phone, run_id: str, after: int | str = 0) -> list[dict]:
    out: list[dict] = []
    async with caller.session.get(f"{caller.base}/runs/{run_id}/events?after={after}", headers=caller._h()) as r:
        assert r.status == 200, await r.text()
        async for raw in r.content:
            line = raw.decode().rstrip("\n")
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
                if out[-1]["type"] == "done":
                    break
    return out


# --- numbered events ----------------------------------------------------------------------------------------------------

async def test_every_event_is_numbered(svc, phone):
    events = await phone.ask(spec())
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0] == {"type": "run", "id": events[0]["id"], "seq": 1}


# --- detach ---------------------------------------------------------------------------------------------------------------

async def test_detached_run_survives_a_dropped_connection(svc, phone, fake):
    fake.gate = asyncio.Event()
    run_id, _ = await start_and_drop(phone, spec(detach=True))
    await until(lambda: run_id in svc.scheduler.running)
    await asyncio.sleep(0.5)  # longer than the keep-alive: the dropped connection has been noticed
    run = svc.runs[run_id]
    assert run.result is None and not run.closed
    status, body = await phone.get(f"/runs/{run_id}")
    assert status == 200 and body["state"] == "running" and body["done"] is None
    fake.gate.set()
    await until(lambda: run.result is not None)
    status, body = await phone.get(f"/runs/{run_id}")
    assert body["state"] == "done" and body["done"]["ending"] == "done" and body["done"]["answer"] == "ready"
    assert body["last_seq"] == body["done"]["seq"]
    events = await read_events(phone, run_id)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["type"] == "run" and events[-1]["type"] == "done"


async def test_normal_run_still_stops_when_the_caller_goes(svc, phone, fake):
    fake.gate = asyncio.Event()
    run_id, _ = await start_and_drop(phone, spec())
    await until(lambda: run_id not in svc.runs)
    status, _ = await phone.get(f"/runs/{run_id}")
    assert status == 404
    assert svc.usage.recent("signal-lab")[0]["ending"] == "stopped"


async def test_replay_after_seq_then_live(svc, phone, fake):
    go_on = asyncio.Event()

    async def script(prompt, options, tools):
        for x in stream_text("part one"):
            yield x
        await go_on.wait()
        for x in stream_text("part two"):
            yield x
        yield result("part one\n\npart two")

    fake.script = script
    run_id, seen = await start_and_drop(phone, spec(detach=True), after_type="text")
    first_text = seen[-1]
    assert first_text["delta"] == "part one"
    reader = asyncio.create_task(read_events(phone, run_id, after=first_text["seq"]))
    await asyncio.sleep(0.3)
    go_on.set()
    later = await reader
    assert later[0]["seq"] == first_text["seq"] + 1
    assert "part two" in "".join(e.get("delta", "") for e in later if e["type"] == "text")
    assert later[-1]["type"] == "done" and later[-1]["answer"] == "part one\n\npart two"
    again = await read_events(phone, run_id, after=later[-1]["seq"])  # nothing after the end: it just closes
    assert again == []


async def test_waiting_running_done(svc, phone, fake):
    svc.scheduler.set_workers(1)
    fake.gate = asyncio.Event()
    first, _ = await start_and_drop(phone, spec(detach=True, prompt="one"))
    second, _ = await start_and_drop(phone, spec(detach=True, prompt="two"))
    await until(lambda: len(svc.scheduler.waiting) == 1)
    states = {rid: (await phone.get(f"/runs/{rid}"))[1]["state"] for rid in (first, second)}
    assert states == {first: "running", second: "waiting"}
    fake.gate.set()
    await until(lambda: all(svc.runs[r].result is not None for r in (first, second)))
    assert (await phone.get(f"/runs/{second}"))[1]["state"] == "done"


async def test_finished_detached_runs_are_dropped_after_the_keep_time(svc, phone, monkeypatch):
    monkeypatch.setattr(runs, "KEEP_DETACHED_S", 0.3)
    run_id, _ = await start_and_drop(phone, spec(detach=True), after_type="done")
    assert (await phone.get(f"/runs/{run_id}"))[0] == 200
    await asyncio.sleep(0.4)
    status, body = await phone.get(f"/runs/{run_id}")
    assert status == 404 and "hour" in body["error"]
    assert run_id not in svc.runs


async def test_at_most_so_many_detached_runs_per_app(svc, phone, program, monkeypatch):
    monkeypatch.setattr(runs, "MAX_DETACHED_PER_APP", 2)
    ids = [(await start_and_drop(phone, spec(detach=True), after_type="done"))[0] for _ in range(2)]
    status, body = await phone.ask(spec(detach=True))
    assert status == 429 and "2 long runs" in body["error"]
    assert (await phone.ask(spec()))[-1]["ending"] == "done"  # normal runs aren't counted
    assert (await program.ask(spec(app="village", detach=True)))[-1]["ending"] == "done"  # another app has its own
    status, body = await phone.post(f"/runs/{ids[0]}/stop", {})  # "I have read it": frees its place
    assert status == 200 and ids[0] not in svc.runs
    assert (await phone.ask(spec(detach=True)))[-1]["ending"] == "done"


async def test_stop_still_stops_a_detached_run(svc, phone, fake):
    fake.gate = asyncio.Event()
    run_id, _ = await start_and_drop(phone, spec(detach=True))
    await until(lambda: run_id in svc.scheduler.running)
    assert (await phone.post(f"/runs/{run_id}/stop", {}))[0] == 200
    await until(lambda: svc.runs[run_id].result is not None)
    assert svc.runs[run_id].result["ending"] == "stopped"


async def test_request_checks(svc, phone):
    tool = {"name": "echo", "description": "", "input_schema": {"type": "object"}}
    status, body = await phone.ask(spec(detach=True, tools=[tool]))
    assert status == 400 and "nobody to answer" in body["error"]
    status, body = await phone.ask(spec(detach="yes"))
    assert status == 400 and "true or false" in body["error"]
    status, body = await phone.ask(spec(priority="urgent"))
    assert status == 400 and "background" in body["error"]
    run_id, _ = await start_and_drop(phone, spec(detach=True), after_type="done")
    async with phone.session.get(f"{phone.base}/runs/{run_id}/events?after=-1", headers=phone._h()) as r:
        assert r.status == 400


# --- who may see a run --------------------------------------------------------------------------------------------------

async def test_only_the_owner_sees_a_run(svc, phone, program):
    run_id, _ = await start_and_drop(phone, spec(detach=True), after_type="done")
    _, other_code = svc.pair("Signal Lab", "Tablet")
    other = Phone(phone.base, other_code)
    try:
        for p in (other, program):
            assert (await p.get(f"/runs/{run_id}"))[0] == 404
            assert (await p.post(f"/runs/{run_id}/stop", {}))[0] == 404
            async with p.session.get(f"{p.base}/runs/{run_id}/events", headers=p._h()) as r:
                assert r.status == 404
        assert (await phone.get("/runs/no-such-run"))[0] == 404
        assert (await phone.get(f"/runs/{run_id}", auth="WRONG-CODE"))[0] == 401
        prog_id, _ = await start_and_drop(program, spec(app="village", detach=True), after_type="done")
        assert (await program.get(f"/runs/{prog_id}"))[0] == 200
        assert (await phone.get(f"/runs/{prog_id}"))[0] == 404
    finally:
        await other.close()


async def test_forget_ends_detached_runs(svc, fake):
    dev, code = svc.pair("Signal Lab", "Old phone")
    p = Phone(f"http://127.0.0.1:{svc.phone_port}", code)
    try:
        fake.gate = asyncio.Event()
        running, _ = await start_and_drop(p, spec(detach=True))
        await until(lambda: running in svc.scheduler.running)
        run = svc.runs[running]
        svc.forget(dev.id)
        await until(lambda: run.result is not None)
        assert run.result["ending"] == "stopped" and running not in svc.runs
    finally:
        await p.close()


async def test_switching_off_ends_detached_runs_only(svc, fake):
    dev, code = svc.pair("Signal Lab", "Phone two")
    p = Phone(f"http://127.0.0.1:{svc.phone_port}", code)
    try:
        fake.gate = asyncio.Event()
        detached, _ = await start_and_drop(p, spec(detach=True, prompt="long"))
        watched = asyncio.create_task(p.ask(spec(prompt="watched")))
        await until(lambda: len(svc.scheduler.running) == 2)
        drun = svc.runs[detached]
        svc.set_device_enabled(dev.id, False)
        await until(lambda: drun.result is not None)
        assert drun.result["ending"] == "stopped" and detached not in svc.runs
        fake.gate.set()
        events = await watched
        assert events[-1]["ending"] == "done"  # the run it was watching carried on, as before
        assert (await p.get("/check"))[0] == 403
        svc.set_device_enabled(dev.id, True)
        assert (await p.get("/check"))[0] == 200
    finally:
        await p.close()


# --- background priority ------------------------------------------------------------------------------------------------

async def test_queue_order_with_background_runs(svc, phone, program, fake):
    svc.scheduler.set_workers(1)
    fake.gate = asyncio.Event()
    tasks = [asyncio.create_task(program.ask(spec(app="village", prompt="first")))]
    await until(lambda: len(svc.scheduler.running) == 1)
    order = [(phone, "bg-phone", "background"), (program, "bg-program", "background"),
             (program, "program", "interactive"), (phone, "phone", "interactive")]
    for n, (who, prompt, priority) in enumerate(order, 1):
        body = spec(prompt=prompt, priority=priority)
        if who is program:
            body["app"] = "village"
        tasks.append(asyncio.create_task(who.ask(body)))
        await until(lambda n=n: len(svc.scheduler.waiting) == n)
    assert [r.spec.prompt for r in svc.scheduler.waiting] == ["phone", "program", "bg-phone", "bg-program"]
    fake.gate.set()
    await asyncio.gather(*tasks)
    assert fake.started == ["first", "phone", "program", "bg-phone", "bg-program"]


async def test_background_runs_hear_their_place(svc, phone, fake):
    svc.scheduler.set_workers(1)
    fake.gate = asyncio.Event()
    notes: list[str] = []
    first = asyncio.create_task(phone.ask(spec(prompt="first")))
    await until(lambda: len(svc.scheduler.running) == 1)
    bg = asyncio.create_task(phone.ask(spec(prompt="bg", priority="background"),
                                       on_event=lambda e: notes.append(e["what"]) if e["type"] == "working" else None))
    await until(lambda: len(svc.scheduler.waiting) == 1)
    person = asyncio.create_task(phone.ask(spec(prompt="person")))
    await until(lambda: len(svc.scheduler.waiting) == 2)
    await until(lambda: "Waiting for the PC (1 ahead)…" in notes)
    assert notes[0] == "Waiting for the PC (0 ahead)…"
    fake.gate.set()
    await asyncio.gather(first, bg, person)


async def test_background_has_its_own_waiting_limit(svc, phone, fake):
    svc.scheduler.set_workers(1)
    svc.settings.max_waiting = 1
    fake.gate = asyncio.Event()
    tasks = [asyncio.create_task(phone.ask(spec(prompt="running")))]
    await until(lambda: len(svc.scheduler.running) == 1)
    tasks.append(asyncio.create_task(phone.ask(spec(prompt="person"))))
    await until(lambda: len(svc.scheduler.waiting) == 1)
    tasks.append(asyncio.create_task(phone.ask(spec(prompt="bg", priority="background"))))
    await until(lambda: len(svc.scheduler.waiting) == 2)  # a full line for people doesn't turn background away
    assert (await phone.ask(spec(prompt="bg2", priority="background")))[-1]["ending"] == FULL
    assert (await phone.ask(spec(prompt="person2")))[-1]["ending"] == FULL
    fake.gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r[-1]["ending"] == "done" for r in results)


async def test_background_wait_limit(svc, phone, fake):
    assert svc.settings.background_wait_limit_min == 30
    svc.scheduler.set_workers(1)
    svc.settings.background_wait_limit_min = 0.005  # 0.3 s
    fake.gate = asyncio.Event()
    first = asyncio.create_task(phone.ask(spec(prompt="first")))
    await until(lambda: len(svc.scheduler.running) == 1)
    person = asyncio.create_task(phone.ask(spec(prompt="person")))
    events = await phone.ask(spec(prompt="bg", priority="background"))
    assert events[-1]["ending"] == TOO_LONG_IN_LINE and events[-1]["error_kind"] == "unavailable"
    assert len(svc.scheduler.waiting) == 1  # the person, with the normal 10 minutes, still waits
    fake.gate.set()
    assert (await person)[-1]["ending"] == "done"
    await first


# --- the client and its fake --------------------------------------------------------------------------------------------

ASK = dict(app="village", model="claude-opus-5-5", system="Be brief.", prompt="Say ready")


async def test_client_detach_resume_and_get_run(svc, fake):
    fake.gate = asyncio.Event()
    stream = cc.run(**ASK, detach=True, priority="background")
    started = await stream.__anext__()
    await stream.aclose()  # leaving early: with detach the run carries on
    assert isinstance(started, cc.Started) and started.seq == 1
    run = svc.runs[started.run_id]
    assert run.spec.detach and run.spec.priority == "background"
    await until(lambda: run.state == "running")
    await asyncio.sleep(0.4)
    assert not run.closed
    state = await cc.get_run(started.run_id)
    assert state.state == "running" and state.result is None
    fake.gate.set()
    events = [e async for e in cc.resume(started.run_id, after=started.seq)]
    assert events[0].seq == 2 and isinstance(events[-1], cc.Done) and events[-1].result.answer == "ready"
    state = await cc.get_run(started.run_id)
    assert state.state == "done" and state.result.ok and state.last_seq == events[-1].seq


async def test_client_without_detach_still_stops_on_leaving(svc, fake):
    fake.gate = asyncio.Event()
    stream = cc.run(**ASK)
    started = await stream.__anext__()
    run = svc.runs[started.run_id]
    await stream.aclose()
    await until(lambda: run.result is not None)
    assert run.result["ending"] == "stopped"


async def test_client_unknown_run(svc):
    with pytest.raises(cc.ClaudeInError) as e:
        await cc.get_run("no-such-run")
    assert e.value.status == 404
    with pytest.raises(cc.ClaudeInError):
        async for _ in cc.resume("no-such-run"):
            pass


async def test_fake_detach_get_run_and_resume():
    fake = FakeClaudeIn().reply("Long answer")
    events = [e async for e in fake.run(**ASK, detach=True, priority="background")]
    assert [e.seq for e in events] == [1, 2, 3]
    assert fake.calls[0]["detach"] is True and fake.calls[0]["priority"] == "background"
    run_id = events[0].run_id
    state = await fake.get_run(run_id)
    assert state.state == "done" and state.result.answer == "Long answer" and state.last_seq == 3
    later = [e async for e in fake.resume(run_id, after=1)]
    assert [type(e).__name__ for e in later] == ["Text", "Done"]
    with pytest.raises(cc.ClaudeInError):
        await fake.get_run("nope")
    tool = cc.Tool("t", "", {"type": "object"}, lambda i: "x")
    with pytest.raises(cc.ClaudeInError):
        await fake.ask(**ASK, detach=True, tools=[tool])
