"""Visual widgets: stylesheet, Timeline, snapshot view/dialog."""

import bisect
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog,
                               QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

import mp4trim_core as core

# ------------------------------------------------------------------- style

STYLE = """
QMainWindow, QDialog { background: #000000; }
QWidget { color: #e8e8ea; font-size: 10pt; }
QMenuBar { background: #000000; padding: 2px; border-bottom: 1px solid #131318; }
QMenuBar::item { padding: 5px 12px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: #1a1a20; }
QMenu { background: #0a0a0d; border: 1px solid #232329; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 28px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #2e7d32; }
QMenu::item:disabled { color: #55555c; }
QMenu::separator { height: 1px; background: #232329; margin: 6px 8px; }
QPushButton {
    background: #101014; border: 1px solid #26262c; border-radius: 8px;
    padding: 7px 14px; font-weight: 600;
}
QPushButton:hover { background: #1a1a20; border-color: #3a3a44; }
QPushButton:pressed { background: #08080a; }
QPushButton:disabled { color: #55555c; background: #0a0a0d; border-color: #18181d; }
QPushButton#accent { background: #2e7d32; border-color: #3a9440; color: #f2fff2; }
QPushButton#accent:hover { background: #37953c; }
QPushButton#accent:disabled { background: #122415; color: #5c7a60; }
QPushButton#discord { background: #4752c4; border-color: #5865f2; color: #f0f2ff; }
QPushButton#discord:hover { background: #5865f2; }
QPushButton#discord:disabled { background: #181b3a; color: #5c6190; }
QPushButton#tp { min-width: 36px; max-width: 36px; padding: 6px 0; font-size: 11pt; }
QPushButton#nav { min-width: 30px; max-width: 30px; padding: 6px 0; font-size: 11pt; }
QPushButton#play {
    min-width: 44px; max-width: 44px; min-height: 30px; padding: 4px 0;
    font-size: 13pt; border-radius: 19px; background: #e8e8ea; color: #000000;
    border: none;
}
QPushButton#play:hover { background: #ffffff; }
QPushButton#play:disabled { background: #1d1d23; color: #55555c; }
QPushButton#mark { padding: 6px 10px; font-weight: 600; }
QPushButton#link {
    background: transparent; border: none; color: #8ab4f8; padding: 2px 6px;
}
QPushButton#link:hover { text-decoration: underline; }
QPushButton#cancel { padding: 2px 10px; }
QComboBox {
    background: #101014; border: 1px solid #26262c; border-radius: 8px;
    padding: 6px 10px; min-width: 150px; font-weight: 600;
}
QComboBox:hover { border-color: #3a3a44; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #0a0a0d; border: 1px solid #232329; outline: none;
    selection-background-color: #4752c4; padding: 4px;
}
QProgressBar {
    background: #101014; border: none; border-radius: 4px;
    max-height: 8px; min-width: 180px;
}
QProgressBar::chunk { background: #5865f2; border-radius: 4px; }
QToolTip { background: #0a0a0d; color: #e8e8ea; border: 1px solid #232329; padding: 4px; }
QLabel#time { font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 13pt; color: #f0f0f2; }
QLabel#dur { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #6a6a74; }
QLabel#range { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #8fd694; }
QLabel#est { color: #a0a0a8; }
QLabel#muted { color: #8a8a92; }
QLabel#drop { color: #6a6a74; font-size: 13pt; background: #000000; }
QStatusBar { background: #000000; color: #8a8a92; border-top: 1px solid #131318; }
QStatusBar::item { border: none; }
"""

SHORTCUTS_HELP = """<table cellspacing=6>
<tr><td><b>Space</b></td><td>play / pause</td></tr>
<tr><td><b>PgUp</b> / <b>PgDn</b></td><td>previous / next video in the folder</td></tr>
<tr><td><b>I</b> / <b>O</b></td><td>set in / out at the playhead</td></tr>
<tr><td><b>Home</b> / <b>End</b></td><td>jump to in / out</td></tr>
<tr><td><b>,</b> / <b>.</b></td><td>previous / next frame</td></tr>
<tr><td><b>Left</b> / <b>Right</b></td><td>1 s back / forward (Shift = 10 s)</td></tr>
<tr><td><b>K</b></td><td>snap in-point to the keyframe (exact lossless start)</td></tr>
<tr><td><b>Mouse wheel</b></td><td>zoom the timeline (Shift+wheel pans, double-click resets)</td></tr>
<tr><td><b>Drag below the timeline</b></td><td>fine scrubbing: further down = slower (½, ¼, fine)</td></tr>
<tr><td><b>Enter</b></td><td>trim (lossless)</td></tr>
<tr><td><b>D</b></td><td>export Discord MP4</td></tr>
<tr><td><b>G</b></td><td>export GIF</td></tr>
<tr><td><b>S</b></td><td>snapshot / crop current frame</td></tr>
<tr><td><b>Ctrl+O</b></td><td>open (drag &amp; drop works too)</td></tr>
</table>"""


# ---------------------------------------------------------------- timeline

class Timeline(QWidget):
    """Zoomable timeline: thumbnails, ruler, draggable in/out handles.

    Drag the green handle right to cut the beginning, the red handle left to
    cut the end. Click elsewhere to scrub. Wheel zooms around the cursor,
    Shift+wheel pans, double-click resets the zoom.
    """

    seeked = Signal(int)
    range_changed = Signal()
    drag_state = Signal(bool)   # True while the mouse holds playhead/handle

    RULER = 18
    OVERVIEW = 8
    # Apple-style fine scrubbing: dragging further below the bar drops the
    # horizontal sensitivity through tiers. (min px below bar, rate, label)
    SCRUB_TIERS = [(140, 0.05, "fine scrubbing"), (90, 0.25, "¼ speed"),
                   (40, 0.5, "½ speed"), (0, 1.0, "")]

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(88)
        self.setMouseTracking(True)
        self.reset(0)

    def reset(self, duration: int):
        self.duration = duration
        self.position = 0
        self.mark_in = 0
        self.mark_out = duration
        self.view0, self.view1 = 0, duration
        self._thumb_ms: list[int] = []
        self._thumb_img: list[QImage] = []
        self._drag = None   # None | "in" | "out" | "seek"
        self._hover = None  # None | "in" | "out"
        self._rate = 1.0            # active scrub sensitivity
        self._anchor_x = 0.0        # x where the current rate segment began
        self._anchor_ms = 0.0       # target value at that x
        self._last_x = 0.0          # previous mouse x during a drag
        self.update()

    def add_thumb(self, ms: int, img: QImage):
        i = bisect.bisect(self._thumb_ms, ms)
        self._thumb_ms.insert(i, ms)
        self._thumb_img.insert(i, img)
        self.update()

    # --- coordinates ---
    @property
    def zoomed(self) -> bool:
        return self.duration > 0 and (self.view1 - self.view0) < self.duration

    def _span(self) -> float:
        return max(self.view1 - self.view0, 1)

    def _x_to_ms(self, x: float) -> int:
        if self.duration <= 0 or self.width() <= 0:
            return 0
        frac = min(max(x / self.width(), 0.0), 1.0)
        return int(self.view0 + frac * self._span())

    def _ms_to_x(self, ms: float) -> float:
        return (ms - self.view0) / self._span() * self.width()

    def _set_view(self, v0: float, span: float):
        span = min(max(span, min(1500, self.duration)), self.duration)
        v0 = min(max(v0, 0), self.duration - span)
        self.view0, self.view1 = int(v0), int(v0 + span)
        self.update()

    def reset_zoom(self):
        self._set_view(0, self.duration)

    def follow(self, ms: int):
        """Keep the playhead visible while zoomed."""
        if self.zoomed and not (self.view0 <= ms <= self.view1):
            self._set_view(ms - self._span() * 0.1, self._span())

    # --- interaction ---
    def _hit(self, x: float) -> str:
        if self.duration <= 0:
            return "seek"
        xi, xo = self._ms_to_x(self.mark_in), self._ms_to_x(self.mark_out)
        di, do = abs(x - xi), abs(x - xo)
        if di <= core.HANDLE_GRAB_PX and do <= core.HANDLE_GRAB_PX:
            # handles overlap: left half grabs in, right half grabs out
            return "in" if x <= (xi + xo) / 2 else "out"
        if di <= core.HANDLE_GRAB_PX:
            return "in"
        if do <= core.HANDLE_GRAB_PX:
            return "out"
        return "seek"

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._drag = self._hit(e.position().x())
        self.drag_state.emit(True)
        self._apply_drag(e.position().x())
        # anchor for fine scrubbing at the (possibly jumped-to) value
        self._rate = 1.0
        self._anchor_x = e.position().x()
        self._last_x = e.position().x()
        self._anchor_ms = float(self._drag_value())

    def mouseDoubleClickEvent(self, e):
        if self._hit(e.position().x()) == "seek":
            self.reset_zoom()

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag:
            self._fine_drag(x, e.position().y())
        else:
            over = self._hit(x)
            hover = over if over in ("in", "out") else None
            if hover != self._hover:
                self._hover = hover
                self.update()
            self.setCursor(Qt.SizeHorCursor if hover else Qt.PointingHandCursor)
            if self.duration > 0:
                self.setToolTip(core.fmt_ms(self._x_to_ms(x)))

    def leaveEvent(self, _):
        if self._hover:
            self._hover = None
            self.update()

    def mouseReleaseEvent(self, _):
        if self._drag:
            self.drag_state.emit(False)
        self._drag = None
        self._rate = 1.0
        self.update()

    def wheelEvent(self, e):
        if self.duration <= 0:
            return
        d = e.angleDelta()
        steps = (d.y() or d.x()) / 120
        span = self._span()
        if e.modifiers() & Qt.ShiftModifier or (d.x() and not d.y()):
            self._set_view(self.view0 - steps * span * 0.15, span)
        else:
            anchor = self._x_to_ms(e.position().x())
            ratio = (anchor - self.view0) / span
            new_span = span * (0.8 ** steps)
            new_span = min(max(new_span, min(1500, self.duration)), self.duration)
            self._set_view(anchor - ratio * new_span, new_span)
        e.accept()

    def _apply_drag(self, x: float):
        ms = self._x_to_ms(x)
        self._set_drag_value(ms)

    def _drag_value(self) -> int:
        return {"in": self.mark_in, "out": self.mark_out,
                "seek": self.position}.get(self._drag, self.position)

    def _set_drag_value(self, ms: float):
        ms = int(ms)
        if self._drag == "in":
            self.mark_in = max(0, min(ms, self.mark_out - 100))
            self.range_changed.emit()
            self.seeked.emit(self.mark_in)
        elif self._drag == "out":
            self.mark_out = min(self.duration, max(ms, self.mark_in + 100))
            self.range_changed.emit()
            self.seeked.emit(self.mark_out)
        else:
            self.seeked.emit(max(0, min(ms, self.duration)))
        self.update()

    def _rate_for(self, y: float) -> tuple[float, str]:
        """Scrub sensitivity from vertical distance below the bar."""
        below = y - (self.height() - self.OVERVIEW)
        for min_px, rate, label in self.SCRUB_TIERS:
            if below >= min_px:
                return rate, label
        return 1.0, ""

    def _fine_drag(self, x: float, y: float):
        """Relative scrubbing, Apple style: T = T_anchor + ΔX·rate·(ms/px).

        On a tier change the anchor rebases at the previous x, so motion in
        the same event still counts at the new rate and returning to full
        speed never snaps the value to the absolute cursor position.
        """
        rate, _ = self._rate_for(y)
        if rate != self._rate:
            self._anchor_x = self._last_x
            self._anchor_ms = float(self._drag_value())
            self._rate = rate
        ms_per_px = self._span() / max(self.width(), 1)
        self._set_drag_value(self._anchor_ms + (x - self._anchor_x)
                             * ms_per_px * rate)
        self._last_x = x
    # --- painting ---
    def _draw_handle(self, p: QPainter, x: float, bar: QRectF, color: str,
                     active: bool):
        c = QColor(color)
        if active:
            c = c.lighter(125)
        p.fillRect(QRectF(x - 1.5, bar.top() - 3, 3, bar.height() + 6), c)
        tab = QRectF(x - 7, bar.center().y() - 14, 14, 28)
        path = QPainterPath()
        path.addRoundedRect(tab, 5, 5)
        p.fillPath(path, c)
        p.setPen(QColor(0, 0, 0, 110))
        cy = bar.center().y()
        for dy in (-5, 0, 5):
            p.drawLine(QPointF(x - 2.5, cy + dy), QPointF(x + 2.5, cy + dy))
        p.setPen(Qt.NoPen)

    def _draw_ruler(self, p: QPainter, w: int):
        span = self._span()
        px_per_ms = w / span
        steps = [100, 200, 500, 1000, 2000, 5000, 10_000, 15_000, 30_000,
                 60_000, 120_000, 300_000, 600_000, 1_800_000, 3_600_000]
        step = next((s for s in steps if s * px_per_ms >= 80), steps[-1])
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        t = (self.view0 // step + 1) * step if self.view0 % step else self.view0
        while t <= self.view1:
            x = self._ms_to_x(t)
            p.setPen(QColor("#4a4a52"))
            p.drawLine(QPointF(x, self.RULER - 5), QPointF(x, self.RULER - 1))
            s, ms = divmod(int(t), 1000)
            m, s = divmod(s, 60)
            h, m = divmod(m, 60)
            label = (f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")
            if step < 1000:
                label += f".{ms // 100}"
            p.setPen(QColor("#7a7a82"))
            p.drawText(QPointF(x + 3, self.RULER - 6), label)
            t += step
        p.setPen(Qt.NoPen)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#000000"))
        bar = QRectF(0, self.RULER, w, h - self.RULER - self.OVERVIEW - 2)
        track = QPainterPath()
        track.addRoundedRect(bar, 6, 6)
        p.fillPath(track, QColor("#101014"))

        if self.duration <= 0:
            p.end()
            return

        self._draw_ruler(p, w)
        x_in, x_out = self._ms_to_x(self.mark_in), self._ms_to_x(self.mark_out)
        p.save()
        p.setClipPath(track)
        if self._thumb_ms:
            # filmstrip: fixed-width tiles, each shows the thumbnail nearest
            # to the time at its centre (so zooming never repeats one image)
            first = self._thumb_img[0]
            iw = max(first.width() * bar.height() / max(first.height(), 1), 8)
            x = 0.0
            while x < w:
                t = self._x_to_ms(x + iw / 2)
                i = bisect.bisect(self._thumb_ms, t)
                if i == len(self._thumb_ms) or (
                        i > 0 and t - self._thumb_ms[i - 1]
                        < self._thumb_ms[i] - t):
                    i -= 1
                p.drawImage(QRectF(x, bar.top(), iw, bar.height()),
                            self._thumb_img[max(i, 0)])
                x += iw
        else:
            grad = QLinearGradient(0, bar.top(), 0, bar.bottom())
            grad.setColorAt(0, QColor("#3a9440"))
            grad.setColorAt(1, QColor("#256b2a"))
            p.fillRect(QRectF(x_in, bar.top(), max(x_out - x_in, 0),
                              bar.height()), grad)
        # dim what gets cut away
        shade = QColor(0, 0, 0, 215)
        p.fillRect(QRectF(0, bar.top(), max(x_in, 0), bar.height()), shade)
        p.fillRect(QRectF(x_out, bar.top(), max(w - x_out, 0), bar.height()),
                   shade)
        p.restore()
        # kept-range frame
        pen = QPen(QColor("#66bb6a"), 2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(x_in, bar.top() + 1, max(x_out - x_in, 0),
                          bar.height() - 2))
        p.setPen(Qt.NoPen)

        # playhead
        x_pos = self._ms_to_x(self.position)
        if -6 <= x_pos <= w + 6:
            p.fillRect(QRectF(x_pos - 1, self.RULER - 4, 2,
                              bar.height() + 6), QColor("#f0f0f2"))
            p.setBrush(QColor("#f0f0f2"))
            y = self.RULER - 4
            p.drawPolygon(QPolygonF([QPointF(x_pos - 5, y - 6),
                                     QPointF(x_pos + 5, y - 6),
                                     QPointF(x_pos, y + 1)]))

        self._draw_handle(p, x_in, bar, "#66bb6a",
                          self._hover == "in" or self._drag == "in")
        self._draw_handle(p, x_out, bar, "#ef5350",
                          self._hover == "out" or self._drag == "out")

        # fine-scrub tier badge above the playhead while dragging slowed
        if self._drag and self._rate < 1.0:
            _, label = next(((r, l) for _, r, l in self.SCRUB_TIERS
                             if r == self._rate), (1.0, ""))
            if label:
                f = QFont(self.font())
                f.setPointSizeF(8.5)
                f.setBold(True)
                p.setFont(f)
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(label) + 16
                bx = min(max(self._ms_to_x(self._drag_value()) - tw / 2, 4),
                         w - tw - 4)
                badge = QRectF(bx, bar.top() + 4, tw, fm.height() + 6)
                path = QPainterPath()
                path.addRoundedRect(badge, 6, 6)
                p.fillPath(path, QColor(0, 0, 0, 230))
                p.setBrush(Qt.NoBrush)  # playhead brush would fill the pill
                p.setPen(QPen(QColor("#5865f2"), 1))
                p.drawPath(path)
                p.setPen(QColor("#e8e8ea"))
                p.drawText(badge, Qt.AlignCenter, label)
                p.setPen(Qt.NoPen)

        # overview strip: whole file, kept range, and the zoom window
        oy = h - self.OVERVIEW + 2
        full = QRectF(0, oy, w, 4)
        p.fillRect(full, QColor("#2a2a2f"))
        k = w / self.duration
        p.fillRect(QRectF(self.mark_in * k, oy, (self.mark_out - self.mark_in) * k,
                          4), QColor("#2e6b32"))
        if self.zoomed:
            p.fillRect(QRectF(self.view0 * k, oy, max((self.view1 - self.view0) * k,
                                                      2), 4),
                       QColor(240, 240, 242, 150))
        p.fillRect(QRectF(self.position * k - 1, oy - 1, 2, 6), QColor("#f0f0f2"))
        p.end()


# ---------------------------------------------------------------- snapshot

class SnapshotView(QWidget):
    """Shows a frame fit-to-window; drag a rectangle to select a crop."""

    def __init__(self, image: QImage):
        super().__init__()
        self.image = image
        self.sel: QRect | None = None
        self._anchor: QPoint | None = None
        self.setMinimumSize(560, 340)
        self.setCursor(Qt.CrossCursor)

    def _fit(self):
        iw, ih = self.image.width(), self.image.height()
        s = min(self.width() / iw, self.height() / ih)
        dw, dh = iw * s, ih * s
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2,
                      dw, dh), s

    def _to_img(self, pos) -> QPoint:
        r, s = self._fit()
        x = (pos.x() - r.left()) / s
        y = (pos.y() - r.top()) / s
        return QPoint(int(min(max(x, 0), self.image.width())),
                      int(min(max(y, 0), self.image.height())))

    def mousePressEvent(self, e):
        self._anchor = self._to_img(e.position())
        self.sel = None
        self.update()

    def mouseMoveEvent(self, e):
        if self._anchor is not None:
            self.sel = QRect(self._anchor, self._to_img(e.position())).normalized()
            self.update()

    def mouseReleaseEvent(self, _):
        self._anchor = None
        if self.sel and (self.sel.width() < 4 or self.sel.height() < 4):
            self.sel = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0d0d0f"))
        r, s = self._fit()
        p.drawImage(r, self.image)
        if self.sel:
            sv = QRectF(r.left() + self.sel.x() * s, r.top() + self.sel.y() * s,
                        self.sel.width() * s, self.sel.height() * s)
            shade = QPainterPath()
            shade.addRect(QRectF(self.rect()))
            shade.addRect(sv)
            p.fillPath(shade, QColor(0, 0, 0, 150))
            p.setPen(QColor("#66bb6a"))
            p.drawRect(sv)
            p.drawText(sv.adjusted(6, 4, 0, 0), Qt.AlignTop | Qt.AlignLeft,
                       f"{self.sel.width()}×{self.sel.height()}")
        p.end()


class SnapshotDialog(QDialog):
    def __init__(self, image: QImage, default_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Snapshot · drag to crop")
        self.resize(900, 580)
        self.default_path = default_path
        self.view = SnapshotView(image)

        self.info = QLabel(f"{image.width()}×{image.height()} · "
                           "drag to crop, or copy the full frame")
        btn_copy = QPushButton("Copy to clipboard")
        btn_copy.setObjectName("accent")
        btn_copy.clicked.connect(self.copy)
        btn_save = QPushButton("Save PNG…")
        btn_save.clicked.connect(self.save)
        btn_reset = QPushButton("Reset crop")
        btn_reset.clicked.connect(self.reset)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self.info)
        row.addStretch()
        for b in (btn_reset, btn_save, btn_copy, btn_close):
            row.addWidget(b)

        root = QVBoxLayout(self)
        root.addWidget(self.view, stretch=1)
        root.addLayout(row)

    def cropped(self) -> QImage:
        v = self.view
        return v.image.copy(v.sel) if v.sel else v.image

    def copy(self):
        img = self.cropped()
        QApplication.clipboard().setImage(img)
        self.info.setText(f"Copied {img.width()}×{img.height()} to clipboard")

    def save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save snapshot", self.default_path, "PNG (*.png)")
        if path:
            img = self.cropped()
            img.save(path, "PNG")
            self.info.setText(f"Saved {img.width()}×{img.height()} → "
                              f"{Path(path).name}")

    def reset(self):
        self.view.sel = None
        self.view.update()
        img = self.view.image
        self.info.setText(f"{img.width()}×{img.height()} · "
                          "drag to crop, or copy the full frame")


