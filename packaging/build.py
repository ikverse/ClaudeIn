"""Builds ClaudeIn-Setup.exe: icon, version details, PyInstaller's starter built from source, the program, the installer,
then a Windows Defender scan of both.

    .venv\\Scripts\\python.exe packaging\\build.py                  everything
    .venv\\Scripts\\python.exe packaging\\build.py --no-installer   only the program, in build\\dist\\ClaudeIn
    .venv\\Scripts\\python.exe packaging\\build.py --test           a "ClaudeIn Test" installer with its own app ID, which
                                                                   never stops or changes the real ClaudeIn
    ... --prebuilt-starter     use PyInstaller's ready-made starter (when Visual Studio's C++ tools aren't on this PC)

Needs PyInstaller (`pip install -r requirements-build.txt`), Visual Studio's C++ tools (for the starter), and Inno Setup 6
(for the installer). Everything is made under build\\ (not kept in git).

Why the starter is built here: every PyInstaller program ships the same small starter program, and since malware uses
PyInstaller too, Windows Defender's guesswork often flags it. One compiled on this PC is different (phase 7).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = BUILD / "dist"
PROGRAM = DIST / "ClaudeIn"
ICON = BUILD / "claudein.ico"
VERSION_FILE = BUILD / "version_info.txt"
STARTER_MARK = BUILD / "own-starter.sha256"  # the fingerprint of the starter compiled here
PUBLISHER = "ikverse"
SITE = "https://github.com/ikverse/ClaudeIn"


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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- version details --------------------------------------------------------------------------------------------------------

def version_info_text(version: str) -> str:
    """PyInstaller's version-resource recipe: the details Windows shows under the program's Properties, Details."""
    nums = tuple(int(x) for x in version.split(".")) + (0,)
    strings = [("CompanyName", PUBLISHER), ("FileDescription", "ClaudeIn: lets your apps use Claude through this PC"),
               ("FileVersion", version), ("InternalName", "ClaudeIn"), ("LegalCopyright", f"© {PUBLISHER}"),
               ("OriginalFilename", "ClaudeIn.exe"), ("ProductName", "ClaudeIn"), ("ProductVersion", version),
               ("Comments", SITE)]
    table = ",\n        ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings)
    return (f"VSVersionInfo(\n  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, "
            f"OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n  kids=[\n"
            f"    StringFileInfo([StringTable('040904B0', [\n        {table}])]),\n"
            f"    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n  ]\n)\n")


# --- PyInstaller's starter, built here ---------------------------------------------------------------------------------------

def find_vcvars() -> Path | None:
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return None
    r = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                       capture_output=True, text=True)
    path = Path(r.stdout.strip().splitlines()[0]) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat" if r.stdout.strip() else None
    return path if path and path.exists() else None


def starter_path() -> Path:
    import PyInstaller

    return Path(PyInstaller.__file__).parent / "bootloader" / "Windows-64bit-intel" / "runw.exe"


def own_starter() -> None:
    """Makes sure PyInstaller uses a starter compiled on this PC, compiling it (a few minutes) when it doesn't yet."""
    if STARTER_MARK.exists() and starter_path().exists() and STARTER_MARK.read_text().strip() == sha256(starter_path()):
        print("PyInstaller's starter: the one compiled on this PC.")
        return
    vcvars = find_vcvars()
    if vcvars is None:
        sys.exit("Building PyInstaller's starter needs Visual Studio's C++ tools, which weren't found. "
                 "Install them, or build with --prebuilt-starter (more likely to trip Windows Defender).")
    import PyInstaller

    before = sha256(starter_path()) if starter_path().exists() else ""
    print(f"Compiling PyInstaller {PyInstaller.__version__}'s starter from source (a few minutes)…", flush=True)
    pip = (f'"{sys.executable}" -m pip install --force-reinstall --no-deps --no-cache-dir --no-binary pyinstaller '
           f'"pyinstaller=={PyInstaller.__version__}"')
    # One string, not a list: Python's quoting of a list garbles the nested quotes cmd needs. /s keeps them as written.
    r = subprocess.run(f'cmd /d /s /c "call "{vcvars}" >nul && {pip}"', cwd=ROOT,
                       env={**os.environ, "PYINSTALLER_COMPILE_BOOTLOADER": "1"})
    # PyInstaller's source package has no ready-made Windows starters, so a successful install means it compiled one.
    if r.returncode != 0 or not starter_path().exists():
        sys.exit("Compiling PyInstaller's starter failed.")
    after = sha256(starter_path())
    print("Starter fingerprint:", after[:16], "(was", (before[:16] or "none") + ")")
    STARTER_MARK.write_text(after + "\n")
    print("PyInstaller's starter: compiled on this PC.")


# --- Windows Defender ---------------------------------------------------------------------------------------------------------

def find_mpcmdrun() -> Path | None:
    for p in (Path(os.environ.get("ProgramData", "")) / "Microsoft" / "Windows Defender" / "Platform" / "CurrentVersion"
              / "MpCmdRun.exe", Path(os.environ.get("ProgramFiles", "")) / "Windows Defender" / "MpCmdRun.exe"):
        if p.exists():
            return p
    return None


def defender_scan(*paths: Path) -> None:
    """Scans with Windows Defender, reporting only (nothing is quarantined or changed). A finding stops the build."""
    mp = find_mpcmdrun()
    if mp is None:
        print("Windows Defender's scanner wasn't found; the build wasn't scanned.")
        return
    for p in paths:
        r = subprocess.run([str(mp), "-Scan", "-ScanType", "3", "-File", str(p), "-DisableRemediation"],
                           capture_output=True, text=True)
        found = [x.strip() for x in (r.stdout or "").splitlines() if re.search(r"threat|found", x, re.I)]
        if r.returncode == 2:
            sys.exit(f"Windows Defender flags {p.name}: " + " | ".join(found[:4]))
        if r.returncode != 0:
            print(f"Windows Defender couldn't scan {p.name} (exit {r.returncode}).")
        else:
            print(f"Windows Defender: no threat found in {p.name}.")


def main(argv: list[str]) -> None:
    sys.path.insert(0, str(ROOT))  # run as a script, so the project folder isn't on the path yet
    from claudein import config

    test = "--test" in argv
    shutil.rmtree(DIST, ignore_errors=True)
    BUILD.mkdir(exist_ok=True)
    VERSION_FILE.write_text(version_info_text(config.VERSION), encoding="utf-8")
    if "--prebuilt-starter" in argv:
        print("Using PyInstaller's ready-made starter (more likely to trip Windows Defender).")
    else:
        own_starter()
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
        defender_scan(PROGRAM)
        return
    iscc = find_iscc()
    if iscc is None:
        sys.exit("Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup), so no installer was made.")
    out = BUILD / "installer"
    run(str(iscc), f"/DAppVersion={config.VERSION}", f"/DSourceDir={PROGRAM}", f"/DOutputDir={out}", f"/DIcon={ICON}",
        *(["/DTestBuild"] if test else []), str(ROOT / "packaging" / "ClaudeIn.iss"))
    made = out / (f"ClaudeIn-Test-Setup-{config.VERSION}.exe" if test else f"ClaudeIn-Setup-{config.VERSION}.exe")
    print("Installer:", made, f"({made.stat().st_size / 1e6:.0f} MB)")
    defender_scan(PROGRAM, made)


if __name__ == "__main__":
    main(sys.argv[1:])
