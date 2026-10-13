"""FX editor window for mp4trim: speed ramps + animated crop/pan.

A separate window (the main UI is cluttered). Self-contained: imports only
PySide6 and mp4trim_fx. No QMediaPlayer in here; the app pushes frames.

Wiring contract (mp4trim_app.py):

    win = FxWindow(fx, duration_ms, (src_w, src_h), parent=main)
    win.changed.connect(...)          # any edit (mutates the SAME FxState)
    win.seek_requested.connect(...)   # user scrubbed inside the editor
    win.set_position(ms)              # app pushes the playhead
    win.set_range(t_in, t_out)        # kept range markers
    win.set_frame(qimage)             # current frame for the crop canvas

Standalone demo:  py -3.12 mp4trim_fxwin.py
"""

from __future__ import annotations

import math
import sys

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPolygonF)
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel,
                               QMenu, QPushButton, QVBoxLayout, QWidget)

from mp4trim_fx import INTERPS, SPEED_MAX, SPEED_MIN, FxState

# Matches mp4trim_widgets.STYLE (true-black OLED, liquid glass) without
# importing it (that module pulls in mp4trim_core).
FX_STYLE = """
QWidget#fxwin { background: #000000; }
QWidget { color: #e8e8ea; font-size: 10pt; }
QPushButton {
    background: #101014; border: 1px solid #26262c; border-radius: 8px;
    padding: 6px 14px; font-weight: 600;
}
QPushButton:hover { background: #1a1a20; border-color: #3a3a44; }
QPushButton:pressed { background: #08080a; border-color: #222228; }
QPushButton:disabled { color: #55555c; background: #0a0a0d; border-color: #18181d; }
QPushButton#accent { background: #2e7d32; border-color: #3a9440; color: #f2fff2; }
QPushButton#accent:hover { background: #37953c; border-color: #46a843; }
QPushButton#accent:pressed { background: #256b2a; border-color: #2e7d32; }
QCheckBox { spacing: 8px; font-weight: 600; }
QCheckBox::indicator {
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid #2a2a32; background: #101014;
}
QCheckBox::indicator:hover { border-color: #3a3a44; }
QCheckBox::indicator:checked { background: #2e7d32; border: 1px solid #3a9440; }
QLabel#est { color: #a0a0a8; font-family: 'Cascadia Mono', 'Consolas', monospace; }
QLabel#muted { color: #8a8a92; }
QMenu {
    background: #0a0a0d; border: 1px solid #2a2a32; border-radius: 10px;
    padding: 6px 4px;
}
QMenu::item { padding: 6px 28px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #2e7d32; color: #f2fff2; }
QMenu::item:disabled { color: #55555c; }
QMenu::separator { height: 1px; background: #2a2a32; margin: 5px 8px; }
QToolTip {
    background: #0d0d12; color: #e8e8ea; border: 1px solid #2a2a32;
    border-radius: 6px; padding: 5px 8px; font-size: 9pt;
}
"""

SNAP_PCTS = (25, 50, 75, 100, 150, 200, 300, 400)


def _fmt_s(ms: int) -> str:
    return f"{ms / 1000:.1f}s"


# -------------------------------------------------------------- crop canvas

class CropCanvas(QWidget):
    """Letterboxed frame with an aspect-locked, draggable crop rectangle.

    8 resize handles + move. The aspect ratio is LOCKED to the source video
    aspect; rect dims are shown while dragging. Coordinates are SOURCE pixels.
    """

    rect_changed = Signal(tuple)     # live while dragging
    rect_committed = Signal(tuple)   # on mouse release (one commit per drag)

    HANDLE = 9      # hit radius around a handle, view px
    MIN_W = 32      # minimal crop width, source px

    _CURSORS = {"nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
                "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
                "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
                "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
                "move": Qt.SizeAllCursor}

    def __init__(self, video_size: tuple[int, int], parent=None):
        super().__init__(parent)
        self.src_w = max(int(video_size[0]), 1)
        self.src_h = max(int(video_size[1]), 1)
        self.aspect = self.src_w / self.src_h          # locked w/h
        self._frame: QImage | None = None
        self._rect = [0.0, 0.0, float(self.src_w), float(self.src_h)]
        self._drag: str | None = None     # None|move|nw|n|ne|e|se|s|sw|w
        self._press_src = (0.0, 0.0)
        self._press_rect = tuple(self._rect)
        self.setMinimumHeight(240)
        self.setMouseTracking(True)

    # --- public ---
    def set_frame(self, img: QImage):
        self._frame = None if img is None or img.isNull() else img
        self.update()

    def set_rect(self, rect):
        if rect is None:
            rect = (0, 0, self.src_w, self.src_h)
        self._rect = [float(v) for v in rect]
        self._clamp_move()
        self.update()

    def current_rect(self) -> tuple[float, float, float, float]:
        return tuple(round(v, 2) for v in self._rect)

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    # --- geometry ---
    def _fit(self) -> tuple[QRectF, float]:
        """Letterbox display rect and the view/source scale."""
        s = min(self.width() / self.src_w, self.height() / self.src_h)
        dw, dh = self.src_w * s, self.src_h * s
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2,
                      dw, dh), s

    def _to_src(self, pos: QPointF) -> tuple[float, float]:
        r, s = self._fit()
        return ((pos.x() - r.left()) / s, (pos.y() - r.top()) / s)

    def _view_rect(self) -> QRectF:
        r, s = self._fit()
        x, y, w, h = self._rect
        return QRectF(r.left() + x * s, r.top() + y * s, w * s, h * s)

    def _handles(self, vr: QRectF) -> dict[str, QPointF]:
        return {"nw": vr.topLeft(), "ne": vr.topRight(),
                "sw": vr.bottomLeft(), "se": vr.bottomRight(),
                "n": QPointF(vr.center().x(), vr.top()),
                "s": QPointF(vr.center().x(), vr.bottom()),
                "w": QPointF(vr.left(), vr.center().y()),
                "e": QPointF(vr.right(), vr.center().y())}

    def _hit(self, pos: QPointF) -> str | None:
        vr = self._view_rect()
        for name, pt in self._handles(vr).items():
            if (abs(pos.x() - pt.x()) <= self.HANDLE
                    and abs(pos.y() - pt.y()) <= self.HANDLE):
                return name
        if vr.contains(pos):
            return "move"
        return None

    # --- drag logic (plain methods so tests can drive them directly) ---
    def _start_drag(self, pos: QPointF) -> str | None:
        self._drag = self._hit(pos)
        if self._drag:
            self._press_src = self._to_src(pos)
            self._press_rect = tuple(self._rect)
        return self._drag

    def _drag_to(self, pos: QPointF):
        if not self._drag:
            return
        mx, my = self._to_src(pos)
        if self._drag == "move":
            px, py = self._press_src
            rx, ry, rw, rh = self._press_rect
            self._rect = [rx + (mx - px), ry + (my - py), rw, rh]
            self._clamp_move()
        else:
            self._resize(self._drag, mx, my)
        self.update()
        self.rect_changed.emit(self.current_rect())

    def _end_drag(self):
        if self._drag:
            self._drag = None
            self.update()
            self.rect_committed.emit(self.current_rect())

    def _clamp_move(self):
        x, y, w, h = self._rect
        w = min(max(w, self.MIN_W), self.src_w)
        h = w / self.aspect
        if h > self.src_h:
            h = float(self.src_h)
            w = h * self.aspect
        x = min(max(x, 0.0), self.src_w - w)
        y = min(max(y, 0.0), self.src_h - h)
        self._rect = [x, y, w, h]

    def _resize(self, mode: str, mx: float, my: float):
        """Aspect-locked resize anchored on the opposite corner/edge."""
        A = self.aspect
        x0, y0, w0, h0 = self._press_rect
        x1, y1 = x0 + w0, y0 + h0
        min_w = self.MIN_W

        def from_wh(wc, hc, wmax):
            w = max(wc, hc * A, min_w)
            w = min(w, max(wmax, min_w))
            return w, w / A

        if mode == "se":
            w, h = from_wh(mx - x0, my - y0, min(self.src_w - x0, (self.src_h - y0) * A))
            self._rect = [x0, y0, w, h]
        elif mode == "nw":
            w, h = from_wh(x1 - mx, y1 - my, min(x1, y1 * A))
            self._rect = [x1 - w, y1 - h, w, h]
        elif mode == "ne":
            w, h = from_wh(mx - x0, y1 - my, min(self.src_w - x0, y1 * A))
            self._rect = [x0, y1 - h, w, h]
        elif mode == "sw":
            w, h = from_wh(x1 - mx, my - y0, min(x1, (self.src_h - y0) * A))
            self._rect = [x1 - w, y0, w, h]
        elif mode in ("e", "w"):
            cy = y0 + h0 / 2
            wc = (mx - x0) if mode == "e" else (x1 - mx)
            wmax = min((self.src_w - x0) if mode == "e" else x1,
                       2 * min(cy, self.src_h - cy) * A)
            w = min(max(wc, min_w), max(wmax, min_w))
            h = w / A
            x = x0 if mode == "e" else x1 - w
            self._rect = [x, cy - h / 2, w, h]
        elif mode in ("n", "s"):
            cx = x0 + w0 / 2
            hc = (my - y0) if mode == "s" else (y1 - my)
            hmax = min((self.src_h - y0) if mode == "s" else y1,
                       2 * min(cx, self.src_w - cx) / A)
            h = min(max(hc, min_w / A), max(hmax, min_w / A))
            w = h * A
            y = y0 if mode == "s" else y1 - h
            self._rect = [cx - w / 2, y, w, h]

    # --- mouse ---
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start_drag(e.position())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._drag_to(e.position())
        else:
            hit = self._hit(e.position())
            self.setCursor(self._CURSORS.get(hit, Qt.ArrowCursor))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._end_drag()

    # --- painting ---
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#000000"))
        r, s = self._fit()
        if self._frame is not None:
            p.drawImage(r, self._frame)
        else:
            p.fillRect(r, QColor("#101014"))
            p.setPen(QColor("#6a6a74"))
            p.drawText(r, Qt.AlignCenter, "waiting for frame…")
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r)

        vr = self._view_rect().intersected(r)
        # darken everything outside the crop
        shade = QPainterPath()
        shade.addRect(r)
        inner = QPainterPath()
        inner.addRect(vr)
        p.fillPath(shade.subtracted(inner), QColor(0, 0, 0, 150))
        # crop frame
        p.setPen(QPen(QColor("#66bb6a"), 2))
        p.drawRect(vr)
        # handles
        p.setPen(QPen(QColor(0, 0, 0, 160), 1))
        p.setBrush(QColor("#f0f0f2"))
        for pt in self._handles(vr).values():
            p.drawRect(QRectF(pt.x() - 3.5, pt.y() - 3.5, 7, 7))
        # dims badge while dragging
        if self._drag:
            x, y, w, h = self._rect
            label = f"{w:.0f}×{h:.0f}  @ {x:.0f},{y:.0f}"
            f = QFont(self.font())
            f.setPointSizeF(8.5)
            f.setBold(True)
            p.setFont(f)
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label) + 16
            bx = min(max(vr.left() + 6, r.left() + 2), r.right() - tw - 2)
            by = min(max(vr.top() + 6, r.top() + 2), r.bottom() - fm.height() - 8)
            badge = QRectF(bx, by, tw, fm.height() + 6)
            path = QPainterPath()
            path.addRoundedRect(badge, 6, 6)
            p.fillPath(path, QColor(0, 0, 0, 230))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#66bb6a"), 1))
            p.drawPath(path)
            p.setPen(QColor("#e8e8ea"))
            p.drawText(badge, Qt.AlignCenter, label)
        p.end()


# --------------------------------------------------------------- speed lane

class SpeedLane(QWidget):
    """Horizontal strip over the whole clip: speed curve + keyframes.

    * curve samples FxTrack.value_at, log-scaled 25%..400%
    * speed keyframe diamonds: drag horizontally to retime, vertically to
      change speed (snaps to 25/50/75/100/150/200/300/400% within 3%);
      double-click empty space adds a 100% keyframe; right-click = menu
      (percent presets, interp, delete)
    * a second thin row shows crop keyframes (drag to retime, right-click
      delete + interp)
    * clicking/scrubbing empty space emits `seeked`
    """

    edited = Signal()
    seeked = Signal(int)

    CROP_ROW = 18
    PAD_Y = 9        # inner padding of the curve area, view px
    DIAMOND = 6

    _INTERP_COLOR = {"linear": "#5865f2", "ease": "#8ab4f8",
                     "hold": "#a0a0a8"}

    def __init__(self, fx: FxState, duration_ms: int, parent=None):
        super().__init__(parent)
        self.fx = fx
        self.duration = max(int(duration_ms), 1)
        self.position = 0
        self.t_in, self.t_out = 0, self.duration
        self._drag = None        # None | ["scrub"] | ["speed", ms] | ["crop", ms]
        self._hover = None       # ("speed"|"crop", ms) | None
        self.setFixedHeight(132)
        self.setMouseTracking(True)

    # --- public ---
    def set_position(self, ms: int):
        self.position = int(ms)
        self.update()

    def set_range(self, t_in: int, t_out: int):
        self.t_in, self.t_out = int(t_in), int(t_out)
        self.update()

    # --- coordinates ---
    def _areas(self) -> tuple[QRectF, QRectF]:
        w, h = self.width(), self.height()
        curve = QRectF(0, 2, w, h - self.CROP_ROW - 10)
        crop = QRectF(0, h - self.CROP_ROW - 4, w, self.CROP_ROW)
        return curve, crop

    def _ms_to_x(self, ms: float) -> float:
        return ms / self.duration * self.width()

    def _x_to_ms(self, x: float) -> int:
        frac = min(max(x / max(self.width(), 1), 0.0), 1.0)
        return int(frac * self.duration)

    def _y_for(self, speed: float) -> float:
        curve, _ = self._areas()
        u = (math.log2(min(max(speed, SPEED_MIN), SPEED_MAX)) + 2) / 4
        top, bot = curve.top() + self.PAD_Y, curve.bottom() - self.PAD_Y
        return bot - u * (bot - top)

    def _speed_for_y(self, y: float) -> float:
        curve, _ = self._areas()
        top, bot = curve.top() + self.PAD_Y, curve.bottom() - self.PAD_Y
        u = min(max((bot - y) / max(bot - top, 1), 0.0), 1.0)
        return 2.0 ** (u * 4 - 2)

    @staticmethod
    def _snap(speed: float) -> float:
        pct = speed * 100.0
        for preset in SNAP_PCTS:
            if abs(pct - preset) <= max(3.0, preset * 0.03):
                return preset / 100.0
        return speed

    # --- hit testing ---
    def _hit(self, pos: QPointF):
        rad = self.DIAMOND + 4
        _, crop_row = self._areas()
        cy = crop_row.center().y()
        if abs(pos.y() - cy) <= rad:
            for k in self.fx.crop.kfs:
                if abs(pos.x() - self._ms_to_x(k.ms)) <= rad:
                    return ("crop", k)
        for k in self.fx.speed.kfs:
            if (abs(pos.x() - self._ms_to_x(k.ms)) <= rad
                    and abs(pos.y() - self._y_for(k.value)) <= rad):
                return ("speed", k)
        return None

    # --- editing (plain methods so tests can drive them directly) ---
    def _add_speed_kf(self, ms: int, value: float = 1.0):
        self.fx.speed.set_kf(int(ms), value)
        self.update()
        self.edited.emit()

    def _set_speed_value(self, ms: int, value: float):
        for k in self.fx.speed.kfs:
            if k.ms == int(ms):
                self.fx.speed.set_kf(ms, value, k.interp)
                self.update()
                self.edited.emit()
                return

    def _set_interp(self, kind: str, ms: int, interp: str):
        tr = self.fx.speed if kind == "speed" else self.fx.crop
        for k in tr.kfs:
            if k.ms == int(ms):
                tr.set_kf(ms, k.value, interp)
                self.update()
                self.edited.emit()
                return

    def _delete_kf(self, kind: str, ms: int):
        tr = self.fx.speed if kind == "speed" else self.fx.crop
        tr.remove_kf(ms)
        self.update()
        self.edited.emit()

    def _retime(self, kind: str, old_ms: int, new_ms: int,
                new_value=None) -> int:
        tr = self.fx.speed if kind == "speed" else self.fx.crop
        kf = next((k for k in tr.kfs if k.ms == int(old_ms)), None)
        if kf is None:
            return old_ms
        new_ms = min(max(int(new_ms), 0), self.duration)
        value = kf.value if new_value is None else new_value
        interp = kf.interp
        tr.remove_kf(old_ms)
        tr.set_kf(new_ms, value, interp)
        self.update()
        self.edited.emit()
        return new_ms

    # --- mouse ---
    def mousePressEvent(self, e):
        pos = e.position()
        if e.button() == Qt.RightButton:
            hit = self._hit(pos)
            if hit:
                self._context_menu(hit, e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        hit = self._hit(pos)
        if hit:
            self._drag = [hit[0], hit[1].ms]
        else:
            self._drag = ["scrub"]
            self.seeked.emit(self._x_to_ms(pos.x()))
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.LeftButton or self._hit(e.position()):
            return
        curve, _ = self._areas()
        if curve.contains(e.position()):
            self._add_speed_kf(self._x_to_ms(e.position().x()))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._drag is None:
            hit = self._hit(pos)
            hover = (hit[0], hit[1].ms) if hit else None
            if hover != self._hover:
                self._hover = hover
                self.update()
            self.setCursor(Qt.PointingHandCursor if hit
                           else Qt.CrossCursor)
            return
        if self._drag[0] == "scrub":
            self.seeked.emit(self._x_to_ms(pos.x()))
        elif self._drag[0] == "speed":
            value = self._snap(self._speed_for_y(pos.y()))
            self._drag[1] = self._retime("speed", self._drag[1],
                                         self._x_to_ms(pos.x()), value)
        elif self._drag[0] == "crop":
            self._drag[1] = self._retime("crop", self._drag[1],
                                         self._x_to_ms(pos.x()))

    def mouseReleaseEvent(self, _):
        self._drag = None
        self.update()

    def leaveEvent(self, _):
        if self._hover:
            self._hover = None
            self.update()

    # --- context menu ---
    def _context_menu(self, hit, global_pos):
        kind, kf = hit
        ms = kf.ms
        m = QMenu(self)
        if kind == "speed":
            cur_pct = round(kf.value * 100)
            for pct in SNAP_PCTS:
                act = m.addAction(f"{pct}%")
                act.setCheckable(True)
                act.setChecked(pct == cur_pct)
                act.triggered.connect(
                    lambda _=False, p=pct: self._set_speed_value(ms, p / 100))
            m.addSeparator()
        for interp in INTERPS:
            act = m.addAction(interp)
            act.setCheckable(True)
            act.setChecked(kf.interp == interp)
            act.triggered.connect(
                lambda _=False, i=interp: self._set_interp(kind, ms, i))
        m.addSeparator()
        m.addAction("Delete keyframe").triggered.connect(
            lambda: self._delete_kf(kind, ms))
        m.exec(global_pos)

    # --- painting ---
    def _diamond(self, p: QPainter, x: float, y: float, fill: str,
                 border: str, active: bool):
        d = self.DIAMOND + (2 if active else 0)
        poly = QPolygonF([QPointF(x, y - d), QPointF(x + d, y),
                          QPointF(x, y + d), QPointF(x - d, y)])
        p.setBrush(QColor(fill).lighter(130) if active else QColor(fill))
        p.setPen(QPen(QColor(border), 1.4))
        p.drawPolygon(poly)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        w = self.width()
        p.fillRect(self.rect(), QColor("#000000"))
        curve, crop_row = self._areas()

        track = QPainterPath()
        track.addRoundedRect(curve, 6, 6)
        p.fillPath(track, QColor("#101014"))
        row = QPainterPath()
        row.addRoundedRect(crop_row, 5, 5)
        p.fillPath(row, QColor("#0a0a0d"))

        p.save()
        p.setClipPath(track)
        # kept-range shading: dim what gets cut away
        x_in, x_out = self._ms_to_x(self.t_in), self._ms_to_x(self.t_out)
        grad = QLinearGradient(0, curve.top(), 0, curve.bottom())
        grad.setColorAt(0, QColor(46, 125, 50, 36))
        grad.setColorAt(1, QColor(46, 125, 50, 10))
        p.fillRect(QRectF(x_in, curve.top(), max(x_out - x_in, 0),
                          curve.height()), grad)
        shade = QColor(0, 0, 0, 170)
        p.fillRect(QRectF(0, curve.top(), max(x_in, 0), curve.height()), shade)
        p.fillRect(QRectF(x_out, curve.top(), max(w - x_out, 0),
                          curve.height()), shade)

        # reference gridlines at 25 / 100 / 400 %
        f = QFont(self.font())
        f.setPointSizeF(7.0)
        p.setFont(f)
        for pct in (400, 100, 25):
            y = self._y_for(pct / 100)
            p.setPen(QPen(QColor(255, 255, 255, 18 if pct != 100 else 34),
                          1, Qt.DashLine))
            p.drawLine(QPointF(0, y), QPointF(w, y))
            p.setPen(QColor("#6a6a74"))
            p.drawText(QPointF(4, y - 2), f"{pct}%")

        # speed curve
        p.setPen(QPen(QColor("#5865f2"), 2))
        pts = []
        step = max(w / 320, 1.0)
        x = 0.0
        while x <= w:
            pts.append(QPointF(x, self._y_for(
                self.fx.speed.value_at(self._x_to_ms(x)))))
            x += step
        p.drawPolyline(QPolygonF(pts))
        p.setPen(Qt.NoPen)
        p.restore()

        # kept-range edge ticks on the crop row too
        p.fillRect(QRectF(x_in - 0.5, crop_row.top(), 1, crop_row.height()),
                   QColor("#66bb6a"))
        p.fillRect(QRectF(x_out - 0.5, crop_row.top(), 1, crop_row.height()),
                   QColor("#ef5350"))

        # playhead
        x_pos = self._ms_to_x(self.position)
        p.fillRect(QRectF(x_pos - 1, 0, 2, self.height()),
                   QColor("#f0f0f2"))
        p.setBrush(QColor("#f0f0f2"))
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([QPointF(x_pos - 5, 0), QPointF(x_pos + 5, 0),
                                 QPointF(x_pos, 7)]))

        # crop keyframe diamonds (thin row)
        cy = crop_row.center().y()
        for k in self.fx.crop.kfs:
            active = (self._hover == ("crop", k.ms)
                      or (self._drag and self._drag[0] == "crop"
                          and self._drag[1] == k.ms))
            self._diamond(p, self._ms_to_x(k.ms), cy, "#66bb6a",
                          self._INTERP_COLOR[k.interp], active)

        # speed keyframe diamonds
        for k in self.fx.speed.kfs:
            active = (self._hover == ("speed", k.ms)
                      or (self._drag and self._drag[0] == "speed"
                          and self._drag[1] == k.ms))
            x, y = self._ms_to_x(k.ms), self._y_for(k.value)
            self._diamond(p, x, y, "#f0f0f2",
                          self._INTERP_COLOR[k.interp], active)
            if active:
                label = f"{k.value * 100:.0f}%"
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(label) + 12
                bx = min(max(x - tw / 2, 2), w - tw - 2)
                by = max(y - self.DIAMOND - fm.height() - 8, 1)
                badge = QRectF(bx, by, tw, fm.height() + 5)
                path = QPainterPath()
                path.addRoundedRect(badge, 5, 5)
                p.fillPath(path, QColor(0, 0, 0, 230))
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor("#5865f2"), 1))
                p.drawPath(path)
                p.setPen(QColor("#e8e8ea"))
                p.drawText(badge, Qt.AlignCenter, label)
                p.setPen(Qt.NoPen)
        p.end()


# ---------------------------------------------------------------- FxWindow

class FxWindow(QWidget):
    """The FX editor window. Mutates the FxState passed in, emits `changed`."""

    changed = Signal()            # any edit
    seek_requested = Signal(int)  # user scrubbed inside the editor
    play_toggled = Signal()       # user hit play/pause in the editor

    def __init__(self, fx: FxState, duration_ms: int,
                 video_size: tuple[int, int], parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self.setObjectName("fxwin")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(FX_STYLE)
        self.setWindowTitle("FX · speed ramps & crop")
        self.resize(900, 640)

        self.fx = fx
        self.duration = max(int(duration_ms), 1)
        self._pos = 0
        self.t_in, self.t_out = 0, self.duration

        # --- crop canvas + its button row ---
        self.canvas = CropCanvas(video_size)
        self.canvas.rect_changed.connect(self._on_rect_changed)
        self.canvas.rect_committed.connect(self._on_rect_committed)

        self.btn_add_kf = QPushButton("Add crop keyframe at playhead")
        self.btn_add_kf.setObjectName("accent")
        self.btn_add_kf.clicked.connect(self.add_crop_keyframe)
        self.chk_follow = QCheckBox("Follow mode")
        self.chk_follow.setToolTip(
            "Every crop drag auto-sets/updates a crop keyframe\n"
            "at the current playhead position.")
        self.btn_clear_crop = QPushButton("Clear crop")
        self.btn_clear_crop.clicked.connect(self.clear_crop)
        self.lbl_rect = QLabel("")
        self.lbl_rect.setObjectName("muted")

        self.btn_play = QPushButton("▶")
        self.btn_play.setObjectName("play")
        self.btn_play.setFixedWidth(44)
        self.btn_play.setToolTip("Play / pause (Space)")
        self.btn_play.clicked.connect(self.play_toggled)
        self.lbl_pos = QLabel("0:00.000")
        self.lbl_pos.setObjectName("est")

        crop_row = QHBoxLayout()
        crop_row.setSpacing(10)
        crop_row.addWidget(self.btn_play)
        crop_row.addWidget(self.lbl_pos)
        crop_row.addSpacing(8)
        crop_row.addWidget(self.btn_add_kf)
        crop_row.addWidget(self.chk_follow)
        crop_row.addWidget(self.btn_clear_crop)
        crop_row.addStretch()
        crop_row.addWidget(self.lbl_rect)

        # --- speed lane ---
        self.lane = SpeedLane(fx, self.duration)
        self.lane.edited.connect(self._emit_changed)
        self.lane.seeked.connect(self.seek_requested)
        lane_hint = QLabel("double-click: add speed keyframe · drag diamond: "
                           "retime / change speed · right-click: presets, "
                           "interp, delete")
        lane_hint.setObjectName("muted")

        # --- bottom row ---
        self.chk_mute = QCheckBox("Mute audio in slowed spans")
        self.chk_mute.setChecked(fx.mute_slowed)
        self.chk_mute.toggled.connect(self._on_mute)
        self.chk_hq = QCheckBox("HQ slow-mo (frame interpolation, much "
                                "slower export)")
        self.chk_hq.setChecked(fx.hq_interp)
        self.chk_hq.toggled.connect(self._on_hq)
        self.lbl_out = QLabel("")
        self.lbl_out.setObjectName("est")
        self.btn_reset = QPushButton("Reset all FX")
        self.btn_reset.clicked.connect(self.reset_all)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addWidget(self.chk_mute)
        bottom.addWidget(self.chk_hq)
        bottom.addStretch()
        bottom.addWidget(self.lbl_out)
        bottom.addWidget(self.btn_reset)
        bottom.addWidget(self.btn_close)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        root.addWidget(self.canvas, stretch=1)
        root.addLayout(crop_row)
        root.addWidget(self.lane)
        root.addWidget(lane_hint)
        root.addLayout(bottom)

        self._sync_canvas_rect()
        self._update_output_label()

    # --- app-facing API ---
    def set_position(self, ms: int):
        """App pushes the playhead (source ms)."""
        self._pos = min(max(int(ms), 0), self.duration)
        self.lane.set_position(self._pos)
        m, rest = divmod(self._pos, 60_000)
        self.lbl_pos.setText(f"{m}:{rest // 1000:02d}.{rest % 1000:03d}")
        self._sync_canvas_rect()

    def set_playing(self, playing: bool):
        """App reflects the player state on the editor's play button."""
        self.btn_play.setText("⏸" if playing else "▶")

    def set_range(self, t_in: int, t_out: int):
        """App pushes the kept range markers."""
        self.t_in, self.t_out = int(t_in), int(t_out)
        self.lane.set_range(self.t_in, self.t_out)
        self._update_output_label()

    def set_frame(self, img: QImage):
        """App pushes the current video frame (shown letterboxed)."""
        self.canvas.set_frame(img)

    # --- crop actions ---
    def add_crop_keyframe(self):
        self.fx.crop.set_kf(self._pos, self.canvas.current_rect())
        self._emit_changed()

    def clear_crop(self):
        if not self.fx.crop:
            return
        self.fx.crop.clear()
        self.canvas.set_rect(None)
        self._emit_changed()

    def reset_all(self):
        self.fx.speed.clear()
        self.fx.crop.clear()
        self.fx.mute_slowed = False
        self.fx.hq_interp = False
        for cb in (self.chk_mute, self.chk_hq):
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.canvas.set_rect(None)
        self._emit_changed()

    # --- internals ---
    def _sync_canvas_rect(self):
        if self.fx.crop and not self.canvas.dragging:
            self.canvas.set_rect(self.fx.crop.value_at(self._pos))

    def _on_rect_changed(self, rect):
        x, y, w, h = rect
        self.lbl_rect.setText(f"crop {w:.0f}×{h:.0f} @ {x:.0f},{y:.0f}")

    def _on_rect_committed(self, rect):
        self._on_rect_changed(rect)
        if self.chk_follow.isChecked():
            self.fx.crop.set_kf(self._pos, rect)
            self._emit_changed()

    def _on_mute(self, on: bool):
        self.fx.mute_slowed = bool(on)
        self._emit_changed()

    def _on_hq(self, on: bool):
        self.fx.hq_interp = bool(on)
        self._emit_changed()

    def _update_output_label(self):
        src = max(self.t_out - self.t_in, 0)
        out = self.fx.output_duration_ms(self.t_in, self.t_out)
        self.lbl_out.setText(f"Output: {_fmt_s(out)} (from {_fmt_s(src)})")

    def _emit_changed(self):
        self._update_output_label()
        self.lane.update()
        self.canvas.update()
        self.changed.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
        elif e.key() == Qt.Key_Space:
            self.play_toggled.emit()
        else:
            super().keyPressEvent(e)


# -------------------------------------------------------------------- demo

def _test_pattern(w: int = 1280, h: int = 720) -> QImage:
    """Gradient + grid + markers so crop/letterbox are easy to eyeball."""
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0.0, QColor("#15213a"))
    grad.setColorAt(0.5, QColor("#232042"))
    grad.setColorAt(1.0, QColor("#0d2b1e"))
    p.fillRect(0, 0, w, h, grad)
    p.setPen(QPen(QColor(255, 255, 255, 34), 1))
    for x in range(0, w, 80):
        p.drawLine(x, 0, x, h)
    for y in range(0, h, 80):
        p.drawLine(0, y, w, y)
    p.setPen(QPen(QColor("#66bb6a"), 3))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(w / 2, h / 2), h / 3, h / 3)
    p.setPen(QColor("#e8e8ea"))
    f = QFont()
    f.setPointSize(22)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter,
               f"mp4trim FX demo · {w}×{h}")
    p.end()
    return img


def _demo():
    app = QApplication(sys.argv)
    fx = FxState()
    win = FxWindow(fx, 60_000, (1280, 720))
    win.changed.connect(lambda: print(
        "changed:",
        "speed", [(k.ms, round(k.value, 3), k.interp) for k in fx.speed.kfs],
        "| crop", [(k.ms, tuple(round(v) for v in k.value))
                   for k in fx.crop.kfs],
        "| mute", fx.mute_slowed, "hq", fx.hq_interp))
    win.seek_requested.connect(
        lambda ms: (print(f"seek_requested({ms})"), win.set_position(ms)))
    win.set_frame(_test_pattern())
    win.set_range(4_000, 52_000)
    win.set_position(10_000)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    _demo()
