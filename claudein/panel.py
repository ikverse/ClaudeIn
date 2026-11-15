"""The ClaudeIn panel: frosted glass above the clock, opened by a left-click on the tray icon (plan 4.6)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (BodyLabel, CaptionLabel, FluentIcon, HorizontalSeparator, ProgressBar, StrongBodyLabel,
                            TogglePushButton, TransparentPushButton, isDarkTheme)
from qframelesswindow import AcrylicWindow

from . import autostart
from .windows import claude_line, glass_allowed, plan_text, solid, status_line, tint

if TYPE_CHECKING:
    from .service import Service

WIDTH = 360


class Panel(AcrylicWindow):
    def __init__(self, svc: "Service", icon: QIcon, open_settings: Callable[[str], None],
                 open_pairing: Callable[[], None]):
        super().__init__()
        self.svc = svc
        self.hidden_at = 0.0
        self.titleBar.hide()
        self.setResizeEnabled(False)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedWidth(WIDTH)

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 12)
        v.setSpacing(10)

        top = QHBoxLayout()
        pic = QLabel()
        pic.setPixmap(icon.pixmap(32, 32))
        top.addWidget(pic)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(StrongBodyLabel("ClaudeIn"))
        self.status = CaptionLabel("")
        names.addWidget(self.status)
        top.addLayout(names, 1)
        v.addLayout(top)

        tiles = QHBoxLayout()
        self.run_tile = TogglePushButton(FluentIcon.PLAY, "Running")
        self.run_tile.setMinimumHeight(56)
        self.run_tile.toggled.connect(lambda on: self.svc.scheduler.set_paused(not on))
        self.start_tile = TogglePushButton(FluentIcon.POWER_BUTTON, "Start with Windows")
        self.start_tile.setMinimumHeight(56)
        self.start_tile.toggled.connect(self._autostart)
        tiles.addWidget(self.run_tile)
        tiles.addWidget(self.start_tile)
        v.addLayout(tiles)

        v.addWidget(BodyLabel("Plan · 5-hour window"))
        self.bar = ProgressBar()
        v.addWidget(self.bar)
        self.plan = CaptionLabel("")
        v.addWidget(self.plan)
        self.claude = CaptionLabel("")
        self.claude.setWordWrap(True)
        v.addWidget(self.claude)
        self.problem = CaptionLabel("")
        self.problem.setWordWrap(True)
        self.problem.setTextColor("#c42b1c", "#ff99a4")
        v.addWidget(self.problem)

        v.addWidget(HorizontalSeparator())
        bottom = QHBoxLayout()
        for icon_, text, action in ((FluentIcon.ADD, "Pair", open_pairing),
                                    (FluentIcon.PIE_SINGLE, "Usage", lambda: open_settings("usage")),
                                    (FluentIcon.SETTING, "Settings", lambda: open_settings("home"))):
            b = TransparentPushButton(icon_, text)
            b.clicked.connect(lambda _=False, a=action: (self.hide(), a()))
            bottom.addWidget(b)
        v.addLayout(bottom)

    def _autostart(self, on: bool) -> None:
        if on != autostart.enabled():
            autostart.set_enabled(on)

    def refresh(self) -> None:
        st = self.svc.status()
        self.status.setText(status_line(st) if not st["problems"] else
                            ("Paused" if st["paused"] else "Running") + " · problem")
        for w, on in ((self.run_tile, not st["paused"]), (self.start_tile, autostart.enabled())):
            w.blockSignals(True)
            w.setChecked(on)
            w.blockSignals(False)
        self.run_tile.setText("Running" if not st["paused"] else "Paused")
        self.run_tile.setIcon(FluentIcon.PLAY if not st["paused"] else FluentIcon.PAUSE)
        pct, words = plan_text(st["plan"])
        self.bar.setValue(pct)
        self.plan.setText(words)
        self.claude.setText(claude_line(st))
        self.problem.setText("\n".join(st["problems"]))
        self.problem.setVisible(bool(st["problems"]))
        self.adjustSize()

    def toggle(self, tray_rect: QRect) -> None:
        if self.isVisible():
            self.hide()
            return
        if time.monotonic() - self.hidden_at < 0.3:
            return  # this click is the one that just closed it
        self.refresh()
        self.adjustSize()
        self._place(tray_rect)
        self.show()
        self._glass()
        self.raise_()
        self.activateWindow()

    def _place(self, tray_rect: QRect) -> None:
        screen = QGuiApplication.screenAt(tray_rect.center()) or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        w, h = self.width(), self.sizeHint().height()
        x = min(max(tray_rect.center().x() - w // 2, avail.left() + 12), avail.right() - w - 12)
        if tray_rect.isValid() and tray_rect.center().y() < avail.center().y():
            y = avail.top() + 12  # taskbar at the top
        else:
            y = avail.bottom() - h - 12
        if not tray_rect.isValid():
            x = avail.right() - w - 12
        self.move(x, y)

    def _glass(self) -> None:
        if glass_allowed():
            self.windowEffect.setAcrylicEffect(self.winId(), tint())
            self.setStyleSheet("Panel{background:transparent}")
        else:
            self.windowEffect.removeBackgroundEffect(self.winId())
            c = solid()
            self.setStyleSheet(f"Panel{{background:rgb({c.red()},{c.green()},{c.blue()})}}")

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            QTimer.singleShot(0, self._hide_if_inactive)

    def _hide_if_inactive(self) -> None:
        if not self.isActiveWindow():
            self.hide()
            self.hidden_at = time.monotonic()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)
