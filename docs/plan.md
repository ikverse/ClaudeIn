# ClaudeIn: plan

Status (2026-11-15): **phase 1 built** (a phone has paired; a phone question through ClaudeIn not yet confirmed) and
**phase 2 built**; **phase 3's client library built** (the Village move waits for Village's project); **phase 5a built**
(2026-11-18: sign in with Claude from inside ClaudeIn, finding the Claude Code installed on the PC, and the guard against
Claude Code updates; section 4.9), **phase 5b built** (2026-11-18: `ClaudeIn-Setup.exe`, see phase 5b below) and
**phase 6 built** (2026-11-18: updates from the GitHub releases, section 4.10; no release published yet) and **phase 7
built** (2026-11-18: fewer false Windows Defender alarms). Written 2026-11-15. Every phase below needs its own "approve" before any code is
written (see `CLAUDE.md`).

## 1. What ClaudeIn is, in plain words

ClaudeIn is one small program that sits in the Windows tray (the hidden icons by the clock) and lets the owner's apps use
Claude through the Claude Code installed on this PC, signed in (from ClaudeIn, in the browser) with the Claude account the
owner chooses. No API keys anywhere.

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
  - a home network: 10.x, 172.16–31.x, 192.168.x, and the newer IPv6 home addresses (fe80::/10 and fc00::/7), which
    phones often use, especially with a name like `my-pc.local`
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
- **Allowed models per app** (owner, 2026-11-15). Each app has a list of models it may use. It is empty by default,
  which means any model. The owner can set a list per app in Devices (4.6). A run asking for another model ends with
  `failed: <app> isn't allowed to use <model>.`
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
  successfully. **Change (owner, 2026-11-18):** ClaudeIn does **not** use the SDK's bundled copy of Claude Code. It runs
  the **Claude Code installed separately on the PC** (the SDK is pointed at it with `cli_path`), so ClaudeIn can be
  installed on any computer without carrying a 355 MB copy. Finding it, signing in and guarding against its updates are
  in section 4.9. The tray panel and the Home page show the version in use. The SDK gives us:
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
  - `ANTHROPIC_API_KEY` blanked (and `ANTHROPIC_AUTH_TOKEN` and the Bedrock/Vertex/Foundry switches, which this PC
    may have set), so the Claude login is always used
  - run in an empty temporary folder

  If an SDK option for one of the Kotlin flags (`--restricted`) is missing, it is passed as an extra command-line flag.
- **No console windows.** ClaudeIn runs without a window (`pythonw`), and on Windows that can make a black console box
  appear when Claude Code starts. Claude Code is started with Windows' "no window" setting (`CREATE_NO_WINDOW`) so
  nothing flashes or stays on screen.
- **Everything stops together.** Each Claude Code run is put inside a Windows "job" (a job object) set to end every
  program in it when the run stops. So stopping a run, or ClaudeIn itself ending, never leaves Claude Code or anything
  it started still running.
- **One program, two halves.** The windows (PySide6) and the server and runner (asyncio) share one loop through the
  small `qasync` add-on, so the windows stay smooth while Claude answers.
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
  - A run that has waited in line 10 minutes (can be changed) without starting leaves the queue and ends with
    `failed: The PC was busy for too long. Try again.`
- **PC sleep** (owner, 2026-11-15). While a run is going or waiting, ClaudeIn asks Windows to keep the PC awake. Then
  the PC sleeps as normal. A sleeping PC means "PC not available" on the phone. A "Keep PC awake" switch in General (off
  by default) keeps it awake all the time, for long trips away.
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
    - **Change (owner, 2026-11-15):** the rise is trusted only when ClaudeIn was busy the whole time since the previous
      report (no idle gap over 30 seconds); otherwise the owner's own Claude use in the gap could be in it, so the run's
      tokens are used instead. The rate is today's, else the most recent day's in the last 7 days, else 0. Own use
      *while* a question runs still lands on that question.
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
- **Home:** status, plan use, runs working and waiting, and a "Set up Claude…" button while Claude Code or the sign-in
  is missing.
- **Claude** (phase 5a): Claude Code on this PC, the Claude account (sign in, switch, sign out), "Its own Claude login",
  the Claude Code versions ClaudeIn has seen, and the Advanced known-good copy (4.9).
- **Devices:** one card per pairing (Signal Lab — Phone, Signal Lab EGX — Phone, Village (this PC)), each with an
  on/off switch and Forget…, plus "Pair a device". Each app also has its list of allowed models (4.3).
- **Usage:** the usage view from 4.5.
- **General:** runs at once (1–4), Start with Windows, Keep PC awake (4.4), Open logs folder, New local token, Back up
  pairings and Restore pairings (below), and Check Claude Code (asks one tiny Haiku question to confirm the login).
- **Caps** are added here in phase 2.

The pairing window (4.3) uses the same glass.

**Glass check first.** Frosted glass sometimes lags when a window is dragged. So phase 1 starts with a small test
window that the owner drags around. If it lags, the settings and pairing windows use the calmer Mica glass instead,
and the small tray panel (which is never dragged) keeps the frosted glass.
**Result (owner, 2026-11-15):** both Acrylic and Mica dragged smoothly, so **Acrylic is used everywhere** (panel, menu,
settings and pairing windows). The test window stays as `Glass-Test.bat`.
**Change (owner, 2026-11-15):** in use, the settings window's Acrylic looked washed-out grey whenever a bright window
sat behind it (the glass lets about 40% of what's behind show through). So the **settings and pairing windows now use
Mica**, which tints with the desktop wallpaper and stays dark. The tray panel and menu keep Acrylic.

**Restart after a crash.** "Start with Windows" also sets up a Windows scheduled task, in the owner's own account with
no admin rights, that starts ClaudeIn again within a minute if it has stopped without Quit. Quit leaves it stopped
until the next sign-in. **Change (phase 7, 2026-11-18):** Windows Defender took this every-minute task (with the rest of
the program) for malware. It is now its own switch on General, "Restart ClaudeIn if it stops", **off by default**, and
runs every 5 minutes when on. "Start with Windows" is only the Startup shortcut.

**Back up and restore pairings.** "Back up pairings" saves the certificate, its key and the pairing list (hashed codes
only) as one file wherever the owner picks. "Restore pairings" puts them back, so a lost `.claudein` folder doesn't
mean pairing every phone again. The file lets someone pretend to be ClaudeIn to the phones, so the window says to keep
it somewhere private.

- "Signed in as <email> (<plan>) ✓" comes from Claude Code's own `claude auth status`, looked at when ClaudeIn starts,
  every 15 minutes, and after a sign-in, a sign-out or a run that says it is signed out. It is free: ClaudeIn never sends
  a question on its own, so it uses no plan time just by running. "Check Claude Code" (one tiny Haiku question) stays.
- **Claude page** in the settings window (phase 5a, section 4.9): step 1 Claude Code, step 2 the Claude account, then the
  versions ClaudeIn has seen and an Advanced known-good copy. The panel shows a "Sign in with Claude" button when needed.
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
  **Private** networks only. That first answer applies to Python in general, so phase 4 replaces it with a rule for
  ClaudeIn's own `.venv` Python and only port 8790 (8791 is loopback and needs none). Phase 4 also covers the Tailscale
  network.

### 4.8 Files and setup

```
ClaudeIn\
  CLAUDE.md, docs\plan.md, docs\protocol.md (phase 1)
  Setup-ClaudeIn.bat   makes .venv (Python 3.14 like Village), installs requirements, adds Start Menu and
                       Startup shortcuts that run .venv\Scripts\pythonw.exe -m claudein (no console window)
  requirements.txt     claude-agent-sdk, aiohttp, cryptography, PySide6 (6.11 or later, the first that works with
                       Python 3.14), PySide6-Fluent-Widgets, qasync, Pillow, qrcode
  claudein\            the service: config, tls, netguard, pairing, server, queue, runner, usage, tray, windows
  client\claudein_client\   the small Python client library (phase 3)
  packaging\           launch.py, ClaudeIn.spec (PyInstaller), ClaudeIn.iss (Inno Setup), make_icon.py, build.py (phase 5b)
  Build-Installer.bat  makes build\installer\ClaudeIn-Setup-<version>.exe (needs Inno Setup 6)
  requirements-build.txt   pyinstaller
  tests\
%USERPROFILE%\.claudein\   settings.json, cert.pem/key.pem, devices.json (hashed codes), local.json (token),
                           usage.db, logs\
```
"Start with Windows" puts or removes a shortcut in the owner's own Startup folder. It needs no admin rights.

Phase 5a adds to `%USERPROFILE%\.claudein\`: `claude\` (ClaudeIn's own Claude login, when "Its own Claude login" is on),
`claude-versions.json` (the versions seen, no secrets) and, only if the owner asks for it, `claude-code\<version>\`
(ClaudeIn's own copy of Claude Code). `settings.json` gains `own_login` (default true) and `claude_copy` ("pc" or "own").

### 4.9 Claude Code on the PC: finding it, signing in, and surviving its updates (phase 5a)

**Decisions (owner, 2026-11-18):** ClaudeIn must be installable on any computer and sign in with the Claude account the
owner picks, by the normal Claude browser sign-in. Claude Code is **installed separately** (not bundled). A Claude Code
update must never crash ClaudeIn.

**Finding it** (`claudein\claudecode.py`). In order: an absolute PATH folder with `claude.exe`, then
`%USERPROFILE%\.local\bin\claude.exe`; or ClaudeIn's own copy when `claude_copy` is "own". npm's `claude.cmd` shim is
recognised and refused (the SDK refuses it too) with a message saying to install the standard one. Nothing is
downloaded or installed unless the owner clicks a button for it.

**First run.** If Claude Code is missing or signed out when ClaudeIn starts, the Claude page opens by itself:
1. *Claude Code:* "Open download page" (the official setup page) and "Check again". ClaudeIn never runs an installer
   for the owner.
2. *Claude account:* "Sign in with Claude" runs `claude auth login --claudeai` with no window. Claude opens the normal
   browser sign-in; ClaudeIn waits (5 minutes at most), shows "Open the page again", "Copy link" (Anthropic's own
   addresses only) and "Cancel", then reads `auth status`. "Switch account" signs out and in again; "Sign out" runs
   `auth logout`. Only a **Claude plan** sign-in is accepted. A Console (API billing) sign-in is refused, so nothing is
   ever billed by use.

**Its own login.** By default ClaudeIn keeps its own login in `.claudein\claude` (`CLAUDE_CONFIG_DIR`), so it can use a
different account from the one used in a terminal. A switch turns that off to use the PC's Claude Code login instead.

**The guard** (so an update can't crash ClaudeIn):
- *A free check:* `--version`, `--help` and `auth status`, no question and no plan time. It is remembered by the program
  file's size and date and runs again only when the file changes (before each run, which costs one file lookup).
- *Needed flags:* `--restricted`, `--tools`, `--allowedTools`, `--permission-mode`, `--setting-sources`,
  `--strict-mcp-config`, `--mcp-config`, `--output-format`, `--input-format`, `--verbose`, `--model`. If one is gone, runs
  end at once with "Claude Code <version> isn't compatible with ClaudeIn yet: it doesn't have <flag>…" and
  `error_kind: "unavailable"`; Claude Code is not started and the lock-down is never loosened. An older version than
  2.1.296 (the oldest tried) is refused the same way ("too old").
- *Flags that can be left out:* `--no-session-persistence`, `--include-partial-messages`, `--max-budget-usd` are left
  out, and the Claude page says so.
- *Unreadable messages are skipped,* counted and logged by type only, instead of ending the run (the SDK already skips
  types it doesn't know).
- *One retry:* if Claude Code stops without an answer before anything reached the caller (no text, no tool call), the
  question is asked once more. After text or a tool call there is no retry, so nothing is repeated.
- *Counting:* each run counts as good (Claude Code spoke in its own words, even an error) or bad (it stopped without an
  answer). Two bad runs in a row mark the version "not working" in the tray and Home; a good run clears it.
- *No updates in the middle of a run:* ClaudeIn starts Claude Code with `DISABLE_AUTOUPDATER=1`.
- *Versions seen* (`claude-versions.json`): per version, the check result, the flags it lacked, and good and bad runs, shown
  on the Claude page. The service never stops being up: problems show in the tray and in the apps as "PC not available"
  style failures, not as crashes.

**The safety net** (Advanced, on the owner's click only). "Download <known-good>" fetches that Claude Code from
Anthropic's release site (`downloads.claude.ai/claude-code-releases`), checks it against the release manifest's SHA-256
and Windows' Authenticode signature ("Anthropic, PBC"), keeps it in `.claudein\claude-code\<version>\` (one copy only),
and uses it from then on with `DISABLE_UPDATES=1` so it never changes. "Use the one on this PC" switches back. The
known-good version is 2.1.325, the one ClaudeIn was built and tested with. The manifest's own GPG signature is not
checked (it needs gpg); the Windows signature covers authenticity.

**The limit.** A future update could change something the check can't see (for example a hidden flag), and ClaudeIn would
only learn it when runs fail. The owner then gets a clear message and the safety-net button, not a crash.

### 4.10 Updates from GitHub releases (phase 6)

**Decision (owner, 2026-11-18):** installed copies update from the **GitHub Releases of `ikverse/ClaudeIn`** (the main,
public repository). The installer is attached to each release, never committed to git. A separate repository would not
make the source any less public, and the update source must be public anyway (a private one would need a token inside
every copy, which anyone could take out).

**Checking** (`claudein/updates.py`): GitHub's public "latest release" address, no login, a minute after start, then every
12 hours, while General's "Check for updates automatically" is on (on by default); also "Check now". It sends only
"ClaudeIn/<version>". Drafts, pre-releases, older or equal versions, and releases without a signed installer are ignored.
No releases yet, GitHub's rate limit (60 an hour per network) or no internet only show a quiet line on the General page.

**Telling:** one tray notice per new version (clicking it opens General) and General's Updates card with the release notes
and **Install <version>**. Nothing installs without that click.

**Installing:** only from `https://github.com/ikverse/ClaudeIn/releases/download/…`, and only in the installed
`ClaudeIn.exe` (a `.venv` copy is updated with git). It downloads the installer and its `.sig` to `.claudein\updates\`
(keeping only that one), checks the **Ed25519 signature** against the public key in `claudein/update_key.py`, then starts
the installer quietly (`/SILENT /UPDATE=1`) and quits. The installer stops any ClaudeIn left, keeps the earlier Start menu
and startup choices, and with `/UPDATE=1` starts the new ClaudeIn when it is done. The signature covers
`"ClaudeIn <version>\n<sha256 of the installer>\n"`, so a changed file and an older signed installer under a newer name
are both refused. Getting into the GitHub account is not enough to push a bad update; the private key is also needed.

**The signing key:** made once by `packaging\make_signing_key.py`. The private key is
`%USERPROFILE%\.claudein-release\signing-key.pem`, outside the project and never in git; it is never replaced. **Keep a
private backup.** If it is stolen, its holder can sign updates every copy will install; if it is lost, copies already out
there won't accept new updates and need one manual install of a version with a new key.

**Releasing:** `Release.bat 0.3.0 "What changed"` (`packaging\release.py`). It checks: master, no uncommitted changes,
version not lower and tag unused, the key matches `update_key.py`, `gh` signed in. It sets the version, runs the tests,
commits "Release 0.3.0", builds, signs, tags, pushes master and the tag, and creates the release with the installer and
its signature. `--dry-run` does everything but tag, push and publish.

**First updater version:** 0.2.0. Copies on 0.1.0 have no updater and need one manual install.

## 5. Phases (each approved on its own)

### Phase 1: the service (tray, pairing, queue, usage), compatible with today's phones
Covers sections 4.1–4.4, the usage window from 4.5, the tray panel, menu and settings window in 4.6 (without caps), 4.7
and 4.8, and `docs/protocol.md`. It starts with the glass check (4.6), shown to the owner before the rest is built.

**Done when:**
- Both phone apps, re-paired to `<PC address>:8790`, ask questions and use their tools exactly as with the Kotlin host.
- Two questions run at once and a third waits with "Waiting for the PC…".
- Each run shows in Usage under the right app.

The Kotlin host then stays unused. Retiring it is an app-project change (section 6).

### Phase 2: caps
Covers the caps window, the cap check before each run, the cap message, and resetting at midnight (PC time).
**Built 2026-11-15** (Caps page in Settings, points columns in Usage, cap check when asked and again before a waiting
question starts). Waiting on the owner's hand check 8.

### Phase 3: Python client library and the Village move
**Client library built 2026-11-15** (`client\claudein_client`, no add-ons; how to use it: `docs/protocol.md` 3.4).
**The Village move is not started:** it waits for approval inside Village's project (handover:
`docs/village-handover.md`).
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

### Phase 4: away from home (built 2026-11-18)
- **Found while building (owner, 2026-11-18):** the `.venv` Python hands off to the shared `C:\Python314\pythonw.exe`,
  which is the program Windows Firewall sees, and Village uses the same one. So the rule is for that program (or
  `ClaudeIn.exe` when installed), and **the broad "Python" rules are left alone** so Village's phone page on 8765 keeps
  working. Tightening those needs a matching rule for Village, which goes through Village's own project. The set-up
  does remove Windows' wide "claudein.exe" rules. Where it is: `claudein/tailscale.py`, `claudein/firewall.py`, the
  "Phone access" card in Settings → General (also opened from the tray menu and the panel's "Phones" button).
- **The owner installs or signs up for, themselves:**
  - Tailscale on the PC (likely already there, since Village's phone page uses it)
  - the Tailscale app on each phone, signed in to the same Tailscale account
- **ClaudeIn adds:**
  - It finds the PC's Tailscale address and name and shows them in "Pair a device…" and in the QR code.
  - A one-time firewall rule for ClaudeIn's own `.venv` Python only, that lets port 8790 in only from Tailscale
    (100.64.0.0/10) and the home network, and removes the broader "allow Python" answer from the first start. Windows
    asks the owner for permission once (UAC) and the owner clicks Yes.
  - A "Phone access" check in the tray.
- **Plain advice:** pair the phone with the **Tailscale address**. It works at home and away, so the phone needs only
  one address. No port is opened on the router, and Tailscale Funnel stays off.

### Phase 5a: sign-in, Claude Code on the PC, the guard (built 2026-11-18)
Section 4.9 and its Claude page, panel button and first-run screen. **Done when:** on a PC with Claude Code installed and
no ClaudeIn login, the owner presses Sign in with Claude, finishes the browser page, and the next question works with
that account's plan; and a Claude Code update that removes a needed flag is refused with a clear message while ClaudeIn
keeps running. The real chain was checked with the installed Claude Code 2.1.296 (live test, PC login).

### Phase 5b: one installable program (built 2026-11-18)
`Build-Installer.bat` (or `.venv\Scripts\python.exe packaging\build.py`) makes `build\installer\ClaudeIn-Setup-<version>.exe`
for any Windows PC: a PyInstaller folder build **without** the SDK's bundled `claude.exe` (the build fails if one gets in),
wrapped by Inno Setup 6 (`packaging\ClaudeIn.iss`).
- **Size:** the installer is about 57 MB; installed, ClaudeIn is about 171 MB.
- **Installs for the current user only:** no administrator rights, no UAC question. Two ticks, both on by default: add to
  the Start menu, and start with Windows (the Startup shortcut, plus the restart task that `ClaudeIn.exe --setup` adds).
  It offers to start ClaudeIn when it finishes.
- **Updating:** installing a newer one over an old one first stops a running `ClaudeIn.exe` (and sets the quit flag so the
  restart task doesn't bring it back mid-update; the next normal start clears it).
- **Uninstalling** runs `ClaudeIn.exe --remove-startup` and stops ClaudeIn. It removes the Startup shortcut and restart task
  **only if they start the installed program**, so a ClaudeIn run from `.venv` keeps its own. The `.claudein` folder
  (pairings, certificate, ClaudeIn's Claude login, usage) is **kept**; delete it by hand to remove everything.
- **First start on a new computer** opens the Claude page: install Claude Code (step 1), then Sign in with Claude (step 2).
- `autostart.py` and the restart after Restore start `ClaudeIn.exe` (no `-m claudein`) when installed; from `.venv` nothing
  changes. An early failure before logging starts is written to `.claudein\startup-error.txt`.
- **Not done here:** the firewall rule (it stays in phase 4), and code-signing. Windows SmartScreen will warn ("unknown
  publisher") on other computers until the installer is signed, which needs a paid certificate; "More info, Run anyway"
  works. Tailscale is still needed on a computer whose phones connect from away.
- **Checked:** a real silent install into a temporary folder, the installed program answering `/hello` and finding Claude
  Code, and a silent uninstall, with the real shortcuts, task and running ClaudeIn untouched.

### Phase 6: updates from GitHub releases (built 2026-11-18)
Section 4.10: the checker and Updates card, the tray notice, the signed download and quiet reinstall, the installer's
restart after an update, the signing key and `Release.bat`. Version 0.2.0. **Done when:** a release published with
`Release.bat` shows up in an installed older ClaudeIn, which installs it and starts again on the new version. Tested with a
fake GitHub (newer/same/older, no releases, rate limit, no internet, unsigned, wrong key, replayed signature, changed file,
files from elsewhere, `.venv` copy, the switch, nothing personal sent) and a dry-run release. **Not yet done:** the first
real release (publishing it is the owner's call), so a real update from one version to the next is not yet seen.

### Phase 7: fewer false Windows Defender alarms (built 2026-11-18)
**Why:** right after the owner's ClaudeIn was reinstalled with the first 0.2.0 build, Windows Defender quarantined
`ClaudeIn.exe` as `Trojan:Win32/Bearfoos.A!ml` (a machine-learning guess, not a known-malware match) and removed its
shortcuts and restart task. Unsigned PyInstaller programs that start with Windows, restart themselves every minute, stop
processes by name and can download and run an installer look like malware to that guesswork.
- **Version details** in `ClaudeIn.exe` (publisher `ikverse`, product, version, description, the GitHub address; made by
  `build.py`) and in the installer (publisher, website, version).
- **PyInstaller's starter compiled on this PC** (`build.py`, with Visual Studio's C++ tools, once per PyInstaller version;
  remembered in `build\own-starter.sha256`). `--prebuilt-starter` skips it where there are no C++ tools.
- **The restart task** is its own switch, off by default, every 5 minutes (see 4.6). A normal start and `--setup` make the
  task match the switch, which also clears the every-minute task older versions left.
- **Closing politely:** `ClaudeIn.exe --quit` asks the running copy to close (as Quit in the tray) and waits up to 20 s.
  The installer and uninstaller use it, and stop ClaudeIn by force only if it is still running after that.
- **Updates start the normal Windows way** (ShellExecute, like a double-click) with the installer's progress showing.
- **Test installers** (`build.py --test`): "ClaudeIn Test", their own app ID and folder, no shortcuts, no restart task, and
  they never stop the real ClaudeIn.
- **Defender scan of every build** (`MpCmdRun`, report only); a finding stops the build.
- **Checked:** the compiled starter, the version details, both scans clean, the test build installed and run for 2 minutes
  with no Defender detection, `--quit` closing it in about a second, and the test uninstall leaving the real install alone.
- **Limits:** the 2-minute run doesn't include the Startup shortcut (test builds make none). An unsigned program can still
  be flagged, and each new version starts with no reputation. The reliable fix is code-signing (not done; a paid
  certificate, Microsoft's Trusted Signing, or free open-source signing, to be looked into). The owner can also report a
  false alarm to Microsoft (microsoft.com/wdsi/filesubmission).

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
  - The address filter rejects a public IP and lets in IPv6 home addresses (fe80::, fc00::/7).
  - A model not on an app's allowed list is refused; an empty list allows any.
  - A run that waits in line too long ends with the "busy for too long" message.
  - Stopping a run leaves no Claude Code or child program running (job object).
  - Backup then restore brings back the same certificate and pairings.
  - The loopback token is refused on the network port.
  - Endings and `done` fields match the Kotlin host's for the same recorded stream.
  - **Phase 5a (a fake `claude` program):** missing, broken, too old and incompatible Claude Code refuse runs without
    starting it; optional flags are left out and the lock-down stays; the check is remembered until the file changes; an
    update that removes a flag is refused and then recovers; signed out and Console accounts are refused; an expired
    login shows up by itself; sign-in, cancel, failure, time-out and sign-out; no email or link in logs or files; one
    retry only when nothing reached the caller; "not working" after two bad runs and recovery; unreadable messages
    skipped; the own-copy download (good, wrong checksum, wrong signature, unreachable); finding the program (PATH,
    relative folders, npm shim, newest own copy); the Claude page in each state.
- **A live test, off by default:** one tiny Haiku question through the real Claude Code, run on purpose
  (`CLAUDEIN_LIVE=1`, plus `CLAUDEIN_LIVE_PC_LOGIN=1` to use the PC's Claude Code login instead of ClaudeIn's own).

**By hand (the owner):**
1. Run `Setup-ClaudeIn.bat` once. The ClaudeIn icon appears in the hidden icons, and the Claude page opens by itself.
   Press **Sign in with Claude** (step 2), finish the browser page, and the page says "Signed in as <you> (<plan>)".
   (Phase 5a.) Then pressing Sign out, and Sign in again with another account, switches the account.
2. The icon is still there after a restart of the PC ("Start with Windows").
   - Drag the glass test window around: it moves smoothly (phase 1, first step).
   - While a question runs, no black console window appears.
   - End ClaudeIn in Task Manager: it comes back within a minute. Quit it from the tray: it stays off.
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
6. **PC sleep:** ClaudeIn keeps the PC awake only while a run is going or waiting, with an optional "Keep PC awake"
   switch, off by default (see 4.4).
7. **Models:** each app may have a list of allowed models, empty (any model) by default (see 4.3).
8. **(2026-11-18) Installable on any computer, signing in with the chosen Claude account by the browser.** Claude Code is
   **installed separately**, not bundled (see 4.4 and 4.9). ClaudeIn must **never crash because Claude Code updated**:
   a free check, refusal with a clear message, one retry, and a known-good copy the owner can choose to download.
   ClaudeIn keeps **its own Claude login** by default (separate from the terminal's). One phase at a time: 5a
   (sign-in and guard) first, 5b (installer) after.
9. **(2026-11-18) Updates come from the GitHub Releases of the main repository** `ikverse/ClaudeIn` (public), with the
   installer attached to releases, not committed. Updates are signed with the owner's own key and are only installed on
   the owner's click (see 4.10).
10. **(2026-11-18) Fewer false Defender alarms** (phase 7): publisher name `ikverse`; the restart task off by default and
    every 5 minutes when on.

Decided here without asking (can be changed): port 8790/8791; `/hello` keeps the old name for now; 2 runs at once;
10 minutes at most in line; 20 can wait; runs stop after 15 minutes; logs are kept 14 days.
