"""Publishes a ClaudeIn release on GitHub, so installed copies can update themselves (plan 4.10).

    Release.bat 0.2.0 "What changed, in a sentence or two"
    Release.bat 0.2.0 "notes" --dry-run      everything except tagging, pushing and publishing

It stops at the first problem:
 1. Checks: on master; no uncommitted changes to tracked files; the version looks like 1.2.3 and isn't lower than the
    current one; its tag isn't used yet (here or on GitHub); the signing key exists and matches claudein/update_key.py;
    gh is signed in.
 2. Sets the version in claudein/config.py (if it changes) and runs the tests. Failing tests put the old version back.
 3. Commits "Release 1.2.3" (only config.py), builds the installer and signs it (ClaudeIn-Setup-1.2.3.exe.sig).
 4. Tags v1.2.3, pushes master and the tag, and creates the GitHub release with the installer and its signature.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claudein import config, updates  # noqa: E402
from claudein.claudecode import parse_version  # noqa: E402

CONFIG = ROOT / "claudein" / "config.py"
_VERSION_LINE = re.compile(r'^VERSION = "[^"]*"$', re.MULTILINE)


class Stop(Exception):
    pass


def set_version(text: str, version: str) -> str:
    """config.py's text with VERSION set to [version]."""
    if len(_VERSION_LINE.findall(text)) != 1:
        raise Stop("Couldn't find the one VERSION line in claudein/config.py.")
    return _VERSION_LINE.sub(f'VERSION = "{version}"', text)


def check_version(new: str, current: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        raise Stop(f'"{new}" isn\'t a version like 0.2.0.')
    if parse_version(new) < parse_version(current):
        raise Stop(f"{new} is lower than the current version {current}.")


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Stop(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def run(*cmd: str) -> None:
    print(">", " ".join(cmd), flush=True)
    if subprocess.run(list(cmd), cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}).returncode != 0:
        raise Stop(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} failed.")


def preflight(version: str) -> None:
    from make_signing_key import KEY, load_private_key, public_key_text

    check_version(version, config.VERSION)
    if git("rev-parse", "--abbrev-ref", "HEAD") != "master":
        raise Stop("Release from the master branch.")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise Stop("There are uncommitted changes. Commit or put them aside first.")
    tag = f"v{version}"
    if git("tag", "--list", tag) or git("ls-remote", "--tags", "origin", tag):
        raise Stop(f"The tag {tag} is already used.")
    if not KEY.exists():
        raise Stop(f"No signing key at {KEY}. Run packaging\\make_signing_key.py once (or put your backup there).")
    if public_key_text(load_private_key()) != updates.PUBLIC_KEY:
        raise Stop("The signing key doesn't match claudein/update_key.py, so installed copies would refuse it.")
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        raise Stop("gh isn't signed in to GitHub (run: gh auth login).")


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv
    if not args:
        print(__doc__)
        return 2
    version, notes = args[0], (args[1] if len(args) > 1 else f"ClaudeIn {args[0]}")
    try:
        preflight(version)
        old = CONFIG.read_text(encoding="utf-8")
        changed = version != config.VERSION
        if changed:
            CONFIG.write_text(set_version(old, version), encoding="utf-8")
        try:
            run(sys.executable, "-m", "pytest", "-q")
        except Stop:
            if changed:
                CONFIG.write_text(old, encoding="utf-8")
            raise Stop("The tests failed, so nothing was released (the version was put back).")
        if changed and not dry:
            git("commit", "-m", f"Release {version}", "--", "claudein/config.py")
        run(sys.executable, str(ROOT / "packaging" / "build.py"))
        installer = ROOT / "build" / "installer" / updates.installer_name(version)
        if not installer.exists():
            raise Stop(f"The build didn't make {installer.name}.")
        from make_signing_key import load_private_key

        sig = installer.with_name(installer.name + ".sig")
        sig.write_text(updates.sign(installer, version, load_private_key()), encoding="ascii")
        import hashlib

        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        if not updates.verify(updates.PUBLIC_KEY, version, digest, sig.read_text(encoding="ascii")):
            raise Stop("The new signature didn't check out.")
        print("Signed:", sig)
        if dry:
            if changed:
                CONFIG.write_text(old, encoding="utf-8")
            print(f"Dry run: built and signed {installer.name}; nothing was tagged, pushed or published.")
            return 0
        tag = f"v{version}"
        git("tag", "-a", tag, "-m", f"ClaudeIn {version}")
        git("push", "origin", "master")
        git("push", "origin", tag)
        run("gh", "release", "create", tag, str(installer), str(sig), "--repo", updates.REPO, "--title",
            f"ClaudeIn {version}", "--notes", notes, "--verify-tag")
        print(f"Released ClaudeIn {version}: https://github.com/{updates.REPO}/releases/tag/{tag}")
        return 0
    except Stop as e:
        print("Stopped:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
