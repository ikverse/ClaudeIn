"""Runs one question through Claude Code, via the Claude Agent SDK, locked down as the Kotlin host does (plan 4.4).

Events and endings are worked out exactly as `ClaudeRunner.execute` in the Kotlin host, so phones see what they see
today. Additions for programs: `error_kind` on the done event, image tool answers, `max_turns`, `builtin_tools`.

The Claude Code it runs is the one the guard (claudecode.py) found and checked. A question the guard refuses, or a run
that stops without an answer, ends cleanly as a failed run; nothing here can take the service down.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from . import runs, winproc
from .claudecode import ClaudeCode, Unavailable, patch_sdk_parser
from .runs import Run, RunSpec

log = logging.getLogger("claudein")

BRIDGE_NAME = "signallab"  # the MCP server name the Kotlin host used, so tool names stay mcp__signallab__<name>

#: query_fn(prompt, options, tools) -> async iterator of SDK messages. Tests pass a fake; tools maps each tool's
#: name to its handler, so a fake can "call" tools the way Claude Code would.
QueryFn = Callable[[str, Any, dict[str, Callable[[dict], Awaitable[dict]]]], AsyncIterator[Any]]


def sdk_query(prompt: str, options: Any, tools: dict) -> AsyncIterator[Any]:
    from claude_agent_sdk import query

    return query(prompt=prompt, options=options)


def prompt_text(spec: RunSpec) -> str:
    """The question as Claude Code reads it: the conversation so far, then the question (as ClaudeCode.kt input())."""
    out = []
    if spec.history:
        out.append("Earlier in this conversation:\n\n")
        for t in spec.history:
            out.append(("The owner: " if t.role == "user" else "You: ") + t.text.strip() + "\n\n")
        out.append("The owner now asks:\n")
    out.append(spec.prompt)
    return "".join(out)


def reset_time(limits: dict | None) -> str | None:
    """"HH:MM" (PC time) when the plan limit resets, from Claude Code's rate-limit info."""
    if not limits:
        return None
    at = limits.get("resetsAt")
    if not isinstance(at, (int, float)):
        return None
    return time.strftime("%H:%M", time.localtime(at))


@dataclass
class _Attempt:
    done: dict
    got_result: bool  # Claude Code finished in its own words (even if the words were an error)
    touched: bool  # something already reached the caller (text or a tool call), so a retry could repeat it
    signed_out: bool


class Runner:
    def __init__(self, query_fn: QueryFn | None = None, work_root: Path | None = None,
                 claude: ClaudeCode | None = None):
        self.query_fn = query_fn or sdk_query
        self.work_root = work_root
        self.claude = claude
        self.last_limits: dict | None = None  # Claude Code's latest rate-limit info, for the tray and usage
        if query_fn is None:
            winproc.patch_sdk()
            patch_sdk_parser()

    @property
    def signed_in(self) -> bool | None:
        return self.claude.signed_in if self.claude is not None else None

    def build(self, run: Run, workdir: Path, stderr: Callable[[str], None], path: Path | None = None,
              on_tool: Callable[[], None] | None = None):
        """The SDK options and the tool handlers for [run]."""
        from claude_agent_sdk import ClaudeAgentOptions, SdkMcpTool, create_sdk_mcp_server

        spec = run.spec
        handlers: dict[str, Callable[[dict], Awaitable[dict]]] = {}
        sdk_tools = []
        for t in spec.tools:
            async def handler(args: dict, _name: str = t.name) -> dict:
                if on_tool is not None:
                    on_tool()
                return await run.call_tool(_name, args)

            handlers[t.name] = handler
            sdk_tools.append(SdkMcpTool(name=t.name, description=t.description, input_schema=t.input_schema,
                                        handler=handler))
        mcp = {BRIDGE_NAME: create_sdk_mcp_server(BRIDGE_NAME, "1", sdk_tools)} if sdk_tools else {}
        system_file = workdir / "system.txt"
        system_file.write_text(spec.system, encoding="utf-8")
        # Flags this Claude Code no longer has are left out (the lock-down ones are required, so the guard has already
        # refused the run if one is gone).
        gone = self.claude.missing_optional() if self.claude is not None else set()
        extra: dict[str, str | None] = {"restricted": None}
        if "--no-session-persistence" not in gone:
            extra["no-session-persistence"] = None
        options = ClaudeAgentOptions(
            model=spec.model,
            system_prompt={"type": "file", "path": str(system_file)},
            tools=list(spec.builtin_tools),
            allowed_tools=list(spec.builtin_tools) + [f"mcp__{BRIDGE_NAME}__{t.name}" for t in spec.tools],
            mcp_servers=mcp,
            strict_mcp_config=True,
            permission_mode="dontAsk",
            setting_sources=[],
            max_budget_usd=None if "--max-budget-usd" in gone else spec.max_budget_usd,
            max_turns=spec.max_turns,
            include_partial_messages="--include-partial-messages" not in gone,
            cwd=str(workdir),
            env=self.claude.env() if self.claude is not None else {"ANTHROPIC_API_KEY": ""},
            extra_args=extra,
            stderr=stderr,
            cli_path=str(path) if path is not None else None,
        )
        return options, handlers

    async def execute(self, run: Run) -> dict:
        """Runs [run] to its end; emits its events and returns its done event (not yet sent).

        If Claude Code stops without an answer before anything reached the caller, it is asked once more: that is what
        a hiccup, or an update that changed something, looks like. Never raises Claude Code's problems.
        """
        attempt: _Attempt | None = None
        for tries in (1, 2):
            path: Path | None = None
            if self.claude is not None:
                try:
                    path = await self.claude.ready()
                except Unavailable as e:
                    return runs.done("failed: " + e.message, error_kind="unavailable")
            attempt = await self._attempt(run, path)
            if attempt.got_result or attempt.touched or attempt.signed_out or run.closed or tries == 2:
                break
            log.info("Claude Code stopped without an answer; trying once more")
        assert attempt is not None
        if self.claude is not None:
            self.claude.note_run(attempt.got_result or attempt.signed_out)
            if attempt.signed_out:
                self.claude.note_signed_out()
            elif attempt.done.get("ending") in ("done", "cut short", "declined", "rounds"):
                self.claude.note_signed_in()
        return attempt.done

    async def _attempt(self, run: Run, path: Path | None) -> _Attempt:
        from claude_agent_sdk import (AssistantMessage, CLINotFoundError, RateLimitEvent, ResultMessage,
                                      ServerToolUseBlock, StreamEvent, TextBlock, ToolUseBlock)

        workdir = Path(tempfile.mkdtemp(prefix="claudein-", dir=self.work_root))
        errors: list[str] = []

        def stderr(line: str) -> None:
            errors.append(line)
            del errors[:-40]

        touched = False

        def tool_called() -> None:
            nonlocal touched
            touched = True

        job = winproc.Job()
        token = winproc.current_job.set(job)
        texts: list[str] = []
        messages = 0
        result: Any = None
        limits: dict | None = None
        refused = cut_short = False
        crash: BaseException | None = None
        try:
            options, handlers = self.build(run, workdir, stderr, path, tool_called)
            stream = self.query_fn(prompt_text(run.spec), options, handlers)
            try:
                async with contextlib.aclosing(stream) as messages_in:
                    async for m in messages_in:
                        if isinstance(m, StreamEvent):
                            e = m.event or {}
                            et = e.get("type")
                            if et == "message_start":
                                if messages > 0:
                                    run.emit(runs.text("\n\n"))
                                messages += 1
                            elif et == "content_block_delta":
                                d = e.get("delta") or {}
                                if d.get("type") == "text_delta":
                                    t = d.get("text") or ""
                                    if t:
                                        touched = True
                                    run.emit(runs.text(t))
                            elif et == "content_block_start":
                                b = e.get("content_block") or {}
                                if b.get("type") in ("tool_use", "server_tool_use"):
                                    w = runs.working_for_tool(b.get("name") or "")
                                    if w:
                                        run.emit(w)
                        elif isinstance(m, AssistantMessage):
                            if m.stop_reason == "refusal":
                                refused = True
                            elif m.stop_reason == "max_tokens":
                                cut_short = True
                            said = []
                            for b in m.content:
                                if isinstance(b, TextBlock):
                                    said.append(b.text)
                                elif isinstance(b, (ToolUseBlock, ServerToolUseBlock)) and not b.name.startswith("mcp__"):
                                    touched = True
                                    run.emit({"type": "server_tool", "name": b.name,
                                              "input": json.dumps(b.input or {}, separators=(",", ":"))})
                            s = "".join(said).strip()
                            if s:
                                texts.append(s)
                        elif isinstance(m, RateLimitEvent):
                            limits = m.rate_limit_info.raw or None
                        elif isinstance(m, ResultMessage):
                            result = m
            except CLINotFoundError as e:
                crash = e
            except Exception as e:  # Claude Code may exit with an error after its result; the result wins
                if result is None:
                    crash = e
        finally:
            winproc.current_job.reset(token)
            job.close()
            shutil.rmtree(workdir, ignore_errors=True)

        if limits:
            self.last_limits = limits
        done, signed_out = self._ending(result, texts, refused, cut_short, limits, crash, errors)
        return _Attempt(done, result is not None, touched, signed_out)

    def _ending(self, r: Any, texts: list[str], refused: bool, cut_short: bool, limits: dict | None,
                crash: BaseException | None, errors: list[str]) -> tuple[dict, bool]:
        """(the done event, whether Claude Code said it is signed out)."""
        from claude_agent_sdk import CLINotFoundError

        answer = "\n\n".join(texts) or ((r.result or "") if r is not None else "")
        usage = getattr(r, "usage", None) if r is not None else None
        cost = getattr(r, "total_cost_usd", None) if r is not None else None
        turns = getattr(r, "num_turns", 0) if r is not None else 0
        kind: str | None = None
        signed_out = False

        if r is None:
            if isinstance(crash, CLINotFoundError):
                ending = ("failed: Claude Code could not be started. Open ClaudeIn on the PC, go to Claude, and "
                          "press Check again.")
                kind = "unavailable"
            else:
                tail = " ".join(x.strip() for x in errors[-3:] if x.strip())[-600:]
                ending = f"failed: Claude Code stopped without an answer. {tail}".strip()
                signed_out = _signed_out(tail)
                kind = "unavailable" if signed_out else "service"
            return runs.done(ending, answer, None, None, 0, limits, kind), signed_out

        subtype = r.subtype or ""
        text = (r.result or "").strip()
        status = r.api_error_status
        if refused or r.stop_reason == "refusal":
            ending = "declined"
        elif subtype in ("error_max_budget_usd", "error_max_turns"):
            ending, kind = "rounds", "limit"
        elif r.is_error or subtype != "success":
            plan_hit = (status == 429 or (limits or {}).get("status") == "rejected"
                        or "usage limit" in text.lower() or "limit reached" in text.lower())
            if plan_hit:
                kind = "rate_limited"
                at = reset_time(limits)
                ending = (f"failed: Claude plan limit reached; it resets at {at}." if at
                          else "failed: " + (text or subtype)[:300])
            else:
                if _signed_out(text):
                    kind = "unavailable"
                    signed_out = True
                elif status is None or status >= 500 or "overloaded" in text.lower():
                    kind = "service"
                ending = "failed: " + (text or subtype)[:300]
        elif cut_short or r.stop_reason == "max_tokens":
            ending = "cut short"
        else:
            ending = "done"
        return runs.done(ending, answer, usage, cost, turns or 0, limits, kind), signed_out


def _signed_out(text: str) -> bool:
    t = text.lower()
    return "/login" in t or "not logged in" in t or "invalid api key" in t or "please log in" in t
