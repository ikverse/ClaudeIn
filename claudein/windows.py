"""The Settings window and the pairing window (plan 4.6), Windows 11 Fluent style with Acrylic frosted glass.

The glass turns solid on its own when Windows would turn it off: the window isn't in front, "Transparency effects" is
off, or battery saver is on.
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import json
import os
import time
import winreg
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QTableWidgetItem, QVBoxLayout,
                               QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, EditableComboBox, FluentIcon, FluentWidget,
                            FluentWindow, IconWidget, InfoBar, InfoBarPosition, LineEdit, MessageBox,
                            NavigationItemPosition, PrimaryPushButton, ProgressBar, PushButton, ScrollArea,
                            SimpleCardWidget, StrongBodyLabel, SubtitleLabel, SwitchButton, TableWidget,
                            TitleLabel, isDarkTheme)

from . import autostart, tls
from .pairing import shown
from .service import lan_addresses
from .usage import ENDINGS, start_of_today

if TYPE_CHECKING:
    from .service import Service


# --- glass ---------------------------------------------------------------------------------------------------------------

class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def transparency_on() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "EnableTransparency")[0])
    except OSError:
        return True


def battery_saver_on() -> bool:
    s = _PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return False
    return bool(s.SystemStatusFlag & 1)


def glass_allowed() -> bool:
    return transparency_on() and not battery_saver_on()


def tint() -> str:
    """The colour mixed into the glass (RGBA hex), as in the glass check."""
    return "20202099" if isDarkTheme() else "F2F2F299"


def solid() -> QColor:
    return QColor(32, 32, 32) if isDarkTheme() else QColor(243, 243, 243)


class GlassMixin:
    """For FluentWidget/FluentWindow: Mica while in front and allowed, solid otherwise.

    Mica tints with the desktop wallpaper, so it stays dark whatever window is behind. Acrylic (still used by the tray
    panel) blurs the windows behind and looked washed-out grey over bright ones (owner, 2026-11-15).
    """

    _glass = False

    def _init_glass(self) -> None:
        self._glass = False
        self.setMicaEffectEnabled(False)

    def _normalBackgroundColor(self):
        return QColor(0, 0, 0, 0) if self._glass else solid()

    def refresh_glass(self) -> None:
        want = self.isVisible() and self.isActiveWindow() and glass_allowed()
        if want == self._glass:
            return
        self._glass = want
        self.setMicaEffectEnabled(want)  # qfluentwidgets' own Mica switch; does nothing before Windows 11
        self._glass = self.isMicaEffectEnabled()
        self.setBackgroundColor(self._normalBackgroundColor())
        self.update()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.Type.ActivationChange:
            QTimer.singleShot(0, self.refresh_glass)

    def _onThemeChangedFinished(self):
        if self._glass:
            self.windowEffect.setMicaEffect(self.winId(), isDarkTheme())
        self.setBackgroundColor(self._normalBackgroundColor())


# --- small building blocks -----------------------------------------------------------------------------------------------

def row(icon, title: str, text: str, widget: QWidget | None) -> SimpleCardWidget:
    """A settings card: icon, title and explanation on the left, a control on the right."""
    card = SimpleCardWidget()
    h = QHBoxLayout(card)
    h.setContentsMargins(16, 12, 16, 12)
    h.setSpacing(14)
    if icon is not None:
        ic = IconWidget(icon)
        ic.setFixedSize(20, 20)
        h.addWidget(ic)
    col = QVBoxLayout()
    col.setSpacing(0)
    col.addWidget(StrongBodyLabel(title))
    if text:
        cap = CaptionLabel(text)
        cap.setWordWrap(True)
        col.addWidget(cap)
    h.addLayout(col, 1)
    if widget is not None:
        h.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
    return card


def center(w: QWidget, width: int, height: int) -> None:
    """Gives a window its size and puts it in the middle of its screen (the main screen at first)."""
    screen = w.screen() or QGuiApplication.primaryScreen()
    g = screen.availableGeometry()
    w.resize(min(width, g.width() - 40), min(height, g.height() - 40))
    w.move(g.center().x() - w.width() // 2, g.center().y() - w.height() // 2)


def clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            clear(item.layout())


class Page(ScrollArea):
    """A scrolling page of the Settings window."""

    def __init__(self, name: str, title: str):
        super().__init__()
        self.setObjectName(name)
        self.body = QWidget()
        self.body.setObjectName(name + "Body")
        self.v = QVBoxLayout(self.body)
        self.v.setContentsMargins(36, 20, 36, 36)
        self.v.setSpacing(10)
        self.v.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.v.addWidget(TitleLabel(title))
        self.setWidget(self.body)
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea{border:none;background:transparent}")
        self.body.setStyleSheet(f"#{name}Body{{background:transparent}}")

    def refresh(self) -> None:
        pass


def status_line(st: dict) -> str:
    if st["problems"]:
        return "Problem: " + st["problems"][0]
    state = "Paused" if st["paused"] else "Running"
    return f"{state} · {st['working']} working, {st['waiting']} waiting"


def claude_line(st: dict) -> str:
    v = st["claude_version"] or "missing"
    s = {True: "signed in ✓", False: "signed out ✗", None: "not checked yet"}[st["signed_in"]]
    return f"Claude Code {v} — {s}"


def plan_text(plan: dict | None) -> tuple[int, str]:
    """(percent for the bar, words under it)."""
    if not plan or plan.get("five_hour") is None:
        return 0, "No numbers yet. They come with the first question."
    five = round(plan["five_hour"] * 100)
    week = plan.get("seven_day")
    tail = f" · week {round(week * 100)}%" if isinstance(week, (int, float)) else ""
    return five, f"{five}% used{tail}"


def when(t: float) -> str:
    d = datetime.fromtimestamp(t)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%d %b %H:%M")


def seconds(ms: int | None) -> str:
    s = (ms or 0) / 1000
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min"


# --- pages ---------------------------------------------------------------------------------------------------------------

class HomePage(Page):
    def __init__(self, svc: "Service"):
        super().__init__("home", "Home")
        self.svc = svc
        self.status = SubtitleLabel("")
        self.v.addWidget(self.status)
        plan = SimpleCardWidget()
        pv = QVBoxLayout(plan)
        pv.setContentsMargins(16, 12, 16, 12)
        pv.addWidget(StrongBodyLabel("Plan · 5-hour window"))
        self.bar = ProgressBar()
        pv.addWidget(self.bar)
        self.plan_words = CaptionLabel("")
        pv.addWidget(self.plan_words)
        self.v.addWidget(plan)
        self.claude = row(FluentIcon.ROBOT, "Claude Code", "", None)
        self.claude_text = CaptionLabel("")
        self.claude.layout().itemAt(1).layout().addWidget(self.claude_text)
        self.v.addWidget(self.claude)
        self.v.addWidget(StrongBodyLabel("Questions now"))
        self.runs_box = QVBoxLayout()
        self.v.addLayout(self.runs_box)

    def refresh(self) -> None:
        st = self.svc.status()
        self.status.setText(status_line(st))
        pct, words = plan_text(st["plan"])
        self.bar.setValue(pct)
        self.plan_words.setText(words + "  (for the whole Claude plan)")
        self.claude_text.setText(claude_line(st) + (". It updates when Setup-ClaudeIn.bat is run again."))
        clear(self.runs_box)
        s = self.svc.scheduler
        items = [(r, "working") for r, _ in s.running.values()] + [(r, "waiting") for r in s.waiting]
        if not items:
            self.runs_box.addWidget(CaptionLabel("None right now."))
        for r, what in items:
            self.runs_box.addWidget(row(FluentIcon.PHONE if r.is_phone else FluentIcon.HOME, r.device_label,
                                        f"{what} · {r.spec.model} · asked {when(r.asked_at)}", None))


class DevicesPage(Page):
    def __init__(self, svc: "Service", open_pairing: Callable[[], None]):
        super().__init__("devices", "Devices")
        self.svc = svc
        pair = PrimaryPushButton(FluentIcon.ADD, "Pair a device")
        pair.clicked.connect(open_pairing)
        self.v.addWidget(pair, 0, Qt.AlignmentFlag.AlignLeft)
        self.list = QVBoxLayout()
        self.list.setSpacing(10)
        self.v.addLayout(self.list)
        self._shape = None

    def refresh(self) -> None:
        p = self.svc.pairings
        # rebuild only when something shown changed (typing in a models box must not be interrupted)
        shape = (tuple((d.id, d.enabled, d.last_used, d.first_used) for d in p.devices.values()),
                 tuple((a, tuple(x.models)) for a, x in p.apps.items()))
        if shape == self._shape:
            return
        self._shape = shape
        clear(self.list)
        by_app: dict[str, list] = {}
        for d in p.devices.values():
            by_app.setdefault(d.app_id, []).append(d)
        app_ids = list(dict.fromkeys(list(by_app) + [a for a in p.apps if a != "claudein"]))
        if not app_ids:
            self.list.addWidget(BodyLabel("Nothing paired yet. Press “Pair a device” to add a phone."))
        for aid in app_ids:
            self.list.addSpacing(8)
            self.list.addWidget(SubtitleLabel(p.app_name(aid)))
            models = LineEdit()
            models.setPlaceholderText("Any model")
            models.setText(", ".join(p.apps[aid].models) if aid in p.apps else "")
            models.setFixedWidth(300)
            models.editingFinished.connect(lambda a=aid, w=models: self._save_models(a, w.text()))
            self.list.addWidget(row(FluentIcon.SETTING, "Allowed models",
                                    "Model ids separated by commas, such as claude-opus-5-5. Empty means any model.",
                                    models))
            devices = by_app.get(aid, [])
            if not devices:
                self.list.addWidget(row(FluentIcon.HOME, f"{p.app_name(aid)} (this PC)",
                                        "A program on this PC. It uses the local token instead of a pairing.", None))
            for d in devices:
                sw = SwitchButton()
                sw.setOnText("On")
                sw.setOffText("Off")
                sw.setChecked(d.enabled)
                sw.checkedChanged.connect(lambda on, i=d.id: self.svc.pairings.set_enabled(i, on))
                forget = PushButton(FluentIcon.DELETE, "Forget…")
                forget.clicked.connect(lambda _=False, dev=d: self._forget(dev))
                box = QWidget()
                hb = QHBoxLayout(box)
                hb.setContentsMargins(0, 0, 0, 0)
                hb.addWidget(sw)
                hb.addWidget(forget)
                used = f"last used {when(d.last_used)}" if d.last_used else "not used yet (the code expires 30 minutes after pairing)"
                self.list.addWidget(row(FluentIcon.PHONE, d.label, f"Paired {when(d.created)} · {used}", box))

    def _save_models(self, aid: str, text: str) -> None:
        self.svc.pairings.set_models(aid, text.replace(";", ",").split(","))

    def _forget(self, dev) -> None:
        box = MessageBox(f"Forget {dev.label}?",
                         "Its code stops working for good, and any question it is asking stops now. "
                         "To use it again, pair it again.", self.window())
        box.yesButton.setText("Forget")
        box.cancelButton.setText("Cancel")
        if box.exec():
            self.svc.forget(dev.id)


class UsagePage(Page):
    def __init__(self, svc: "Service"):
        super().__init__("usage", "Usage")
        self.svc = svc
        self.plan = CaptionLabel("")
        self.plan.setWordWrap(True)
        self.v.addWidget(self.plan)
        self.v.addWidget(StrongBodyLabel("Per app"))
        self.table = TableWidget()
        cols = ["App", "Today: questions", "Today: tokens", "Share of today", "Today: points", "Today: API price",
                "7 days: questions", "7 days: tokens", "7 days: points", "7 days: API price"]
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(160)
        self.v.addWidget(self.table)
        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("Last 20 runs of"))
        self.pick = ComboBox()
        self.pick.setMinimumWidth(200)
        self.pick.currentIndexChanged.connect(lambda _: self._fill_recent())
        head.addWidget(self.pick)
        head.addStretch(1)
        self.v.addLayout(head)
        self.recent = TableWidget()
        rcols = ["Time", "Device", "Model", "Waited", "Took", "Ended"]
        self.recent.setColumnCount(len(rcols))
        self.recent.setHorizontalHeaderLabels(rcols)
        self.recent.verticalHeader().hide()
        self.recent.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.recent.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.recent.horizontalHeader().setStretchLastSection(True)
        self.recent.setMinimumHeight(300)
        self.v.addWidget(self.recent)
        self._apps: list[str] = []

    def refresh(self) -> None:
        u, p = self.svc.usage, self.svc.pairings
        pct, words = plan_text(self.svc.plan_use())
        self.plan.setText(f"Plan use Claude Code last reported: {words}. These are for the whole Claude plan, not "
                          "per app; each app's share is shown as its share of today's tokens. "
                          f"{POINTS_NOTE}")
        today = u.totals(start_of_today())
        week = u.totals(time.time() - 7 * 86400)
        apps = sorted(set(week) | set(today))
        all_today = sum(x["tokens"] for x in today.values()) or 0
        self.table.setRowCount(len(apps))
        zero = {"questions": 0, "tokens": 0, "api_price_usd": 0, "plan_points": 0}
        for i, a in enumerate(apps):
            t = today.get(a, zero)
            w = week.get(a, zero)
            share = f"{round(100 * t['tokens'] / all_today)}%" if all_today else "—"
            for j, val in enumerate([p.app_name(a), t["questions"], f"{t['tokens']:,}", share,
                                     f"{t['plan_points']:.1f}", f"${t['api_price_usd']:.2f}", w["questions"],
                                     f"{w['tokens']:,}", f"{w['plan_points']:.1f}", f"${w['api_price_usd']:.2f}"]):
                self.table.setItem(i, j, QTableWidgetItem(str(val)))
        if apps != self._apps:
            current = self._apps[self.pick.currentIndex()] if 0 <= self.pick.currentIndex() < len(self._apps) else None
            self._apps = apps
            self.pick.blockSignals(True)
            self.pick.clear()
            self.pick.addItems([p.app_name(a) for a in apps])
            if current in apps:
                self.pick.setCurrentIndex(apps.index(current))
            self.pick.blockSignals(False)
        self._fill_recent()

    def _fill_recent(self) -> None:
        i = self.pick.currentIndex()
        runs = self.svc.usage.recent(self._apps[i]) if 0 <= i < len(self._apps) else []
        self.recent.setRowCount(len(runs))
        for r, x in enumerate(runs):
            ended = x["ending"] if x["ending"] in ENDINGS else f"failed ({x['error_kind'] or 'other'})"
            for j, val in enumerate([when(x["asked_at"]), x["device"], x["model"], seconds(x["queued_ms"]),
                                     seconds(x["run_ms"]), ended]):
                self.recent.setItem(r, j, QTableWidgetItem(str(val)))


POINTS_NOTE = ("Plan points are estimates: 100 points is one full 5-hour window of the Claude plan, and Claude only "
               "reports the plan's total, so ClaudeIn shares it out between the apps by when they ran and their tokens.")


class CapsPage(Page):
    """Phase 2: an optional daily cap per app, in plan points. Off for every app unless switched on."""

    def __init__(self, svc: "Service"):
        super().__init__("caps", "Caps")
        self.svc = svc
        intro = BodyLabel("Give an app a daily limit. When it reaches its limit, its new questions are refused until "
                          "midnight; questions already going still finish.")
        intro.setWordWrap(True)
        self.v.addWidget(intro)
        note = CaptionLabel(POINTS_NOTE)
        note.setWordWrap(True)
        self.v.addWidget(note)
        self.list = QVBoxLayout()
        self.list.setSpacing(10)
        self.v.addLayout(self.list)
        self._shape = None
        self._today: dict[str, CaptionLabel] = {}

    def refresh(self) -> None:
        p, u = self.svc.pairings, self.svc.usage
        apps = [a for a in p.apps if a != "claudein"]
        shape = tuple((a, p.apps[a].cap_points) for a in apps)
        if shape != self._shape:  # rebuild only when the apps or caps changed, so typing isn't interrupted
            self._shape = shape
            self._today = {}
            clear(self.list)
            if not apps:
                self.list.addWidget(BodyLabel("No apps yet. Apps appear here once paired, or once a program asks."))
            for aid in apps:
                cap = p.apps[aid].cap_points
                sw = SwitchButton()
                sw.setOnText("On")
                sw.setOffText("Off")
                sw.setChecked(cap is not None)
                points = LineEdit()
                points.setFixedWidth(90)
                points.setPlaceholderText("points")
                points.setText(f"{cap:g}" if cap is not None else "60")
                points.setEnabled(cap is not None)
                sw.checkedChanged.connect(lambda on, a=aid, w=points: self._switch(a, on, w))
                points.editingFinished.connect(lambda a=aid, w=points: self._save(a, w))
                box = QWidget()
                hb = QHBoxLayout(box)
                hb.setContentsMargins(0, 0, 0, 0)
                hb.addWidget(points)
                hb.addWidget(CaptionLabel("points a day"))
                hb.addSpacing(12)
                hb.addWidget(sw)
                card = row(FluentIcon.SPEED_OFF, p.app_name(aid), "", box)
                today = CaptionLabel("")
                card.layout().itemAt(1).layout().addWidget(today)
                self._today[aid] = today
                self.list.addWidget(card)
        for aid, label in self._today.items():
            used = u.points_today(aid)
            cap = p.cap(aid)
            if cap is None:
                label.setText(f"Today: {used:.1f} points · no cap")
            elif used >= cap:
                label.setText(f"Today: {used:.1f} of {cap:g} points · limit reached, new questions refused until midnight")
            else:
                label.setText(f"Today: {used:.1f} of {cap:g} points")

    def _switch(self, aid: str, on: bool, field: LineEdit) -> None:
        field.setEnabled(on)
        if on:
            self._save(aid, field)
        else:
            self.svc.pairings.set_cap(aid, None)

    def _save(self, aid: str, field: LineEdit) -> None:
        if not field.isEnabled():
            return
        try:
            value = float(field.text().replace(",", "."))
            if value < 0:
                raise ValueError
        except ValueError:
            InfoBar.warning("Not a number", "Type the number of points a day, such as 60.", duration=4000,
                            position=InfoBarPosition.TOP, parent=self.window())
            return
        self.svc.pairings.set_cap(aid, value)


class GeneralPage(Page):
    def __init__(self, svc: "Service", restart: Callable[[], None]):
        super().__init__("general", "General")
        self.svc = svc
        self.restart = restart

        self.workers = ComboBox()
        self.workers.addItems(["1", "2", "3", "4"])
        self.workers.setCurrentIndex(svc.settings.workers - 1)
        self.workers.currentIndexChanged.connect(lambda i: svc.scheduler.set_workers(i + 1))
        self.v.addWidget(row(FluentIcon.SPEED_HIGH, "Runs at once",
                             "How many questions Claude Code answers at the same time. The rest wait in line.",
                             self.workers))

        self.autostart = SwitchButton()
        self.autostart.setOnText("On")
        self.autostart.setOffText("Off")
        self.autostart.setChecked(autostart.enabled())
        self.autostart.checkedChanged.connect(self._autostart)
        self.v.addWidget(row(FluentIcon.POWER_BUTTON, "Start with Windows",
                             "Starts ClaudeIn when you sign in, and again within a minute if it ever stops without "
                             "Quit.", self.autostart))

        self.awake = SwitchButton()
        self.awake.setOnText("On")
        self.awake.setOffText("Off")
        self.awake.setChecked(svc.settings.keep_awake)
        self.awake.checkedChanged.connect(svc.scheduler.set_keep_awake)
        self.v.addWidget(row(FluentIcon.BRIGHTNESS, "Keep PC awake",
                             "Off: the PC stays awake only while a question is going or waiting. On: always, for "
                             "long trips away.", self.awake))

        logs = PushButton(FluentIcon.FOLDER, "Open")
        logs.clicked.connect(lambda: os.startfile(str(svc.folder / "logs")))
        self.v.addWidget(row(FluentIcon.DOCUMENT, "Logs folder",
                             "What ClaudeIn did: times, apps, models and how runs ended. Never questions, answers or "
                             "codes.", logs))

        token = PushButton(FluentIcon.SYNC, "New token")
        token.clicked.connect(self._new_token)
        self.v.addWidget(row(FluentIcon.CERTIFICATE, "Local token",
                             "The key programs on this PC (such as Village) use. A new one stops the old one at once.",
                             token))

        backup_b = PushButton(FluentIcon.SAVE, "Back up…")
        backup_b.clicked.connect(self._backup)
        restore_b = PushButton(FluentIcon.HISTORY, "Restore…")
        restore_b.clicked.connect(self._restore)
        box = QWidget()
        hb = QHBoxLayout(box)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.addWidget(backup_b)
        hb.addWidget(restore_b)
        self.v.addWidget(row(FluentIcon.SAVE_AS, "Pairings",
                             "Saves the certificate and the paired devices in one file, so a lost .claudein folder "
                             "doesn't mean pairing every phone again.", box))

        self.check_b = PushButton(FluentIcon.SEND, "Check")
        self.check_b.clicked.connect(lambda: asyncio.ensure_future(self._check()))
        self.v.addWidget(row(FluentIcon.ROBOT, "Check Claude Code",
                             "Asks Claude one tiny Haiku question to confirm the login works. Uses a very small "
                             "amount of the plan.", self.check_b))

    def refresh(self) -> None:
        self.workers.blockSignals(True)
        self.workers.setCurrentIndex(self.svc.settings.workers - 1)
        self.workers.blockSignals(False)
        for w, value in ((self.awake, self.svc.settings.keep_awake), (self.autostart, autostart.enabled())):
            w.blockSignals(True)
            w.setChecked(value)
            w.blockSignals(False)

    def _info(self, ok: bool, title: str, text: str) -> None:
        (InfoBar.success if ok else InfoBar.error)(title, text, duration=6000 if ok else -1,
                                                   position=InfoBarPosition.TOP, parent=self.window())

    def _autostart(self, on: bool) -> None:
        ok = autostart.set_enabled(on)
        if not ok:
            self._info(False, "Restart task not added",
                       "ClaudeIn will start with Windows, but Windows refused the task that restarts it after a crash.")

    def _new_token(self) -> None:
        box = MessageBox("Make a new local token?",
                         "Programs on this PC that are running now will be refused until they read the new token.",
                         self.window())
        box.yesButton.setText("New token")
        box.cancelButton.setText("Cancel")
        if box.exec():
            self.svc.token.renew()
            self._info(True, "New local token made", "The old one no longer works.")

    def _backup(self) -> None:
        box = MessageBox("Keep this file private",
                         "The backup lets whoever has it pretend to be ClaudeIn to your phones. Save it somewhere "
                         "only you can reach, such as a private folder or a USB stick you keep.", self.window())
        box.yesButton.setText("Choose where")
        box.cancelButton.setText("Cancel")
        if not box.exec():
            return
        name = f"ClaudeIn pairings {datetime.now():%Y-%m-%d}.claudein-backup"
        path, _ = QFileDialog.getSaveFileName(self.window(), "Back up pairings", str(Path.home() / name),
                                              "ClaudeIn backup (*.claudein-backup)")
        if path:
            from . import backup

            backup.export(self.svc.folder, Path(path))
            self._info(True, "Pairings backed up", path)

    def _restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.window(), "Restore pairings", str(Path.home()),
                                              "ClaudeIn backup (*.claudein-backup);;All files (*)")
        if not path:
            return
        box = MessageBox("Restore pairings?",
                         "This replaces the certificate and the paired devices with the ones in the backup. "
                         "ClaudeIn then restarts. Questions going now will stop.", self.window())
        box.yesButton.setText("Restore and restart")
        box.cancelButton.setText("Cancel")
        if not box.exec():
            return
        try:
            self.svc.restore(Path(path))
        except (ValueError, OSError) as e:
            self._info(False, "Couldn't restore", str(e) or "That file couldn't be read.")
            return
        self.restart()

    async def _check(self) -> None:
        self.check_b.setEnabled(False)
        self.check_b.setText("Checking…")
        try:
            d = await self.svc.check_claude()
        finally:
            self.check_b.setEnabled(True)
            self.check_b.setText("Check")
        ending = d.get("ending", "")
        if ending == "done":
            self._info(True, "Claude Code works", f"Signed in. It answered: {d.get('answer', '').strip()[:60]}")
        else:
            self._info(False, "Claude Code didn't answer", ending.removeprefix("failed: "))


# --- the windows ---------------------------------------------------------------------------------------------------------

class SettingsWindow(GlassMixin, FluentWindow):
    def __init__(self, svc: "Service", open_pairing: Callable[[], None], restart: Callable[[], None], icon):
        super().__init__()
        self._init_glass()
        self.setWindowTitle("ClaudeIn")
        self.setWindowIcon(icon)
        self.resize(980, 720)
        self.setMinimumSize(760, 520)
        self.home = HomePage(svc)
        self.devices = DevicesPage(svc, open_pairing)
        self.usage = UsagePage(svc)
        self.caps = CapsPage(svc)
        self.general = GeneralPage(svc, restart)
        self.addSubInterface(self.home, FluentIcon.HOME, "Home")
        self.addSubInterface(self.devices, FluentIcon.PHONE, "Devices")
        self.addSubInterface(self.usage, FluentIcon.PIE_SINGLE, "Usage")
        self.addSubInterface(self.caps, FluentIcon.SPEED_OFF, "Caps")
        self.addSubInterface(self.general, FluentIcon.SETTING, "General", NavigationItemPosition.BOTTOM)
        self.navigationInterface.setExpandWidth(200)
        self.stackedWidget.currentChanged.connect(lambda _: self.refresh())

    def open(self, page: str = "home") -> None:
        self.switchTo(getattr(self, page))
        if not self.isVisible():
            self.showNormal()
            center(self, 1000, 760)
        self.showNormal()
        self.refresh()
        self.raise_()
        self.activateWindow()

    def refresh(self) -> None:
        if self.isVisible():
            w = self.stackedWidget.currentWidget()
            if isinstance(w, Page):
                w.refresh()


def qr_payload(addresses: list[str], port: int, code: str, fingerprint: str) -> str:
    """What the pairing QR code holds, for when the apps learn to scan it."""
    return json.dumps({"claudein": 1, "addresses": addresses, "port": port, "code": shown(code),
                       "fingerprint": fingerprint}, separators=(",", ":"))


def qr_pixmap(text: str, size: int = 184) -> QPixmap:
    import qrcode

    img = qrcode.make(text, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)


class PairWindow(GlassMixin, FluentWidget):
    def __init__(self, svc: "Service", icon):
        super().__init__()
        self._init_glass()
        self.svc = svc
        self.device_id: str | None = None
        self.setWindowTitle("Pair a device")
        self.setWindowIcon(icon)
        self.resize(680, 680)
        self.setMinimumSize(600, 640)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 48, 32, 24)
        outer.setSpacing(10)
        outer.addWidget(TitleLabel("Pair a device"))

        self.ask = QWidget()
        a = QVBoxLayout(self.ask)
        a.setContentsMargins(0, 0, 0, 0)
        a.setSpacing(8)
        a.addWidget(BodyLabel("Which app, on which device? Each pairing is one app on one device."))
        a.addWidget(StrongBodyLabel("App"))
        self.app = EditableComboBox()
        self.app.addItems(["Signal Lab", "Signal Lab EGX"])
        self.app.setText("Signal Lab")
        a.addWidget(self.app)
        a.addWidget(StrongBodyLabel("Device"))
        self.device = LineEdit()
        self.device.setText("Phone")
        a.addWidget(self.device)
        go = PrimaryPushButton(FluentIcon.ADD, "Make a code")
        go.clicked.connect(self._make)
        a.addWidget(go, 0, Qt.AlignmentFlag.AlignLeft)
        a.addStretch(1)
        outer.addWidget(self.ask, 1)

        self.result = QWidget()
        r = QVBoxLayout(self.result)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(6)
        self.for_whom = SubtitleLabel("")
        r.addWidget(self.for_whom)
        r.addWidget(BodyLabel("In the app's pairing screen, type:"))
        sides = QHBoxLayout()
        sides.setSpacing(20)
        left = QVBoxLayout()
        left.setSpacing(4)
        left.addWidget(StrongBodyLabel("Address"))
        self.address = BodyLabel("")
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(self.address)
        left.addSpacing(6)
        left.addWidget(StrongBodyLabel("Code"))
        self.code = TitleLabel("")
        f = QFont("Cascadia Mono, Consolas")
        f.setPixelSize(30)
        self.code.setFont(f)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(self.code)
        left.addSpacing(6)
        left.addWidget(StrongBodyLabel("Certificate"))
        self.fp = BodyLabel("")
        self.fp.setWordWrap(True)
        left.addWidget(self.fp)
        left.addStretch(1)
        sides.addLayout(left, 1)
        self.qr = QLabel()
        self.qr.setFixedSize(196, 196)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setStyleSheet("background:white;border-radius:8px")
        sides.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignTop)
        r.addLayout(sides)
        r.addWidget(CaptionLabel("The QR code holds the same three things, for when the apps learn to scan it."))
        r.addSpacing(6)
        self.state = SimpleCardWidget()
        sl = QHBoxLayout(self.state)
        sl.setContentsMargins(14, 10, 14, 10)
        self.state_icon = IconWidget(FluentIcon.SYNC)
        self.state_icon.setFixedSize(18, 18)
        sl.addWidget(self.state_icon)
        self.state_text = BodyLabel("")
        self.state_text.setWordWrap(True)
        sl.addWidget(self.state_text, 1)
        r.addWidget(self.state)
        r.addStretch(1)
        done = PushButton("Done")
        done.clicked.connect(self.close)
        r.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)
        self.result.hide()
        outer.addWidget(self.result, 1)

    def start(self) -> None:
        self.device_id = None
        self.result.hide()
        self.ask.show()
        if not self.isVisible():
            self.showNormal()
            center(self, 680, 680)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _make(self) -> None:
        app_name, device = self.app.text().strip(), self.device.text().strip()
        if not app_name or not device:
            InfoBar.warning("Missing name", "Type an app name and a device name.", duration=4000,
                            position=InfoBarPosition.TOP, parent=self)
            return
        dev, code = self.svc.pair(app_name, device)
        self.device_id = dev.id
        port = self.svc.phone_port or self.svc.settings.phone_port
        addrs = lan_addresses()
        self.for_whom.setText(dev.label)
        self.address.setText("\n".join(f"{a}:{port}" for a in addrs) or f"(this PC's address):{port}")
        self.code.setText(shown(code))
        self.fp.setText(f"The app should show a certificate ending in {tls.short_fingerprint(self.svc.folder)}.")
        self.qr.setPixmap(qr_pixmap(qr_payload(addrs, port, code, tls.fingerprint(self.svc.folder))))
        self.ask.hide()
        self.result.show()
        self.refresh()

    def refresh(self) -> None:
        if self.device_id is None:
            return
        dev = self.svc.pairings.devices.get(self.device_id)
        if dev is not None and dev.first_used:
            self.state.setStyleSheet("SimpleCardWidget{background:rgba(16,124,16,0.35);border-radius:6px}")
            self.state_icon.setIcon(FluentIcon.ACCEPT)
            self.state_text.setText("Paired ✓ The device has used its code. You can close this window.")
        elif dev is None:
            self.state.setStyleSheet("")
            self.state_icon.setIcon(FluentIcon.CANCEL)
            self.state_text.setText("This code has expired or was forgotten. Pair again for a new one.")
        else:
            self.state.setStyleSheet("")
            self.state_icon.setIcon(FluentIcon.SYNC)
            self.state_text.setText("Waiting for the device to use the code… The code works once you type it, "
                                    "and expires if not used within 30 minutes. It is never shown again.")

    def closeEvent(self, e):
        # don't keep the code on screen after closing
        self.code.setText("")
        self.qr.clear()
        self.device_id = None
        super().closeEvent(e)
