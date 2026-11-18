"""Asking Claude through ClaudeIn from a program on this PC (protocol: ClaudeIn's docs/protocol.md)."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Union

from . import _http


class ClaudeInUnavailable(Exception):
    """ClaudeIn isn't running (or isn't reachable): show "PC not available". There is no other route to Claude."""


class ClaudeInError(Exception):
    """ClaudeIn refused the request before it started (bad request, wrong token, not allowed)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"{message} (HTTP {status})")
        self.status = status
        self.message = message


# --- tools ---------------------------------------------------------------------------------------------------------------

@dataclass
class Image:
    """A picture in the question (run(images=[...])) or in a tool's answer. [data] is the raw bytes or base64 text."""

    data: Union[bytes, str]
    mime_type: str = "image/png"

    def block(self) -> dict:
        data = self.data if isinstance(self.data, str) else base64.b64encode(self.data).decode("ascii")
        return {"type": "image", "data": data, "mime_type": self.mime_type}


#: What a tool handler may return: text, a picture, a list of those, or an MCP-style dict
#: {"content": [{"type": "text", "text": ...}, {"type": "image", "data": ..., "mimeType": ...}], "is_error": bool}.
ToolAnswer = Union[str, Image, list, dict, None]


@dataclass
class Tool:
    """A tool the program runs itself. [handler] gets the input dict; it may be a normal or an async function.

    A normal function runs on the event loop's own thread (so it may use objects tied to that thread, such as a
    sqlite connection); keep it quick, or make it async.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Union[ToolAnswer, Awaitable[ToolAnswer]]]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def answer_body(value: ToolAnswer) -> dict:
    """A handler's return value as ClaudeIn's tool-answer body."""
    if isinstance(value, dict):
        content = []
        for b in value.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                content.append({"type": "text", "text": str(b.get("text", ""))})
            elif b.get("type") == "image":
                content.append({"type": "image", "data": b.get("data", ""),
                                "mime_type": b.get("mime_type") or b.get("mimeType") or "image/png"})
        if "text" in value and not content:
            content.append({"type": "text", "text": str(value["text"])})
        return {"content": content or [{"type": "text", "text": ""}], "is_error": bool(value.get("is_error"))}
    items = value if isinstance(value, list) else [value]
    content = []
    for item in items:
        if isinstance(item, Image):
            content.append(item.block())
        elif item is not None:
            content.append({"type": "text", "text": item if isinstance(item, str) else json.dumps(item)})
    return {"content": content or [{"type": "text", "text": ""}], "is_error": False}


async def call_handler(tool: Tool | None, name: str, tool_input: dict) -> dict:
    """Runs a tool's handler; a crash becomes an error answer for Claude instead of crashing the program."""
    if tool is None:
        return {"content": [{"type": "text", "text": f"No tool called {name}."}], "is_error": True}
    try:
        value = tool.handler(tool_input)
        if inspect.isawaitable(value):
            value = await value
        return answer_body(value)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001  (any handler failure goes back to Claude as an error)
        return {"content": [{"type": "text", "text": f"The tool failed: {type(e).__name__}: {e}"}], "is_error": True}


# --- events and the result -------------------------------------------------------------------------------------------------

@dataclass
class Started:
    run_id: str


@dataclass
class Text:
    delta: str


@dataclass
class Working:
    what: str


@dataclass
class ServerTool:
    name: str
    input: str  # JSON text, as ClaudeIn sends it


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Result:
    ending: str  # done | stopped | declined | rounds | cut short | failed: <reason>
    answer: str = ""
    messages: list[str] = field(default_factory=list)  # the text between tool calls, in order
    usage: dict = field(default_factory=dict)
    api_price_usd: float | None = None
    turns: int = 0
    limits: dict | None = None
    error_kind: str | None = None  # rate_limited | service | limit | paused | cap | not_allowed | unavailable | format
    queued_ms: int = 0
    structured: Any = None  # the answer in the shape asked for with output_schema (None otherwise)

    @property
    def ok(self) -> bool:
        return self.ending == "done"

    @property
    def reason(self) -> str:
        """The words after "failed: ", or the ending itself."""
        return self.ending[len("failed: "):] if self.ending.startswith("failed: ") else self.ending

    @classmethod
    def from_done(cls, d: dict, messages: list[str]) -> "Result":
        return cls(ending=str(d.get("ending", "")), answer=str(d.get("answer", "")), messages=messages,
                   usage=dict(d.get("usage") or {}), api_price_usd=d.get("api_price_usd"),
                   turns=int(d.get("turns") or 0), limits=d.get("limits"), error_kind=d.get("error_kind"),
                   queued_ms=int(d.get("queued_ms") or 0), structured=d.get("structured"))


@dataclass
class Done:
    result: Result


Event = Union[Started, Text, Working, ServerTool, ToolCall, Done]


class _Messages:
    """Collects the answer's text, split where Claude stopped to use a tool."""

    def __init__(self):
        self.done: list[str] = []
        self.current: list[str] = []

    def text(self, delta: str) -> None:
        self.current.append(delta)

    def cut(self) -> None:
        s = "".join(self.current).strip()
        if s:
            self.done.append(s)
        self.current = []

    def all(self) -> list[str]:
        self.cut()
        return self.done


# --- where ClaudeIn is -----------------------------------------------------------------------------------------------------

def _home() -> Path:
    override = os.environ.get("CLAUDEIN_HOME")
    return Path(override) if override else Path.home() / ".claudein"


class _Local:
    """local.json (token and port), read once and again after a 401 ("New local token" in the tray)."""

    def __init__(self):
        self.token: str | None = None
        self.port: int | None = None
        self.read_from: Path | None = None

    def load(self, force: bool = False) -> tuple[str, int]:
        path = _home() / "local.json"
        if force or self.token is None or self.read_from != path:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.token, self.port, self.read_from = str(data["token"]), int(data["port"]), path
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise ClaudeInUnavailable("PC not available: ClaudeIn isn't set up on this PC "
                                          "(no local.json).") from e
        return self.token, self.port


_local = _Local()


async def _send(method: str, path: str, body: dict | None) -> _http.Response:
    """One request, with the token re-read once if ClaudeIn says it is wrong."""
    for attempt in (0, 1):
        token, port = _local.load(force=attempt == 1)
        try:
            resp = await _http.request(port, method, path, token, body)
        except (OSError, asyncio.TimeoutError) as e:
            raise ClaudeInUnavailable("PC not available: ClaudeIn isn't running.") from e
        if resp.status == 401 and attempt == 0:
            await resp.close()
            continue
        return resp
    raise AssertionError("unreachable")


async def _post_quietly(path: str, body: dict) -> None:
    try:
        resp = await _send("POST", path, body)
        await resp.json()
        await resp.close()
    except (ClaudeInUnavailable, OSError, asyncio.TimeoutError):
        pass


# --- asking ----------------------------------------------------------------------------------------------------------------

async def run(*, app: str, model: str, system: str, prompt: str, history: list[dict] | None = None,
              tools: list[Tool] | None = None, builtin_tools: list[str] | None = None, max_turns: int | None = None,
              max_budget_usd: float | None = None, tool_timeout_s: float | None = None,
              images: list[Image] | None = None, output_schema: dict | None = None) -> AsyncIterator[Event]:
    """Asks one question and yields what happens, ending with Done(result).

    [images] go to Claude in front of the prompt, in order. With [output_schema] (a JSON Schema of an object) the
    answer comes back parsed in result.structured, or the run fails with error_kind "format".

    Raises ClaudeInUnavailable when ClaudeIn isn't running, and ClaudeInError when it refuses the request outright.
    Stopping early (break, aclose(), or cancelling the task) stops the run on the PC too.
    """
    tools = list(tools or [])
    by_name = {t.name: t for t in tools}
    body: dict[str, Any] = {"app": app, "model": model, "system": system, "prompt": prompt}
    if history:
        body["history"] = history
    if tools:
        body["tools"] = [t.spec() for t in tools]
    if builtin_tools is not None:
        body["builtin_tools"] = list(builtin_tools)
    if max_turns is not None:
        body["max_turns"] = max_turns
    if max_budget_usd is not None:
        body["max_budget_usd"] = max_budget_usd
    if tool_timeout_s is not None:
        body["tool_timeout_s"] = tool_timeout_s
    if images:
        body["images"] = [{"data": b["data"], "mime_type": b["mime_type"]} for b in (i.block() for i in images)]
    if output_schema is not None:
        body["output_schema"] = output_schema

    resp = await _send("POST", "/runs", body)
    if resp.status != 200:
        err = await resp.json()
        await resp.close()
        raise ClaudeInError(resp.status, str(err.get("error") or "ClaudeIn refused the request."))

    run_id: str | None = None
    finished = False
    pending: set[asyncio.Task] = set()
    texts = _Messages()

    async def answer(call_id: str, name: str, tool_input: dict) -> None:
        a = await call_handler(by_name.get(name), name, tool_input)
        await _post_quietly(f"/runs/{run_id}/tools/{call_id}", a)

    try:
        async for line in resp.lines():
            if not line.startswith("data: "):
                continue  # ": still working" and blank lines
            try:
                e = json.loads(line[6:])
            except ValueError:
                continue
            kind = e.get("type")
            if kind == "run":
                run_id = str(e.get("id"))
                yield Started(run_id)
            elif kind == "text":
                texts.text(str(e.get("delta", "")))
                yield Text(str(e.get("delta", "")))
            elif kind == "working":
                yield Working(str(e.get("what", "")))
            elif kind == "server_tool":
                texts.cut()
                yield ServerTool(str(e.get("name", "")), str(e.get("input", "")))
            elif kind == "tool":
                texts.cut()
                call = ToolCall(str(e.get("id")), str(e.get("name")), dict(e.get("input") or {}))
                task = asyncio.ensure_future(answer(call.id, call.name, call.input))
                pending.add(task)
                task.add_done_callback(pending.discard)
                yield call
            elif kind == "done":
                finished = True
                yield Done(Result.from_done(e, texts.all()))
                return
            # other event types are ignored, as the protocol asks
        raise ClaudeInUnavailable("PC not available: ClaudeIn stopped before answering.")
    finally:
        for t in pending:
            t.cancel()
        await resp.close()
        if not finished and run_id is not None:
            await _post_quietly(f"/runs/{run_id}/stop", {})  # stopped early: stop Claude Code on the PC too


async def ask(**kwargs) -> Result:
    """Like run(), but returns only the result."""
    stream = run(**kwargs)
    try:
        async for e in stream:
            if isinstance(e, Done):
                return e.result
    finally:
        await stream.aclose()
    raise ClaudeInUnavailable("PC not available: ClaudeIn stopped before answering.")
