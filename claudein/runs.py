"""One question: what was asked (RunSpec), and the run in progress (its events and waiting tool calls)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_BUILTIN_TOOLS = ["WebSearch", "WebFetch"]
KNOWN_BUILTIN_TOOLS = {"WebSearch", "WebFetch"}  # never shell, files or code: plan 4.4 and 4.7

MAX_IMAGES = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # each picture, once decoded
#: The schema goes to Claude Code on its command line, and Windows caps a command line at about 32,000 characters.
MAX_SCHEMA_BYTES = 16 * 1024
_IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/webp": "webp",
                "image/gif": "gif"}
_WHITESPACE = re.compile(r"\s+")


class BadRequest(ValueError):
    pass


@dataclass
class Turn:
    role: str
    text: str


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict


@dataclass
class Picture:
    """A picture in the question: base64 text (no spaces or line breaks) and its type."""

    data: str
    mime_type: str


def _kind_of(raw: bytes) -> str | None:
    """What the picture's first bytes say it is."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _pictures(v: Any) -> list[Picture]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise BadRequest('"images" must be a list of pictures.')
    if len(v) > MAX_IMAGES:
        raise BadRequest(f"Too many pictures: at most {MAX_IMAGES}.")
    out = []
    for i, p in enumerate(v, 1):
        if not isinstance(p, dict) or not isinstance(p.get("data"), str):
            raise BadRequest(f'Picture {i} needs "data" (base64) and "mime_type".')
        mime = str(p.get("mime_type") or "").strip().lower()
        kind = _IMAGE_TYPES.get(mime)
        if kind is None:
            raise BadRequest(f"Picture {i}: the type must be PNG, JPEG, WebP or GIF.")
        data = _WHITESPACE.sub("", p["data"])  # some phones break base64 into lines
        if len(data) > (MAX_IMAGE_BYTES // 3 + 1) * 4:
            raise BadRequest(f"Picture {i} is larger than 5 MB.")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest(f"Picture {i} is not valid base64.") from None
        if not raw:
            raise BadRequest(f"Picture {i} is empty.")
        if len(raw) > MAX_IMAGE_BYTES:
            raise BadRequest(f"Picture {i} is larger than 5 MB.")
        if _kind_of(raw) != kind:
            raise BadRequest(f"Picture {i} is not a {kind.upper()} picture.")
        out.append(Picture(data, "image/jpeg" if kind == "jpeg" else mime))
    return out


def _schema(v: Any) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict) or v.get("type") != "object":
        raise BadRequest('"output_schema" must be a JSON Schema whose "type" is "object".')
    if len(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise BadRequest("output_schema is too long: at most 16 KB.")
    return v


@dataclass
class RunSpec:
    model: str
    system: str
    prompt: str
    history: list[Turn] = field(default_factory=list)
    tools: list[ToolDef] = field(default_factory=list)
    max_budget_usd: float = 5.0
    builtin_tools: list[str] = field(default_factory=lambda: list(DEFAULT_BUILTIN_TOOLS))
    max_turns: int | None = None
    tool_timeout_s: float = 60.0
    app: str | None = None  # optional "app" from the request
    images: list[Picture] = field(default_factory=list)  # pictures in the question (protocol 2)
    output_schema: dict | None = None  # the shape the answer must have (protocol 2)

    @classmethod
    def parse(cls, o: Any) -> "RunSpec":
        """Reads a request body; raises BadRequest naming what is wrong (same rules as the Kotlin host, plus additions)."""
        if not isinstance(o, dict):
            raise BadRequest("The request is not a JSON object.")

        def need(key: str) -> str:
            v = o.get(key)
            if not isinstance(v, str) or not v.strip():
                raise BadRequest(f'Missing "{key}".')
            return v

        model = need("model")
        if not re.fullmatch(r"[a-z0-9.\-]{3,60}", model):
            raise BadRequest("That is not a model id.")
        history = []
        for t in o.get("history") or []:
            if not isinstance(t, dict) or not isinstance(t.get("role"), str) or not isinstance(t.get("text"), str):
                raise BadRequest("Each history entry needs a role and a text.")
            history.append(Turn(t["role"], t["text"]))
        tools = []
        for t in o.get("tools") or []:
            name = t.get("name") if isinstance(t, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", name):
                raise BadRequest("Tool names are letters, digits, dashes and underscores.")
            schema = t.get("input_schema") if isinstance(t.get("input_schema"), dict) else {"type": "object"}
            schema = dict(schema)
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            tools.append(ToolDef(name, str(t.get("description") or ""), schema))
        if len({t.name for t in tools}) != len(tools):
            raise BadRequest("Two tools have the same name.")

        budget = _number(o.get("max_budget_usd"), 5.0)
        builtin = o.get("builtin_tools", DEFAULT_BUILTIN_TOOLS)
        if not isinstance(builtin, list) or any(b not in KNOWN_BUILTIN_TOOLS for b in builtin):
            raise BadRequest("builtin_tools may only list WebSearch and WebFetch.")
        max_turns = o.get("max_turns")
        if max_turns is not None:
            if not isinstance(max_turns, int) or isinstance(max_turns, bool):
                raise BadRequest("max_turns must be a whole number.")
            max_turns = max(1, min(200, max_turns))
        app = o.get("app")
        if app is not None and (not isinstance(app, str) or not app.strip()):
            raise BadRequest('"app" must be a name.')
        return cls(
            model=model,
            system=need("system"),
            prompt=need("prompt"),
            history=history,
            tools=tools,
            max_budget_usd=max(0.1, min(50.0, budget)),
            builtin_tools=list(dict.fromkeys(builtin)),
            max_turns=max_turns,
            tool_timeout_s=max(5.0, min(600.0, _number(o.get("tool_timeout_s"), 60.0))),
            app=app.strip() if isinstance(app, str) else None,
            images=_pictures(o.get("images")),
            output_schema=_schema(o.get("output_schema")),
        )


def _number(v: Any, default: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return float(v)


# --- events, exactly as the Kotlin host sends them ---------------------------------------------------------------------

def text(delta: str) -> dict:
    return {"type": "text", "delta": delta}


def working(what: str) -> dict:
    return {"type": "working", "what": what}


def working_for_tool(tool: str) -> dict | None:
    if tool in ("WebSearch", "web_search"):
        return working("Searching the web…")
    if tool in ("WebFetch", "web_fetch"):
        return working("Reading a page…")
    return None


def done(ending: str, answer: str = "", usage: dict | None = None, cost: float | None = None, turns: int = 0,
         limits: dict | None = None, error_kind: str | None = None, structured: Any = None) -> dict:
    """The last event. [usage] is Claude Code's raw usage; [limits] its raw rate_limit_info; [structured] the answer
    in the requested shape (sent only when there is one)."""
    u: dict[str, int] = {}
    if usage:
        u = {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        }
        stu = usage.get("server_tool_use")
        if isinstance(stu, dict):
            u["searches"] = int(stu.get("web_search_requests") or 0)
            u["fetches"] = int(stu.get("web_fetch_requests") or 0)
    o: dict[str, Any] = {"type": "done", "ending": ending, "answer": answer, "usage": u, "turns": turns}
    if cost is not None and cost == cost:  # not NaN
        o["api_price_usd"] = cost
    if limits:
        windows = limits.get("unifiedWindows") or {}
        lim: dict[str, Any] = {"status": str(limits.get("status") or "")}
        for key in ("five_hour", "seven_day"):
            u_ = (windows.get(key) or {}).get("utilization")
            if isinstance(u_, (int, float)):
                lim[key] = u_
        o["limits"] = lim
    if error_kind:
        o["error_kind"] = error_kind
    if structured is not None:
        o["structured"] = structured
    return o


class Run:
    """One question in progress, from the moment it is asked until its `done` event."""

    def __init__(self, spec: RunSpec, app_id: str, app_name: str, device_id: str | None, device_label: str,
                 is_phone: bool):
        self.id = str(uuid.uuid4())
        self.spec = spec
        self.app_id = app_id
        self.app_name = app_name
        self.device_id = device_id
        self.device_label = device_label
        self.is_phone = is_phone
        self.asked_at = time.time()
        self.asked_mono = time.monotonic()
        self.started_mono: float | None = None
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = False
        self.close_ending = "stopped"
        self.close_kind: str | None = None
        self.result: dict | None = None  # the done event, once there is one
        self.finished = asyncio.Event()
        self.on_close: Callable[["Run"], None] | None = None  # set by the queue
        self._pending: dict[str, asyncio.Future] = {}
        self._calls = 0

    @property
    def queued_ms(self) -> int:
        end = self.started_mono if self.started_mono is not None else time.monotonic()
        return int((end - self.asked_mono) * 1000)

    def emit(self, event: dict) -> None:
        if self.result is None:
            self.events.put_nowait(event)

    def finish(self, done_event: dict) -> None:
        """Sends the done event (once) and marks the run over."""
        if self.result is not None:
            return
        done_event.setdefault("queued_ms", self.queued_ms)
        self.events.put_nowait(done_event)
        self.result = done_event
        self.finished.set()
        for f in self._pending.values():
            if not f.done():
                f.set_result({"content": [{"type": "text", "text": "The run has ended."}], "is_error": True})

    def close(self, ending: str = "stopped", error_kind: str | None = None) -> None:
        """Stops the run (stop request, dropped connection, Forget, time limit). Its done event says [ending]."""
        if self.closed or self.result is not None:
            return
        self.closed = True
        self.close_ending = ending
        self.close_kind = error_kind
        for f in self._pending.values():
            if not f.done():
                f.set_result({"content": [{"type": "text", "text": "The phone has stopped this run."}], "is_error": True})
        if self.on_close is not None:
            self.on_close(self)
        else:
            self.finish(done(ending, error_kind=error_kind))

    async def call_tool(self, name: str, tool_input: dict) -> dict:
        """Asks the phone or program to run a tool and waits for its answer (MCP-style result for the SDK)."""
        if self.closed or self.result is not None:
            return {"content": [{"type": "text", "text": "The phone has stopped this run."}], "is_error": True}
        self._calls += 1
        call_id = f"c{self._calls}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        self.emit({"type": "tool", "id": call_id, "name": name, "input": tool_input})
        try:
            return await asyncio.wait_for(fut, self.spec.tool_timeout_s)
        except asyncio.TimeoutError:
            secs = int(self.spec.tool_timeout_s)
            who = "phone" if self.is_phone else "app"
            return {"content": [{"type": "text", "text": f"The {who} did not answer within {secs} seconds."}],
                    "is_error": True}
        finally:
            self._pending.pop(call_id, None)

    def answer(self, call_id: str, body: Any) -> bool:
        """The answer to a tool call: {"text", "is_error"} or {"content": [text/image blocks], "is_error"}."""
        fut = self._pending.get(call_id)
        if fut is None or fut.done() or not isinstance(body, dict):
            return False
        content = []
        if isinstance(body.get("content"), list):
            for b in body["content"]:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    content.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "image" and isinstance(b.get("data"), str):
                    mime = b.get("mime_type") or b.get("mimeType") or "image/png"
                    content.append({"type": "image", "data": b["data"], "mimeType": mime})
        if not content:
            content = [{"type": "text", "text": str(body.get("text") or "")}]
        fut.set_result({"content": content, "is_error": bool(body.get("is_error"))})
        return True
