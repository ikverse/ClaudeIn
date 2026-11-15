# Handover: moving Village onto ClaudeIn (phase 3, second half)

Written in ClaudeIn's project on 2026-11-15, for Village's project (`..\PlatformExperimental`). **Nothing here is approved
or built.** It must be listed and approved inside Village's project, by its own phase rules (plan section 5, phase 3).

## What ClaudeIn now offers

- ClaudeIn runs in the tray and answers programs on `127.0.0.1:8791` (token in `%USERPROFILE%\.claudein\local.json`).
- The Python client `claudein_client` lives in `ClaudeIn\client`. It needs no add-ons. Install it into Village's
  `.venv` with one line:
  `.venv\Scripts\python.exe -m pip install -e ..\ClaudeIn\client`
- How to use it: `ClaudeIn\docs\protocol.md`, section 3.4. A fake for tests: `claudein_client.testing.FakeClaudeIn`.

## Suggested shape of the change (for Village's own list)

Village today calls `claude_agent_sdk.query(prompt=..., options=ClaudeAgentOptions(...))` in `station/runner.py`,
`station/voice.py`, `agents/mainbrain/run.py`, `agents/glitch/run.py` and `agents/vector/…`, and its tests swap
`query` through the `query_fn` hook.

1. **One small adapter in Village**, for example `station/claudein_query.py`, with the same shape as `query`:
   `claudein_query(prompt=..., options=...)` yields the same SDK message types Village already reads
   (`AssistantMessage` with `TextBlock`/`ToolUseBlock`, `UserMessage` with `ToolResultBlock`, `ResultMessage`).
   Then `execute()` and the other callers keep their loops and only change which function they call. The `query_fn`
   test hook keeps working.
   - `options.model`, `max_turns`, `max_budget_usd` pass straight through. `tools=[]` becomes `builtin_tools=[]`.
   - `system_prompt={"type": "file", "path": ...}`: read the file and send its text as `system`.
   - The in-process tools (`create_sdk_mcp_server(SERVER, tools=_sdk_tools(...))`): the adapter takes the same
     `SdkMcpTool` list and turns each into `claudein_client.Tool(name, description, input_schema, handler)`. Village's
     handlers already return MCP-style dicts, which the client accepts as they are (text and pictures).
   - `app="village"` on every run, so ClaudeIn counts Village apart from the phones.
   - Options with no ClaudeIn meaning (`cwd`, `env`, `setting_sources`, `strict_mcp_config`, `permission_mode`,
     `max_buffer_size`) are dropped: ClaudeIn applies its own lock-down.
2. **Errors** (`station/errors.py`), from the client's `result.error_kind`:
   - `rate_limited` → `RateLimited`
   - `service` → `TransientError`
   - `limit` → `LimitHit`
   - `cap` (ClaudeIn's daily cap for Village) → treat like `RateLimited` until midnight, or a new error. Village decides.
   - Or simply build a `ResultMessage` with the matching `subtype` / `api_error_status`, so `_classify_error` stays
     as it is.
3. **ClaudeIn not running** (`ClaudeInUnavailable`): Village pauses its agent lanes the way it does for a rate limit,
   and says "ClaudeIn is not running".
4. **Costs:** `result.api_price_usd` stands in for `total_cost_usd`, and `result.usage` uses the names `input`,
   `output`, `cache_read`, `cache_write`. Village keeps its own USD caps; ClaudeIn's per-app cap (plan points) sits on
   top.
5. **Unchanged:** Village's station page stays on 8765. `station/tools/demo_run.py` keeps calling Claude Code
   directly (owner, 2026-11-15).

## Things to check in Village's project

- Model ids must be lower-case letters, digits, `.` and `-` (ClaudeIn refuses others with 400).
- Tool names may use letters, digits, `_` and `-` (up to 64).
- A tool call waits at most `tool_timeout_s` (default 60, up to 600) for its handler; pass a larger value if some
  Village tools are slow.
- Normal (non-async) handlers run on the event loop's thread, so Village's sqlite connection can be used in them as
  today.
- `result.messages` gives the text split where Claude stopped to use a tool. It is close to, but not exactly, the
  per-message text Village records today from `AssistantMessage` blocks.
