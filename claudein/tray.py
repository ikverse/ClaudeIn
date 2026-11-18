"""The tray icon (plan 4.6): its colour shows the state, hovering shows the status, left-click opens the panel and
right-click a short frosted-glass menu."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSystemTrayIcon
from qfluentwidgets import Action, FluentIcon
from qfluentwidgets.components.material import AcrylicSystemTrayMenu

from .panel import Panel
from .windows import PairWindow, SettingsWindow, status_line

if TYPE_CHECKING:
    from .service import Service

COLOURS = {
    "normal": "#0F6CBD",   # Windows blue
    "working": "#107C10",  # green
    "paused": "#6B6B6B",   # grey
    "problem": "#C42B1C",  # red
}


def state_of(st: dict) -> str:
    if st["problems"]:
        return "problem"
    if st["paused"]:
        return "paused"
    if st["working"] or st["waiting"]:
        return "working"
    return "normal"


def icon_image(state: str, size: int = 64) -> QImage:
    """A coloured rounded square with a white mark; the thin dark edge keeps it readable on a light taskbar."""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    rect = QRectF(s * 0.06, s * 0.06, s * 0.88, s * 0.88)
    p.setPen(QPen(QColor(0, 0, 0, 90), s * 0.03))
    p.setBrush(QColor(COLOURS[state]))
    p.drawRoundedRect(rect, s * 0.22, s * 0.22)
    white = QPen(QColor("white"), s * 0.11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(white)
    p.setBrush(Qt.BrushStyle.NoBrush)
    c = QPointF(s / 2, s / 2)
    if state == "paused":
        for dx in (-0.1, 0.1):
            p.drawLine(QPointF(c.x() + dx * s, s * 0.32), QPointF(c.x() + dx * s, s * 0.68))
    elif state == "problem":
        p.drawLine(QPointF(c.x(), s * 0.28), QPointF(c.x(), s * 0.56))
        p.drawPoint(QPointF(c.x(), s * 0.72))
    else:
        arc = QRectF(s * 0.28, s * 0.28, s * 0.44, s * 0.44)
        path = QPainterPath()
        path.arcMoveTo(arc, 45)
        path.arcTo(arc, 45, 270)  # a "C"
        p.drawPath(path)
        if state == "working":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("white"))
            p.drawEllipse(QPointF(s * 0.66, s * 0.5), s * 0.07, s * 0.07)
    p.end()
    return img


def icon_for(state: str) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        icon.addPixmap(QPixmap.fromImage(icon_image(state, size)))
    return icon


def save_ico(path) -> None:
    """The icon file the Start Menu and Startup shortcuts use."""
    import io

    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    icon_image("normal", 256).save(buf, "PNG")
    Image.open(io.BytesIO(bytes(buf.data()))).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)])


class Tray(QObject):
    def __init__(self, svc: "Service", quit_: Callable[[], None], restart: Callable[[], None]):
        super().__init__()
        self.svc = svc
        self.icons = {s: icon_for(s) for s in COLOURS}
        self.icon = QSystemTrayIcon(self.icons["normal"])
        self.settings = SettingsWindow(svc, self.open_pairing, restart, self.icons["normal"])
        self.pairing = PairWindow(svc, self.icons["normal"])
        self.panel = Panel(svc, self.icons["normal"], self.open_settings, self.open_pairing)

        self.menu = AcrylicSystemTrayMenu()
        self.menu.addAction(Action(FluentIcon.HOME, "Open ClaudeIn", triggered=lambda: self.open_settings("home")))
        self.pause_action = Action(FluentIcon.PAUSE, "Pause all", triggered=self._toggle_pause)
        self.menu.addAction(self.pause_action)
        self.menu.addAction(Action(FluentIcon.PHONE, "Phone access", triggered=lambda: self.open_settings("phones")))
        self.menu.addSeparator()
        self.menu.addAction(Action(FluentIcon.CLOSE, "Quit", triggered=quit_))
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.refresh)
        self.icon.show()
        self.refresh()

    def schedule_refresh(self) -> None:
        """Called on every change in the engine; refreshes at most four times a second."""
        if not self._timer.isActive():
            self._timer.start()

    def refresh(self) -> None:
        st = self.svc.status()
        self.icon.setIcon(self.icons[state_of(st)])
        self.icon.setToolTip(("ClaudeIn · " + status_line(st))[:127])
        self.pause_action.setText("Resume" if st["paused"] else "Pause all")
        self.pause_action.setIcon(FluentIcon.PLAY.icon() if st["paused"] else FluentIcon.PAUSE.icon())
        if self.panel.isVisible():
            self.panel.refresh()
        self.settings.refresh()
        self.pairing.refresh()

    def _toggle_pause(self) -> None:
        self.svc.scheduler.set_paused(not self.svc.settings.paused)

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.panel.toggle(self.icon.geometry())

    def open_settings(self, page: str = "home") -> None:
        if page == "phones":
            self.settings.open_phone_access()
        else:
            self.settings.open(page)

    def open_pairing(self) -> None:
        self.pairing.start()

    def balloon(self, text: str) -> None:
        self.icon.showMessage("ClaudeIn", text, self.icons["normal"], 5000)

    def hide(self) -> None:
        self.icon.hide()
