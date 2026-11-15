"""A fake ClaudeIn for a program's own tests: no ClaudeIn, no Claude, no plan use.

    fake = FakeClaudeIn()
    fake.reply("Hello")                                       # a plain answer
    fake.reply("Done", tools=[("get_notes", {"day": 3})])     # calls the program's tool first, then answers
    fake.fail("rate_limited", "Claude plan limit reached; it resets at 14:00.")
    fake.unavailable()                                        # the next run raises ClaudeInUnavailable

    result = await fake.ask(app="village", model=..., system=..., prompt=..., tools=[...])
    fake.calls          # what each run was asked (the same fields run() sends)
    fake.tool_answers   # what the program's tools answered: (name, input, answer body)

fake.run and fake.ask take the same arguments as claudein_client.run and ask, so a program can pass them in where
it would pass the real ones (like Village's query_fn hook today). Unscripted runs answer "ok".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from ._client import (ClaudeInUnavailable, Done, Event, Result, Started, Text, Tool, ToolCall, call_handler)


@dataclass
class _Script:
    text: str = "ok"
    tools: list[tuple[str, dict]] = field(default_factory=list)
    ending: str = "done"
    error_kind: str | None = None
    unavailable: bool = False
    usage: dict = field(default_factory=lambda: {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0})
    limits: dict | None = None


class FakeClaudeIn:
    def __init__(self):
        self._queue: list[_Script] = []
        self.calls: list[dict] = []
        self.tool_answers: list[tuple[str, dict, dict]] = []

    # --- scripting -----------------------------------------------------------------------------------------------

    def reply(self, text: str, tools: list[tuple[str, dict]] | None = None, ending: str = "done",
              usage: dict | None = None, limits: dict | None = None) -> "FakeClaudeIn":
        s = _Script(text=text, tools=list(tools or []), ending=ending, limits=limits)
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
                  tool_timeout_s: float | None = None) -> AsyncIterator[Event]:
        s = self._queue.pop(0) if self._queue else _Script()
        if s.unavailable:
            raise ClaudeInUnavailable("PC not available: ClaudeIn isn't running.")
        self.calls.append({"app": app, "model": model, "system": system, "prompt": prompt, "history": history,
                           "tools": [t.name for t in tools or []], "builtin_tools": builtin_tools,
                           "max_turns": max_turns, "max_budget_usd": max_budget_usd,
                           "tool_timeout_s": tool_timeout_s})
        by_name = {t.name: t for t in tools or []}
        yield Started(f"fake-{len(self.calls)}")
        messages: list[str] = []
        for i, (name, tool_input) in enumerate(s.tools, 1):
            yield ToolCall(f"c{i}", name, tool_input)
            body = await call_handler(by_name.get(name), name, tool_input)
            self.tool_answers.append((name, tool_input, body))
        if s.text:
            yield Text(s.text)
            messages.append(s.text)
        yield Done(Result(ending=s.ending, answer=s.text, messages=messages, usage=dict(s.usage),
                          api_price_usd=0.0, turns=len(s.tools) + 1, limits=s.limits, error_kind=s.error_kind))

    async def ask(self, **kwargs) -> Result:
        async for e in self.run(**kwargs):
            if isinstance(e, Done):
                return e.result
        raise ClaudeInUnavailable("PC not available.")


def answer_text(body: dict) -> str:
    """The text of a tool answer body (for asserting in tests)."""
    return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")


__all__ = ["FakeClaudeIn", "answer_text"]
