"""Phase 6: updates from ClaudeIn's GitHub releases (plan 4.10). A fake GitHub on this PC and test keys only: no real
GitHub, no real signing key, no installer is started."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PySide6.QtWidgets import QApplication

from claudein import config, updates
from claudein.updates import Release, UpdateState, Updater

from .conftest import until

ROOT = Path(__file__).resolve().parent.parent
RELEASES = "/repos/ikverse/ClaudeIn/releases/latest"
DOWNLOAD = "/ikverse/ClaudeIn/releases/download/"
INSTALLER = b"MZ fake installer " + b"x" * 3000


def keypair() -> tuple[Ed25519PrivateKey, str]:
    k = Ed25519PrivateKey.generate()
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return k, base64.b64encode(raw).decode()


class FakeGitHub:
    """Serves one "latest release" and its files, and remembers what was asked."""

    def __init__(self):
        self.release: dict | None = None
        self.status = 200
        self.files: dict[str, bytes] = {}
        self.seen: list[tuple[str, dict]] = []
        self.server: TestServer | None = None

    @property
    def base(self) -> str:
        return str(self.server.make_url("")).rstrip("/")

    async def start(self) -> "FakeGitHub":
        app = web.Application()

        async def latest(request):
            self.seen.append((request.path, dict(request.headers)))
            if self.status != 200:
                return web.json_response({"message": "no"}, status=self.status)
            return web.json_response(self.release)

        async def download(request):
            self.seen.append((request.path, dict(request.headers)))
            data = self.files.get(request.match_info["name"])
            return web.Response(body=data) if data is not None else web.Response(status=404)

        app.router.add_get(RELEASES, latest)
        app.router.add_get(DOWNLOAD + "{tag}/{name}", download)
        self.server = TestServer(app)
        await self.server.start_server()
        return self

    def publish(self, version: str, key: Ed25519PrivateKey | None, *, payload: bytes = INSTALLER,
                signed_version: str | None = None, signed_payload: bytes | None = None, notes: str = "Faster sign-in.",
                url_base: str | None = None, **extra) -> None:
        name = updates.installer_name(version)
        tag = f"v{version}"
        base = url_base or f"{self.base}{DOWNLOAD}{tag}/"
        self.files = {name: payload}
        assets = [{"name": name, "browser_download_url": base + name, "size": len(payload)}]
        if key is not None:
            data = signed_payload if signed_payload is not None else payload
            import hashlib

            sig = base64.b64encode(key.sign(updates.signed_message(signed_version or version,
                                                                   hashlib.sha256(data).hexdigest()))).decode()
            self.files[name + ".sig"] = sig.encode()
            assets.append({"name": name + ".sig", "browser_download_url": base + name + ".sig", "size": len(sig)})
        self.release = {"tag_name": tag, "draft": False, "prerelease": False, "body": notes,
                        "html_url": f"{self.base}/release/{tag}", "assets": assets, **extra}

    def downloads(self) -> int:
        return sum(1 for p, _ in self.seen if p.startswith(DOWNLOAD))


@pytest.fixture
async def github():
    g = await FakeGitHub().start()
    try:
        yield g
    finally:
        await g.server.close()


@pytest.fixture
def keys():
    return keypair()


def make_updater(tmp_path: Path, github: FakeGitHub, public: str, launched: list | None = None, **kw) -> Updater:
    settings = config.Settings()
    kw.setdefault("can_install", True)
    kw.setdefault("version", "0.2.0")
    return Updater(tmp_path, settings, api=github.base + RELEASES, downloads=github.base + DOWNLOAD,
                   public_key=public, launch=(launched.append if launched is not None else lambda p: None), **kw)


# --- checking ---------------------------------------------------------------------------------------------------------------

async def test_a_newer_release_is_offered(tmp_path, github, keys):
    github.publish("0.3.0", keys[0])
    u = make_updater(tmp_path, github, keys[1])
    r = await u.check()
    assert r is not None and r.version == "0.3.0" and r.notes == "Faster sign-in."
    assert u.status.state == "available" and u.available is r and u.status.checked_at


@pytest.mark.parametrize("version", ["0.2.0", "0.1.9"])
async def test_the_same_or_an_older_release_is_not_offered(tmp_path, github, keys, version):
    github.publish(version, keys[0])
    u = make_updater(tmp_path, github, keys[1])
    assert await u.check() is None
    assert u.status.state == "up_to_date" and u.available is None


async def test_no_releases_yet_means_up_to_date(tmp_path, github, keys):
    github.status = 404
    u = make_updater(tmp_path, github, keys[1])
    assert await u.check() is None and u.status.state == "up_to_date"


async def test_a_github_limit_is_quiet(tmp_path, github, keys):
    github.status = 403
    u = make_updater(tmp_path, github, keys[1])
    assert await u.check() is None
    assert u.status.state == "failed" and "limiting" in u.status.error and u.available is None


async def test_github_out_of_reach_is_quiet(tmp_path, keys):
    u = Updater(tmp_path, config.Settings(), api="http://127.0.0.1:9/nothing", public_key=keys[1], can_install=True)
    assert await u.check() is None
    assert u.status.state == "failed" and "Couldn't reach" in u.status.error


async def test_a_release_without_a_signature_is_not_offered(tmp_path, github, keys):
    github.publish("0.3.0", None)
    u = make_updater(tmp_path, github, keys[1])
    assert await u.check() is None and u.status.state == "up_to_date"


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
async def test_drafts_and_pre_releases_are_not_offered(tmp_path, github, keys, flag):
    github.publish("0.3.0", keys[0], **{flag: True})
    u = make_updater(tmp_path, github, keys[1])
    assert await u.check() is None


async def test_the_check_sends_nothing_about_the_owner(tmp_path, github, keys):
    github.publish("0.3.0", keys[0])
    u = make_updater(tmp_path, github, keys[1])
    await u.check()
    path, headers = github.seen[-1]
    assert path == RELEASES
    assert headers["User-Agent"] == "ClaudeIn/0.2.0"
    assert not any(h.lower() in ("authorization", "cookie") for h in headers)


async def test_automatic_checks_follow_the_switch(tmp_path, github, keys):
    github.publish("0.3.0", keys[0])
    u = make_updater(tmp_path, github, keys[1])
    u.settings.check_updates = False
    task = asyncio.ensure_future(u.watch(first_s=0, every_s=0.05))
    try:
        await asyncio.sleep(0.3)
        assert github.seen == []  # off: GitHub is never asked
        u.settings.check_updates = True
        await until(lambda: u.status.state == "available")
    finally:
        task.cancel()


# --- installing -------------------------------------------------------------------------------------------------------------

async def test_a_signed_update_is_downloaded_checked_and_started(tmp_path, github, keys):
    github.publish("0.3.0", keys[0])
    launched: list[Path] = []
    u = make_updater(tmp_path, github, keys[1], launched)
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates" / "ClaudeIn-Setup-0.2.5.exe").write_bytes(b"old")
    await u.check()
    assert await u.install() is True
    final = tmp_path / "updates" / "ClaudeIn-Setup-0.3.0.exe"
    assert launched == [final] and final.read_bytes() == INSTALLER
    assert sorted(p.name for p in (tmp_path / "updates").iterdir()) == [final.name]  # only this installer is kept
    assert u.status.state == "installing"


async def check_refused(u: Updater, tmp_path: Path, launched: list, why: str) -> None:
    await u.check()
    assert await u.install() is False
    assert launched == [] and u.status.state == "failed" and why in u.status.error
    folder = tmp_path / "updates"
    assert not folder.exists() or not list(folder.iterdir())  # nothing is left behind
    assert u.available is not None  # still offered, so the owner can try again


async def test_an_update_signed_with_another_key_is_thrown_away(tmp_path, github, keys):
    other, _ = keypair()
    github.publish("0.3.0", other)
    launched: list = []
    await check_refused(make_updater(tmp_path, github, keys[1], launched), tmp_path, launched, "isn't signed")


async def test_an_older_signed_installer_under_a_newer_name_is_refused(tmp_path, github, keys):
    github.publish("0.3.0", keys[0], signed_version="0.2.5")  # a real old signature, replayed
    launched: list = []
    await check_refused(make_updater(tmp_path, github, keys[1], launched), tmp_path, launched, "isn't signed")


async def test_a_changed_installer_is_refused(tmp_path, github, keys):
    github.publish("0.3.0", keys[0], payload=INSTALLER + b"evil", signed_payload=INSTALLER)
    launched: list = []
    await check_refused(make_updater(tmp_path, github, keys[1], launched), tmp_path, launched, "isn't signed")


async def test_files_from_anywhere_else_are_never_downloaded(tmp_path, github, keys):
    github.publish("0.3.0", keys[0], url_base=github.base + "/someone-else/releases/")
    launched: list = []
    await check_refused(make_updater(tmp_path, github, keys[1], launched), tmp_path, launched,
                        "aren't in ClaudeIn's own GitHub releases")
    assert github.downloads() == 0


async def test_a_copy_run_from_venv_is_not_updated_here(tmp_path, github, keys):
    github.publish("0.3.0", keys[0])
    launched: list = []
    u = make_updater(tmp_path, github, keys[1], launched, can_install=False)
    await u.check()
    assert u.available is not None  # it is still told about the new version
    assert await u.install() is False
    assert "git" in u.status.error and launched == [] and github.downloads() == 0


async def test_install_without_an_offer_does_nothing(tmp_path, github, keys):
    u = make_updater(tmp_path, github, keys[1])
    assert await u.install() is False and github.downloads() == 0


# --- the pieces that must agree ---------------------------------------------------------------------------------------------

def test_signing_and_checking_agree(tmp_path):
    k, pub = keypair()
    f = tmp_path / "ClaudeIn-Setup-1.2.3.exe"
    f.write_bytes(INSTALLER)
    import hashlib

    digest = hashlib.sha256(INSTALLER).hexdigest()
    sig = updates.sign(f, "1.2.3", k)
    assert updates.verify(pub, "1.2.3", digest, sig)
    assert not updates.verify(pub, "1.2.4", digest, sig)
    assert not updates.verify(pub, "1.2.3", "0" * 64, sig)
    assert not updates.verify(pub, "1.2.3", digest, "not a signature")
    assert not updates.verify("not a key", "1.2.3", digest, sig)


def test_the_built_in_public_key_is_a_real_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(base64.b64decode(updates.PUBLIC_KEY))  # raises if it isn't


def test_the_installer_restarts_claudein_after_an_update():
    iss = (ROOT / "packaging" / "ClaudeIn.iss").read_text(encoding="utf-8")
    assert "/UPDATE=1" in updates.INSTALLER_ARGS
    assert "{param:update|0}" in iss and "Check: IsUpdate" in iss


def load_release():
    spec = importlib.util.spec_from_file_location("claudein_release", ROOT / "packaging" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_release_sets_the_version_and_refuses_bad_ones():
    rel = load_release()
    text = 'x = 1\nVERSION = "0.2.0"\nHELLO_APP = "a"\n'
    assert rel.set_version(text, "0.3.1") == 'x = 1\nVERSION = "0.3.1"\nHELLO_APP = "a"\n'
    with pytest.raises(rel.Stop):
        rel.set_version("no version here", "1.0.0")
    rel.check_version("0.3.0", "0.2.0")
    rel.check_version("0.2.0", "0.2.0")  # the same is fine while its tag is unused
    for bad in ("0.1.9", "v0.3.0", "0.3", "latest"):
        with pytest.raises(rel.Stop):
            rel.check_version(bad, "0.2.0")


def test_the_version_in_config_is_the_one_the_release_script_edits():
    rel = load_release()
    assert rel.set_version((ROOT / "claudein" / "config.py").read_text(encoding="utf-8"), config.VERSION) == \
        (ROOT / "claudein" / "config.py").read_text(encoding="utf-8")


# --- the service and the window ---------------------------------------------------------------------------------------------

async def test_the_service_checks_in_the_background_and_stops_cleanly(home, fake):
    from claudein.service import Service

    from .conftest import make_claude

    u = Updater(home, config.Settings(), api="http://127.0.0.1:9/nothing")
    s = Service(home, query_fn=fake.query, claude=make_claude(home)[0], updater=u)
    assert s.updates is u
    await s.start(use_tls=False, phone_port=0, local_port=0)
    task = s._update_watch
    assert task is not None and not task.done()
    await s.stop()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


async def test_a_new_service_uses_its_own_settings_for_updates(home, fake):
    from claudein.service import Service

    from .conftest import make_claude

    s = Service(home, query_fn=fake.query, claude=make_claude(home)[0])
    try:
        assert s.updates.settings is s.settings and s.settings.check_updates is True
        assert s.updates.can_install is False  # tests run from .venv, never as ClaudeIn.exe
    finally:
        s.usage.close()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


async def test_the_general_page_shows_and_offers_the_update(qapp, svc):
    from claudein.windows import GeneralPage

    quits = []
    page = GeneralPage(svc, lambda: None, lambda: quits.append(1))
    u = svc.updates
    page.refresh()
    assert page.update_install_b.isHidden() and f"ClaudeIn {config.VERSION}" in page.update_card.caption.text()
    assert "updated with git" in page.update_card.caption.text()  # tests run from .venv

    release = Release("9.9.9", "New things.", "x", "y", 10, "z")
    u.can_install = True
    u.status = UpdateState("available", release)
    page.refresh()
    assert not page.update_install_b.isHidden() and page.update_install_b.text() == "Install 9.9.9"
    assert "ClaudeIn 9.9.9 is available." in page.update_card.caption.text()
    assert "New things." in page.update_card.caption.text()

    u.status = UpdateState("downloading", release, done=5_000_000, total=10_000_000)
    page.refresh()
    assert not page.update_install_b.isEnabled() and not page.update_bar.isHidden()
    assert "5 of 10 MB" in page.update_card.caption.text()

    u.status = UpdateState("failed", release, error="The update isn't signed with ClaudeIn's key.")
    page.refresh()
    assert page.update_install_b.isEnabled() and "isn't signed" in page.update_card.caption.text()

    async def installed() -> bool:
        return True

    u.install = installed
    await page._run_install()
    assert quits == [1]  # the installer runs; ClaudeIn closes so it can be replaced

    page.update_auto.setChecked(False)
    assert svc.settings.check_updates is False and config.Settings.load(svc.folder).check_updates is False
    page.refresh()
    assert "Automatic checks are off" in page.update_card.caption.text()
