# Connecting an app to ClaudeIn

This guide is for anyone, person or coding agent, who wants a project to use Claude through ClaudeIn. Read it before
writing any code that talks to Claude. The full wire format is in [`protocol.md`](protocol.md); this guide says which
parts you need and in what order.

## What ClaudeIn gives your app

- Claude answers, through the Claude Code the owner installed and signed in to inside ClaudeIn, on the owner's Claude
  plan. **No API keys, ever.**
- Streaming text, Claude's built-in web search and page reading, and **your own tools**: Claude asks, your app runs
  the tool and answers.
- A queue: a few runs at once (2 by default), the rest wait in order. You never get a "busy" refusal.
- Per-app usage records and an optional daily cap, both set by the owner in ClaudeIn's tray.

What it does **not** give: files on the PC, a shell, or Claude Code settings. Claude runs locked down. Your app's tools
are the only way Claude touches your app's data.

## Step 1: pick your route

| Your app is… | Route | Address | Who is asking |
|---|---|---|---|
| A **Python program on the same PC** | the `claudein_client` library (Step 2A) | `http://127.0.0.1:8791` | a token the library reads for you |
| A **phone or tablet app** (or a program on another computer) | the HTTP protocol directly (Step 2B) | `https://<PC address>:8790` | a pairing code, plus a pinned certificate |
| A **program on the same PC in another language** | the HTTP protocol directly (Step 2B, local part) | `http://127.0.0.1:8791` | the token in `%USERPROFILE%\.claudein\local.json` |

Never use port 8765 (it belongs to another app on this PC).

## Step 2A: Python program on the PC

### Install the client

The client has no dependencies and needs Python 3.11 or newer. Install it into **your project's own** environment:

```bat
<your project>\.venv\Scripts\python.exe -m pip install -e <path to ClaudeIn>\client
```

### Ask a question

```python
import asyncio
import claudein_client
from claudein_client import ClaudeInUnavailable

async def main():
    try:
        result = await claudein_client.ask(
            app="my-app",                      # required: your app's name (see "App names" below)
            model="claude-opus-5-5",
            system="You are a helpful assistant for My App.",
            prompt="Summarise today's notes.",
            builtin_tools=[],                  # [] = no web search; default is ["WebSearch", "WebFetch"]
            max_turns=10,
        )
    except ClaudeInUnavailable:
        print("PC not available")              # ClaudeIn isn't running or isn't set up
        return
    if result.ok:
        print(result.answer)
    else:
        print("Claude couldn't answer:", result.reason, result.error_kind)

asyncio.run(main())
```

### Give Claude your own tools

```python
from claudein_client import Tool, Image

NOTES_SCHEMA = {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}

def get_notes(args: dict):                     # a normal or an async function
    return f"Notes for {args['day']}: ..."     # text, Image(bytes, "image/png"), a list of both, or an MCP-style dict

result = await claudein_client.ask(
    app="my-app", model="claude-opus-5-5", system="...", prompt="What did I write on Monday?",
    tools=[Tool("get_notes", "Reads the notes for one day.", NOTES_SCHEMA, get_notes)],
    tool_timeout_s=60,                         # how long ClaudeIn waits for your handler (5-600)
)
```

- The library runs your handler and sends its answer back for you. A handler that raises becomes an error answer for
  Claude; the run goes on.
- Normal (non-async) handlers run on the event loop's own thread, so they can use objects that belong to that thread,
  such as a sqlite connection.

### Send pictures with the question

```python
from claudein_client import Image

result = await claudein_client.ask(
    app="my-app", model="claude-opus-5-5", system="...", prompt="What do these two charts show?",
    images=[Image(open("a.png", "rb").read()),                    # bytes or base64 text
            Image(open("b.jpg", "rb").read(), "image/jpeg")],     # png, jpeg, webp or gif
)
```

Up to 20 pictures, each at most 5 MB. Claude sees them in the order given, before the prompt.

### Get an answer your code can read

```python
SHAPE = {"type": "object",
         "properties": {"trend": {"type": "string", "enum": ["up", "down", "flat"]}, "why": {"type": "string"}},
         "required": ["trend", "why"]}

result = await claudein_client.ask(app="my-app", model="claude-opus-5-5", system="...", prompt="...",
                                   output_schema=SHAPE)
if result.ok:
    print(result.structured["trend"])          # already parsed; result.answer still has the text
elif result.error_kind == "format":
    ...                                        # Claude didn't answer in that shape: ask again, or simplify
```

The schema's top-level `type` must be `"object"`, and the whole schema at most 16 KB.

### Stream the answer as it is written

```python
from claudein_client import Started, Text, Working, ServerTool, ToolCall, Done

async for event in claudein_client.run(app="my-app", model="claude-opus-5-5", system="...", prompt="..."):
    if isinstance(event, Text):
        show(event.delta)                      # the answer, a piece at a time
    elif isinstance(event, Working):
        show_status(event.what)                # e.g. "Waiting for the PC (1 ahead)…", "Searching the web…"
    elif isinstance(event, Done):
        result = event.result
```

Leaving the loop early (`break`, then `aclose()`), or cancelling the task, stops the run on the PC too.

### What the result holds

| Field | Meaning |
|---|---|
| `ok` | `True` when `ending == "done"` |
| `ending` | `done`, `stopped`, `declined`, `rounds`, `cut short`, or `failed: <reason>` |
| `answer` | the whole answer text |
| `messages` | the answer split where Claude stopped to use a tool, in order |
| `reason` | the words after `failed: ` (or the ending itself); safe to show to a person |
| `error_kind` | what kind of failure (see Step 3), or `None` |
| `usage` | token counts: `input`, `output`, `cache_read`, `cache_write`, `searches`, `fetches` |
| `api_price_usd` | what the run would have cost at API prices (the plan pays, not this) |
| `turns`, `limits`, `queued_ms` | turns used, how full the Claude plan is (0-1), time spent waiting in line |
| `structured` | the parsed answer when you passed `output_schema`, else `None` |

### Test without ClaudeIn

```python
from claudein_client.testing import FakeClaudeIn, answer_text

fake = FakeClaudeIn().reply("Hello!").fail("rate_limited", "Claude plan limit reached; it resets at 14:00.")
result = await fake.ask(app="my-app", model="claude-opus-5-5", system="s", prompt="hi")   # -> "Hello!"

fake = FakeClaudeIn().reply("Done.", tools=[("get_notes", {"day": "Monday"})])           # Claude "calls" a tool
fake = FakeClaudeIn().unavailable()                                                       # raises ClaudeInUnavailable
fake = FakeClaudeIn().reply('{"trend": "up"}', structured={"trend": "up"})              # a shaped answer
fake = FakeClaudeIn().fail("format", "Claude didn't answer in the requested shape.")     # a shape failure
```

`fake.calls` records what was asked; `fake.tool_answers` records what your handlers answered (`answer_text(body)`
gets the text out). Make your code take the ask/run function as a parameter so tests can pass `fake.ask` /
`fake.run` instead.

## Step 2B: phone apps and other languages (the HTTP protocol)

### Connect

**Phones and tablets** (port 8790, HTTPS):

1. The owner clicks **Pair a device…** in ClaudeIn's tray, picks or types your app's name and a device name, and gets
   an address, a 10-character code and the end of a certificate fingerprint.
2. The owner types the address and code into your app. Your app calls `GET /hello` (no code needed), reads the
   server certificate, shows the last 6 characters of its SHA-256 fingerprint for the owner to compare, then **pins
   that certificate**. Accept no other certificate afterwards.
3. Every later request sends `Authorization: Bearer <code>` (case, spaces and dashes in the code don't matter).

- Each code is for **one app on one device**. Another device or another app needs its own pairing.
- Away from home, the owner pairs with the PC's **Tailscale address**, which works at home and away. ClaudeIn is never
  open to the internet.
- A code not used within 30 minutes of pairing expires. The owner can switch a device off or forget it at any time
  (your app then gets 401/403).

**Programs on the PC** (port 8791, plain HTTP, only from this PC): read `%USERPROFILE%\.claudein\local.json`
(`{"token": "...", "port": 8791}`) and send `Authorization: Bearer <token>`. If you get a 401, read the file again
once (the owner may have made a new token). Programs must send `"app"` on every run.

### The four calls you need

1. `GET /check` - is the PC there? `{"ok": true, "busy": false, "queue": 0, "features": [...]}`
2. `POST /runs` with `{"model", "system", "prompt"}` (plus optional `history`, `tools`, `builtin_tools`,
   `max_turns`, `max_budget_usd`, `tool_timeout_s`, `app`, `images`, `output_schema`). The answer is a stream of
   server-sent events: `run` (the id) first, then `working`, `text`, `server_tool`, `tool`, and `done` last.
3. `POST /runs/<run id>/tools/<call id>` with `{"text": "...", "is_error": false}` - your answer to each `tool`
   event, within `tool_timeout_s` seconds.
4. `POST /runs/<run id>/stop` with `{}` - stop a run.

Fields, limits, events and pictures in tool answers are all in [`protocol.md`](protocol.md), sections 2 and 3.
**Ignore event types you don't know**; new ones may be added. Lines starting with `:` are keep-alives.

**Pictures and set-shape answers** (protocol 2): look for `"images"` and `"output_schema"` in the `features` list of
`/hello` or `/check` first. Pictures go in `"images": [{"data": "<base64>", "mime_type": "image/png"}, ...]`; a
schema goes in `"output_schema"`, and the parsed answer comes back as `structured` in `done`. Details in
[`protocol.md`](protocol.md), section 2.

## Step 3: handle every way a run can end

A run always ends with one `done` event (the Python client turns it into `Result`). ClaudeIn's own refusals are
ordinary `done` events with `ending: "failed: <reason>"`, so showing `reason` to the person is always fine.

| `error_kind` | What happened | What your app should do |
|---|---|---|
| *(none)*, `ending: "done"` | answered | show the answer |
| `rate_limited` | the Claude plan's limit is reached; the reason says when it resets | wait until then; don't retry in a loop |
| `service` | Claude's service had a problem | try again later, a few times at most |
| `limit` | hit `max_turns` / `max_budget_usd`, or the run passed 15 minutes | ask a smaller question |
| `paused` | the owner paused ClaudeIn | say "the PC is paused" |
| `cap` | today's cap for your app is used up | wait until midnight (PC time) |
| `not_allowed` | the owner hasn't allowed your app to use that model | use another model, or ask the owner |
| `format` | you asked for `output_schema` and Claude didn't answer in that shape | ask again once, or simplify the schema |
| `unavailable` | Claude Code can't run (or can't do what you asked, such as `output_schema`), the line was full, or the run waited too long | say "PC not available" |

And before any run starts:

- **ClaudeIn can't be reached** (`ClaudeInUnavailable` in Python, a connection error otherwise): say **"PC not
  available"**. Never fall back to anything else.
- **400** - your request is wrong (the body says what). Fix the code.
- **401** - wrong code or token. Phones: ask the owner to pair again. Programs: re-read `local.json` once.
- **403** - this device is switched off, or `app` doesn't match the pairing.
- **429** - too many wrong codes from this address; blocked for an hour.

## Rules every connected app follows

1. **No API keys and no other route to Claude.** If ClaudeIn isn't there, the app says "PC not available". Don't add
   `anthropic` or `claude_agent_sdk` calls as a fallback.
2. **Always name your app.** Use one fixed name, e.g. `"app": "my-app"`. Names become ids (`"My App"` → `my-app`);
   the owner's usage view, caps and allowed models are per app.
3. **Model ids** are lower-case letters, digits, `.` and `-` (e.g. `claude-opus-5-5`). The owner may limit which
   models your app can use.
4. **Tool names** are letters, digits, `_` and `-`, up to 64 characters. Give each a clear description and a JSON
   schema.
5. **Don't pass Claude Code settings** (working folder, permission modes, MCP servers, settings files). ClaudeIn applies
   its own lock-down and the protocol has no fields for them. Everything Claude needs from your app goes in `system`,
   `prompt`, `history` or your tools.
6. **Keep secrets out of prompts and tool answers** unless Claude really needs them. ClaudeIn never logs prompts,
   answers or tool data, but they do reach Claude.
7. **Don't store the pairing code or token anywhere the user can't control**, and never in logs.
8. **Be patient.** A run may wait in line (you'll get `working` events), and phone questions go ahead of program
   questions. ClaudeIn itself ends a run that waits more than 10 minutes
   or runs more than 15, so don't cut a run off sooner on your side.

## A block to paste into your project's `CLAUDE.md`

So that your project's coding agent always uses ClaudeIn, add this to that project's `CLAUDE.md` (fix the paths and
the app name):

```markdown
## Claude access (ClaudeIn)

This project uses Claude only through ClaudeIn (a tray app on the owner's PC). Before writing code that talks to
Claude, read `<path to ClaudeIn>\docs\connecting-an-app.md`.
- Python on this PC: `claudein_client` (installed with `pip install -e <path to ClaudeIn>\client`), always with
  `app="<my-app>"`.
- Phones: ClaudeIn's HTTPS protocol on port 8790, paired with the tray's "Pair a device…".
- No API keys and no fallback. If ClaudeIn isn't reachable, show "PC not available".
- Handle every `error_kind` (rate_limited, service, limit, paused, cap, not_allowed, format, unavailable).
- Tests use `claudein_client.testing.FakeClaudeIn`, never the real ClaudeIn.
```

## Checklist: is my app connected properly?

- [ ] It uses the right route (client library on the PC, HTTPS + pairing on phones).
- [ ] Every run sends the same `app` name.
- [ ] Phones pin the certificate after the owner compares the fingerprint.
- [ ] "ClaudeIn not reachable" shows "PC not available", with no fallback.
- [ ] Every `error_kind` has a sensible message, and `rate_limited` / `cap` don't retry in a loop.
- [ ] Tool handlers answer within `tool_timeout_s`, and errors in them don't crash the app.
- [ ] `working` events are shown while the run waits or searches.
- [ ] The person can stop a long run (`/stop`, or leaving the `run()` loop).
- [ ] Apps that send pictures or ask for a set shape check `features` first (phone apps), keep pictures within 20 and
      5 MB each, and handle `error_kind: "format"`.
- [ ] Tests use `FakeClaudeIn` (or a fake server) and pass without ClaudeIn running.
- [ ] No codes, tokens or prompts end up in logs.
