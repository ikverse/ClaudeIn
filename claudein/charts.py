"""The Usage page's charts, drawn by ClaudeIn itself in the Fluent look (no chart library: the installer leaves Qt's
out to stay small). Light and dark follow the app's theme each time they are drawn."""

from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget
from qfluentwidgets import isDarkTheme

# One colour per app, in a fixed order, so an app keeps its colour whatever the filters.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER = "#888780"


def series_colour(i: int) -> QColor:
    pal = SERIES_DARK if isDarkTheme() else SERIES_LIGHT
    return QColor(pal[i] if 0 <= i < len(pal) else OTHER)


def ending_colour(ending: str) -> QColor:
    dark = isDarkTheme()
    if ending == "done":
        return QColor("#6ccb5f" if dark else "#0f7b0f")
    if ending == "failed":
        return QColor("#ff99a4" if dark else "#c42b1c")
    return QColor("#9a9a9a" if dark else "#8a8a8a")


def _ink() -> tuple[QColor, QColor, QColor, QColor]:
    """(text, muted text, grid line, empty track)."""
    if isDarkTheme():
        return QColor(255, 255, 255), QColor(154, 154, 154), QColor(58, 58, 58), QColor(255, 255, 255, 20)
    return QColor(27, 27, 27), QColor(110, 110, 110), QColor(229, 229, 229), QColor(0, 0, 0, 16)


def nice_ticks(top: float, count: int = 4) -> list[float]:
    """Round steps from 0 to at least [top]."""
    if top <= 0:
        return [0, 1]
    raw = top / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    n = math.ceil(top / step - 1e-9)
    return [i * step for i in range(n + 1)]


class StackedBars(QWidget):
    """Bars over time, one stacked part per app, with a legend above. Hover shows the numbers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.labels: list[str] = []
        self.series: list[tuple[str, QColor, list[float]]] = []
        self.fmt: Callable[[float], str] = lambda v: f"{v:g}"
        self._plot = QRectF()

    def set_data(self, labels: list[str], series: list[tuple[str, QColor, list[float]]],
                 fmt: Callable[[float], str]) -> None:
        self.labels, self.series, self.fmt = labels, series, fmt
        self.update()

    def totals(self) -> list[float]:
        return [sum(s[2][i] for s in self.series) for i in range(len(self.labels))]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _text, muted, grid, _track = _ink()
        small = self.font()
        small.setPointSizeF(max(7.5, small.pointSizeF() - 1.5))
        sfm = QFontMetrics(small)

        # legend
        x, y = 0.0, 0.0
        p.setFont(small)
        for name, colour, _ in self.series:
            w = 16 + sfm.horizontalAdvance(name) + 18
            if x + w > self.width() and x > 0:
                x, y = 0.0, y + sfm.height() + 4
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(QRectF(x, y + (sfm.height() - 10) / 2, 10, 10), 2, 2)
            p.setPen(muted)
            p.drawText(QPointF(x + 16, y + sfm.ascent()), name)
            x += w
        top_y = y + sfm.height() + 12

        totals = self.totals()
        ticks = nice_ticks(max(totals, default=0))
        left = max(sfm.horizontalAdvance(self.fmt(t)) for t in ticks) + 10
        plot = QRectF(left, top_y + 4, self.width() - left - 4, self.height() - top_y - sfm.height() - 14)
        self._plot = plot
        if plot.height() < 20 or plot.width() < 20:
            return
        tmax = ticks[-1] or 1

        for t in ticks:
            ty = plot.bottom() - plot.height() * t / tmax
            p.setPen(grid)
            p.drawLine(QPointF(plot.left(), ty), QPointF(plot.right(), ty))
            p.setPen(muted)
            p.drawText(QRectF(0, ty - sfm.height() / 2, left - 8, sfm.height()),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.fmt(t))

        n = len(self.labels)
        if not n:
            return
        slot = plot.width() / n
        bw = min(28.0, max(2.0, slot * 0.62))
        gap = 2.0 if bw > 6 else 0.0  # a see-through gap between the stacked parts
        for i in range(n):
            cx = plot.left() + slot * (i + 0.5)
            base = plot.bottom()
            parts = [(s[1], s[2][i]) for s in self.series if s[2][i] > 0]
            for j, (colour, v) in enumerate(parts):
                h = plot.height() * v / tmax
                last = j == len(parts) - 1
                shown_h = h if last else max(0.5, h - gap)
                r = QRectF(cx - bw / 2, base - h, bw, shown_h) if last else QRectF(cx - bw / 2, base - h + gap,
                                                                                     bw, shown_h)
                path = QPainterPath()
                path.setFillRule(Qt.FillRule.WindingFill)  # the two shapes overlap; both must stay filled
                if last and h > 3:  # round the top end only
                    path.addRoundedRect(r, 3, 3)
                    path.addRect(QRectF(r.left(), r.top() + 3, r.width(), r.height() - 3))
                else:
                    path.addRect(r)
                p.fillPath(path, colour)
                base -= h

        # x labels, skipped so they don't collide
        p.setPen(muted)
        widest = max(sfm.horizontalAdvance(lb) for lb in self.labels) + 10
        every = max(1, math.ceil(widest / slot))
        for i, lb in enumerate(self.labels):
            if i % every == 0:
                cx = plot.left() + slot * (i + 0.5)
                p.drawText(QRectF(cx - widest / 2, plot.bottom() + 4, widest, sfm.height()),
                           Qt.AlignmentFlag.AlignHCenter, lb)
        p.end()

    def mouseMoveEvent(self, e):
        n = len(self.labels)
        pos = e.position()
        if not n or not self._plot.contains(pos):
            QToolTip.hideText()
            return
        i = min(n - 1, int((pos.x() - self._plot.left()) / (self._plot.width() / n)))
        lines = [f"<b>{self.labels[i]}</b>"]
        lines += [f"{name}: {self.fmt(vals[i])}" for name, _, vals in self.series if vals[i]]
        if len(lines) == 1:
            lines.append("Nothing")
        QToolTip.showText(e.globalPosition().toPoint(), "<br>".join(lines), self)

    def leaveEvent(self, _):
        QToolTip.hideText()


class ShareList(QWidget):
    """A short list of rows: a coloured mark, a name, a value on the right and a thin bar of its share under it."""

    ROW = 34

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[str, str, float, QColor]] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_rows(self, rows: list[tuple[str, str, float, QColor]]) -> None:
        """[rows]: (name, value text, share 0..1, colour)."""
        self.rows = rows
        self.setFixedHeight(max(1, len(rows)) * self.ROW)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        text, muted, _grid, track = _ink()
        fm = QFontMetrics(self.font())
        w = self.width()
        for i, (name, value, share, colour) in enumerate(self.rows):
            y = i * self.ROW
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(QRectF(0, y + (fm.height() - 10) / 2, 10, 10), 2, 2)
            vw = fm.horizontalAdvance(value)
            p.setPen(text)
            p.drawText(QRectF(16, y, w - vw - 28, fm.height()), Qt.AlignmentFlag.AlignVCenter,
                       fm.elidedText(name, Qt.TextElideMode.ElideRight, int(w - vw - 28)))
            p.setPen(muted)
            p.drawText(QRectF(w - vw, y, vw, fm.height()), Qt.AlignmentFlag.AlignVCenter, value)
            bar = QRectF(0, y + fm.height() + 5, w, 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(track)
            p.drawRoundedRect(bar, 3, 3)
            if share > 0:
                p.setBrush(colour)
                p.drawRoundedRect(QRectF(0, bar.top(), max(6.0, w * min(1.0, share)), 6), 3, 3)
        p.end()
