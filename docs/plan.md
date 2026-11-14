# ClaudeIn: plan

Status: **plan only, nothing built.** Written 2026-11-15. Every phase below needs its own "approve" before any code is
written (see `CLAUDE.md`).

## 1. What ClaudeIn is, in plain words

ClaudeIn is one small program that sits in the Windows tray (the hidden icons by the clock) and lets the owner's apps use
Claude through the Claude Code login already on this PC. No API keys anywhere.

- The two Signal Lab phone apps (crypto and EGX) talk to it over the home Wi-Fi, or over Tailscale when away from home.
- Programs on this PC (Village first, future apps later) talk to it directly on the PC.
- It runs a few questions at once (2 by default) and queues the rest in order. No more "the PC is busy".
- It counts what each app uses (questions, tokens, plan-limit use, how runs ended) and can stop an app for the day once
  it reaches a daily cap the owner sets.
- When ClaudeIn is not running, apps say "PC not available". They never fall back to an API key.

It replaces the Kotlin analyst host in `signal-lab-egx/analyst-host/` and the hand-started `tools\start-analyst-host.cmd`.

## 2. What exists today (read, not changed)

**The analyst host** (`signal-lab-egx/analyst-host/…/host/`):
- `PhoneServer.kt`: HTTPS on port 8765, default for both phones. Routes:
  - `GET /hello` (no code needed) answers `{"app": "Signal Lab analyst host", "protocol": 1, "claude": "<version>"}`.
  - `GET /check` answers `{"ok": true, "busy": …}`.
  - `POST /runs` takes a RunSpec and streams server-sent events (SSE). The first event is `{"type":"run","id":…}`,
    and a `: still working` comment goes out every 5 s.
  - `POST /runs/<id>/tools/<call>` carries the phone's tool answer `{"text", "is_error"}`.
  - `POST /runs/<id>/stop` stops a run.
  - Every route except `/hello` needs `Authorization: Bearer <pairing code>`. A wrong code waits a little longer each time.
  - Only one run at a time. A second one gets **409**.
- `Run.kt`: RunSpec = `model`, `system`, `prompt`, `history[{role,text}]`, `tools[{name,description,input_schema}]`,
  `max_budget_usd` (0.1–50, default 5). A tool call is sent to the phone as `{"type":"tool","id","name","input"}` and
  waits up to 60 s for the answer.
- `ToolBridge.kt`: a loopback-only MCP server that shows the phone's tools to Claude Code.
- `ClaudeCode.kt`: runs `claude -p --output-format stream-json --verbose --include-partial-messages` with the app's
  system prompt in place of Claude Code's own. Only WebSearch and WebFetch plus the bridge's tools are allowed. It uses
  `--restricted`, `--strict-mcp-config`, `--permission-mode dontAsk`, `--max-budget-usd` and `--no-session-persistence`.
  Events sent back:
  - `text` {delta}
  - `working` {what}
  - `server_tool` {name,input}
  - `tool`
  - `done` {ending, answer, usage{input,output,cache_read,cache_write,searches,fetches}, turns, api_price_usd,
    limits{status,five_hour,seven_day}}

  Endings are `done`, `stopped`, `declined`, `rounds`, `cut short` and `failed: …`.
- `HostConfig.kt`: **one shared pairing code** for every phone, and a self-made EC certificate the phone pins by its
  SHA-256 (the phone shows the last 6 characters).

**The phones** (`HostClient.kt`, `HostPairing.kt`, `EgxHost.kt`):
- Default port 8765. An address may carry `:port` and may be a name such as `my-pc.local`.
- When pairing, they check that `/hello` says `"app": "Signal Lab analyst host"`, pin the certificate and verify the code.
- They ignore event types they don't know, and they show `working` text as it arrives.
- A `failed: …` ending is shown as "The analyst could not be reached: …". EGX turns a 409 into "The PC is answering
  another question."

**Village** (`PlatformExperimental`):
- Calls Claude through the **Claude Agent SDK** (`claude_agent_sdk.query`), in `station/runner.py`, `station/voice.py`,
  `agents/mainbrain/run.py`, `agents/glitch/run.py` and `agents/vector/…`. Its tools are Python functions served
  in-process (`create_sdk_mcp_server`).
- Its runs use: no built-in tools, the system prompt from a file, `max_turns`, `max_budget_usd`, `ANTHROPIC_API_KEY`
  blanked, and `setting_sources=[]`.
- It classifies errors (rate limited, service error, limit hit).
- Its own daily caps are in USD at API prices.
- `station/tools/demo_run.py` separately runs plain `claude -p` with file tools inside a throwaway folder.
- Its station page is on port 8765, and its phone page goes over Tailscale (`tailscale serve --bg 8765`), so Tailscale is
  already part of how the owner works.

## 3. Decisions already made by the owner (2026-11-15)

1. Users: Signal Lab crypto, Signal Lab EGX, Village and future apps. So we need a written protocol and a small client
   library.
2. Phones work at home and away from home, over a private network (Tailscale). No port is opened to the internet.
3. ClaudeIn not running means the app says "PC not available". No API-key fallback.
4. A few runs at once (2 by default, can be changed); the rest wait in order.
5. A usage view per app, with optional daily caps on the apps the owner picks.
6. Village moves onto ClaudeIn, in its own later phase approved inside Village's project.
7. Python. A tray icon that starts with Windows with no console window, set up like Village (a setup .bat, a `.venv`
   and a shortcut).
8. Its own project, in this folder, named ClaudeIn.

## 4. Design

### 4.1 Ports and addresses

- **Phones:** HTTPS on **port 8790** on every network address of the PC. This doesn't clash with Village's 8765.
- **Programs on this PC:** plain HTTP on **127.0.0.1:8791** only. That traffic never leaves the PC.
- Both ports can be changed in the settings file, in case something else ever uses them.
- **Who may connect at all.** ClaudeIn drops any connection that doesn't come from one of these:
  - this PC (loopback)
  - a home network (10.x, 172.16–31.x, 192.168.x)
  - Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48)

  This check comes on top of the code and TLS. The router opens nothing, so nothing on the internet can reach it.

### 4.2 Protocol (compatible with today's phones)

All of today's analyst-host routes, events and endings stay exactly as they are, so both phone apps work after one change:
the address gets `:8790` on the end, which means re-pairing once.

- `GET /hello` keeps answering `"app": "Signal Lab analyst host"` so today's phones accept it. It adds
  `"service": "ClaudeIn"` and `"version"`. Once both apps also accept `"app": "ClaudeIn"` (a later app change), ClaudeIn
  can switch. Until then the old name stays.
- `GET /check` keeps `ok`. `busy` is now true only when the queue is full or ClaudeIn is paused. It adds `queue`
  (how many are waiting).
- **No more 409.** A run that can't start yet still gets its SSE stream at once and waits in line. Every few seconds it
  gets a `working` event such as "Waiting for the PC (1 ahead)…". Today's phones already show that text.
- **Which app made a run.** For a phone, this comes from its pairing: each pairing is one app on one device (see 4.3),
  so today's phones need no change to be counted per app. A request may also carry `"app": "<name>"`. From a phone it must
  match the pairing (if it doesn't, the request is refused). From a PC program it is required and names the app.
- **Optional additions to the RunSpec**, ignored by today's phones and needed for Village and future apps:
  - `builtin_tools` (default `["WebSearch","WebFetch"]`; Village sends `[]`)
  - `max_turns`
  - `tool_timeout_s` (default 60)
- **Tool answers** may also carry image content (Village tools sometimes return a picture), not only `text`.
- **Additions to the `done` event**: `queued_ms`, and `error_kind` (`rate_limited` | `service` | `limit` | `paused` |
  `cap` | `unavailable`) so programs can react the way Village does today.
- **ClaudeIn's own refusals** are normal `done` endings, so phones show them with no change:
  - `failed: ClaudeIn is paused on the PC.`
  - `failed: Today's limit for <app> is used up (resets at midnight).`
  - `failed: Claude plan limit reached; it resets at HH:MM.`
- The full protocol is written down in `docs/protocol.md` in phase 1. That document is what future apps follow.

### 4.3 Pairing and access

- **One code per pairing, not one shared code.** "Pair a device…" in the tray asks for the app (Signal Lab, Signal Lab
  EGX, or a new name) and a device name ("Phone", "Tablet"). It then opens a small window showing:
  - the address to type (the home Wi-Fi address and, once Tailscale is on, the Tailscale address), with `:8790`
  - a new 10-character code (shown as two groups of five, as today)
  - the end of the certificate's fingerprint to compare on the phone
  - a **QR code** holding all of these, ready for when the apps learn to scan it (a later app change; typing works today)

  The window turns green when the phone first uses the code.
- **Codes are kept only as a SHA-256 hash.** ClaudeIn can check a code but cannot show it again. A lost code means
  "Forget" that device and pair it again. A code that is never used expires after 30 minutes.
- **Per-device on/off and Forget.** Off refuses that device's runs until it is switched back on. Forget deletes its code
  for good, and its running runs stop at once.
- **Wrong codes are slowed down.** Each wrong code adds a delay of up to 5 s, as today, and 20 wrong codes in a row from
  one address block that address for an hour.
- **Programs on the PC don't pair.** ClaudeIn writes a random token to `%USERPROFILE%\.claudein\local.json` (token and
  port), readable only by the owner's Windows account. A program reads it and sends `Authorization: Bearer <token>` to
  127.0.0.1:8791. "New local token" in the tray replaces it. The token is accepted only on the loopback port, never from
  the network.
- **The TLS certificate** is self-made on first start (EC P-256, 10 years), like the Kotlin host's. It is a new
  certificate, so both phones pair once again anyway (the port change already needs that).

### 4.4 Runner

- Claude Code is run through the **Claude Agent SDK for Python**. That is the same library Village already uses
  successfully with the Claude login on this PC, and it runs the same `claude` program the Kotlin host runs. It gives us:
  - the **tool bridge in-process** (`create_sdk_mcp_server`): a tool call waits for the phone's or program's answer,
    with no separate HTTP bridge server to keep safe
  - streaming text (`include_partial_messages`)
  - the plan-limit events (`rate_limit_event`)
  - the system prompt passed in a file, which avoids Windows' 32,000-character command-line limit that the Kotlin host
    works around
- **Same lock-down as the Kotlin host:**
  - only the built-in tools the run asks for (WebSearch and WebFetch for phones, none for Village)
  - only that run's own MCP tools
  - `permission_mode="dontAsk"`
  - `setting_sources=[]` (none of the owner's own Claude Code settings, CLAUDE.md, plugins or hooks)
  - `strict_mcp_config`
  - no saved session
  - `max_budget_usd` and `max_turns` from the request
  - `ANTHROPIC_API_KEY` blanked, so the Claude login is always used
  - run in an empty temporary folder

  If an SDK option for one of the Kotlin flags (`--restricted`) is missing, it is passed as an extra command-line flag.
- **The conversation history** is put in front of the prompt exactly as `ClaudeCode.kt` `input()` does it.
- **Events and endings** are worked out the same way as `ClaudeRunner.execute`, so phones see exactly what they see today.
- **Queue:** one queue with **N workers (2 by default; 1–4 in the tray)**. **Phone questions go first** (owner,
  2026-11-15): a waiting phone run starts before any waiting program run, and within each group runs keep their order.
  A program's run already going is never stopped for a phone. At most 20 runs wait. More
  than that get `failed: The PC has too many questions waiting.`
- **Stopping:**
  - `POST /stop`, a dropped connection (noticed at the next 5-second keep-alive) or Forget on its device each stop the
    run, kill Claude Code and its child processes, and free the worker.
  - A run that hasn't started yet just leaves the queue.
  - A run that has lasted longer than 15 minutes (can be changed) is stopped.
- **Plan limit hit:** the run ends with `error_kind: rate_limited` and the reset time. The tray shows it. Waiting runs
  stay queued and are tried in turn.
- **One copy only:** a second start of ClaudeIn sees the first one running, says so in a balloon, and exits.

### 4.5 Usage and caps

- A small SQLite file, `%USERPROFILE%\.claudein\usage.db`, gets one row per run: app, device, model, when it was asked,
  how long it waited and ran, how it ended, tokens (input, output, cache read, cache write), web searches and fetches,
  API-price equivalent in USD, and the 5-hour and 7-day plan-limit use reported at its end. **Never the question, the
  answer or tool data.**
- **Usage window** (from the tray), per app:
  - questions, tokens and API-price USD for today and the last 7 days
  - the plan-limit numbers Claude Code last reported (these are for the whole plan, not per app; each app's share is
    shown as its share of today's tokens)
  - its last 20 runs, with time, model, how long and how it ended
- **Caps (phase 2):** an optional daily cap per app, which is off unless the owner turns it on. **It counts share of the
  Claude plan's limit** (owner, 2026-11-15).
  - **How the share is measured.** After every run, Claude Code reports how full the plan's 5-hour window is, for the
    whole plan.
    - ClaudeIn puts the rise since the previous report down to the runs that ended in between.
    - When runs overlapped, the rise is split by their tokens.
    - When the reported number didn't move (small runs, rounding), the share comes from the run's tokens, at the rate
      seen over that day.
  - **The unit is "plan points".** 100 points = one full 5-hour window. A cap reads, for example, "Village: at most 60
    plan points a day". The usage window shows each app's points for today and the week.
  - These are estimates, because Claude reports only the plan's total. The usage window says so.
  - When an app reaches its cap, its new runs end with the cap message until midnight. Runs already going finish.

### 4.6 Tray icon, panel and windows (owner, 2026-11-15)

**Look.** Windows 11 Fluent style throughout:
- **Acrylic frosted-glass** backgrounds that blur whatever is behind them, like the Start menu and quick settings
- Fluent on/off switches, rounded cards, and Segoe Fluent Icons
- follows Windows light/dark mode and the owner's accent colour
- turns solid on its own when Windows turns the glass off: the window isn't in front, "Transparency effects" is off,
  or battery saver is on

**Tray icon.** It changes colour by state (normal, working, paused, problem: Claude Code missing or signed out, or a
port in use) and stays readable on a light or dark taskbar. Hovering shows the status line.

**Left-click: the ClaudeIn panel.** A frosted-glass panel above the clock, like Windows 11's quick-settings panel:
```
[icon] ClaudeIn
       Running · 1 working, 0 waiting
[ Running / Paused ]  [ Start with Windows ]      (toggle tiles)
Plan · 5-hour window  ▓▓▓░░░░░░  34% used · week 12%
Claude Code 2.x — signed in ✓
──────────
Pair    Usage    Settings
```
"Paused" means new runs are refused and running ones finish.

**Right-click: a short menu**, also frosted glass: Open ClaudeIn, Pause all, Quit.

**Settings window**, frosted glass, with a side menu:
- **Home:** status, plan use, runs working and waiting.
- **Devices:** one card per pairing (Signal Lab — Phone, Signal Lab EGX — Phone, Village (this PC)), each with an
  on/off switch and Forget…, plus "Pair a device".
- **Usage:** the usage view from 4.5.
- **General:** runs at once (1–4), Start with Windows, Open logs folder, New local token, and Check Claude Code (asks one
  tiny Haiku question to confirm the login).
- **Caps** are added here in phase 2.

The pairing window (4.3) uses the same glass.

- "Signed in ✓" comes from the last run that worked or from "Check Claude Code". ClaudeIn never sends a question on its
  own at start, so it uses no plan time just by running.
- The panel and windows are real desktop windows made with **PySide6 and PySide6-Fluent-Widgets**. They are not web
  pages, so there is no extra page that other programs on the PC could reach. PySide6's own tray icon is used.
- PySide6-Fluent-Widgets is free for personal use (GPL licence). That's fine because ClaudeIn stays on this PC and is
  not sold or shared. PySide6 adds about 150 MB to `.venv`.

### 4.7 Safety

- It only listens on home and Tailscale addresses and on loopback. Nothing is reachable from the internet (no
  port-forward, no Tailscale Funnel).
- It uses HTTPS with a pinned certificate for phones, and a separate code for each device that can be revoked. Codes are
  stored only as hashes. The local token is readable only by the owner's account.
- Claude Code runs locked down (4.4): no shell, no files, none of the owner's settings, only the tools the run lists.
- **No secrets in logs.** Logs (`%USERPROFILE%\.claudein\logs\`, kept 14 days) record only: time, app, device, model,
  wait, duration, ending and token counts. They never record codes, tokens, prompts, answers or tool data. A test checks
  this.
- **Windows Firewall:** the first time ClaudeIn listens, Windows asks whether to allow it. The owner allows it on
  **Private** networks only. Phase 4 covers the Tailscale network.

### 4.8 Files and setup

```
ClaudeIn\
  CLAUDE.md, docs\plan.md, docs\protocol.md (phase 1)
  Setup-ClaudeIn.bat   makes .venv (Python 3.14 like Village), installs requirements, adds Start Menu and
                       Startup shortcuts that run .venv\Scripts\pythonw.exe -m claudein (no console window)
  requirements.txt     claude-agent-sdk, aiohttp, cryptography, PySide6, PySide6-Fluent-Widgets, Pillow, qrcode
  claudein\            the service: config, tls, netguard, pairing, server, queue, runner, usage, tray, windows
  client\claudein_client\   the small Python client library (phase 3)
  tests\
%USERPROFILE%\.claudein\   settings.json, cert.pem/key.pem, devices.json (hashed codes), local.json (token),
                           usage.db, logs\
```
"Start with Windows" puts or removes a shortcut in the owner's own Startup folder. It needs no admin rights.

## 5. Phases (each approved on its own)

### Phase 1: the service (tray, pairing, queue, usage), compatible with today's phones
Covers sections 4.1–4.4, the usage window from 4.5, the menu in 4.6 (without caps), 4.7 and 4.8, and `docs/protocol.md`.

**Done when:**
- Both phone apps, re-paired to `<PC address>:8790`, ask questions and use their tools exactly as with the Kotlin host.
- Two questions run at once and a third waits with "Waiting for the PC…".
- Each run shows in Usage under the right app.

The Kotlin host then stays unused. Retiring it is an app-project change (section 6).

### Phase 2: caps
Covers the caps window, the cap check before each run, the cap message, and resetting at midnight (PC time).

### Phase 3: Python client library and the Village move
- **Client library** (here, in `client\`), used like this:
  ```python
  async for event in claudein_client.run(app="village", model=..., system=..., prompt=...,
                                         tools=[Tool(name, description, schema, handler)],
                                         builtin_tools=[], max_turns=..., max_budget_usd=...): ...
  ```
  - It answers tool calls by calling the handler (sync or async).
  - It gives back a result: ending, answer, usage, cost, turns, limits and error_kind.
  - It raises `ClaudeInUnavailable` ("PC not available") when ClaudeIn isn't running.
  - It ships with a fake for tests.
- **Village move** (planned here, **built only after approval inside Village's project**, by its own phase rules):
  - `station/runner.py`, `station/voice.py` and `agents/{mainbrain,glitch,vector}` swap `claude_agent_sdk.query` for
    the client library, through a small adapter that keeps Village's `query_fn` test hook.
  - Village's in-process tools become `Tool` handlers.
  - Its error types map from `error_kind` (rate_limited → RateLimited, service → TransientError, limit → LimitHit).
  - ClaudeIn not running → Village pauses its agent lanes the way it does for a rate limit, and says "ClaudeIn is not
    running".
  - Village keeps its own USD caps. ClaudeIn's per-app cap sits on top.
  - Village's station page stays on 8765.
  - `demo_run.py` (plain `claude -p` with file tools in a throwaway folder) **keeps calling Claude Code directly** (owner,
    2026-11-15). It is a sandboxed job, not a question, and it doesn't show in ClaudeIn's usage.

### Phase 4: away from home
- **The owner installs or signs up for, themselves:**
  - Tailscale on the PC (likely already there, since Village's phone page uses it)
  - the Tailscale app on each phone, signed in to the same Tailscale account
- **ClaudeIn adds:**
  - It finds the PC's Tailscale address and name and shows them in "Pair a device…" and in the QR code.
  - A one-time firewall rule that lets port 8790 in only from Tailscale (100.64.0.0/10) and the home network. Windows
    asks the owner for permission once (UAC) and the owner clicks Yes.
  - A "Phone access" check in the tray.
- **Plain advice:** pair the phone with the **Tailscale address**. It works at home and away, so the phone needs only
  one address. No port is opened on the router, and Tailscale Funnel stays off.

## 6. Later changes in the Signal Lab apps (belong to `signal-lab-egx`, need their own approval there)

1. The default port becomes 8790 (`HostClient.PORT`, `EgxHost` `PORT`), so the owner can type the address without
   `:8790`.
2. Accept `/hello` `"app": "ClaudeIn"` as well as the old name, so ClaudeIn can later drop the old name.
3. Send `"app": "signal-lab"` / `"signal-lab-egx"` in each run (optional, since ClaudeIn already knows the app from the
   pairing).
4. Wording: when the PC can't be reached, say "PC not available". If the crypto app still falls back to its own API-key
   route, decide there whether to keep that (the owner said no fallback).
5. Optional: scan the pairing QR code instead of typing.
6. Remove the EGX 409 message (it can't happen any more), and retire `analyst-host\` and `tools\start-analyst-host.cmd`
   once ClaudeIn has been used for a while.

## 7. Tests

**On the PC (pytest, no real Claude, no real phone):**
- **A fake Claude Code:** the SDK's `query` is swapped for a function that plays back recorded message streams (plain
  answer, web search, a tool call, refusal, max_tokens, budget hit, rate limit, a crash). The same idea as Village's
  `query_fn` and the Kotlin `claude-plain-reply.jsonl`.
- **A fake phone:** an HTTP client that pairs, opens the SSE stream, answers tool calls, stops runs and drops the
  connection mid-run.
- **Checks:**
  - `/hello` stays compatible.
  - A wrong code is slowed down and blocked.
  - A switched-off or forgotten device is refused, and its runs stop.
  - The app is taken from the pairing.
  - A queue of 2 makes the third run wait, with a "waiting" event.
  - Queue-full message.
  - A dropped connection kills the run and frees the worker.
  - Tool time-out.
  - Pause.
  - Caps (phase 2).
  - Usage rows are written.
  - **Log files and usage.db contain no code, token, prompt or answer.**
  - The address filter rejects a public IP.
  - The loopback token is refused on the network port.
  - Endings and `done` fields match the Kotlin host's for the same recorded stream.
- **A live test, off by default:** one tiny Haiku question through the real Claude Code, run on purpose.

**By hand (the owner):**
1. Run `Setup-ClaudeIn.bat` once. The ClaudeIn icon appears in the hidden icons.
2. The icon is still there after a restart of the PC ("Start with Windows").
3. Pair the crypto app and the EGX app (Pair a device…, type address and code, compare the fingerprint).
4. Ask a question in each app at home. Both show in Usage.
5. Ask in both apps at the same time: both answer, or one says "Waiting for the PC…".
6. Pause all, then ask: the app shows the paused message. Quit ClaudeIn, then ask: "PC not available".
7. Phase 4: turn the phone's Wi-Fi off, keep Tailscale on, and ask: it answers.
8. Phase 2: set a tiny cap on one app and check that it stops for the day while the other still works.

## 8. Questions answered by the owner (2026-11-15)

1. **Caps count share of the plan's limit**, in "plan points" (see 4.5).
2. **Phone questions go ahead** of waiting program runs (see 4.4).
3. **Village's tool demos stay direct** (see phase 3).
4. **A local git repository** is made now. A GitHub repository would be asked about separately.
5. **Windows 11 Fluent look with Acrylic frosted glass** for the tray panel, menu and windows, built with PySide6 and
   PySide6-Fluent-Widgets instead of tkinter and pystray (see 4.6). Chosen from a mockup.

Decided here without asking (can be changed): port 8790/8791; `/hello` keeps the old name for now; 2 runs at once; 20 can wait; runs stop after 15 minutes; logs are kept 14 days.
