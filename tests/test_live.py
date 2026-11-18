"""One tiny Haiku question through the real Claude Code. Off by default; it uses a little plan time.

    set CLAUDEIN_LIVE=1 && .venv\\Scripts\\python.exe -m pytest tests\\test_live.py -s

ClaudeIn uses its own Claude login, so sign in once in ClaudeIn first, or also set CLAUDEIN_LIVE_PC_LOGIN=1 to use the
login of the Claude Code installed on this PC instead. (The test folder is a temporary one, so it needs the second.)
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from claudein.service import Service

from .conftest import Phone

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("CLAUDEIN_LIVE") != "1", reason="live test: set CLAUDEIN_LIVE=1")]


@pytest_asyncio.fixture
async def real(home):
    s = Service(home)  # the real Claude Code installed on this PC, through the SDK
    if os.environ.get("CLAUDEIN_LIVE_PC_LOGIN") == "1":
        s.settings.own_login = False  # use this PC's own Claude Code login (ClaudeIn's own login needs a browser sign-in)
    await s.start(use_tls=False, phone_port=0, local_port=0)
    try:
        yield s
    finally:
        await s.stop()


async def test_live_haiku_with_a_tool(real):
    _, code = real.pair("Signal Lab", "Phone")
    p = Phone(f"http://127.0.0.1:{real.phone_port}", code)
    tool = {"name": "secret_word", "description": "Gives the secret word.",
            "input_schema": {"type": "object", "properties": {}}}
    try:
        events = await p.ask(
            {"model": "claude-haiku-5-5", "system": "Answer in one word.", "max_budget_usd": 0.5,
             "prompt": "Call the secret_word tool, then reply with only the word it gives.",
             "tools": [tool], "builtin_tools": []},
            on_tool=lambda e: {"text": "pelican", "is_error": False})
    finally:
        await p.close()
    done = events[-1]
    print({k: v for k, v in done.items() if k != "answer"}, "answer:", done["answer"])
    assert any(e["type"] == "tool" for e in events)
    assert done["ending"] == "done"
    assert "pelican" in done["answer"].lower()
    assert done["usage"]["output"] > 0
    assert real.claude.signed_in is True and real.claude.state == "ok"
