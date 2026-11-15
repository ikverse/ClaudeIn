"""Shared test pieces: a fake Claude Code, a running ClaudeIn on free ports, and a fake phone."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
import pytest_asyncio
from claude_agent_sdk._internal.message_parser import parse_message

from claudein import server as server_mod
from claudein.service import Service

DATA = Path(__file__).parent / "data"


def recorded(name: str) -> list[Any]:
    """A recorded Claude Code output, as the SDK messages it turns into."""
    out = []
    for line in (DATA / name).read_text(encoding="utf-8").splitlines():
        m = parse_message(json.loads(line))
        if m is not None:
            out.append(m)
    return out


def m(o: dict) -> Any:
    return parse_message(o)


def stream_text(text: str) -> list[Any]:
    return [
        m({"type": "stream_event", "uuid": "u", "session_id": "s", "event": {"type": "message_start", "message": {}}}),
        m({"type": "stream_event", "uuid": "u", "session_id": "s",
           "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}}),
        m({"type": "assistant", "message": {"model": "x", "content": [{"type": "text", "text": text}],
                                            "stop_reason": "end_turn"}}),
    ]


def result(text: str = "", subtype: str = "success", is_error: bool = False, **extra) -> Any:
    o = {"type": "result", "subtype": subtype, "duration_ms": 1, "duration_api_ms": 1, "is_error": is_error,
         "num_turns": 1, "session_id": "s", "result": text, "total_cost_usd": 0.01,
         "usage": {"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 7,
                   "cache_creation_input_tokens": 11,
                   "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}}}
    o.update(extra)
    return m(o)


def rate_limit(status: str = "allowed", five: float = 0.34, week: float = 0.12, resets_at: int | None = None) -> Any:
    info = {"status": status, "unifiedWindows": {"five_hour": {"utilization": five}, "seven_day": {"utilization": week}}}
    if resets_at is not None:
        info["resetsAt"] = resets_at
    return m({"type": "rate_limit_event", "rate_limit_info": info, "uuid": "u", "session_id": "s"})


Script = Callable[[str, Any, dict], AsyncIterator[Any]]


class FakeClaude:
    """Stands in for the SDK's query(). Each run plays [script]; it records what it was given."""

    def __init__(self):
        self.script: Script = self.plain
        self.calls: list[tuple[str, Any]] = []
        self.cancelled = 0
        self.gate: asyncio.Event | None = None  # when set, runs wait on it before answering
        self.started: list[str] = []

    @staticmethod
    async def plain(prompt, options, tools):
        for x in stream_text("ready"):
            yield x
        yield rate_limit()
        yield result("ready")

    def query(self, prompt: str, options: Any, tools: dict) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))

        async def gen():
            self.started.append(prompt)
            try:
                if self.gate is not None:
                    await self.gate.wait()
                async for x in self.script(prompt, options, tools):
                    yield x
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

        return gen()


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAUDEIN_HOME", str(tmp_path))
    monkeypatch.setattr(server_mod, "KEEPALIVE_S", 0.2)
    return tmp_path


@pytest_asyncio.fixture
async def svc(home, fake) -> AsyncIterator[Service]:
    s = Service(home, query_fn=fake.query)
    await s.start(use_tls=False, phone_port=0, local_port=0)
    try:
        yield s
    finally:
        await s.stop()


def spec(**over) -> dict:
    o = {"model": "claude-opus-5-5", "system": "Be brief.", "prompt": "Say ready", "max_budget_usd": 1}
    o.update(over)
    return o


class Phone:
    """A fake phone (or program): asks questions, answers tools, stops runs."""

    def __init__(self, base: str, code: str):
        self.base = base
        self.code = code
        self.session = aiohttp.ClientSession()

    async def close(self):
        await self.session.close()

    def _h(self, auth: str | None = "default") -> dict:
        a = self.code if auth == "default" else auth
        return {"Authorization": f"Bearer {a}"} if a else {}

    async def get(self, path: str, auth: str | None = "default") -> tuple[int, dict]:
        async with self.session.get(self.base + path, headers=self._h(auth)) as r:
            return r.status, await r.json()

    async def post(self, path: str, body: dict, auth: str | None = "default") -> tuple[int, dict]:
        async with self.session.post(self.base + path, json=body, headers=self._h(auth)) as r:
            return r.status, await r.json()

    async def ask(self, body: dict, on_tool: Callable[[dict], Awaitable[dict] | dict] | None = None,
                  on_event: Callable[[dict], Awaitable[None] | None] | None = None,
                  auth: str | None = "default") -> list[dict] | tuple[int, dict]:
        """The events of one run, or (status, error body) when it was refused before streaming."""
        async with self.session.post(self.base + "/runs", json=body, headers=self._h(auth)) as r:
            if r.status != 200:
                return r.status, await r.json()
            events: list[dict] = []
            run_id = None
            async for raw in r.content:
                line = raw.decode().rstrip("\n")
                if not line.startswith("data: "):
                    continue
                e = json.loads(line[6:])
                events.append(e)
                if e["type"] == "run":
                    run_id = e["id"]
                if on_event is not None:
                    res = on_event(e)
                    if asyncio.iscoroutine(res):
                        await res
                if e["type"] == "tool" and on_tool is not None:
                    a = on_tool(e)
                    if asyncio.iscoroutine(a):
                        a = await a
                    if a is not None:
                        await self.post(f"/runs/{run_id}/tools/{e['id']}", a)
                if e["type"] == "done":
                    break
            return events


@pytest_asyncio.fixture
async def phone(svc) -> AsyncIterator[Phone]:
    _, code = svc.pair("Signal Lab", "Phone")
    p = Phone(f"http://127.0.0.1:{svc.phone_port}", code)
    try:
        yield p
    finally:
        await p.close()


@pytest_asyncio.fixture
async def program(svc) -> AsyncIterator[Phone]:
    p = Phone(f"http://127.0.0.1:{svc.local_port}", svc.token.token)
    try:
        yield p
    finally:
        await p.close()


async def until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)
