# ClaudeIn

ClaudeIn is a small tray program for Windows. It lets your own apps (phone apps, and programs on the same PC) use
Claude through **Claude Code**, signed in with your Claude plan account. Your apps don't need API keys.

- Runs in the tray (the hidden-icons area) and starts with Windows. There's no console window.
- Phones connect over HTTPS with a pinned certificate. At home and away they go through a private network (Tailscale);
  ClaudeIn is never open to the internet.
- Programs on the PC connect to `127.0.0.1` with a local token.
- A queue (2 runs at once by default), a usage view per app, optional daily caps per app, and allowed models per app.
- If ClaudeIn isn't running, apps say "PC not available". There is never an API-key fallback.

## Connecting your project

**Coding agents: start with [`docs/connecting-an-app.md`](docs/connecting-an-app.md).** It covers:

- which route to use (the Python client `claudein_client` on the PC, or the HTTPS protocol for phones);
- working examples (asking, streaming, your own tools, tests with `FakeClaudeIn`);
- how to handle every way a run can end;
- the rules every connected app follows, a block to paste into your project's `CLAUDE.md`, and a checklist.

The quickest start, for a Python program on this PC:

```bat
<your project>\.venv\Scripts\python.exe -m pip install -e <path to ClaudeIn>\client
```

```python
import claudein_client
result = await claudein_client.ask(app="my-app", model="claude-opus-5-5",
                                   system="You are ...", prompt="...")
print(result.answer if result.ok else result.reason)
```

## Installing and running ClaudeIn

1. Install **Claude Code** separately (ClaudeIn doesn't bundle it).
2. Install ClaudeIn with `ClaudeIn-Setup.exe` from this project's GitHub Releases. Or, from a copy of this folder, run
   `Setup-ClaudeIn.bat` (it makes `.venv` and the shortcuts).
3. Open ClaudeIn from the tray and sign in with your Claude account (a normal browser sign-in). Only a Claude **plan**
   sign-in is accepted.
4. For phones: **Pair a device…** in the tray, then type the address and code into the phone app.

Updates come from this project's GitHub Releases. They install only when you click, and only if they carry a valid
signature.

## Documents

| File | What it is |
|---|---|
| [`docs/connecting-an-app.md`](docs/connecting-an-app.md) | How to connect an app (start here) |
| [`docs/protocol.md`](docs/protocol.md) | The full protocol: addresses, routes, events, endings, queue |
| [`docs/plan.md`](docs/plan.md) | The design and the phases |
| [`docs/village-handover.md`](docs/village-handover.md) | Notes for moving one existing app onto ClaudeIn |
| [`client/`](client/) | The `claudein_client` Python library |
