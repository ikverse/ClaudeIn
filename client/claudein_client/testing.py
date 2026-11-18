"""A fake ClaudeIn for a program's own tests: no ClaudeIn, no Claude, no plan use.

    fake = FakeClaudeIn()
    fake.reply("Hello")                                       # a plain answer
    fake.reply("Done", tools=[("get_notes", {"day": 3})])     # calls the program's tool first, then answers
    fake.reply('{"up": true}', structured={"up": True})  # an answer in the shape output_schema asked for
    fake.fail("rate_limited", "Claude plan limit reached; it resets at 14:00.")
    fake.fail("format", "Claude didn't answer in the requested shape.")
    fake.unavailable()                                        # the next run raises ClaudeInUnavailable

    result = await fake.ask(app="village", model=..., system=..., prompt=..., tools=[...])
    fake.calls          # what each run was asked (the same fields run() sends)
    fake.tool_answers   # what the program's tools answered: (name, input, answer body)

    state = await fake.get_run(run_id)              # every fake run is kept, as a detached run is
    async for event in fake.resume(run_id, after=2): ...   # its events after number 2

fake.run and fake.ask take the same arguments as claudein_client.run and ask, so a program can pass them in where
it would pass the real ones (like Village's query_fn hook today). Unscripted runs answer "ok".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ._client import (ClaudeInError, ClaudeInUnavailable, Done, Event, Image, Result, RunState, Started, Text, Tool,
                      ToolCall, call_handler)


@dataclass
class _Script:
    text: str = "ok"
    tools: list[tuple[str, dict]] = field(default_factory=list)
    ending: str = "done"
    error_kind: str | None = None
    unavailable: bool = False
    usage: dict = field(default_factory=lambda: {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0})
    limits: dict | None = None
    structured: Any = None


class FakeClaudeIn:
    def __init__(self):
        self._queue: list[_Script] = []
        self.calls: list[dict] = []
        self.tool_answers: list[tuple[str, dict, dict]] = []
        self.runs: dict[str, list[Event]] = {}  # every run's events so far, by run id

    # --- scripting -----------------------------------------------------------------------------------------------

    def reply(self, text: str, tools: list[tuple[str, dict]] | None = None, ending: str = "done",
              usage: dict | None = None, limits: dict | None = None, structured: Any = None) -> "FakeClaudeIn":
        s = _Script(text=text, tools=list(tools or []), ending=ending, limits=limits, structured=structured)
        if usage is not None:
            s.usage = usage
        self._queue.append(s)
        return self

    def fail(self, error_kind: str, reason: str) -> "FakeClaudeIn":
        """A run that ends with "failed: <reason>" and that error_kind."""
        self._queue.append(_Script(text="", ending=f"failed: {reason}", error_kind=error_kind, usage={}))
        return self

    def unavailable(self) -> "FakeClaudeIn":
        self._queue.append(_Script(unavailable=True))
        return self

    # --- the same calls as claudein_client ------------------------------------------------------------------------

    async def run(self, *, app: str, model: str, system: str, prompt: str, history: list[dict] | None = None,
                  tools: list[Tool] | None = None, builtin_tools: list[str] | None = None,
                  max_turns: int | None = None, max_budget_usd: float | None = None,
                  tool_timeout_s: float | None = None, images: list[Image] | None = None,
                  output_schema: dict | None = None, detach: bool = False,
                  priority: str | None = None) -> AsyncIterator[Event]:
        if detach and tools:
            raise ClaudeInError(400, "\"detach\" can't be used with \"tools\".")
        if priority not in (None, "interactive", "background"):
            raise ClaudeInError(400, "\"priority\" must be \"interactive\" or \"background\".")
        s = self._queue.pop(0) if self._queue else _Script()
        if s.unavailable:
            raise ClaudeInUnavailable("PC not available: ClaudeIn isn't running.")
        self.calls.append({"app": app, "model": model, "system": system, "prompt": prompt, "history": history,
                           "tools": [t.name for t in tools or []], "builtin_tools": builtin_tools,
                           "max_turns": max_turns, "max_budget_usd": max_budget_usd,
                           "tool_timeout_s": tool_timeout_s, "images": list(images or []),
                           "output_schema": output_schema, "detach": detach, "priority": priority})
        by_name = {t.name: t for t in tools or []}
        run_id = f"fake-{len(self.calls)}"
        log: list[Event] = []
        self.runs[run_id] = log

        def note(e: Event) -> Event:
            e.seq = len(log) + 1
            log.append(e)
            return e

        yield note(Started(run_id))
        messages: list[str] = []
        for i, (name, tool_input) in enumerate(s.tools, 1):
            yield note(ToolCall(f"c{i}", name, tool_input))
            body = await call_handler(by_name.get(name), name, tool_input)
            self.tool_answers.append((name, tool_input, body))
        if s.text:
            yield note(Text(s.text))
            messages.append(s.text)
        yield note(Done(Result(ending=s.ending, answer=s.text, messages=messages, usage=dict(s.usage),
                               api_price_usd=0.0, turns=len(s.tools) + 1, limits=s.limits, error_kind=s.error_kind,
                               structured=s.structured if s.ending == "done" else None)))

    async def ask(self, **kwargs) -> Result:
        async for e in self.run(**kwargs):
            if isinstance(e, Done):
                return e.result
        raise ClaudeInUnavailable("PC not available.")

    async def get_run(self, run_id: str) -> RunState:
        log = self._log(run_id)
        done = next((e for e in log if isinstance(e, Done)), None)
        return RunState(run_id, "done" if done else "running", done.result if done else None, 0, len(log))

    async def resume(self, run_id: str, after: int = 0) -> AsyncIterator[Event]:
        for e in list(self._log(run_id)):
            if e.seq > after:
                yield e

    def _log(self, run_id: str) -> list[Event]:
        if run_id not in self.runs:
            raise ClaudeInError(404, "No such run (it ended over an hour ago, or ClaudeIn restarted).")
        return self.runs[run_id]


def answer_text(body: dict) -> str:
    """The text of a tool answer body (for asserting in tests)."""
    return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


__all__ = ["FakeClaudeIn", "answer_text"]
