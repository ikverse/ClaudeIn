"""Start with Windows, restart if it stops, and the Start Menu shortcut (plan 4.6, 4.8 and phase 7). No admin rights.

- "Start with Windows" puts ClaudeIn.lnk in the owner's Startup folder. Nothing else.
- "Restart ClaudeIn if it stops" (its own switch, off by default since phase 7) adds a scheduled task in the owner's own
  account that runs `pythonw -m claudein --watchdog` every 5 minutes. The watchdog start does nothing when ClaudeIn is
  already running, or when the owner chose Quit (quit.flag); otherwise it starts ClaudeIn again. (It used to be on with
  "Start with Windows" and run every minute; Windows Defender's guesswork took that for malware.)
- Quit writes quit.flag. Any normal start (sign-in, Start Menu, setup) removes it.
"""

from __future__ import annotations

import html
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import config

#: True when running as the installed ClaudeIn.exe (PyInstaller), False when run from .venv with `pythonw -m claudein`.
FROZEN = bool(getattr(sys, "frozen", False))
#: The folder ClaudeIn starts in: the program's own folder when installed, the project folder from .venv.
PROJECT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
TASK_NAME = "ClaudeIn keep running"
RESTART_INTERVAL = "PT5M"  # how often the restart task looks (Windows' notation: 5 minutes)
QUIT_FLAG = "quit.flag"
ICON = "claudein.ico"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pythonw() -> Path:
    """The program that starts ClaudeIn: ClaudeIn.exe when installed, else the .venv's pythonw.exe (no console window)."""
    exe = Path(sys.executable)
    if FROZEN:
        return exe
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def launch_args(extra: str = "") -> str:
    """The arguments that go with pythonw(): `-m claudein` from .venv, none for ClaudeIn.exe, then [extra]."""
    return " ".join(x for x in ("" if FROZEN else "-m claudein", extra) if x)


def _programs() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_shortcut() -> Path:
    return _programs() / "Startup" / "ClaudeIn.lnk"


def start_menu_shortcut() -> Path:
    return _programs() / "ClaudeIn.lnk"


def make_shortcut(path: Path, arguments: str | None = None) -> None:
    import win32com.client  # comes with pywin32

    path.parent.mkdir(parents=True, exist_ok=True)
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
    sc.TargetPath = str(pythonw())
    sc.Arguments = launch_args() if arguments is None else arguments
    sc.WorkingDirectory = str(PROJECT)
    sc.Description = "ClaudeIn: lets your apps use Claude through this PC"
    icon = config.home() / ICON
    if icon.exists():
        sc.IconLocation = str(icon)
    sc.Save()


# --- quit flag ----------------------------------------------------------------------------------------------------------

def set_quit_flag() -> None:
    (config.home() / QUIT_FLAG).write_text("Quit from the tray. Cleared at the next normal start.", encoding="utf-8")


def clear_quit_flag() -> None:
    (config.home() / QUIT_FLAG).unlink(missing_ok=True)


def quit_flag_set() -> bool:
    return (config.home() / QUIT_FLAG).exists()


# --- scheduled task -----------------------------------------------------------------------------------------------------

def task_xml() -> str:
    start = datetime.now().replace(microsecond=0).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts ClaudeIn again within 5 minutes if it stopped without Quit.</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Repetition><Interval>{RESTART_INTERVAL}</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <StartWhenAvailable>false</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(pythonw()))}</Command>
      <Arguments>{escape(launch_args("--watchdog"))}</Arguments>
      <WorkingDirectory>{escape(str(PROJECT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=_NO_WINDOW)


def add_task(name: str | None = None) -> bool:
    """[name] defaults to TASK_NAME, read when called (so tests can use a private name)."""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
        f.write(task_xml())
        xml = f.name
    try:
        return _schtasks("/Create", "/TN", name or TASK_NAME, "/XML", xml, "/F").returncode == 0
    finally:
        os.unlink(xml)


def remove_task(name: str | None = None) -> None:
    _schtasks("/Delete", "/TN", name or TASK_NAME, "/F")


def task_exists(name: str | None = None) -> bool:
    return _schtasks("/Query", "/TN", name or TASK_NAME).returncode == 0


# --- the switch in the panel and General ----------------------------------------------------------------------------------

def enabled() -> bool:
    return startup_shortcut().exists()


def _shortcut_target(path: Path) -> str:
    import win32com.client

    return str(win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path)).TargetPath)


def remove_if_ours() -> None:
    """For the uninstaller: removes the Startup shortcut and the restart task, but only those that start THIS program.
    (A ClaudeIn run from .venv, with its own shortcut and task of the same names, is left alone.)"""
    ours = str(pythonw()).lower()
    shortcut = startup_shortcut()
    try:
        if shortcut.exists() and _shortcut_target(shortcut).lower() == ours:
            shortcut.unlink()
    except Exception:  # a shortcut that can't be read is left alone
        pass
    xml = html.unescape(_schtasks("/Query", "/TN", TASK_NAME, "/XML").stdout or "").lower()
    if f"<command>{ours}</command>" in xml:
        remove_task(TASK_NAME)


def set_enabled(on: bool) -> bool:
    """"Start with Windows": the Startup shortcut only. (The restart task is its own switch: set_restart.)"""
    if on:
        make_shortcut(startup_shortcut())
    else:
        startup_shortcut().unlink(missing_ok=True)
    return True


def set_restart(on: bool) -> bool:
    """"Restart ClaudeIn if it stops". Returns False if Windows refused to add the task."""
    if on:
        return add_task()
    remove_task()
    return True


def sync_restart(on: bool) -> None:
    """Makes the restart task match the setting: added (or refreshed, for this program and the 5-minute interval) when on,
    removed when off. Clears away the every-minute task that older versions added with "Start with Windows"."""
    if on:
        add_task()
    elif task_exists():
        remove_task()
