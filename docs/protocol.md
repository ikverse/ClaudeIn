# ClaudeIn protocol (version 1)

How an app talks to ClaudeIn. It is the Kotlin analyst host's protocol (`signal-lab-egx/analyst-host`) with optional
additions, so today's Signal Lab phones work unchanged apart from the address.

## 1. Where

| Who | Address | Security | Who is asking |
|---|---|---|---|
| Phones and tablets | `https://<PC address>:8790` | TLS with a self-made certificate the device pins by its SHA-256 | `Authorization: Bearer <pairing code>` |
| Programs on the PC | `http://127.0.0.1:8791` | never leaves the PC | `Authorization: Bearer <token>` from `%USERPROFILE%\.claudein\local.json` |

- The phone port only accepts this PC, home networks (10.x, 172.16–31.x, 192.168.x, fe80::/10, fc00::/7) and Tailscale
  (100.64.0.0/10, fd7a:115c:a1e0::/48). The local port only accepts this PC.
- `local.json` is `{"token": "...", "port": 8791}`. It is readable only by the owner's Windows account. "New local token"
  in the tray replaces it, so a program reads it again after a 401.
- **Pairing codes** are 10 characters (shown as `ABCDE-FGH23`; case, spaces and dashes don't matter). Each code belongs to
  one app on one device. A code not used within 30 minutes of pairing expires.
- **The certificate fingerprint** is the SHA-256 of the certificate's DER bytes, as upper-case hex. People compare the
  last 6 characters.
- If ClaudeIn can't be reached at all, the app says **"PC not available"**. There is no other route to Claude.

## 2. Routes

Every route except `/hello` needs the `Authorization` header. Bodies are JSON in UTF-8.

### `GET /hello`
No code needed. A device reads the certificate here while pairing.
```json
{"app": "Signal Lab analyst host", "protocol": 1, "claude": "2.1.325", "service": "ClaudeIn", "version": "0.2.0"}
```
`app` keeps the Kotlin host's name until both phone apps also accept `"ClaudeIn"`. New apps should look at `service`.
`claude` is missing when its version is unknown.

### `GET /check`
```json
{"ok": true, "busy": false, "queue": 0}
```
`busy` is true only when ClaudeIn is paused or the line is full. `queue` is how many runs are waiting.

### `POST /runs`
Asks one question. The answer is a stream of server-sent events (see 3).

```json
{
  "model": "claude-opus-5-5",
  "system": "You are …",
  "prompt": "What changed today?",
  "history": [{"role": "user", "text": "…"}, {"role": "assistant", "text": "…"}],
  "tools": [{"name": "get_prices", "description": "…", "input_schema": {"type": "object", "properties": {}}}],
  "max_budget_usd": 5,

  "app": "village",
  "builtin_tools": ["WebSearch", "WebFetch"],
  "max_turns": 20,
  "tool_timeout_s": 60
}
```

| Field | Needed | Meaning |
|---|---|---|
| `model` | yes | model id: lower-case letters, digits, `.` and `-` |
| `system` | yes | the system prompt (it replaces Claude Code's own) |
| `prompt` | yes | the question |
| `history` | no | earlier turns, words only; put in front of the question as the Kotlin host does |
| `tools` | no | tools the app runs itself (3.3). Names: letters, digits, `_`, `-`, up to 64 |
| `max_budget_usd` | no | 0.1–50, default 5. A ceiling at API prices; on a plan it only stops a runaway run |
| `app` | programs: yes; phones: no | which app is asking. From a phone it must match the pairing's app or the request gets 403 |
| `builtin_tools` | no | default `["WebSearch","WebFetch"]`; `[]` for none. Nothing else is allowed |
| `max_turns` | no | 1–200 |
| `tool_timeout_s` | no | 5–600, default 60: how long a tool call waits for the app's answer |

App names become ids: `"Signal Lab EGX"` → `signal-lab-egx`.

Errors before the stream starts: 400 `{"error": "Missing \"prompt\"."}` (bad request), 401 (wrong code or token),
403 (device switched off, or `app` doesn't match the pairing), 429 (address blocked after 20 wrong codes; one hour).
A wrong code waits a little longer each time, up to 5 s.

### `POST /runs/<id>/tools/<call>`
The app's answer to a tool call.
```json
{"text": "BTC 64,210", "is_error": false}
```
Or, with pictures (new):
```json
{"content": [{"type": "text", "text": "Here is the chart."},
             {"type": "image", "data": "<base64>", "mime_type": "image/png"}],
 "is_error": false}
```
Answers `{"ok": true}`, or 404 when no such call is waiting (it timed out, or the run ended). Only the device or program
that asked the run may answer.

### `POST /runs/<id>/stop`
Stops the run at once (Claude Code and everything it started end). Body `{}`. Answers `{"ok": true}` or 404. A run still
waiting in line just leaves the line.

## 3. The event stream

`Content-Type: text/event-stream`. Each event is one line `data: <JSON>` and a blank line. A comment line
`: still working` goes out every 5 seconds; if it can't be delivered, ClaudeIn stops the run. Apps must ignore event
types they don't know.

### 3.1 Events
| `type` | Fields | Meaning |
|---|---|---|
| `run` | `id` | always first: the run's id, for `/stop` and tool answers |
| `working` | `what` | something to show while waiting: `"Waiting for the PC (1 ahead)…"`, `"Searching the web…"`, `"Reading a page…"` |
| `text` | `delta` | the answer as it is written; `"\n\n"` between Claude's messages |
| `server_tool` | `name`, `input` (a JSON string) | Claude used a built-in tool such as WebSearch |
| `tool` | `id`, `name`, `input` (object) | the app must run this tool and answer at `/runs/<run id>/tools/<id>` |
| `done` | see 3.2 | always last |

### 3.2 `done`
```json
{"type": "done", "ending": "done", "answer": "…", "turns": 2,
 "usage": {"input": 12, "output": 340, "cache_read": 5000, "cache_write": 800, "searches": 1, "fetches": 0},
 "api_price_usd": 0.08,
 "limits": {"status": "allowed", "five_hour": 0.34, "seven_day": 0.12},
 "queued_ms": 0,
 "error_kind": "rate_limited"}
```
- `usage` may be `{}` and `api_price_usd`, `limits` and `error_kind` may be missing (for example on a refusal).
- `limits` is for the whole Claude plan: `five_hour` and `seven_day` are 0–1.
- `queued_ms` (new) is how long the run waited in line.

**Endings** (as the Kotlin host):

| `ending` | Meaning |
|---|---|
| `done` | answered |
| `stopped` | stopped by `/stop`, a dropped connection, or the owner forgetting the device |
| `declined` | Claude declined to answer |
| `rounds` | the run reached `max_budget_usd` or `max_turns` |
| `cut short` | the answer hit the length limit |
| `failed: <reason>` | anything else; show the reason to the person |

**`error_kind`** (new) says what kind of failure it was, so programs can react:

| `error_kind` | When | What an app might do |
|---|---|---|
| `rate_limited` | the Claude plan's limit is reached (ending says when it resets) | wait until then |
| `service` | Claude's service had a problem, or Claude Code stopped without an answer | try again later |
| `limit` | `rounds`, or the run lasted longer than 15 minutes | ask a smaller question |
| `paused` | the owner paused ClaudeIn | say the PC is paused |
| `cap` | today's cap for this app is used up (phase 2) | wait until midnight |
| `not_allowed` | the owner hasn't allowed this app to use that model | use another model |
| `unavailable` | Claude Code can't run (not installed, won't start, too old, not compatible with ClaudeIn yet, or signed out), the line was full, or the run waited too long. The ending says which, in words for the owner | say the PC is not available |

**ClaudeIn's own refusals** are normal `done` events, so phones show them unchanged:
- `failed: ClaudeIn is paused on the PC.`
- `failed: The PC has too many questions waiting.`
- `failed: The PC was busy for too long. Try again.`
- `failed: <app> isn't allowed to use <model>.`
- `failed: Claude plan limit reached; it resets at HH:MM.`
- `failed: Today's limit for <app> is used up (resets at midnight).` (phase 2)

### 3.2.1 Daily caps (phase 2)
The owner may give an app a daily cap in **plan points** (100 points = one full 5-hour window of the Claude plan). Caps
are off unless the owner switches one on. Once an app's points for the day reach its cap, its new runs end at once with
`failed: Today's limit for <app> is used up (resets at midnight).` and `error_kind: "cap"`; runs already going finish.
The check is made when a run is asked and again just before a waiting run starts. Days run midnight to midnight, PC
time.

Points are estimates, because Claude Code only reports how full the whole plan's 5-hour window is (`limits.five_hour`).
After each run, the rise since the previous report goes to the runs that ended in between, split by their tokens, but
only if ClaudeIn was busy the whole time since that report (no idle gap over 30 seconds), so the owner's own Claude use
while ClaudeIn sat idle isn't charged to an app. If the number went down (the window reset), the new number counts as
the rise. If it didn't move, or the rise isn't trusted, a run's points come from its tokens at the measured
points-per-token rate: today's, else the most recent day's in the last 7 days, else 0. Points aren't sent to apps.

### 3.3 Tools
When Claude calls one of the app's tools, ClaudeIn sends a `tool` event and waits up to `tool_timeout_s` seconds for the
answer. With no answer in time, Claude is told "The phone did not answer within 60 seconds." (or "The app …") and goes on.

## 3.4 Using the Python client (programs on this PC)

Python programs don't need to speak this protocol themselves. The `claudein_client` package (in ClaudeIn's `client\`
folder) does it, with no add-ons to install. Install it into the program's own environment with:
`pip install -e <path to ClaudeIn>\client`

```python
import claudein_client
from claudein_client import Tool, Image

async def get_notes(args):                 # a normal or an async function
    return "the notes"                     # text, Image(...), a list of both, or an MCP-style dict

async for event in claudein_client.run(app="village", model="claude-opus-5-5", system="...", prompt="...",
                                       tools=[Tool("get_notes", "Reads the notes.", schema, get_notes)],
                                       builtin_tools=[], max_turns=20, max_budget_usd=2):
    ...   # Started, Text, Working, ServerTool, ToolCall, then Done(result)

result = await claudein_client.ask(...)    # the same, just the result
result.ok, result.ending, result.answer, result.messages, result.usage, result.api_price_usd,
result.turns, result.limits, result.error_kind, result.reason
```
- It reads `local.json` for the token and port, and reads it again once if ClaudeIn says the token is wrong.
- It runs the program's tool handlers and sends their answers back. A handler that crashes becomes an error answer for
  Claude. A normal (non-async) handler runs on the event loop's own thread.
- `result.messages` is the answer split where Claude stopped to use a tool, in order.
- `ClaudeInUnavailable` means "PC not available" (ClaudeIn isn't running or set up). `ClaudeInError` means ClaudeIn
  refused the request outright (400, 401, 403).
- Stopping early (`break` with `aclose()`, or cancelling the task) stops the run on the PC too.
- For tests, `claudein_client.testing.FakeClaudeIn` has the same `run()` and `ask()`, scripted with `reply()`,
  `fail()` and `unavailable()`.

## 4. Queue

ClaudeIn runs a few questions at once (2 by default; the owner can pick 1–4). The rest wait in order, and phone questions
go ahead of waiting program questions. A waiting run gets `working` events with its place in line. At most 20 wait; a run
waits at most 10 minutes; a run lasts at most 15 minutes. **There is no 409 any more.**

## 5. What ClaudeIn never keeps

Logs and usage records hold only: time, app, device, model, wait, duration, the kind of ending, and token counts. Never
codes, tokens, prompts, answers or tool data.
