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
from claudein.claudecode import OPTIONAL_FLAGS, REQUIRED_FLAGS, ClaudeCode, CliResult
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


EMAIL = "owner@example.com"
LOGIN_LINK = "https://claude.com/cai/oauth/authorize?code=true&client_id=test&state=secretstate"


def help_text(flags: list[str]) -> str:
    return "Usage: claude [options] [prompt]\n\nOptions:\n" + "".join(f"  {f} <value>  does something\n" for f in flags)


class FakeProc:
    """The running `claude auth login`: prints its lines, then waits until finished or killed."""

    def __init__(self, cli: "FakeCli"):
        self.cli = cli
        self.killed = False
        self.done = asyncio.Event()
        if cli.login_finishes_at_once:
            self.done.set()

    async def lines(self):
        for line in self.cli.login_lines:
            yield line

    async def wait(self) -> int:
        await self.done.wait()
        if self.killed:
            return 1
        if self.cli.login_exit == 0 and self.cli.login_signs_in:
            self.cli.signed_in = True
        return self.cli.login_exit

    def kill(self) -> None:
        self.killed = True
        self.done.set()


class FakeCli:
    """Stands in for the claude program's own commands: --version, --help, auth status, auth login, auth logout."""

    def __init__(self):
        self.version = "2.1.325"
        self.flags = [a[0] for a in REQUIRED_FLAGS] + list(OPTIONAL_FLAGS)
        self.signed_in = True
        self.method = "claude.ai"
        self.email = EMAIL
        self.broken = False  # the program doesn't start
        self.calls: list[tuple[list, dict]] = []
        self.procs: list[FakeProc] = []
        self.login_lines = ["Opening browser to sign in…", f"If the browser didn't open, visit: {LOGIN_LINK}"]
        self.login_finishes_at_once = True
        self.login_exit = 0
        self.login_signs_in = True

    def count(self, *args: str) -> int:
        return sum(1 for a, _ in self.calls if a == list(args))

    async def run(self, exe, args, env, timeout) -> CliResult:
        self.calls.append((list(args), dict(env)))
        if self.broken:
            return CliResult(-1, "", "not a valid program")
        if args == ["--version"]:
            return CliResult(0, f"{self.version} (Claude Code)\n")
        if args == ["--help"]:
            return CliResult(0, help_text(self.flags))
        if args[:2] == ["auth", "status"]:
            if not self.signed_in:
                return CliResult(1, json.dumps({"loggedIn": False, "authMethod": "none"}))
            return CliResult(0, json.dumps({"loggedIn": True, "authMethod": self.method, "email": self.email,
                                            "subscriptionType": "max", "planTier": "max_5x"}))
        if args[:2] == ["auth", "logout"]:
            self.signed_in = False
            return CliResult(0, "Logged out\n")
        return CliResult(1, "", "unknown command")

    async def spawn(self, exe, args, env) -> FakeProc:
        self.calls.append((list(args), dict(env)))
        proc = FakeProc(self)
        self.procs.append(proc)
        return proc


def make_claude(folder: Path, cli: FakeCli | None = None, **kw) -> tuple[ClaudeCode, FakeCli]:
    """The guard, around a fake claude program (a stand-in file, so its size and date can change like an update)."""
    cli = cli or FakeCli()
    exe = folder / "fake-claude" / "claude.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"fake claude 1")
    kw.setdefault("find", lambda c: exe if exe.exists() else None)
    claude = ClaudeCode(folder, cli=cli.run, spawn=cli.spawn, **kw)
    claude.fake_exe = exe  # type: ignore[attr-defined]
    return claude, cli


def update_fake_claude(claude: ClaudeCode, cli: FakeCli, version: str, flags: list[str] | None = None) -> None:
    """What a Claude Code update does: a new version, a new file, maybe different flags."""
    cli.version = version
    if flags is not None:
        cli.flags = flags
    claude.fake_exe.write_bytes(b"fake claude " + version.encode() + b" with a different size")  # type: ignore[attr-defined]


@pytest.fixture
def cli() -> FakeCli:
    return FakeCli()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAUDEIN_HOME", str(tmp_path))
    monkeypatch.setattr(server_mod, "KEEPALIVE_S", 0.2)
    return tmp_path


@pytest_asyncio.fixture
async def svc(home, fake, cli) -> AsyncIterator[Service]:
    claude, _ = make_claude(home, cli)
    s = Service(home, query_fn=fake.query, claude=claude)
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
