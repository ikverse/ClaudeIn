"""Builds ClaudeIn-Setup.exe: icon, then the program (PyInstaller), then the installer (Inno Setup).

    .venv\\Scripts\\python.exe packaging\\build.py            both steps
    .venv\\Scripts\\python.exe packaging\\build.py --no-installer   only the program, in build\\dist\\ClaudeIn

Needs PyInstaller (`pip install -r requirements-build.txt`) and, for the installer, Inno Setup 6.
Everything is made under build\\ (not kept in git).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = BUILD / "dist"
PROGRAM = DIST / "ClaudeIn"
ICON = BUILD / "claudein.ico"


def run(*cmd: str) -> None:
    print(">", " ".join(cmd), flush=True)
    r = subprocess.run(list(cmd), cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
    if r.returncode != 0:
        sys.exit(f"Failed: {cmd[0]} (exit {r.returncode})")


def find_iscc() -> Path | None:
    from_path = shutil.which("ISCC")
    candidates = [Path(from_path)] if from_path else []
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            sub = "Programs\\Inno Setup 6" if base == os.environ.get("LOCALAPPDATA") else "Inno Setup 6"
            candidates.append(Path(base) / sub / "ISCC.exe")
    return next((c for c in candidates if c.exists()), None)


def size_mb(folder: Path) -> float:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) / 1e6


def main(argv: list[str]) -> None:
    sys.path.insert(0, str(ROOT))  # run as a script, so the project folder isn't on the path yet
    from claudein import config

    shutil.rmtree(DIST, ignore_errors=True)
    BUILD.mkdir(exist_ok=True)
    run(sys.executable, str(ROOT / "packaging" / "make_icon.py"), str(ICON))
    run(sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "ClaudeIn.spec"), "--noconfirm", "--clean",
        "--distpath", str(DIST), "--workpath", str(BUILD / "work"))

    exe = PROGRAM / "ClaudeIn.exe"
    if not exe.exists():
        sys.exit("The build made no ClaudeIn.exe.")
    stray = [p for p in PROGRAM.rglob("claude.exe")]
    if stray:  # ClaudeIn runs the Claude Code installed on the PC; the SDK's own copy must not ship
        sys.exit("The program contains the SDK's own claude.exe: " + str(stray[0]))
    print(f"Program: {exe} ({size_mb(PROGRAM):.0f} MB in total)")

    if "--no-installer" in argv:
        return
    iscc = find_iscc()
    if iscc is None:
        sys.exit("Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup), so no installer was made.")
    out = BUILD / "installer"
    run(str(iscc), f"/DAppVersion={config.VERSION}", f"/DSourceDir={PROGRAM}", f"/DOutputDir={out}", f"/DIcon={ICON}",
        str(ROOT / "packaging" / "ClaudeIn.iss"))
    made = sorted(out.glob("ClaudeIn-Setup-*.exe"))
    print("Installer:", made[-1], f"({made[-1].stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main(sys.argv[1:])
