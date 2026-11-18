"""Phase 5a: finding Claude Code, the free check, the sign-in, and surviving Claude Code updates (plan 4.9).

Everything here uses a fake `claude` program (conftest.FakeCli); no real Claude Code, browser or plan time is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from PySide6.QtWidgets import QApplication

from claudein import claudecode, config
from claudein import service as service_mod
from claudein.claudecode import ClaudeCode, LoginState
from claudein.service import Service

from .conftest import (EMAIL, LOGIN_LINK, FakeClaude, FakeCli, Phone, make_claude, result, spec, stream_text, until,
                       update_fake_claude)


@contextlib.asynccontextmanager
async def running(home, fake, claude):
    """A ClaudeIn around [claude], with a paired fake phone."""
    s = Service(home, query_fn=fake.query, claude=claude)
    await s.start(use_tls=False, phone_port=0, local_port=0)
    _, code = s.pair("Signal Lab", "Phone")
    p = Phone(f"http://127.0.0.1:{s.phone_port}", code)
    try:
        yield s, p
    finally:
        await p.close()
        await s.stop()


async def done_of(phone: Phone, **over) -> dict:
    return (await phone.ask(spec(**over)))[-1]


# --- finding and checking Claude Code ---------------------------------------------------------------------------------------

async def test_the_check_reads_version_and_account_for_free(svc, fake, cli):
    c = svc.claude
    assert c.state == "ok" and c.version == "2.1.325" and c.signed_in is True
    assert c.account.email == EMAIL and c.account.plan == "Max 5x" and c.account.label == f"{EMAIL} (Max 5x)"
    assert fake.calls == []  # no question was asked
    assert svc.status()["claude_version"] == "2.1.325"


async def test_hello_says_which_claude_code(svc, phone):
    status, body = await phone.get("/hello", auth=None)
    assert status == 200 and body["claude"] == "2.1.325"


async def test_missing_claude_code_refuses_runs_with_a_clear_reason(home, fake):
    claude, _ = make_claude(home, find=lambda c: None)
    async with running(home, fake, claude) as (s, p):
        st = s.status()
        assert st["claude"]["state"] == "missing" and st["claude"]["needs_setup"] and st["signed_in"] is None
        assert any("isn't installed" in x for x in st["problems"])
        done = await done_of(p)
        assert done["error_kind"] == "unavailable" and "isn't installed" in done["ending"]
        assert fake.calls == []  # Claude Code was never started


async def test_a_program_that_does_not_start_is_reported(home, fake, cli):
    claude, _ = make_claude(home, cli)
    cli.broken = True
    async with running(home, fake, claude) as (s, p):
        assert s.claude.state == "broken"
        done = await done_of(p)
        assert done["error_kind"] == "unavailable" and "didn't start" in done["ending"]
        cli.broken = False
        update_fake_claude(claude, cli, "2.1.326")  # reinstalled: a new file
        assert (await done_of(p))["ending"] == "done"
        assert claude.state == "ok"


async def test_too_old_is_refused(svc, phone, fake, cli):
    update_fake_claude(svc.claude, cli, "2.1.100")
    done = await done_of(phone)
    assert done["error_kind"] == "unavailable" and "too old" in done["ending"]
    assert svc.claude.state == "too_old" and fake.calls == []


async def test_a_missing_lock_down_flag_refuses_runs_but_keeps_the_service_up(svc, phone, fake, cli):
    assert (await done_of(phone))["ending"] == "done"
    update_fake_claude(svc.claude, cli, "2.1.500", [f for f in cli.flags if f != "--restricted"])
    n = len(fake.calls)
    done = await done_of(phone)
    assert done["ending"].startswith("failed:") and "--restricted" in done["ending"]
    assert done["error_kind"] == "unavailable"
    assert len(fake.calls) == n  # Claude Code was never started
    assert svc.status()["claude"]["state"] == "incompatible"
    assert any("--restricted" in p for p in svc.problems())
    assert (await phone.get("/hello", auth=None))[0] == 200  # the service itself is fine
    # and it recovers the moment Claude Code does
    update_fake_claude(svc.claude, cli, "2.1.501", cli.flags + ["--restricted"])
    assert (await done_of(phone))["ending"] == "done"
    assert svc.claude.state == "ok" and svc.problems() == []


async def test_missing_optional_flags_are_left_out_but_the_lock_down_stays(svc, phone, fake, cli):
    gone = ["--no-session-persistence", "--include-partial-messages", "--max-budget-usd"]
    update_fake_claude(svc.claude, cli, "2.1.502", [f for f in cli.flags if f not in gone])
    assert (await done_of(phone))["ending"] == "done"
    opts = fake.calls[-1][1]
    assert "restricted" in opts.extra_args and "no-session-persistence" not in opts.extra_args
    assert opts.include_partial_messages is False and opts.max_budget_usd is None
    assert opts.permission_mode == "dontAsk" and opts.setting_sources == [] and opts.strict_mcp_config
    assert svc.claude.probe.missing_optional == gone


async def test_flag_names_must_match_whole(home):
    claude, cli = make_claude(home)
    cli.flags = [f for f in cli.flags if f != "--tools"] + ["--tools-extra"]
    await claude.refresh()
    assert claude.probe.missing_required == ["--tools"] and claude.state == "incompatible"


async def test_either_spelling_of_allowed_tools_is_fine(home):
    claude, cli = make_claude(home)
    cli.flags = [("--allowed-tools" if f == "--allowedTools" else f) for f in cli.flags]
    await claude.refresh()
    assert claude.state == "ok"


async def test_the_check_is_remembered_until_the_file_changes(svc, phone, cli):
    await done_of(phone)
    await done_of(phone)
    assert cli.count("--help") == 1 and cli.count("--version") == 1  # runs didn't repeat the check
    update_fake_claude(svc.claude, cli, "2.1.400")
    assert (await done_of(phone))["ending"] == "done"
    assert cli.count("--version") == 2 and svc.claude.version == "2.1.400"
    assert [r["version"] for r in svc.claude.versions()] == ["2.1.400", "2.1.325"]


async def test_versions_are_remembered_between_starts(home, fake, cli):
    claude, _ = make_claude(home, cli)
    async with running(home, fake, claude) as (s, p):
        await done_of(p)
    claude2, _ = make_claude(home, cli)
    row = claude2.versions()[0]
    assert row["version"] == "2.1.325" and row["check"] == "ok" and row["good"] == 1 and row["current"] is False


async def test_old_version_is_listed_as_not_passing(svc, cli):
    update_fake_claude(svc.claude, cli, "2.1.100")
    await svc.recheck_claude()
    rows = {r["version"]: r for r in svc.claude.versions()}
    assert rows["2.1.100"]["check"] == "too_old" and rows["2.1.325"]["check"] == "ok"


# --- the account ------------------------------------------------------------------------------------------------------------

async def test_signed_out_is_refused_without_starting_claude(svc, phone, fake, cli):
    cli.signed_in = False
    await svc.recheck_claude()
    assert svc.signed_in() is False and svc.claude.needs_setup()
    done = await done_of(phone)
    assert done["error_kind"] == "unavailable" and "signed out" in done["ending"]
    assert fake.calls == []
    cli.signed_in = True  # signed in elsewhere, such as in a terminal: the next question just works
    assert (await done_of(phone))["ending"] == "done"
    assert svc.signed_in() is True


async def test_a_console_account_is_not_used(svc, phone, fake, cli):
    cli.method = "console"
    await svc.recheck_claude()
    assert svc.signed_in() is False and svc.claude.wrong_account_type
    done = await done_of(phone)
    assert done["error_kind"] == "unavailable" and "Console" in done["ending"] and fake.calls == []


async def test_an_expired_login_shows_up_by_itself(home, fake, cli, monkeypatch):
    monkeypatch.setattr(service_mod, "CLAUDE_RECHECK_S", 0.05)
    claude, _ = make_claude(home, cli)
    async with running(home, fake, claude) as (s, p):
        assert s.signed_in() is True
        cli.signed_in = False
        await until(lambda: s.signed_in() is False)
        assert any("signed out" in x for x in s.problems())


async def test_runs_use_the_found_program_and_the_own_login(svc, phone, fake, cli):
    await done_of(phone)
    opts = fake.calls[-1][1]
    assert opts.cli_path == str(svc.claude.fake_exe)
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(svc.folder / "claude")
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        assert opts.env[k] == ""
    assert opts.env["DISABLE_AUTOUPDATER"] == "1"
    status_env = [e for a, e in cli.calls if a[:2] == ["auth", "status"]][-1]
    assert status_env["CLAUDE_CONFIG_DIR"] == str(svc.folder / "claude")  # the status looked at the same login


async def test_own_login_can_be_switched_off(svc, phone, fake):
    await svc.set_own_login(False)
    assert svc.settings.own_login is False and config.Settings.load(svc.folder).own_login is False
    await done_of(phone)
    assert "CLAUDE_CONFIG_DIR" not in fake.calls[-1][1].env


# --- signing in and out -----------------------------------------------------------------------------------------------------

async def test_sign_in_opens_the_browser_page_and_signs_in(home):
    claude, cli = make_claude(home)
    cli.signed_in = False
    cli.login_finishes_at_once = False
    await claude.refresh()
    assert claude.signed_in is False
    task = asyncio.ensure_future(claude.sign_in())
    await until(lambda: claude.login.url is not None)
    assert claude.login.state == "waiting" and claude.login.url == LOGIN_LINK
    args, env = cli.calls[-1]
    assert args == ["auth", "login", "--claudeai"] and env["CLAUDE_CONFIG_DIR"] == str(home / "claude")
    cli.procs[0].done.set()
    assert await task is True
    assert claude.signed_in is True and claude.login.state == "idle" and claude.account.email == EMAIL


async def test_sign_in_can_be_cancelled(home):
    claude, cli = make_claude(home)
    cli.signed_in = False
    cli.login_finishes_at_once = False
    await claude.refresh()
    task = asyncio.ensure_future(claude.sign_in())
    await until(lambda: claude.login.url is not None)
    claude.cancel_sign_in()
    assert await task is False
    assert claude.login.state == "idle" and claude.signed_in is False and cli.procs[0].killed


async def test_a_failed_sign_in_says_so_without_the_link(home):
    claude, cli = make_claude(home)
    cli.signed_in = False
    cli.login_exit = 1
    cli.login_lines = ["Opening browser to sign in…", f"If the browser didn't open, visit: {LOGIN_LINK}",
                       "Error: access denied"]
    assert await claude.sign_in() is False
    assert claude.login.state == "failed" and "access denied" in claude.login.error
    assert "http" not in claude.login.error


async def test_a_sign_in_that_leaves_it_signed_out_is_a_failure(home):
    claude, cli = make_claude(home)
    cli.signed_in = False
    cli.login_signs_in = False
    assert await claude.sign_in() is False
    assert claude.login.state == "failed" and "still says it is signed out" in claude.login.error


async def test_sign_in_gives_up_after_a_while(home, monkeypatch):
    monkeypatch.setattr(claudecode, "LOGIN_TIMEOUT_S", 0.05)
    claude, cli = make_claude(home)
    cli.signed_in = False
    cli.login_finishes_at_once = False
    assert await claude.sign_in() is False
    assert claude.login.state == "failed" and "too long" in claude.login.error and cli.procs[0].killed


async def test_sign_in_needs_claude_code(home):
    claude, _ = make_claude(home, find=lambda c: None)
    assert await claude.sign_in() is False
    assert claude.login.state == "failed" and "isn't installed" in claude.login.error


async def test_a_console_sign_in_is_not_accepted(home):
    claude, cli = make_claude(home)
    cli.signed_in = False
    cli.method = "console"
    assert await claude.sign_in() is False
    assert "Console" in claude.login.error


async def test_sign_out(svc, cli):
    await svc.sign_out()
    assert svc.signed_in() is False and cli.count("auth", "logout") == 1
    assert any("signed out" in p for p in svc.problems())


async def test_no_email_or_link_reaches_logs_or_files(svc, phone, cli, caplog):
    caplog.set_level(logging.DEBUG, logger="claudein")
    cli.signed_in = False
    await svc.recheck_claude()
    assert await svc.sign_in() is True
    await done_of(phone)
    assert "sign-in started" in caplog.text and "sign-in finished" in caplog.text  # what is logged is only that
    blobs = caplog.text.encode()
    for f in svc.folder.rglob("*"):
        if f.is_file() and f.suffix in (".log", ".json", ".txt") and "fake-claude" not in f.parts:
            blobs += f.read_bytes()
    assert EMAIL.encode() not in blobs and b"secretstate" not in blobs and b"oauth" not in blobs
    assert (svc.folder / "claude-versions.json").exists()  # (and that file was read above)


# --- when Claude Code stops without an answer -------------------------------------------------------------------------------

async def test_one_retry_when_it_stops_before_saying_anything(svc, phone, fake):
    attempts = []

    async def script(prompt, options, tools):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the program stopped")
        for x in stream_text("ok"):
            yield x
        yield result("ok")

    fake.script = script
    done = await done_of(phone)
    assert done["ending"] == "done" and len(attempts) == 2 and svc.claude.bad_streak == 0


async def test_no_retry_once_something_reached_the_caller(svc, phone, fake):
    attempts = []

    async def script(prompt, options, tools):
        attempts.append(1)
        for x in stream_text("partial"):
            yield x
        raise RuntimeError("the program stopped")

    fake.script = script
    done = await done_of(phone)
    assert len(attempts) == 1 and done["ending"].startswith("failed: Claude Code stopped without an answer")


async def test_no_retry_after_a_tool_was_called(svc, phone, fake):
    attempts = []

    async def script(prompt, options, tools):
        attempts.append(1)
        await tools["echo"]({})
        raise RuntimeError("the program stopped")
        yield  # noqa: unreachable, makes this an async generator

    fake.script = script
    tool = {"name": "echo", "description": "x", "input_schema": {"type": "object", "properties": {}}}
    events = await phone_ask_with_tool(svc, tool)
    assert len(attempts) == 1 and events[-1]["error_kind"] == "service"


async def phone_ask_with_tool(svc: Service, tool: dict) -> list[dict]:
    _, code = svc.pair("Signal Lab", "Tablet")
    p = Phone(f"http://127.0.0.1:{svc.phone_port}", code)
    try:
        return await p.ask(spec(tools=[tool]), on_tool=lambda e: {"text": "ok", "is_error": False})
    finally:
        await p.close()


async def test_a_version_that_keeps_failing_is_called_out_and_recovers(svc, phone, fake):
    attempts = []

    async def broken(prompt, options, tools):
        attempts.append(1)
        raise RuntimeError("the program stopped")
        yield  # noqa: unreachable

    fake.script = broken
    d1 = await done_of(phone)
    assert d1["error_kind"] == "service" and len(attempts) == 2 and not svc.claude.suspect
    await done_of(phone)
    assert svc.claude.suspect and any("isn't working" in p for p in svc.problems())
    assert svc.status()["claude"]["suspect"] is True
    fake.script = FakeClaude.plain
    assert (await done_of(phone))["ending"] == "done"
    assert not svc.claude.suspect and svc.problems() == []


async def test_a_signed_out_answer_is_not_blamed_on_the_version(svc, phone, fake):
    async def script(prompt, options, tools):
        yield result("Not logged in · Please run /login", is_error=True)

    fake.script = script
    await done_of(phone)
    await done_of(phone)
    assert svc.claude.bad_streak == 0 and svc.signed_in() is False


def test_unreadable_messages_are_skipped_not_fatal(monkeypatch):
    from claude_agent_sdk._internal import client as sdk_client

    monkeypatch.setattr(sdk_client, "parse_message", sdk_client.parse_message)  # put back after this test
    before = claudecode.skipped_messages()
    claudecode.patch_sdk_parser()
    wrapped = sdk_client.parse_message
    claudecode.patch_sdk_parser()
    assert sdk_client.parse_message is wrapped  # never wrapped twice
    assert wrapped({"type": "result"}) is None  # a result missing its fields
    assert wrapped({"type": "from_a_newer_claude_code"}) is None  # a type nobody knows yet
    assert wrapped("not even a message") is None
    assert claudecode.skipped_messages() >= before + 2
    ok = wrapped({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}, "uuid": "u", "session_id": "s"})
    assert ok is not None


# --- ClaudeIn's own copy ----------------------------------------------------------------------------------------------------

async def serve_release(version: str, payload: bytes, checksum: str | None = None) -> TestServer:
    sha = checksum or hashlib.sha256(payload).hexdigest()
    entry = {"binary": "claude.exe", "checksum": sha, "size": len(payload)}
    manifest = {"version": version, "platforms": {"win32-x64": entry, "win32-arm64": entry}}
    app = web.Application()

    async def man(request):
        return web.json_response(manifest)

    async def exe(request):
        return web.Response(body=payload)

    app.router.add_get(f"/{version}/manifest.json", man)
    app.router.add_get(f"/{version}/{{platform}}/claude.exe", exe)
    server = TestServer(app)
    await server.start_server()
    return server


async def good(path: Path) -> bool:
    return True


async def bad(path: Path) -> bool:
    return False


def own_claude(home: Path, server: TestServer, verify=good) -> tuple[ClaudeCode, FakeCli]:
    cli = FakeCli()
    claude = ClaudeCode(home, cli=cli.run, spawn=cli.spawn, releases=str(server.make_url("")).rstrip("/"),
                        verify_signature=verify)
    claude.attach(config.Settings.load(home), lambda: None)
    return claude, cli


async def test_own_copy_is_downloaded_checked_and_used(home):
    payload = b"MZ" + b"x" * 5000
    server = await serve_release("2.1.325", payload)
    try:
        old = home / "claude-code" / "2.1.1"
        old.mkdir(parents=True)
        (old / "claude.exe").write_bytes(b"old")
        claude, _ = own_claude(home, server)
        assert await claude.install_own_copy() is True
    finally:
        await server.close()
    final = home / "claude-code" / "2.1.325" / "claude.exe"
    assert final.read_bytes() == payload and not final.with_name("claude.exe.part").exists()
    assert not old.exists()  # one copy only
    assert claude.settings.claude_copy == "own" and config.Settings.load(home).claude_copy == "own"
    assert claude.install.state == "idle" and claude.path == final and claude.state == "ok"
    assert claude.env()["DISABLE_UPDATES"] == "1"  # its own copy never updates itself


async def test_a_download_with_the_wrong_checksum_is_thrown_away(home):
    server = await serve_release("2.1.325", b"MZ" + b"x" * 100, checksum="0" * 64)
    try:
        claude, _ = own_claude(home, server)
        assert await claude.install_own_copy() is False
    finally:
        await server.close()
    assert claude.install.state == "failed" and "checksum" in claude.install.error
    assert not list((home / "claude-code").rglob("claude.exe*")) and claude.settings.claude_copy == "pc"


async def test_a_download_not_signed_by_anthropic_is_thrown_away(home):
    server = await serve_release("2.1.325", b"MZ" + b"x" * 100)
    try:
        claude, _ = own_claude(home, server, verify=bad)
        assert await claude.install_own_copy() is False
    finally:
        await server.close()
    assert "signed by Anthropic" in claude.install.error
    assert not list((home / "claude-code").rglob("claude.exe*")) and claude.settings.claude_copy == "pc"


async def test_a_download_that_cannot_be_reached_fails_cleanly(home):
    server = await serve_release("2.9.9", b"x")  # serves another version only
    try:
        claude, _ = own_claude(home, server)
        assert await claude.install_own_copy() is False
    finally:
        await server.close()
    assert claude.install.state == "failed" and claude.settings.claude_copy == "pc"


async def test_going_back_to_the_pc_copy(home):
    claude, _ = make_claude(home)
    claude.attach(config.Settings.load(home), lambda: None)
    claude.settings.claude_copy = "own"
    await claude.use_installed_copy()
    assert claude.settings.claude_copy == "pc" and config.Settings.load(home).claude_copy == "pc"


# --- finding the program ----------------------------------------------------------------------------------------------------

def test_installed_copy_is_found_on_the_path(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / "claude.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nobody"))
    monkeypatch.setenv("PATH", os.pathsep.join(["relative-folder", str(tmp_path / "bin")]))
    assert claudecode.installed_copy() == exe  # a relative folder is never searched
    monkeypatch.setenv("PATH", "")
    assert claudecode.installed_copy() is None


def test_the_usual_folder_is_tried_when_the_path_has_nothing(tmp_path, monkeypatch):
    usual = tmp_path / ".local" / "bin"
    usual.mkdir(parents=True)
    (usual / "claude.exe").write_bytes(b"x")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("PATH", "")
    assert claudecode.installed_copy() == usual / "claude.exe"


def test_the_npm_shim_is_recognised_and_not_used(tmp_path, monkeypatch):
    (tmp_path / "npm").mkdir()
    (tmp_path / "npm" / "claude.cmd").write_text("@echo off")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nobody"))
    monkeypatch.setenv("PATH", str(tmp_path / "npm"))
    assert claudecode.installed_copy() is None and claudecode.npm_shim_only()


def test_the_newest_own_copy_wins_by_number_not_by_letters(tmp_path):
    for v in ("2.1.9", "2.1.10"):
        (tmp_path / "claude-code" / v).mkdir(parents=True)
        (tmp_path / "claude-code" / v / "claude.exe").write_bytes(b"x")
    (tmp_path / "claude-code" / "2.1.11").mkdir()  # no program inside
    assert claudecode.own_copy(tmp_path) == tmp_path / "claude-code" / "2.1.10" / "claude.exe"
    assert claudecode.own_copy(tmp_path / "nothing") is None


def test_small_helpers():
    assert claudecode.parse_version("2.1.325 (Claude Code)") == (2, 1, 325) and claudecode.parse_version("hi") is None
    assert claudecode.pretty_plan("max", "max_5x") == "Max 5x" and claudecode.pretty_plan("pro", None) == "Pro"
    assert claudecode.pretty_plan(None, None) is None
    assert claudecode.safe_link("https://claude.com/cai/oauth/authorize?x=1")
    assert claudecode.safe_link("https://console.anthropic.com/oauth")
    for url in ("http://claude.com/x", "https://claude.com.evil.example/x", "https://evil.example@claude.com/x",
                "https://evil.example/claude.com/", "file:///c:/x", None, ""):
        assert not claudecode.safe_link(url), url


def test_settings_keep_a_sane_copy_choice():
    assert config.Settings(claude_copy="somewhere").clamp().claude_copy == "pc"
    assert config.Settings().own_login is True


# --- the real plumbing (a real program, no Claude) --------------------------------------------------------------------------

async def test_real_cli_runs_a_program_with_a_time_limit():
    ok = await claudecode.real_cli(Path(sys.executable), ["-c", "print('hi')"], dict(os.environ), 20)
    assert ok.code == 0 and ok.out.strip() == "hi"
    slow = await claudecode.real_cli(Path(sys.executable), ["-c", "import time; time.sleep(30)"], dict(os.environ), 0.5)
    assert slow.timed_out and slow.code == -1
    gone = await claudecode.real_cli(Path("does-not-exist.exe"), [], dict(os.environ), 5)
    assert gone.code == -1 and not gone.timed_out


async def test_real_spawn_streams_lines_and_can_be_stopped():
    proc = await claudecode.real_spawn(Path(sys.executable), ["-c", "print('a'); print('b')"], dict(os.environ))
    assert [x async for x in proc.lines()] == ["a", "b"] and await proc.wait() == 0
    slow = await claudecode.real_spawn(Path(sys.executable), ["-c", "import time; time.sleep(30)"], dict(os.environ))
    slow.kill()
    assert await asyncio.wait_for(slow.wait(), 10) != 0


# --- the windows ------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


async def test_the_claude_page_follows_the_state(qapp, svc, cli):
    from claudein.windows import ClaudePage, claude_line

    page = ClaudePage(svc)
    page.refresh()
    assert not page.switch_b.isHidden() and page.sign_b.isHidden() and page.download_b.isHidden()
    assert EMAIL in page.step2.caption.text() and "2.1.325" in page.step1.caption.text()
    assert claude_line(svc.status()) == f"Claude Code 2.1.325 — signed in as {EMAIL} (Max 5x) ✓"

    cli.signed_in = False
    await svc.recheck_claude()
    page.refresh()
    assert not page.sign_b.isHidden() and page.switch_b.isHidden() and "Not signed in" in page.step2.caption.text()
    assert claude_line(svc.status()).endswith("signed out ✗")

    svc.claude.login = LoginState("waiting", LOGIN_LINK)
    page.refresh()
    assert not page.waiting.isHidden() and page.again_b.isEnabled() and not page.sign_b.isEnabled()
    svc.claude.login = LoginState("failed", None, "Sign-in didn't finish.")
    page.refresh()
    assert page.waiting.isHidden() and "Sign-in didn't finish." in page.step2.caption.text()

    svc.claude._find = lambda c: None
    await svc.recheck_claude()
    page.refresh()
    assert not page.download_b.isHidden() and page.sign_b.isHidden() and "Do step 1 first." in page.step2.caption.text()
    assert "isn't installed" in page.step1.caption.text()
    assert claude_line(svc.status()) == "Claude Code — not installed"
    assert svc.status()["claude"]["needs_setup"]


async def test_the_settings_window_and_panel_build_with_the_claude_parts(qapp, svc, cli):
    from PySide6.QtGui import QIcon

    from claudein.panel import Panel
    from claudein.windows import SettingsWindow

    opened = []
    window = SettingsWindow(svc, lambda: None, lambda: None, QIcon())
    panel = Panel(svc, QIcon(), opened.append, lambda: None)
    window.open("claude")
    assert window.stackedWidget.currentWidget() is window.claude
    window.home.refresh()
    assert window.home.setup_b.isHidden()  # signed in: nothing to set up
    panel.refresh()
    assert panel.setup_b.isHidden()
    cli.signed_in = False
    await svc.recheck_claude()
    window.home.refresh()
    panel.refresh()
    assert not window.home.setup_b.isHidden() and not panel.setup_b.isHidden()
    assert panel.setup_b.text() == "Sign in with Claude" and "signed out" in panel.problem.text()
    svc.claude._find = lambda c: None
    await svc.recheck_claude()
    panel.refresh()
    assert panel.setup_b.text() == "Set up Claude…"
    window.close()


async def test_the_claude_page_lists_versions_and_offers_the_safety_net(qapp, svc, cli):
    from claudein.windows import ClaudePage

    page = ClaudePage(svc)
    update_fake_claude(svc.claude, cli, "2.1.500", [f for f in cli.flags if f != "--restricted"])
    await svc.recheck_claude()
    page.refresh()
    assert "--restricted" in page.step1.caption.text() and page.versions_box.count() == 2
    assert not page.own_b.isHidden() and page.installed_b.isHidden()
    svc.settings.claude_copy = "own"
    page.refresh()
    assert page.own_b.isHidden() and not page.installed_b.isHidden()
    svc.claude.install = claudecode.InstallState("downloading", 5_000_000, 10_000_000)
    page.refresh()
    assert not page.progress.isHidden() and not page.own_b.isEnabled() and "5 of 10 MB" in page.copy_row.caption.text()
