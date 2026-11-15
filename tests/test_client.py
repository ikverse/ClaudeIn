"""Phase 3: the Python client library, against a real ClaudeIn running with the fake Claude Code."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

import claudein_client as cc
from claudein_client.testing import FakeClaudeIn, answer_text

from .conftest import rate_limit, result, stream_text, until

ASK = dict(app="village", model="claude-opus-5-5", system="Be brief.", prompt="Say ready")


async def test_plain_answer(svc):
    events = [e async for e in cc.run(**ASK)]
    kinds = [type(e).__name__ for e in events]
    assert kinds[0] == "Started" and kinds[-1] == "Done" and "Text" in kinds
    r = events[-1].result
    assert r.ok and r.answer == "ready" and r.messages == ["ready"]
    assert r.usage["output"] == 5 and r.limits["five_hour"] == 0.34
    assert svc.usage.recent("village")[0]["ending"] == "done"


async def test_ask_and_request_fields(svc, fake):
    r = await cc.ask(**ASK, builtin_tools=[], max_turns=3, max_budget_usd=1.5, history=[{"role": "user", "text": "Hi"}])
    assert r.ok
    prompt, opts = fake.calls[-1]
    assert opts.tools == [] and opts.max_turns == 3 and opts.max_budget_usd == 1.5
    assert prompt.startswith("Earlier in this conversation:")


async def test_tools_normal_async_picture_and_crash(svc, fake):
    seen = {}

    async def script(prompt, options, tools):
        seen["sync"] = await tools["add"]({"a": 2, "b": 3})
        for x in stream_text("Adding."):
            yield x
        seen["async"] = await tools["slow"]({})
        seen["pic"] = await tools["pic"]({})
        seen["crash"] = await tools["crash"]({})
        for x in stream_text("All done."):
            yield x
        yield result("All done.")

    fake.script = script

    async def slow(_):
        await asyncio.sleep(0.05)
        return "slept"

    def crash(_):
        raise ValueError("nope")

    schema = {"type": "object", "properties": {}}
    tools = [cc.Tool("add", "Adds", schema, lambda i: str(i["a"] + i["b"])),
             cc.Tool("slow", "Sleeps", schema, slow),
             cc.Tool("pic", "A picture", schema, lambda i: ["a chart", cc.Image(b"\x89PNG", "image/png")]),
             cc.Tool("crash", "Fails", schema, crash)]
    events = [e async for e in cc.run(**ASK, tools=tools)]
    assert [e.name for e in events if isinstance(e, cc.ToolCall)] == ["add", "slow", "pic", "crash"]
    assert seen["sync"]["content"] == [{"type": "text", "text": "5"}]
    assert seen["async"]["content"][0]["text"] == "slept"
    assert seen["pic"]["content"][1] == {"type": "image", "data": "iVBORw==", "mimeType": "image/png"}
    assert seen["crash"]["is_error"] and "ValueError: nope" in seen["crash"]["content"][0]["text"]
    r = events[-1].result
    assert r.ok and r.messages == ["Adding.", "All done."]


async def test_mcp_style_answer_dict(svc, fake):
    seen = {}

    async def script(prompt, options, tools):
        seen["a"] = await tools["t"]({})
        yield result("ok")

    fake.script = script
    handler = lambda i: {"content": [{"type": "text", "text": "x"}, {"type": "image", "data": "AA==",  # noqa: E731
                                                                      "mimeType": "image/jpeg"}], "is_error": True}
    await cc.ask(**ASK, tools=[cc.Tool("t", "", {"type": "object"}, handler)])
    assert seen["a"] == {"content": [{"type": "text", "text": "x"},
                                     {"type": "image", "data": "AA==", "mimeType": "image/jpeg"}], "is_error": True}


async def test_error_kinds(svc, fake):
    async def limited(prompt, options, tools):
        yield rate_limit(status="rejected", five=1.0, resets_at=2_000_000_000)
        yield result("Claude AI usage limit reached", is_error=True, api_error_status=429)

    fake.script = limited
    r = await cc.ask(**ASK)
    assert not r.ok and r.error_kind == "rate_limited" and r.reason.startswith("Claude plan limit reached")

    svc.scheduler.set_paused(True)
    r = await cc.ask(**ASK)
    assert r.error_kind == "paused" and r.reason == "ClaudeIn is paused on the PC."
    svc.scheduler.set_paused(False)

    svc.pairings.set_cap("village", 0)
    assert (await cc.ask(**ASK)).error_kind == "cap"


async def test_refused_before_start(svc):
    with pytest.raises(cc.ClaudeInError) as e:
        await cc.ask(**dict(ASK, model="Not A Model"))
    assert e.value.status == 400 and "model" in e.value.message


async def test_stopping_early_stops_on_the_pc(svc, fake):
    fake.gate = asyncio.Event()
    stream = cc.run(**ASK)
    async for e in stream:
        if isinstance(e, cc.Started):
            await until(lambda: len(svc.scheduler.running) == 1)
            break
    await stream.aclose()
    await until(lambda: fake.cancelled == 1 and not svc.scheduler.running)
    assert svc.usage.recent("village")[0]["ending"] == "stopped"


async def test_cancelled_task_stops_on_the_pc(svc, fake):
    fake.gate = asyncio.Event()
    task = asyncio.create_task(cc.ask(**ASK))
    await until(lambda: len(svc.scheduler.running) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: fake.cancelled == 1 and not svc.scheduler.running)


async def test_new_local_token_is_picked_up(svc):
    assert (await cc.ask(**ASK)).ok  # reads local.json
    svc.token.renew()  # "New local token" in the tray
    assert (await cc.ask(**ASK)).ok  # 401, reads local.json again, works


async def test_not_running(home):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    (home / "local.json").write_text(json.dumps({"token": "x", "port": port}))
    with pytest.raises(cc.ClaudeInUnavailable, match="PC not available"):
        await cc.ask(**ASK)


async def test_not_set_up(home):
    with pytest.raises(cc.ClaudeInUnavailable, match="PC not available"):
        await cc.ask(**ASK)


# --- the fake, for programs' own tests ---------------------------------------------------------------------------------

async def test_fake_plain_and_tools():
    fake = FakeClaudeIn().reply("Done", tools=[("add", {"a": 1, "b": 2})])
    tool = cc.Tool("add", "", {"type": "object"}, lambda i: str(i["a"] + i["b"]))
    events = [e async for e in fake.run(**ASK, tools=[tool])]
    assert [type(e).__name__ for e in events] == ["Started", "ToolCall", "Text", "Done"]
    assert events[-1].result.ok and events[-1].result.answer == "Done"
    name, tool_input, body = fake.tool_answers[0]
    assert name == "add" and answer_text(body) == "3"
    assert fake.calls[0]["tools"] == ["add"] and fake.calls[0]["app"] == "village"


async def test_fake_failures():
    fake = FakeClaudeIn().fail("rate_limited", "Claude plan limit reached; it resets at 14:00.").unavailable()
    r = await fake.ask(**ASK)
    assert r.error_kind == "rate_limited" and r.ending == "failed: Claude plan limit reached; it resets at 14:00."
    with pytest.raises(cc.ClaudeInUnavailable):
        await fake.ask(**ASK)
    assert (await fake.ask(**ASK)).answer == "ok"  # unscripted


async def test_client_needs_no_add_ons():
    import ast
    import pathlib
    import sys

    folder = pathlib.Path(cc.__file__).parent
    for f in folder.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for n in names:
                assert n.split(".")[0] in sys.stdlib_module_names or n.startswith("claudein_client"), (f.name, n)
