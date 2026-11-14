# ClaudeIn

ClaudeIn is a tray program on the owner's Windows PC. It lets the owner's apps (Signal Lab crypto and Signal Lab EGX on
Android, Village in `..\PlatformExperimental`, and future apps) use Claude through the Claude Code login on this PC, with
no API keys. The full plan is in `docs/plan.md`. Started 2026-11-15. **Nothing is built yet.**

## Working rules

- Before any `Edit`/`Write` on source, post a plain-language list of the changes and wait for the literal word
  **approve**. Nothing else counts. This is the global rule in `~/.claude/CLAUDE.md` and it applies here in full.
- Phases are approved one at a time. Finishing one phase is not approval for the next.
- Ask before `git init` or creating any GitHub repository.
- Don't change `signal-lab-egx` or `PlatformExperimental` from here. Changes there go through their own projects and
  their own approval.
- The owner is not a developer. Explain in plain words and short steps.

## Decisions (owner, 2026-11-15)

1. Users: Signal Lab crypto, Signal Lab EGX, Village and future apps. So there is a written protocol
   (`docs/protocol.md`) and a small Python client library.
2. Phones work at home and away, through a private network (Tailscale). Nothing is ever opened to the internet.
3. ClaudeIn not running means the apps say "PC not available". There is never an API-key fallback.
4. A few runs at once (2 by default, can be changed). The rest wait in order. No "busy" (409) refusals.
5. A usage view per app (questions, tokens, plan-limit use, recent runs and how they ended), with optional daily caps on
   the apps the owner picks.
6. Village moves onto ClaudeIn in its own phase, approved inside Village's project.
7. Python. A tray icon in the hidden-icons area, started with Windows, with no console window. Set up like Village
   (`Setup-ClaudeIn.bat` makes `.venv`, plus shortcuts).
8. This folder is its own project.
9. Caps count share of the plan's limit ("plan points", 100 = one full 5-hour window, estimated per app).
10. Phone questions go ahead of waiting program runs. Village's `demo_run.py` keeps calling Claude Code directly.

## Fixed technical choices (from the plan)

- Phones use HTTPS on port 8790 with a self-made certificate the phone pins. Programs on the PC use plain HTTP on
  127.0.0.1:8791 with a token in `%USERPROFILE%\.claudein\local.json`. Never port 8765 (Village's).
- Keep the Kotlin analyst host's protocol (`signal-lab-egx/analyst-host`) so today's phones work unchanged apart from the
  address. `/hello` keeps answering `"app": "Signal Lab analyst host"` until both apps accept `"ClaudeIn"`.
- Each pairing has its own code (one app on one device). Codes are stored only as hashes and can be revoked one by one.
- Claude Code runs through the Claude Agent SDK, locked down as the Kotlin host does it, with `ANTHROPIC_API_KEY`
  blanked.
- Logs and usage records never hold codes, tokens, prompts, answers or tool data.
