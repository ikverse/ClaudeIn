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

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QTableWidgetItem, QVBoxLayout,
                               QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, EditableComboBox, FluentIcon, FluentWidget,
                            FluentWindow, IconWidget, InfoBar, InfoBarPosition, LineEdit, MessageBox,
                            NavigationItemPosition, PrimaryPushButton, ProgressBar, PushButton, ScrollArea,
                            SimpleCardWidget, StrongBodyLabel, SubtitleLabel, SwitchButton, TableWidget,
                            TitleLabel, isDarkTheme)

from . import autostart, tailscale, tls
from .claudecode import INSTALL_PAGE, KNOWN_GOOD, safe_link
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
    card.caption = None  # the explanation label, for cards whose words change
    if text:
        cap = CaptionLabel(text)
        cap.setWordWrap(True)
        col.addWidget(cap)
        card.caption = cap
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


STATE_WORDS = {"checking": "checking…", "missing": "not installed", "broken": "won't start",
               "too_old": "too old for ClaudeIn", "incompatible": "not compatible with ClaudeIn yet"}


def claude_line(st: dict) -> str:
    c = st["claude"]
    if c["state"] != "ok":
        v = f" {c['version']}" if c["version"] else ""
        return f"Claude Code{v} — {STATE_WORDS[c['state']]}"
    if st["signed_in"] is True:
        who = c["email"] or "your Claude account"
        s = f"signed in as {who}" + (f" ({c['plan']})" if c["plan"] else "") + " ✓"
    else:
        s = {False: "signed out ✗", None: "not checked yet"}[st["signed_in"]]
    return f"Claude Code {c['version']} — {s}"


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
        self.setup_b = PrimaryPushButton(FluentIcon.PEOPLE, "Set up Claude…")
        self.setup_b.clicked.connect(lambda: self.window().open("claude"))
        self.claude = row(FluentIcon.ROBOT, "Claude Code", "", self.setup_b)
        self.claude_text = CaptionLabel("")
        self.claude_text.setWordWrap(True)
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
        self.claude_text.setText(claude_line(st))
        self.setup_b.setVisible(st["claude"]["needs_setup"])
        clear(self.runs_box)
        s = self.svc.scheduler
        items = [(r, "working") for r, _ in s.running.values()] + [(r, "waiting") for r in s.waiting]
        if not items:
            self.runs_box.addWidget(CaptionLabel("None right now."))
        for r, what in items:
            self.runs_box.addWidget(row(FluentIcon.PHONE if r.is_phone else FluentIcon.HOME, r.device_label,
                                        f"{what} · {r.spec.model} · asked {when(r.asked_at)}", None))


def hbox(*widgets: QWidget) -> QWidget:
    """Widgets side by side, for the right-hand end of a settings card."""
    box = QWidget()
    h = QHBoxLayout(box)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(8)
    for w in widgets:
        h.addWidget(w)
    return box


def go(coro) -> None:
    """Starts an async action from a button; the engine's loop runs it."""
    asyncio.ensure_future(coro)


class ClaudePage(Page):
    """Step 1: Claude Code on this PC. Step 2: the Claude account whose plan ClaudeIn uses. Then the safety net."""

    def __init__(self, svc: "Service"):
        super().__init__("claude", "Claude")
        self.svc = svc
        intro = BodyLabel("ClaudeIn answers your apps with Claude Code on this PC, signed in with the Claude account you "
                          "choose. What your apps ask counts against that account's plan.")
        intro.setWordWrap(True)
        self.v.addWidget(intro)

        self.download_b = PrimaryPushButton(FluentIcon.LINK, "Open download page")
        self.download_b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(INSTALL_PAGE)))
        self.check_b = PushButton(FluentIcon.SYNC, "Check again")
        self.check_b.clicked.connect(lambda: go(self.svc.recheck_claude()))
        self.step1 = row(FluentIcon.ROBOT, "1 · Claude Code", "Checking…", hbox(self.download_b, self.check_b))
        self.v.addWidget(self.step1)

        self.sign_b = PrimaryPushButton(FluentIcon.PEOPLE, "Sign in with Claude")
        self.sign_b.clicked.connect(lambda: go(self.svc.sign_in()))
        self.switch_b = PushButton(FluentIcon.SYNC, "Switch account")
        self.switch_b.clicked.connect(lambda: go(self._switch()))
        self.out_b = PushButton(FluentIcon.CLOSE, "Sign out")
        self.out_b.clicked.connect(lambda: go(self.svc.sign_out()))
        self.step2 = row(FluentIcon.PEOPLE, "2 · Claude account", "…", hbox(self.sign_b, self.switch_b, self.out_b))
        self.v.addWidget(self.step2)

        self.waiting = SimpleCardWidget()
        wv = QVBoxLayout(self.waiting)
        wv.setContentsMargins(16, 12, 16, 12)
        wv.addWidget(StrongBodyLabel("Finish signing in, in your browser"))
        words = CaptionLabel("A Claude page opened in your browser. Choose the account whose plan you want ClaudeIn to "
                             "use. This window updates by itself when you are done.")
        words.setWordWrap(True)
        wv.addWidget(words)
        self.again_b = PushButton(FluentIcon.LINK, "Open the page again")
        self.again_b.clicked.connect(self._open_link)
        self.copy_b = PushButton(FluentIcon.COPY, "Copy link")
        self.copy_b.clicked.connect(self._copy_link)
        cancel = PushButton(FluentIcon.CANCEL, "Cancel")
        cancel.clicked.connect(self.svc.cancel_sign_in)
        wv.addWidget(hbox(self.again_b, self.copy_b, cancel), 0, Qt.AlignmentFlag.AlignLeft)
        self.v.addWidget(self.waiting)

        self.own_login = SwitchButton()
        self.own_login.setOnText("On")
        self.own_login.setOffText("Off")
        self.own_login.checkedChanged.connect(lambda on: go(self.svc.set_own_login(on)))
        self.v.addWidget(row(FluentIcon.CERTIFICATE, "Its own Claude login",
                             "On: ClaudeIn keeps its own sign-in, so it can use a different account from the one you use "
                             "in a terminal. Off: it uses the sign-in of Claude Code on this PC.", self.own_login))

        self.v.addSpacing(8)
        self.v.addWidget(StrongBodyLabel("Claude Code versions ClaudeIn has seen"))
        self.versions_box = QVBoxLayout()
        self.versions_box.setSpacing(8)
        self.v.addLayout(self.versions_box)
        self._shape = None

        self.v.addSpacing(8)
        self.v.addWidget(StrongBodyLabel("Advanced"))
        self.own_b = PushButton(FluentIcon.DOWNLOAD, f"Download {KNOWN_GOOD}…")
        self.own_b.clicked.connect(self._download)
        self.installed_b = PushButton(FluentIcon.HOME, "Use the one on this PC")
        self.installed_b.clicked.connect(lambda: go(self.svc.use_installed_claude()))
        self.copy_row = row(FluentIcon.CLOUD_DOWNLOAD, "A known-good copy for ClaudeIn",
                            f"If a Claude Code update stops working with ClaudeIn, ClaudeIn can use its own copy of "
                            f"version {KNOWN_GOOD} instead (about 355 MB). It is downloaded from Anthropic, checked against "
                            "Anthropic's checksum and signature, and never updates itself. Your own Claude Code isn't "
                            "touched.", hbox(self.own_b, self.installed_b))
        self.copy_base = self.copy_row.caption.text()
        self.v.addWidget(self.copy_row)
        self.progress = ProgressBar()
        self.progress.hide()
        self.v.addWidget(self.progress)

    # --- actions ---------------------------------------------------------------------------------------------------------

    async def _switch(self) -> None:
        await self.svc.sign_out()
        await self.svc.sign_in()

    def _open_link(self) -> None:
        url = self.svc.claude.login.url
        if safe_link(url):
            QDesktopServices.openUrl(QUrl(url))

    def _copy_link(self) -> None:
        url = self.svc.claude.login.url
        if safe_link(url):
            QGuiApplication.clipboard().setText(url)
            InfoBar.success("Link copied", "Paste it into any browser to sign in.", duration=3000,
                            position=InfoBarPosition.TOP, parent=self.window())

    def _download(self) -> None:
        box = MessageBox(f"Download Claude Code {KNOWN_GOOD}?",
                         "ClaudeIn will download Claude Code (about 355 MB) from Anthropic (downloads.claude.ai) and use it "
                         "only for itself. It checks Anthropic's checksum and signature first. Your own Claude Code "
                         "isn't changed.", self.window())
        box.yesButton.setText("Download")
        box.cancelButton.setText("Cancel")
        if box.exec():
            go(self._run_download())

    async def _run_download(self) -> None:
        ok = await self.svc.install_claude_copy()
        if ok:
            InfoBar.success("Claude Code is ready", f"ClaudeIn now uses its own copy of Claude Code {KNOWN_GOOD}.",
                            duration=6000, position=InfoBarPosition.TOP, parent=self.window())
        else:
            InfoBar.error("Couldn't get Claude Code", self.svc.claude.install.error or "Try again.", duration=-1,
                          position=InfoBarPosition.TOP, parent=self.window())

    # --- showing ---------------------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        svc = self.svc
        c = svc.claude
        state = c.state

        if state == "ok":
            where = "ClaudeIn's own copy" if svc.settings.claude_copy == "own" else "installed on this PC"
            text = f"Found version {c.version}, {where}.\n{c.path}"
            if c.suspect:
                text += "\n" + (c.explain() or "")
            if c.probe and c.probe.missing_optional:
                text += "\nNot in this version, so left out: " + ", ".join(c.probe.missing_optional)
        elif state == "checking":
            text = "Checking…"
        else:
            text = c.explain() or ""
        self.step1.caption.setText(text)
        self.download_b.setVisible(state in ("missing", "broken", "too_old"))

        waiting = c.login.state == "waiting"
        usable = state == "ok"
        if not usable:
            words = "Do step 1 first."
        elif c.signed_in is True and c.account is not None:
            words = f"Signed in as {c.account.label}. ClaudeIn uses this account's plan."
        elif c.wrong_account_type:
            words = c.signed_out_message() or ""
        elif c.signed_in is False:
            words = "Not signed in. Press Sign in with Claude and a browser page opens."
        else:
            words = "Couldn't tell whether Claude Code is signed in. Press Check again, or Sign in with Claude."
        if c.login.state == "failed" and c.login.error:
            words += "\n" + c.login.error
        self.step2.caption.setText(words)
        signed = usable and c.signed_in is True
        self.sign_b.setVisible(usable and not signed)
        self.switch_b.setVisible(signed)
        self.out_b.setVisible(signed or (usable and c.wrong_account_type))
        for b in (self.sign_b, self.switch_b, self.out_b, self.check_b):
            b.setEnabled(not waiting)
        self.waiting.setVisible(waiting)
        self.again_b.setEnabled(bool(c.login.url))
        self.copy_b.setEnabled(bool(c.login.url))

        self.own_login.blockSignals(True)
        self.own_login.setChecked(svc.settings.own_login)
        self.own_login.blockSignals(False)

        rows = c.versions()
        shape = tuple((r["version"], r["check"], r["good"], r["bad"], r["current"]) for r in rows)
        if shape != self._shape:
            self._shape = shape
            clear(self.versions_box)
            if not rows:
                self.versions_box.addWidget(CaptionLabel("None yet."))
            for r in rows:
                ok = r["check"] == "ok" and not (r["bad"] and not r["good"])
                if r["check"] == "ok":
                    what = "Passed the check"
                    if r["good"] or r["bad"]:
                        what += f" · {r['good']} questions answered"
                        if r["bad"]:
                            what += f" · {r['bad']} stopped without an answer"
                elif r["check"] == "incompatible":
                    what = "Not compatible: it lacks " + (", ".join(r["missing"]) or "something ClaudeIn needs")
                elif r["check"] == "too_old":
                    what = "Too old for ClaudeIn"
                else:
                    what = "Didn't start"
                title = r["version"] + ("  (in use)" if r["current"] else "")
                self.versions_box.addWidget(row(FluentIcon.ACCEPT if ok else FluentIcon.CANCEL, title, what, None))

        own = svc.settings.claude_copy == "own"
        busy = c.install.state in ("downloading", "verifying")
        self.own_b.setEnabled(not busy)
        self.own_b.setVisible(not own)
        self.installed_b.setVisible(own)
        extra = ""
        if busy:
            mb = c.install.done / 1e6
            extra = (f"\nDownloading… {mb:.0f} of {c.install.total / 1e6:.0f} MB" if c.install.state == "downloading"
                     else "\nChecking the download…")
        elif c.install.state == "failed":
            extra = "\nLast try failed: " + (c.install.error or "")
        self.copy_row.caption.setText(self.copy_base + extra)
        self.progress.setVisible(busy)
        if busy and c.install.total:
            self.progress.setValue(min(100, round(100 * c.install.done / c.install.total)))


class DevicesPage(Page):
    def __init__(self, svc: "Service", open_pairing: Callable[[], None]):
        super().__init__("devices", "Connected apps")
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
            self.list.addWidget(BodyLabel("No apps connected yet. Press “Pair a device” to connect an app on a phone."))
        for aid in app_ids:
            devices = by_app.get(aid, [])
            card = SimpleCardWidget()
            cv = QVBoxLayout(card)
            cv.setContentsMargins(16, 14, 16, 16)
            cv.setSpacing(8)
            cv.addWidget(SubtitleLabel(p.app_name(aid)))
            n = len(devices)
            cv.addWidget(CaptionLabel(f"{n} device{'' if n == 1 else 's'}" if n else "Program on this PC"))
            self.list.addWidget(card)
            models = LineEdit()
            models.setPlaceholderText("Any model")
            models.setText(", ".join(p.apps[aid].models) if aid in p.apps else "")
            models.setFixedWidth(300)
            models.editingFinished.connect(lambda a=aid, w=models: self._save_models(a, w.text()))
            cv.addWidget(row(FluentIcon.SETTING, "Allowed models",
                             "Model ids separated by commas, such as claude-opus-5-5. Empty means any model.",
                             models))
            if not devices:
                cv.addWidget(row(FluentIcon.HOME, f"{p.app_name(aid)} (this PC)",
                                        "A program on this PC. It uses the local token instead of a pairing.", None))
            for d in devices:
                sw = SwitchButton()
                sw.setOnText("On")
                sw.setOffText("Off")
                sw.setChecked(d.enabled)
                sw.checkedChanged.connect(lambda on, i=d.id: self.svc.set_device_enabled(i, on))
                forget = PushButton(FluentIcon.DELETE, "Forget…")
                forget.clicked.connect(lambda _=False, dev=d: self._forget(dev))
                box = QWidget()
                hb = QHBoxLayout(box)
                hb.setContentsMargins(0, 0, 0, 0)
                hb.addWidget(sw)
                hb.addWidget(forget)
                used = f"last used {when(d.last_used)}" if d.last_used else "not used yet (the code expires 30 minutes after pairing)"
                cv.addWidget(row(FluentIcon.PHONE, d.device, f"Paired {when(d.created)} · {used}", box))

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
    def __init__(self, svc: "Service", restart: Callable[[], None], quit_: Callable[[], None] | None = None):
        super().__init__("general", "General")
        self.svc = svc
        self.restart = restart
        self.quit_ = quit_  # used after an update's installer has started

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
                             "Starts ClaudeIn when you sign in to Windows.", self.autostart))

        self.restart_sw = SwitchButton()
        self.restart_sw.setOnText("On")
        self.restart_sw.setOffText("Off")
        self.restart_sw.setChecked(svc.settings.restart_if_stopped)
        self.restart_sw.checkedChanged.connect(lambda on: asyncio.ensure_future(self._restart_switch(on)))
        self.v.addWidget(row(FluentIcon.SYNC, "Restart ClaudeIn if it stops",
                             "Starts ClaudeIn again within 5 minutes if it ever stops without Quit. Off by default: "
                             "Windows Defender can take this kind of task for malware.", self.restart_sw))

        self.phone_check_b = PushButton(FluentIcon.SYNC, "Check")
        self.phone_check_b.clicked.connect(lambda: asyncio.ensure_future(self.check_phones()))
        self.phone_setup_b = PrimaryPushButton(FluentIcon.CERTIFICATE, "Set up phone access")
        self.phone_setup_b.clicked.connect(lambda: asyncio.ensure_future(self._set_up_phones()))
        self.phone_setup_b.hide()
        pbox = QWidget()
        pb = QHBoxLayout(pbox)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.addWidget(self.phone_setup_b)
        pb.addWidget(self.phone_check_b)
        self.phone_card = row(FluentIcon.PHONE, "Phone access",
                              "Press Check to see whether phones can reach this PC at home and away.", pbox)
        self.v.addWidget(self.phone_card)

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
                             "Asks Claude one tiny Haiku question to confirm it really answers. Uses a very small "
                             "amount of the plan. (Signing in and the version check are on the Claude page and cost "
                             "nothing.)", self.check_b))
        self._build_updates()

    def refresh(self) -> None:
        self.workers.blockSignals(True)
        self.workers.setCurrentIndex(self.svc.settings.workers - 1)
        self.workers.blockSignals(False)
        for w, value in ((self.awake, self.svc.settings.keep_awake), (self.autostart, autostart.enabled()),
                         (self.restart_sw, self.svc.settings.restart_if_stopped)):
            w.blockSignals(True)
            w.setChecked(value)
            w.blockSignals(False)
        self._refresh_updates()

    # --- updates (phase 6) -------------------------------------------------------------------------------------------

    def _build_updates(self) -> None:
        self.update_install_b = PrimaryPushButton(FluentIcon.DOWNLOAD, "Install")
        self.update_install_b.clicked.connect(self._install_update)
        self.update_install_b.hide()
        self.update_check_b = PushButton(FluentIcon.SYNC, "Check now")
        self.update_check_b.clicked.connect(lambda: go(self.svc.updates.check()))
        self.update_auto = SwitchButton()
        self.update_auto.setOnText("Auto")
        self.update_auto.setOffText("Off")
        self.update_auto.setChecked(self.svc.settings.check_updates)
        self.update_auto.checkedChanged.connect(self._auto_updates)
        self.update_card = row(FluentIcon.UPDATE, "Updates", " ",
                               hbox(self.update_install_b, self.update_check_b, self.update_auto))
        self.v.addWidget(self.update_card)
        self.update_bar = ProgressBar()
        self.update_bar.hide()
        self.v.addWidget(self.update_bar)

    def _auto_updates(self, on: bool) -> None:
        self.svc.settings.check_updates = on
        self.svc.settings.save(self.svc.folder)

    def _refresh_updates(self) -> None:
        from .config import VERSION

        u = self.svc.updates
        s = u.status
        lines = [f"This is ClaudeIn {VERSION}." + ("" if u.can_install else
                                                  " It runs from .venv, so it is updated with git, not here.")]
        r = s.release
        if s.state == "checking":
            lines.append("Checking GitHub…")
        elif s.state == "up_to_date":
            lines.append("It is up to date" + (f" (checked {when(s.checked_at)})." if s.checked_at else "."))
        elif r is not None and s.state in ("available", "failed"):
            lines.append(f"ClaudeIn {r.version} is available.")
            if r.notes:
                lines.append(r.notes[:400] + ("…" if len(r.notes) > 400 else ""))
        if s.state == "downloading":
            lines.append(f"Downloading ClaudeIn {r.version}… {s.done / 1e6:.0f} of {s.total / 1e6:.0f} MB")
        elif s.state == "verifying":
            lines.append("Checking the update's signature…")
        elif s.state == "installing":
            lines.append("Starting the installer. ClaudeIn closes now and starts again by itself.")
        if s.state == "failed" and s.error:
            lines.append(s.error)
        lines.append("Looks on GitHub every 12 hours. Installing always asks first." if self.svc.settings.check_updates
                     else "Automatic checks are off. Press Check now to look.")
        self.update_card.caption.setText("\n".join(lines))
        busy = s.state in ("checking", "downloading", "verifying", "installing")
        self.update_check_b.setEnabled(not busy)
        show = u.available is not None and u.can_install
        self.update_install_b.setVisible(show)
        self.update_install_b.setEnabled(show and not busy)
        if show:
            self.update_install_b.setText(f"Install {u.available.version}")
        self.update_bar.setVisible(s.state in ("downloading", "verifying"))
        if s.total:
            self.update_bar.setValue(min(100, round(100 * s.done / s.total)))
        self.update_auto.blockSignals(True)
        self.update_auto.setChecked(self.svc.settings.check_updates)
        self.update_auto.blockSignals(False)

    def _install_update(self) -> None:
        r = self.svc.updates.available
        if r is None:
            return
        box = MessageBox(f"Install ClaudeIn {r.version}?",
                         "ClaudeIn downloads it from its GitHub releases, checks its signature, and closes while it "
                         "updates. It starts again by itself. Questions going now will stop.", self.window())
        box.yesButton.setText("Install")
        box.cancelButton.setText("Not now")
        if box.exec():
            go(self._run_install())

    async def _run_install(self) -> None:
        if await self.svc.updates.install():
            if self.quit_ is not None:
                self.quit_()  # the installer is running; it starts the new ClaudeIn when it is done
        else:
            self._info(False, "Update not installed", self.svc.updates.status.error or "Try again later.")

    async def check_phones(self) -> None:
        """The "Phone access" check: three plain lines, and the set-up button if the firewall rule is missing."""
        self.phone_check_b.setEnabled(False)
        self.phone_check_b.setText("Checking…")
        try:
            pa = await self.svc.phone_access()
        finally:
            self.phone_check_b.setEnabled(True)
            self.phone_check_b.setText("Check")
        self.phone_card.caption.setText("\n".join(("✓ " if ok else "✗ ") + text for ok, text in pa.lines))
        self.phone_setup_b.setVisible(pa.needs_rule)

    async def _set_up_phones(self) -> None:
        box = MessageBox("Set up phone access?",
                         "ClaudeIn adds one Windows Firewall rule: phones may reach port "
                         f"{self.svc.phone_port or self.svc.settings.phone_port}, only from Tailscale and your home "
                         "network, and only to ClaudeIn. It also removes the older, wider ClaudeIn rules.\n\n"
                         "Windows will ask for permission. Click Yes.", self.window())
        box.yesButton.setText("Continue")
        box.cancelButton.setText("Cancel")
        if not box.exec():
            return
        self.phone_setup_b.setEnabled(False)
        try:
            result = await self.svc.set_up_phone_access()
        finally:
            self.phone_setup_b.setEnabled(True)
        if result == "done":
            self._info(True, "Phone access set up", "The firewall rule is in place.")
        elif result == "cancelled":
            self._info(False, "Not set up", "Windows' question was answered No, so nothing changed.")
        else:
            self._info(False, "Couldn't set up phone access", "Windows Firewall refused the change.")
        await self.check_phones()

    def _info(self, ok: bool, title: str, text: str) -> None:
        (InfoBar.success if ok else InfoBar.error)(title, text, duration=6000 if ok else -1,
                                                   position=InfoBarPosition.TOP, parent=self.window())

    def _autostart(self, on: bool) -> None:
        autostart.set_enabled(on)

    async def _restart_switch(self, on: bool) -> None:
        ok = await asyncio.to_thread(autostart.set_restart, on)
        self.svc.settings.restart_if_stopped = on and ok
        self.svc.settings.save(self.svc.folder)
        if not ok:
            self._info(False, "Restart task not added", "Windows refused the task that restarts ClaudeIn.")
            self.refresh()

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
    def __init__(self, svc: "Service", open_pairing: Callable[[], None], restart: Callable[[], None], icon,
                 quit_: Callable[[], None] | None = None):
        super().__init__()
        self._init_glass()
        self.setWindowTitle("ClaudeIn")
        self.setWindowIcon(icon)
        self.resize(980, 720)
        self.setMinimumSize(760, 520)
        self.home = HomePage(svc)
        self.claude = ClaudePage(svc)
        self.devices = DevicesPage(svc, open_pairing)
        self.usage = UsagePage(svc)
        self.caps = CapsPage(svc)
        self.general = GeneralPage(svc, restart, quit_)
        self.addSubInterface(self.home, FluentIcon.HOME, "Home")
        self.addSubInterface(self.claude, FluentIcon.ROBOT, "Claude")
        self.addSubInterface(self.devices, FluentIcon.APPLICATION, "Connected apps")
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

    def open_phone_access(self) -> None:
        """From the tray: the General page, with the "Phone access" check already running."""
        self.open("general")
        asyncio.ensure_future(self.general.check_phones())

    def refresh(self) -> None:
        if self.isVisible():
            w = self.stackedWidget.currentWidget()
            if isinstance(w, Page):
                w.refresh()


def qr_payload(addresses: list[str], port: int, code: str, fingerprint: str, name: str | None = None) -> str:
    """What the pairing QR code holds, for when the apps learn to scan it. The Tailscale address comes first."""
    data = {"claudein": 1, "addresses": addresses, "port": port, "code": shown(code), "fingerprint": fingerprint}
    if name:
        data["name"] = name  # the PC's Tailscale name
    return json.dumps(data, separators=(",", ":"))


def address_text(ts: "tailscale.Tailscale", home: list[str], port: int) -> str:
    """The addresses to type, Tailscale's first: it works at home and away, so a phone needs only that one."""
    lines = [f"{a}:{port}   ← works at home and away (use this one)" for a in (ts.addresses if ts.on else [])]
    lines += [f"{a}:{port}   home only" for a in home]
    if not ts.on:
        lines.append("Tailscale isn't installed on this PC, so phones work only at home." if ts.state == "missing"
                     else "Tailscale is off, so phones work only at home. Turn it on for an address that works away.")
    return "\n".join(lines) or f"(this PC's address):{port}"


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
        self.address.setWordWrap(True)
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
        ts = tailscale.status()
        home = [a for a in lan_addresses() if not tailscale.is_tailscale(a)]
        addrs = (ts.addresses if ts.on else []) + home
        self.for_whom.setText(dev.label)
        self.address.setText(address_text(ts, home, port))
        self.code.setText(shown(code))
        self.fp.setText(f"The app should show a certificate ending in {tls.short_fingerprint(self.svc.folder)}.")
        self.qr.setPixmap(qr_pixmap(qr_payload(addrs, port, code, tls.fingerprint(self.svc.folder),
                                               name=ts.full_name if ts.on else None)))
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
