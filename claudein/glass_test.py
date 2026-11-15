"""Glass check (plan 4.6): a sample window to drag around.

It opens with Acrylic (frosted glass). The button at the bottom reopens it in the
same place with Mica, and back, so the two can be compared while dragging.

Run:  .venv\\Scripts\\pythonw.exe -m claudein.glass_test
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    PrimaryPushButton,
    ProgressBar,
    SimpleCardWidget,
    StrongBodyLabel,
    SwitchButton,
    Theme,
    TitleLabel,
    isDarkTheme,
    setTheme,
    setThemeColor,
)
from qframelesswindow import AcrylicWindow, FramelessWindow, StandardTitleBar
from qframelesswindow.utils import getSystemAccentColor


def _card(icon, title, text, with_switch=True):
    card = SimpleCardWidget()
    row = QHBoxLayout(card)
    row.setContentsMargins(16, 12, 16, 12)
    row.setSpacing(14)
    ic = IconWidget(icon)
    ic.setFixedSize(20, 20)
    row.addWidget(ic)
    col = QVBoxLayout()
    col.setSpacing(0)
    col.addWidget(StrongBodyLabel(title))
    col.addWidget(CaptionLabel(text))
    row.addLayout(col, 1)
    if with_switch:
        sw = SwitchButton()
        sw.setChecked(True)
        row.addWidget(sw)
    return card


def _fill(window, mode, on_switch):
    """Put the sample content into a frameless window."""
    window.setTitleBar(StandardTitleBar(window))
    window.titleBar.raise_()
    window.setWindowTitle(f"ClaudeIn glass check: {mode}")
    window.resize(760, 560)

    body = QVBoxLayout(window)
    body.setContentsMargins(28, 48, 28, 24)
    body.setSpacing(12)

    body.addWidget(TitleLabel(f"Glass check: {mode}"))
    body.addWidget(BodyLabel(
        "Drag this window around by its top bar, over other windows.\n"
        "Does it move smoothly, or does it lag behind the mouse?"))
    body.addSpacing(6)

    body.addWidget(_card(FluentIcon.PHONE, "Signal Lab — Phone", "Last used today"))
    body.addWidget(_card(FluentIcon.PHONE, "Signal Lab EGX — Phone", "Last used yesterday"))
    body.addWidget(_card(FluentIcon.HOME, "Village (this PC)", "Local program"))

    plan = SimpleCardWidget()
    pl = QVBoxLayout(plan)
    pl.setContentsMargins(16, 12, 16, 12)
    pl.addWidget(StrongBodyLabel("Plan · 5-hour window"))
    bar = ProgressBar()
    bar.setValue(34)
    pl.addWidget(bar)
    pl.addWidget(CaptionLabel("34% used · week 12%  (sample numbers)"))
    body.addWidget(plan)

    body.addStretch(1)
    other = "Mica" if mode == "Acrylic" else "Acrylic"
    btn = PrimaryPushButton(FluentIcon.SYNC, f"Switch to {other}")
    btn.clicked.connect(on_switch)
    body.addWidget(btn, 0, Qt.AlignmentFlag.AlignRight)


class AcrylicTest(AcrylicWindow):
    def __init__(self, on_switch):
        super().__init__()
        _fill(self, "Acrylic", on_switch)
        tint = "20202099" if isDarkTheme() else "F2F2F299"
        self.windowEffect.setAcrylicEffect(self.winId(), tint)


class MicaTest(FramelessWindow):
    def __init__(self, on_switch):
        super().__init__()
        _fill(self, "Mica", on_switch)
        self.setStyleSheet("MicaTest{background:transparent}")
        self.windowEffect.setMicaEffect(self.winId(), isDarkTheme())


class Switcher:
    def __init__(self):
        self.window = None
        self.open(AcrylicTest)

    def open(self, kind):
        old = self.window
        self.window = kind(self.switch)
        if old is not None:
            self.window.move(old.pos())
            old.close()
            old.deleteLater()
        self.window.show()

    def switch(self):
        self.open(MicaTest if isinstance(self.window, AcrylicTest) else AcrylicTest)


def main():
    app = QApplication(sys.argv)
    setTheme(Theme.AUTO)
    setThemeColor(getSystemAccentColor(), save=False)
    switcher = Switcher()  # noqa: F841  (kept alive for the app's life)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
