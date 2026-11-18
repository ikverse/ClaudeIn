"""Starts ClaudeIn: the engine and the tray together, with no console window.

    pythonw -m claudein                 normal start (sign-in, Start Menu, setup); clears a previous Quit
    pythonw -m claudein --watchdog      from the restart task: does nothing if running, or after Quit
    pythonw -m claudein --after-restart after Restore pairings: waits for the old copy to finish closing
    python  -m claudein --setup         from Setup-ClaudeIn.bat: icon file and Start Menu shortcut
    python  -m claudein --remove-startup  from the uninstaller: removes the Startup shortcut and restart task that start
                                        this program (not another copy's)

Installed with ClaudeIn-Setup.exe, the same commands are given to ClaudeIn.exe (without `-m claudein`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from PySide6.QtCore import QProcess
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import autostart, config

log = logging.getLogger("claudein")


def instance_name() -> str:
    return "ClaudeIn-" + (os.environ.get("USERNAME") or "user")


def other_copy_running(message: bytes | None = None) -> bool:
    """True if a ClaudeIn is already running for this Windows account; [message] is passed to it."""
    s = QLocalSocket()
    s.connectToServer(instance_name())
    if not s.waitForConnected(500):
        return False
    if message:
        s.write(message)
        s.flush()
        s.waitForBytesWritten(500)
        s.waitForReadyRead(1000)  # the running copy says "ok" once it has the message; hanging up sooner loses it
    s.disconnectFromServer()
    return True


class OneCopy:
    """Holds the name that says "ClaudeIn is running", and hears from later starts."""

    def __init__(self, on_message):
        self.server = QLocalServer()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(instance_name())
        if not self.server.listen(instance_name()):
            raise RuntimeError("Could not take the single-copy name.")
        self.server.newConnection.connect(lambda: self._accept(on_message))

    def _accept(self, on_message) -> None:
        while self.server.hasPendingConnections():
            c = self.server.nextPendingConnection()

            def read(c=c) -> None:
                data = bytes(c.readAll()).decode(errors="ignore")
                if data:
                    c.write(b"ok")
                    c.flush()
                    on_message(data)

            c.readyRead.connect(read)
            c.disconnected.connect(c.deleteLater)
            read()  # the message may have arrived before we started listening for it


def setup() -> None:
    """Makes the icon file and the Start Menu shortcut (and refreshes the Startup one if it is on)."""
    from PySide6.QtGui import QGuiApplication

    from .tray import save_ico

    app = QGuiApplication(sys.argv[:1])  # noqa: F841  (needed for drawing)
    save_ico(config.home() / autostart.ICON)
    autostart.make_shortcut(autostart.start_menu_shortcut())
    if autostart.enabled():
        autostart.make_shortcut(autostart.startup_shortcut())
        autostart.add_task()
    print("Start Menu shortcut made: ClaudeIn")


def main(argv: list[str]) -> int:
    if "--setup" in argv:
        setup()
        return 0
    if "--remove-startup" in argv:  # from the uninstaller: remove the Startup shortcut and restart task that are ours
        autostart.remove_if_ours()
        return 0
    watchdog = "--watchdog" in argv
    if watchdog and autostart.quit_flag_set():
        return 0  # the owner chose Quit; stay off until the next sign-in

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("ClaudeIn")

    if "--after-restart" in argv:
        end = time.monotonic() + 15
        while other_copy_running() and time.monotonic() < end:
            time.sleep(0.3)
    if other_copy_running(None if watchdog else b"show"):
        return 0  # the running copy shows the balloon
    if not watchdog:
        autostart.clear_quit_flag()

    import qasync
    from qfluentwidgets import SystemThemeListener, Theme, setTheme, setThemeColor
    from qframelesswindow.utils import getSystemAccentColor

    from .service import Service
    from .tray import Tray

    setTheme(Theme.AUTO)
    setThemeColor(getSystemAccentColor(), save=False)
    theme_listener = SystemThemeListener(app)
    theme_listener.start()

    holder: dict = {}
    done = asyncio.Event()
    restart_wanted = {"on": False}

    def message(text: str) -> None:
        if "show" in text and "tray" in holder:
            holder["tray"].balloon("ClaudeIn is already running. Its icon is in the hidden icons by the clock.")

    one = OneCopy(message)  # noqa: F841  (kept for the app's life)

    def quit_() -> None:
        autostart.set_quit_flag()
        done.set()

    def restart() -> None:
        restart_wanted["on"] = True
        done.set()

    def on_change() -> None:
        if "tray" in holder:
            holder["tray"].schedule_refresh()

    async def run() -> None:
        svc = Service(on_change=on_change)
        sys.excepthook = lambda t, v, tb: log.error("Unexpected error", exc_info=(t, v, tb))
        await svc.start_safely()
        if svc.settings.keep_awake:
            svc.scheduler.set_keep_awake(True)  # "Keep PC awake" was left on
        holder["tray"] = Tray(svc, quit_, restart)
        if svc.claude.needs_setup():  # first run, or Claude Code or the sign-in is missing: show the two steps
            holder["tray"].open_settings("claude")
        await done.wait()
        holder["tray"].hide()
        await svc.stop()

    try:
        qasync.run(run())
    finally:
        theme_listener.terminate()
    if restart_wanted["on"]:
        QProcess.startDetached(str(autostart.pythonw()), autostart.launch_args("--after-restart").split(),
                               str(autostart.PROJECT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
