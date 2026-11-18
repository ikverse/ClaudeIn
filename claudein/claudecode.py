"""Claude Code on this PC: finding it, checking it, signing in, and guarding against updates (plan 4.9).

ClaudeIn carries no Claude Code of its own. It uses the one installed on the PC, or, if the owner asks for it, one copy it
downloads for itself from Anthropic (checked against Anthropic's checksum and signature). So that a Claude Code update
can never crash ClaudeIn, everything here is cheap and never raises into the service:

- the check costs no plan time (`--version`, `--help`, `auth status`); it is remembered by the program file's size and
  date, so it only runs again when the file changes;
- what Claude Code lacks is named, and the lock-down flags are never dropped: if one is missing, runs are refused with a
  clear message; optional flags are left out with a note;
- every run is counted as good (Claude Code answered in its own words) or bad (it stopped without an answer), so a
  version that keeps failing is shown as "not working" instead of failing quietly;
- messages the SDK can't read are skipped, not fatal.

Nothing here logs an email, a link or any sign-in text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from . import config

log = logging.getLogger("claudein")

IS_WINDOWS = os.name == "nt"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: The Claude Code version ClaudeIn was last tested against; "Download a known-good copy" fetches this one.
KNOWN_GOOD = "2.1.325"
#: Older than this is refused ("update Claude Code"). 2.1.296 is the oldest tried with ClaudeIn.
MINIMUM = (2, 1, 296)
INSTALL_PAGE = "https://code.claude.com/docs/en/setup"
RELEASES = "https://downloads.claude.ai/claude-code-releases"
LOGIN_TIMEOUT_S = 300
VERSIONS_FILE = "claude-versions.json"

#: Flags ClaudeIn cannot run without. Each entry lists names that mean the same. They are the lock-down and the stream.
REQUIRED_FLAGS: list[tuple[str, ...]] = [
    ("--restricted",), ("--tools",), ("--allowedTools", "--allowed-tools"), ("--permission-mode",),
    ("--setting-sources",), ("--strict-mcp-config",), ("--mcp-config",), ("--output-format",), ("--input-format",),
    ("--verbose",), ("--model",),
]
#: Flags ClaudeIn leaves out, with a note, when Claude Code no longer has them.
#: --json-schema is only needed by runs that ask for an answer in a set shape (protocol 2).
OPTIONAL_FLAGS = ("--no-session-persistence", "--include-partial-messages", "--max-budget-usd", "--json-schema")

_URL = re.compile(r"https://[^\s\"'<>]+")
_SKIPPED = 0  # messages the SDK couldn't read, since ClaudeIn started (only a count, for diagnostics)


class Unavailable(Exception):
    """Claude Code can't be used right now. `kind` is missing, broken, too_old, incompatible or signed_out."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# --- running Claude Code's own commands -----------------------------------------------------------------------------------

@dataclass
class CliResult:
    code: int
    out: str = ""
    err: str = ""
    timed_out: bool = False


CliRun = Callable[[Path, list, dict, float], Awaitable[CliResult]]


class Proc(Protocol):
    """What ClaudeIn needs of a running program (the sign-in): its lines, its end, and a way to stop it."""

    def lines(self) -> AsyncIterator[str]: ...
    async def wait(self) -> int: ...
    def kill(self) -> None: ...


Spawn = Callable[[Path, list, dict], Awaitable[Proc]]


def _text(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


async def real_cli(exe: Path, args: list, env: dict, timeout: float = 15.0) -> CliResult:
    """Runs `exe args` with no window, no input and a time limit. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(exe), *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, creationflags=_NO_WINDOW)
    except OSError as e:
        return CliResult(-1, "", e.strerror or type(e).__name__)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CliResult(-1, "", "timed out", timed_out=True)
    except asyncio.CancelledError:
        proc.kill()
        raise
    return CliResult(proc.returncode if proc.returncode is not None else -1, _text(out), _text(err))


class _RealProc:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc

    async def lines(self) -> AsyncIterator[str]:
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                return
            yield _text(raw).rstrip()

    async def wait(self) -> int:
        code = await self.proc.wait()
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        return code

    def kill(self) -> None:
        # Only this one program, not a job: the browser it opened must stay open.
        try:
            self.proc.kill()
        except ProcessLookupError:
            pass


async def real_spawn(exe: Path, args: list, env: dict) -> Proc:
    proc = await asyncio.create_subprocess_exec(
        str(exe), *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env, creationflags=_NO_WINDOW)
    return _RealProc(proc)


async def authenticode_ok(path: Path) -> bool:
    """True if Windows says the file carries a valid signature from Anthropic."""
    if not IS_WINDOWS:
        return False
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:CLAUDEIN_FILE; "
              "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Anthropic') {'OK'} else {'BAD'}")
    r = await real_cli(Path("powershell.exe"), ["-NoProfile", "-NonInteractive", "-Command", script],
                       dict(os.environ, CLAUDEIN_FILE=str(path)), 60)
    return r.out.strip() == "OK"


# --- finding it -----------------------------------------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def vstr(v: tuple[int, ...]) -> str:
    return ".".join(str(x) for x in v)


def _exe_name() -> str:
    return "claude.exe" if IS_WINDOWS else "claude"


def installed_copy() -> Path | None:
    """The Claude Code installed on this PC, found the way the SDK would: an absolute PATH folder, then the usual one."""
    name = _exe_name()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d)
        if d and p.is_absolute() and (p / name).is_file():
            return p / name
    usual = Path.home() / ".local" / "bin" / name
    return usual if usual.is_file() else None


def npm_shim_only() -> bool:
    """True if `claude` is only npm's .cmd shim, which can't be run safely (the SDK refuses it too)."""
    if not IS_WINDOWS:
        return False
    return any(d and (Path(d) / "claude.cmd").is_file() for d in os.environ.get("PATH", "").split(os.pathsep))


def own_copy(folder: Path) -> Path | None:
    """ClaudeIn's own downloaded copy (the newest, if there are several)."""
    root = folder / "claude-code"
    if not root.is_dir():
        return None
    found = []
    for d in root.iterdir():
        v = parse_version(d.name)
        if v and (d / _exe_name()).is_file():
            found.append((v, d / _exe_name()))
    return max(found)[1] if found else None


# --- what was found out ---------------------------------------------------------------------------------------------------

@dataclass
class Probe:
    version: str | None = None
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    flags_checked: bool = True
    error: str | None = None  # the program didn't start or didn't say its version


@dataclass(frozen=True)
class Account:
    email: str | None
    plan: str | None
    method: str | None  # "claude.ai" is a Claude plan; anything else is not what ClaudeIn uses

    @property
    def label(self) -> str:
        who = self.email or "your Claude account"
        return f"{who} ({self.plan})" if self.plan else who


@dataclass
class LoginState:
    state: str = "idle"  # idle | waiting | failed
    url: str | None = None
    error: str | None = None


@dataclass
class InstallState:
    state: str = "idle"  # idle | downloading | verifying | failed
    done: int = 0
    total: int = 0
    error: str | None = None


def pretty_plan(sub: str | None, tier: str | None) -> str | None:
    raw = tier or sub
    if not raw:
        return None
    first, *rest = str(raw).split("_")
    return " ".join([first.capitalize(), *[r.lower() for r in rest]])


def safe_link(url: str | None) -> bool:
    """Only Anthropic's own https pages are ever opened for the owner."""
    m = re.match(r"https://([^/:]+)/", (url or "") + "/")
    host = (m[1] if m else "").lower()
    return any(host == d or host.endswith("." + d) for d in ("claude.com", "claude.ai", "anthropic.com"))


class ClaudeCode:
    def __init__(self, folder: Path, cli: CliRun = real_cli, spawn: Spawn = real_spawn,
                 find: Callable[["ClaudeCode"], Path | None] | None = None, releases: str = RELEASES,
                 verify_signature: Callable[[Path], Awaitable[bool]] = authenticode_ok):
        self.folder = folder
        self.cli, self.spawn, self._find, self.releases, self.verify_signature = cli, spawn, find, releases, verify_signature
        self.settings: config.Settings = config.Settings()
        self.on_change: Callable[[], None] = lambda: None
        self.path: Path | None = None
        self.probe: Probe | None = None
        self.account: Account | None = None
        self.signed_in: bool | None = None  # None: not known
        self.wrong_account_type = False  # signed in, but with a Console (API billing) account
        self.bad_streak = 0  # questions in a row that Claude Code stopped without an answer
        self.looked = False  # has the program been looked for yet
        self.login = LoginState()
        self.install = InstallState()
        self._key: tuple | None = None
        self._lock = asyncio.Lock()
        self._proc: Proc | None = None
        self._cancelled = False
        self.history: dict[str, dict] = self._load_history()

    def attach(self, settings: config.Settings, on_change: Callable[[], None]) -> None:
        self.settings, self.on_change = settings, on_change

    def changed(self) -> None:
        try:
            self.on_change()
        except Exception:  # a window's problem must never stop a run
            log.exception("on_change failed")

    # --- where, which login ---------------------------------------------------------------------------------------------

    @property
    def config_dir(self) -> Path:
        return self.folder / "claude"

    @property
    def own_copy_in_use(self) -> bool:
        return self.settings.claude_copy == "own"

    def locate(self) -> Path | None:
        if self._find is not None:
            return self._find(self)
        return own_copy(self.folder) if self.own_copy_in_use else installed_copy()

    def env(self) -> dict[str, str]:
        """What ClaudeIn sets for every Claude Code it starts: the Claude login only, and no updates in the middle of a run."""
        e = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "CLAUDE_CODE_USE_BEDROCK": "",
             "CLAUDE_CODE_USE_VERTEX": "", "CLAUDE_CODE_USE_FOUNDRY": "", "DISABLE_AUTOUPDATER": "1"}
        if self.own_copy_in_use:
            e["DISABLE_UPDATES"] = "1"
        if self.settings.own_login:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            e["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        return e

    def process_env(self) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        return {**base, **self.env()}

    # --- the check -------------------------------------------------------------------------------------------------------

    @staticmethod
    def _key_of(path: Path) -> tuple | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (str(path), st.st_size, st.st_mtime_ns)

    async def _check_file(self, path: Path) -> None:
        """Runs the free check again if the file is new or changed. Call with the lock held."""
        key = self._key_of(path)
        if key is not None and key == self._key and self.probe is not None:
            return
        env = self.process_env()
        v, h = await asyncio.gather(self.cli(path, ["--version"], env, 10), self.cli(path, ["--help"], env, 20))
        probe = Probe()
        ver = parse_version(v.out) if v.code == 0 else None
        if ver is None:
            probe.error = "didn't start" if not v.timed_out else "didn't answer in time"
        else:
            probe.version = vstr(ver)
            if len(h.out) > 200:  # a real help text; otherwise the flags can't be checked and runs decide
                have = h.out
                for alts in REQUIRED_FLAGS:
                    if not any(re.search(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])", have) for a in alts):
                        probe.missing_required.append(alts[0])
                for flag in OPTIONAL_FLAGS:
                    if not re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", have):
                        probe.missing_optional.append(flag)
            else:
                probe.flags_checked = False
        if self.probe is None or self.probe.version != probe.version:
            self.bad_streak = 0  # a different version gets a fresh start
        self.path, self.probe, self._key = path, probe, key
        self._remember_check()
        log.info("Claude Code check: version %s, %s", probe.version or "unknown", self.state)

    @property
    def version(self) -> str | None:
        return self.probe.version if self.probe else None

    @property
    def state(self) -> str:
        """ok, missing, broken, too_old, incompatible, or checking (not looked at yet)."""
        p = self.probe
        if p is None:
            return "missing" if self.looked else "checking"
        if p.error:
            return "broken"
        v = parse_version(p.version or "")
        if v is not None and v < MINIMUM:
            return "too_old"
        if p.missing_required:
            return "incompatible"
        return "ok"

    @property
    def suspect(self) -> bool:
        return self.bad_streak >= 2

    def missing_optional(self) -> set[str]:
        return set(self.probe.missing_optional) if self.probe else set()

    def features(self) -> list[str]:
        """What apps may ask for on this PC right now (protocol 2's feature list). Empty until Claude Code has been
        checked and found usable, since nothing works before that."""
        if self.state != "ok":
            return []
        out = ["images"]
        if "--json-schema" not in self.missing_optional():
            out.append("output_schema")
        return out

    def needs_setup(self) -> bool:
        return self.state != "ok" or self.signed_in is False

    def explain(self) -> str | None:
        """One plain sentence about what is wrong with Claude Code itself, or None."""
        s, p = self.state, self.probe
        v = self.version or "?"
        if s == "missing":
            if self.own_copy_in_use:
                return ("ClaudeIn's own copy of Claude Code is missing. Open ClaudeIn, go to Claude, and download it "
                        "again or use the one installed on this PC.")
            if npm_shim_only():
                return ("The Claude Code on this PC is the npm kind, which ClaudeIn can't run. Install the standard "
                        "one: open ClaudeIn and go to Claude, step 1.")
            return "Claude Code isn't installed on this PC. Open ClaudeIn, go to Claude, and follow step 1."
        if s == "broken":
            return "Claude Code didn't start. Reinstall it, then press Check again in ClaudeIn, Claude."
        if s == "too_old":
            return (f"Claude Code {v} is too old for ClaudeIn (it needs {vstr(MINIMUM)} or newer). Update Claude Code "
                    "on this PC (run: claude update).")
        if s == "incompatible":
            lack = ", ".join(p.missing_required) if p else "something"
            return (f"Claude Code {v} isn't compatible with ClaudeIn yet: it doesn't have {lack}. ClaudeIn needs an "
                    "update, or use a known-good copy (ClaudeIn, Claude, Advanced).")
        if s == "ok" and self.suspect:
            return (f"Claude Code {v} isn't working with ClaudeIn: two questions in a row stopped without an answer. "
                    "Try a known-good copy (ClaudeIn, Claude, Advanced).")
        return None

    def signed_out_message(self) -> str | None:
        if self.wrong_account_type:
            return ("Claude Code is signed in with an Anthropic Console account (billed by use). ClaudeIn only uses a "
                    "Claude plan. Open ClaudeIn, go to Claude, and sign in with Claude.")
        if self.signed_in is False:
            return "Claude Code is signed out. Open ClaudeIn and press Sign in with Claude."
        return None

    def problems(self) -> list[str]:
        why = self.explain() or self.signed_out_message()
        return [why] if why else []

    # --- looking ---------------------------------------------------------------------------------------------------------

    async def refresh(self) -> None:
        """Looks again at the program and the account. Never raises."""
        try:
            async with self._lock:
                path = self.locate()
                self.looked = True
                if path is None:
                    self.path, self.probe, self._key = None, None, None
                    self.account, self.signed_in, self.wrong_account_type = None, None, False
                else:
                    await self._check_file(path)
                    if self.probe and not self.probe.error:
                        await self._read_account(path)
        except Exception:
            log.exception("Claude Code check failed")
        self.changed()

    async def _read_account(self, path: Path) -> None:
        r = await self.cli(path, ["auth", "status", "--json"], self.process_env(), 20)
        data = None
        text = r.out.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        if not isinstance(data, dict) or "loggedIn" not in data:
            self.account, self.signed_in, self.wrong_account_type = None, None, False
            return
        if not data.get("loggedIn"):
            self.account, self.signed_in, self.wrong_account_type = None, False, False
            return
        method = data.get("authMethod")
        self.account = Account(data.get("email"), pretty_plan(data.get("subscriptionType"), data.get("planTier")), method)
        self.wrong_account_type = method != "claude.ai"
        self.signed_in = not self.wrong_account_type

    async def ready(self) -> Path:
        """The Claude Code to run a question with, or raises Unavailable. Free: no question is asked."""
        async with self._lock:
            path = self.locate()
            self.looked = True
            if path is None:
                self.path, self.probe, self._key = None, None, None
                raise Unavailable("missing", self.explain() or "Claude Code isn't installed.")
            await self._check_file(path)
            if self.state in ("broken", "too_old", "incompatible"):
                raise Unavailable(self.state, self.explain() or "Claude Code can't be used.")
            if self.signed_in is not True:  # maybe signed in since last looked (in a terminal, or in this window)
                await self._read_account(path)
            message = self.signed_out_message()
            if message:
                raise Unavailable("signed_out", message)
            return path

    # --- what the runs say -----------------------------------------------------------------------------------------------

    def note_run(self, answered: bool) -> None:
        """A run ended: Claude Code either spoke in its own words (answered) or stopped without an answer."""
        h = self._entry(self.version)
        if h is None:
            return
        if answered:
            self.bad_streak = 0
            h["good_runs"] = h.get("good_runs", 0) + 1
            h["last_good"] = int(time.time())
        else:
            self.bad_streak += 1
            h["bad_runs"] = h.get("bad_runs", 0) + 1
            h["last_bad"] = int(time.time())
        self._save_history()
        self.changed()

    def note_signed_out(self) -> None:
        if self.signed_in is not False:
            self.signed_in = False
            self.changed()

    def note_signed_in(self) -> None:
        if self.signed_in is not True:
            self.signed_in = True
            self.wrong_account_type = False
            self.changed()

    # --- the list of versions seen ---------------------------------------------------------------------------------------

    def _load_history(self) -> dict[str, dict]:
        try:
            data = json.loads((self.folder / VERSIONS_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_history(self) -> None:
        try:
            keep = sorted(self.history, key=lambda k: parse_version(k) or (0, 0, 0))[-20:]
            config.write_atomic(self.folder / VERSIONS_FILE, json.dumps({k: self.history[k] for k in keep}, indent=1))
        except OSError:
            log.warning("Could not save the list of Claude Code versions.")

    def _entry(self, version: str | None) -> dict | None:
        if not version:
            return None
        return self.history.setdefault(version, {})

    def _remember_check(self) -> None:
        h = self._entry(self.version)
        if h is None:
            return
        h["checked"] = int(time.time())
        h["check"] = self.state
        h["missing"] = list(self.probe.missing_required) if self.probe else []
        h["missing_optional"] = list(self.probe.missing_optional) if self.probe else []
        self._save_history()

    def versions(self) -> list[dict]:
        """For the Verified versions list: newest first."""
        rows = []
        for v, h in self.history.items():
            rows.append({"version": v, "check": h.get("check", "?"), "good": h.get("good_runs", 0),
                         "bad": h.get("bad_runs", 0), "missing": h.get("missing", []), "checked": h.get("checked"),
                         "current": v == self.version})
        return sorted(rows, key=lambda r: parse_version(r["version"]) or (0, 0, 0), reverse=True)

    # --- signing in and out ----------------------------------------------------------------------------------------------

    def _login_failed(self, why: str) -> bool:
        self.login = LoginState("failed", None, why)
        log.info("Claude sign-in failed")
        self.changed()
        return False

    async def sign_in(self) -> bool:
        """Opens Claude's own sign-in in the browser and waits for it. True once signed in with a Claude plan."""
        if self.login.state == "waiting":
            return False
        path = self.locate()
        if path is None:
            return self._login_failed("Claude Code isn't installed yet. Do step 1 first.")
        self._cancelled = False
        self.login = LoginState("waiting")
        self.changed()
        log.info("Claude sign-in started")
        try:
            proc = await self.spawn(path, ["auth", "login", "--claudeai"], self.process_env())
        except OSError:
            return self._login_failed("Claude Code didn't start.")
        self._proc = proc
        tail: list[str] = []

        async def pump() -> None:
            async for line in proc.lines():
                m = _URL.search(line)
                if m and self.login.url is None:
                    self.login.url = m.group(0).rstrip(".,)")
                    self.changed()
                else:
                    text = _URL.sub("", line).strip()
                    if text:
                        tail.append(text)
                        del tail[:-4]

        pumping = asyncio.ensure_future(pump())
        try:
            try:
                code = await asyncio.wait_for(proc.wait(), LOGIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                return self._login_failed("Sign-in took too long. Press Sign in with Claude to try again.")
            except asyncio.CancelledError:
                proc.kill()
                raise
            try:
                await asyncio.wait_for(pumping, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        finally:
            pumping.cancel()
            self._proc = None
        if self._cancelled:
            self.login = LoginState()
            self.changed()
            return False
        if code != 0:
            return self._login_failed("Sign-in didn't finish. " + " ".join(tail[-2:])[:200])
        async with self._lock:
            await self._read_account(path)
        if self.signed_in:
            self.login = LoginState()
            log.info("Claude sign-in finished")
            self.changed()
            return True
        if self.wrong_account_type:
            return self._login_failed("That sign-in was a Console account (billed by use). Sign in with your Claude "
                                      "plan instead.")
        return self._login_failed("Claude Code still says it is signed out. Try again.")

    def cancel_sign_in(self) -> None:
        self._cancelled = True
        if self._proc is not None:
            self._proc.kill()

    async def sign_out(self) -> None:
        path = self.locate()
        if path is not None:
            await self.cli(path, ["auth", "logout"], self.process_env(), 20)
        await self.refresh()

    # --- ClaudeIn's own copy ---------------------------------------------------------------------------------------------

    def _install_failed(self, why: str) -> bool:
        self.install = InstallState("failed", error=why)
        log.warning("Downloading Claude Code failed")
        self.changed()
        return False

    async def install_own_copy(self, version: str = KNOWN_GOOD) -> bool:
        """Downloads one Claude Code for ClaudeIn alone from Anthropic, checks it, and switches to it. True on success.

        The checksum comes from the release's manifest; the file must also carry Anthropic's Windows signature. Only on
        the owner's click. The copy sits in ClaudeIn's own folder and never updates itself.
        """
        import aiohttp

        if self.install.state in ("downloading", "verifying"):
            return False
        self.install = InstallState("downloading")
        self.changed()
        key = "win32-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "win32-x64"
        dest_dir = self.folder / "claude-code" / version
        part = dest_dir / (_exe_name() + ".part")
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{self.releases}/{version}/manifest.json") as r:
                    r.raise_for_status()
                    entry = (await r.json(content_type=None))["platforms"][key]
                want = str(entry["checksum"]).lower()
                self.install.total = int(entry.get("size") or 0)
                dest_dir.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                async with s.get(f"{self.releases}/{version}/{key}/{entry.get('binary', _exe_name())}") as r:
                    r.raise_for_status()
                    with part.open("wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 20):
                            f.write(chunk)
                            h.update(chunk)
                            self.install.done += len(chunk)
                            self.changed()
            self.install.state = "verifying"
            self.changed()
            if h.hexdigest() != want:
                return self._install_failed("The download didn't match Anthropic's checksum, so it was thrown away.")
            if not await self.verify_signature(part):
                return self._install_failed("The download isn't signed by Anthropic, so it was thrown away.")
            final = dest_dir / _exe_name()
            os.replace(part, final)
            check = await self.cli(final, ["--version"], self.process_env(), 20)
            if parse_version(check.out) is None:
                shutil.rmtree(dest_dir, ignore_errors=True)
                return self._install_failed("The downloaded Claude Code didn't start.")
        except asyncio.CancelledError:
            self.install = InstallState()
            raise
        except (OSError, KeyError, ValueError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            return self._install_failed("Couldn't download it: " + (getattr(e, "strerror", None) or type(e).__name__))
        finally:
            part.unlink(missing_ok=True)
        for d in (self.folder / "claude-code").iterdir():  # one copy only
            if d.is_dir() and d.name != version:
                shutil.rmtree(d, ignore_errors=True)
        self.settings.claude_copy = "own"
        self.settings.save(self.folder)
        self.install = InstallState()
        log.info("ClaudeIn now uses its own copy of Claude Code %s", version)
        await self.refresh()
        return True

    async def use_installed_copy(self) -> None:
        self.settings.claude_copy = "pc"
        self.settings.save(self.folder)
        await self.refresh()


# --- reading what Claude Code says, tolerantly ------------------------------------------------------------------------------

def skipped_messages() -> int:
    return _SKIPPED


def patch_sdk_parser() -> None:
    """A message the SDK can't read is skipped (and counted), not fatal: the run goes on, and a missing result still ends
    cleanly as "stopped without an answer". The SDK already skips message types it doesn't know."""
    from claude_agent_sdk._errors import MessageParseError
    from claude_agent_sdk._internal import client as sdk_client

    real = sdk_client.parse_message
    if getattr(real, "_claudein", False):
        return

    def tolerant(data: Any):
        global _SKIPPED
        try:
            return real(data)
        except MessageParseError:
            _SKIPPED += 1
            kind = data.get("type") if isinstance(data, dict) else None
            log.warning("Skipped a Claude Code message ClaudeIn couldn't read (type %s)", kind if isinstance(kind, str) else "?")
            return None

    tolerant._claudein = True  # type: ignore[attr-defined]
    sdk_client.parse_message = tolerant
