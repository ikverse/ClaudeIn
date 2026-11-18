"""claudein_client: ask Claude through ClaudeIn, from a program on this PC.

    import claudein_client
    from claudein_client import Tool

    async for event in claudein_client.run(app="village", model="claude-opus-5-5", system="...", prompt="...",
                                           tools=[Tool("get_notes", "Reads the notes.", schema, handler)],
                                           builtin_tools=[], max_turns=20, max_budget_usd=2):
        ...                                   # Started, Text, Working, ServerTool, ToolCall, then Done(result)

    result = await claudein_client.ask(...)  # the same, just the result

ClaudeIn not running raises ClaudeInUnavailable ("PC not available"). There is no API-key fallback.
For tests, claudein_client.testing.FakeClaudeIn has the same run() and ask().
"""

from ._client import (ClaudeInError, ClaudeInUnavailable, Done, Event, Image, Result, RunState, ServerTool, Started,
                      Text, Tool, ToolCall, Working, ask, get_run, resume, run)

__version__ = "0.1.0"

__all__ = ["run", "ask", "get_run", "resume", "RunState", "Tool", "Image", "Result", "Event", "Started", "Text",
           "Working", "ServerTool", "ToolCall", "Done", "ClaudeInUnavailable", "ClaudeInError", "__version__"]
