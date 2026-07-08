"""mp4trim — minimal MP4 trimmer with playback and a drag-handle timeline.

Playback uses QtMultimedia (hardware decode + audio). If the system decoder
can't handle a file (some hybrid DV profiles), the app drops to an
ffmpeg-decoded frame preview automatically, so colors stay correct.
The trim itself is a pure stream copy: Dolby Vision / HDR10 metadata and
all tracks survive intact.

Usage:  mp4trim [file.mp4]
Drag the green/red handles inward from each end to choose the kept range.
Keys:   Space = play/pause, Left/Right = step 1s (Shift = 10s),
        Enter = trim, Ctrl+O = open
"""

import json
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (
    QProcess, QRectF, QSettings, Qt, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QImage, QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPixmap, QPolygonF,
)
from PySide6.QtCore import QMimeData
from PySide6.QtCore import QPoint, QPointF, QRect
from PySide6.QtGui import QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

APP_VERSION = "1.4.0"
DISCORD_TIERS = [("Free — 10 MB", 10.0), ("Nitro Basic — 50 MB", 50.0),
                 ("Nitro — 500 MB", 500.0)]


def res_path(name: str) -> Path:
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / name



CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
HANDLE_GRAB_PX = 12

STYLE = """
QMainWindow, QDialog { background: #141416; }
QWidget { color: #e8e8ea; font-size: 10pt; }
QMenuBar { background: #1b1b1e; padding: 2px; border: none; }
QMenuBar::item { padding: 5px 12px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: #2c2c31; }
QMenu { background: #1e1e22; border: 1px solid #35353b; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 28px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #2e7d32; }
QMenu::separator { height: 1px; background: #35353b; margin: 6px 8px; }
QPushButton {
    background: #26262b; border: 1px solid #38383f; border-radius: 8px;
    padding: 7px 16px; font-weight: 600;
}
QPushButton:hover { background: #303036; border-color: #4a4a52; }
QPushButton:pressed { background: #1e1e22; }
QPushButton:disabled { color: #6a6a70; background: #202024; }
QPushButton#accent {
    background: #2e7d32; border-color: #3a9440; color: #f2fff2;
}
QPushButton#accent:hover { background: #37953c; }
QPushButton#accent:disabled { background: #24422a; color: #7fa583; }
QPushButton#play {
    font-size: 13pt; padding: 4px 0; min-width: 44px; border-radius: 22px;
}
QPushButton#gif {
    background: #4752c4; border-color: #5865f2; color: #f0f2ff;
}
QPushButton#gif:hover { background: #5865f2; }
QPushButton#gif:disabled { background: #2c3060; color: #7a80b0; }
QLabel#mono { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #b8b8c0; }
QLabel#range { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #8fd694; }
QLabel#drop { color: #7a7a82; font-size: 12pt; background: #0d0d0f; }
QStatusBar { background: #1b1b1e; color: #8a8a92; }
"""


def copy_file_to_clipboard(path: str):
    """Put the file itself on the clipboard (paste into Discord uploads it)."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    QApplication.clipboard().setMimeData(mime)


def fmt_ms(ms: int) -> str:
    s, ms = divmod(max(0, ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def probe_duration_ms(path: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return int(float(json.loads(out.stdout)["format"]["duration"]) * 1000)


def grab_frame(path: str, ms: int, native: bool = False) -> QImage | None:
    scale = [] if native else ["-vf", "scale=960:-2"]
    out = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-ss", f"{ms / 1000:.3f}", "-i", path,
         "-frames:v", "1", *scale, "-f", "image2pipe",
         "-vcodec", "bmp", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )
    if not out.stdout:
        return None
    img = QImage.fromData(out.stdout, "BMP")
    return None if img.isNull() else img


class Timeline(QWidget):
    """Timeline bar with draggable in/out handles.

    Drag the left (green) handle right to cut the beginning, the right (red)
    handle left to cut the end. Clicking anywhere else scrubs the preview.
    """

    seeked = Signal(int)
    range_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(64)
        self.setMouseTracking(True)
        self.duration = 0
        self.position = 0
        self.mark_in = 0
        self.mark_out = 0
        self._drag = None   # None | "in" | "out" | "seek"
        self._hover = None  # None | "in" | "out"

    def _x_to_ms(self, x: float) -> int:
        if self.duration <= 0 or self.width() <= 0:
            return 0
        return int(min(max(x / self.width(), 0.0), 1.0) * self.duration)

    def _ms_to_x(self, ms: int) -> float:
        return 0.0 if self.duration <= 0 else ms / self.duration * self.width()

    def _hit(self, x: float) -> str:
        if self.duration <= 0:
            return "seek"
        if abs(x - self._ms_to_x(self.mark_in)) <= HANDLE_GRAB_PX:
            return "in"
        if abs(x - self._ms_to_x(self.mark_out)) <= HANDLE_GRAB_PX:
            return "out"
        return "seek"

    def mousePressEvent(self, e):
        self._drag = self._hit(e.position().x())
        self._apply_drag(e.position().x())

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag:
            self._apply_drag(x)
        else:
            over = self._hit(x)
            hover = over if over in ("in", "out") else None
            if hover != self._hover:
                self._hover = hover
                self.update()
            self.setCursor(Qt.SizeHorCursor if hover else Qt.PointingHandCursor)

    def leaveEvent(self, _):
        if self._hover:
            self._hover = None
            self.update()

    def mouseReleaseEvent(self, _):
        self._drag = None

    def _apply_drag(self, x: float):
        ms = self._x_to_ms(x)
        if self._drag == "in":
            self.mark_in = min(ms, max(self.mark_out - 100, 0))
            self.range_changed.emit()
            self.seeked.emit(self.mark_in)  # preview the new first frame
        elif self._drag == "out":
            self.mark_out = max(ms, min(self.mark_in + 100, self.duration))
            self.range_changed.emit()
            self.seeked.emit(self.mark_out)  # preview the new last frame
        else:
            self.seeked.emit(ms)
        self.update()

    def _draw_handle(self, p: QPainter, x: float, color: str, active: bool):
        h = self.height()
        c = QColor(color)
        if active:
            c = c.lighter(125)
        # full-height stem
        p.fillRect(QRectF(x - 1.5, 6, 3, h - 12), c)
        # rounded grip tab with dots
        tab = QRectF(x - 7, h / 2 - 13, 14, 26)
        path = QPainterPath()
        path.addRoundedRect(tab, 5, 5)
        p.fillPath(path, c)
        p.setPen(QColor(0, 0, 0, 110))
        for dy in (-5, 0, 5):
            p.drawLine(QPointF(x - 2.5, h / 2 + dy), QPointF(x + 2.5, h / 2 + dy))
        p.setPen(Qt.NoPen)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        bar = QRectF(0, h * 0.32, w, h * 0.36)

        p.fillRect(self.rect(), QColor("#141416"))
        track = QPainterPath()
        track.addRoundedRect(bar, 5, 5)
        p.fillPath(track, QColor("#2a2a2f"))

        if self.duration > 0:
            x_in, x_out = self._ms_to_x(self.mark_in), self._ms_to_x(self.mark_out)

            p.save()
            p.setClipPath(track)
            # trimmed-away ends, dimmed with hatch feel
            p.fillRect(QRectF(0, bar.top(), x_in, bar.height()), QColor("#1c1c20"))
            p.fillRect(QRectF(x_out, bar.top(), w - x_out, bar.height()),
                       QColor("#1c1c20"))
            # kept region, soft green gradient
            grad = QLinearGradient(0, bar.top(), 0, bar.bottom())
            grad.setColorAt(0, QColor("#3a9440"))
            grad.setColorAt(1, QColor("#256b2a"))
            p.fillRect(QRectF(x_in, bar.top(), max(x_out - x_in, 0), bar.height()),
                       grad)
            p.restore()

            # playhead: white line + triangle cap
            x_pos = self._ms_to_x(self.position)
            p.fillRect(QRectF(x_pos - 1, 2, 2, h - 4), QColor("#f0f0f2"))
            p.setBrush(QColor("#f0f0f2"))
            p.drawPolygon(QPolygonF([
                QPointF(x_pos - 5, 0), QPointF(x_pos + 5, 0), QPointF(x_pos, 7)]))

            self._draw_handle(p, x_in, "#66bb6a",
                              self._hover == "in" or self._drag == "in")
            self._draw_handle(p, x_out, "#ef5350",
                              self._hover == "out" or self._drag == "out")
        p.end()


class GifWorker(QThread):
    """Encode the trim range to GIF, stepping down quality until it fits
    the Discord upload limit (palettegen/paletteuse two-pass per rung)."""

    progress = Signal(str)
    done = Signal(str, float, bool)  # path, size MB, fits limit
    failed = Signal(str)

    LADDER = [(480, 20), (480, 15), (400, 15), (360, 12),
              (320, 12), (280, 10), (240, 10)]
    # extra headroom at 50/500 MB buys bigger, smoother rungs first
    HQ_LADDER = [(720, 24), (640, 24), (560, 20)]

    def __init__(self, src: str, t_in: int, t_out: int, dst: str,
                 limit_mb: float):
        super().__init__()
        self.src, self.t_in, self.t_out, self.dst = src, t_in, t_out, dst
        self.limit_mb = limit_mb

    def run(self):
        size_mb = 0.0
        ladder = (self.HQ_LADDER + self.LADDER if self.limit_mb > 10
                  else self.LADDER)
        for i, (width, fps) in enumerate(ladder, 1):
            self.progress.emit(
                f"GIF pass {i}/{len(self.LADDER)}: {width}px @ {fps}fps …")
            flt = (f"[0:v] fps={fps},scale={width}:-1:flags=lanczos,"
                   f"split [a][b];[a] palettegen=stats_mode=diff [p];"
                   f"[b][p] paletteuse=dither=bayer:bayer_scale=4:"
                   f"diff_mode=rectangle")
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{self.t_in / 1000:.3f}",
                 "-to", f"{self.t_out / 1000:.3f}", "-i", self.src,
                 "-filter_complex", flt, "-loop", "0", self.dst],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode != 0:
                self.failed.emit(r.stderr.decode(errors="replace")[-1500:])
                return
            size_mb = Path(self.dst).stat().st_size / 1_048_576
            if size_mb <= self.limit_mb:
                self.done.emit(self.dst, size_mb, True)
                return
        self.done.emit(self.dst, size_mb, False)


class SnapshotView(QWidget):
    """Shows a frame fit-to-window; drag a rectangle to select a crop."""

    def __init__(self, image: QImage):
        super().__init__()
        self.image = image
        self.sel: QRect | None = None  # crop rect in image coordinates
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
            p.fillPath(shade, QColor(0, 0, 0, 150))  # dim outside selection
            p.setPen(QColor("#66bb6a"))
            p.drawRect(sv)
            p.drawText(sv.adjusted(6, 4, 0, 0),
                       Qt.AlignTop | Qt.AlignLeft,
                       f"{self.sel.width()}×{self.sel.height()}")
        p.end()


class SnapshotDialog(QDialog):
    def __init__(self, image: QImage, default_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Snapshot — drag to crop")
        self.resize(900, 580)
        self.default_path = default_path
        self.view = SnapshotView(image)

        self.info = QLabel(f"{image.width()}×{image.height()}  —  "
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
        self.info.setText(f"{img.width()}×{img.height()}  —  "
                          "drag to crop, or copy the full frame")


class Trimmer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mp4trim")
        self.resize(1020, 720)
        self.path: str | None = None
        self.duration = 0
        self.position = 0
        self.proc: QProcess | None = None
        self.settings = QSettings("mp4trim", "mp4trim")
        self.gif_worker: GifWorker | None = None
        self.fallback = False   # True -> ffmpeg frame preview, no playback
        self._prime = False     # pause right after load to show first frame

        # --- playback ---
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setFocusPolicy(Qt.NoFocus)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self.on_player_pos)
        self.player.playbackStateChanged.connect(self.on_play_state)
        self.player.errorOccurred.connect(self.on_player_error)

        self.frame_label = QLabel("Open a file  (Ctrl+O)  or drop one here")
        self.frame_label.setObjectName("drop")
        self.frame_label.setAlignment(Qt.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.video)        # 0 = live playback
        self.stack.addWidget(self.frame_label)  # 1 = ffmpeg frame preview
        self.stack.setCurrentIndex(1)
        self.stack.setMinimumHeight(420)

        # --- timeline + controls ---
        self.timeline = Timeline()
        self.timeline.seeked.connect(self.seek)
        self.timeline.range_changed.connect(self.refresh)

        self.btn_play = QPushButton("▶")
        self.btn_play.setObjectName("play")
        self.btn_play.setEnabled(False)
        self.btn_play.clicked.connect(self.play_pause)
        self.btn_play.setFocusPolicy(Qt.NoFocus)

        self.btn_open = QPushButton("📂 Open")
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_snap = QPushButton("📷 Snapshot")
        self.btn_snap.setEnabled(False)
        self.btn_snap.clicked.connect(self.snapshot)
        self.btn_gif = QPushButton("🎞 GIF for Discord")
        self.btn_gif.setObjectName("gif")
        self.btn_gif.setEnabled(False)
        self.btn_gif.clicked.connect(self.export_gif)

        self.lbl_pos = QLabel("--:--:--.---")
        self.lbl_pos.setObjectName("mono")
        self.lbl_range = QLabel("")
        self.lbl_range.setObjectName("range")
        self.btn_trim = QPushButton("✂ Trim MP4")
        self.btn_trim.setObjectName("accent")
        self.btn_trim.clicked.connect(self.trim)

        for b in (self.btn_open, self.btn_snap, self.btn_gif, self.btn_trim):
            b.setFocusPolicy(Qt.NoFocus)

        row = QHBoxLayout()
        row.setContentsMargins(12, 4, 12, 10)
        row.setSpacing(10)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_play)
        row.addWidget(self.btn_snap)
        row.addWidget(self.lbl_pos)
        row.addStretch()
        row.addWidget(self.lbl_range)
        row.addStretch()
        row.addWidget(self.btn_gif)
        row.addWidget(self.btn_trim)

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack, stretch=1)
        root.addWidget(self.timeline)
        root.addLayout(row)
        host = QWidget()
        host.setLayout(root)
        self.setCentralWidget(host)
        self.setAcceptDrops(True)
        self._build_menu()
        self.statusBar().showMessage("Ready — drop a video to start")

        # debounce frame grabs while scrubbing in fallback mode
        self._frame_timer = QTimer(self, singleShot=True, interval=120)
        self._frame_timer.timeout.connect(self.update_frame)

    def _build_menu(self):
        m_file = self.menuBar().addMenu("&File")
        act_open = QAction("&Open…", self, shortcut=QKeySequence.Open,
                           triggered=self.open_dialog)
        act_trim = QAction("&Trim / Export", self, shortcut="Ctrl+E",
                           triggered=self.trim)
        act_snap = QAction("&Snapshot / Crop…", self, shortcut="S",
                           triggered=self.snapshot)
        act_gif = QAction("Export &GIF for Discord", self, shortcut="G",
                          triggered=self.export_gif)
        act_exit = QAction("E&xit", self, shortcut=QKeySequence.Quit,
                           triggered=self.close)
        m_file.addActions([act_open, act_trim, act_snap, act_gif])
        m_file.addSeparator()
        m_file.addAction(act_exit)

        m_opts = self.menuBar().addMenu("&Options")
        self.act_reencode = QAction(
            "Frame-accurate (re-encode — drops DV metadata)", self, checkable=True)
        self.act_ffpreview = QAction(
            "Force ffmpeg preview (no playback)", self, checkable=True,
            toggled=self.on_force_fallback)
        m_opts.addActions([self.act_reencode, self.act_ffpreview])

        m_opts.addSeparator()
        m_tier = m_opts.addMenu("Discord upload limit (GIF)")
        saved = float(self.settings.value("discord_limit", 10.0))
        self.tier_actions = []
        for name, mb in DISCORD_TIERS:
            act = QAction(name, self, checkable=True, checked=(mb == saved))
            act.triggered.connect(
                lambda _=False, v=mb: self.set_discord_limit(v))
            m_tier.addAction(act)
            self.tier_actions.append((act, mb))

        m_help = self.menuBar().addMenu("&Help")
        m_help.addAction(QAction(
            "&About", self,
            triggered=lambda: QMessageBox.about(
                self, "mp4trim",
                f"mp4trim {APP_VERSION}\n\nDrag the green/red handles inward "
                "to pick the kept range, then Trim.\nStream copy by default — "
                "hybrid DV/HDR10 safe.\n\nRequires ffmpeg on PATH.")))

    # ---- file handling ----

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.load(e.mimeData().urls()[0].toLocalFile())

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", "", "Video (*.mp4 *.mkv *.mov *.m4v);;All files (*)")
        if path:
            self.load(path)

    def load(self, path: str):
        try:
            self.duration = probe_duration_ms(path)
        except Exception as e:
            QMessageBox.warning(self, "mp4trim", f"ffprobe failed:\n{e}")
            return
        self.path = path
        self.position = 0
        self.timeline.duration = self.duration
        self.timeline.mark_in = 0
        self.timeline.mark_out = self.duration
        self.setWindowTitle(f"mp4trim — {Path(path).name}")

        self.fallback = self.act_ffpreview.isChecked()
        self.btn_snap.setEnabled(True)
        self.btn_gif.setEnabled(True)
        if self.fallback:
            self.enter_fallback(silent=True)
        else:
            self.stack.setCurrentIndex(0)
            self.btn_play.setEnabled(True)
            self._prime = True
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()  # paused on first frame via on_player_pos
            self.statusBar().showMessage(Path(path).name)
        self.refresh()

    # ---- playback / preview ----

    def play_pause(self):
        if not self.path or self.fallback:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def on_play_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.btn_play.setText("⏸" if playing else "▶")

    def on_player_pos(self, ms: int):
        if self._prime:
            self._prime = False
            self.player.pause()
        self.position = ms
        self.refresh()

    def on_player_error(self, _err, msg: str):
        if not self.fallback:
            self.enter_fallback(silent=False, detail=msg)

    def on_force_fallback(self, on: bool):
        if self.path:
            if on:
                self.enter_fallback(silent=True)
            else:
                self.load(self.path)

    def enter_fallback(self, silent: bool, detail: str = ""):
        self.fallback = True
        self.player.stop()
        self.player.setSource(QUrl())
        self.stack.setCurrentIndex(1)
        self.btn_play.setEnabled(False)
        self.btn_play.setText("▶")
        note = "ffmpeg preview mode — scrub the timeline (no live playback)"
        if not silent:
            note = "System decoder failed; " + note + (f"  [{detail}]" if detail else "")
        self.statusBar().showMessage(note)
        self.update_frame()

    def seek(self, ms: int):
        if not self.path:
            return
        self.position = min(max(ms, 0), self.duration)
        if self.fallback:
            self._frame_timer.start()
        else:
            self.player.setPosition(self.position)
        self.refresh()

    def update_frame(self):
        if not self.path or not self.fallback:
            return
        img = grab_frame(self.path, self.position)
        if img:
            self.frame_label.setPixmap(QPixmap.fromImage(img).scaled(
                self.frame_label.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def snapshot(self):
        if not self.path:
            return
        self.player.pause()
        img = grab_frame(self.path, self.position, native=True)
        if img is None:
            QMessageBox.warning(self, "mp4trim", "Could not grab this frame.")
            return
        default = str(Path(self.path).with_name(
            f"{Path(self.path).stem}_{self.position // 1000}s.png"))
        SnapshotDialog(img, default, self).exec()

    def discord_limit(self) -> float:
        return float(self.settings.value("discord_limit", 10.0))

    def set_discord_limit(self, mb: float):
        self.settings.setValue("discord_limit", mb)
        for act, v in self.tier_actions:
            act.setChecked(v == mb)
        self.statusBar().showMessage(f"GIF target: {mb:.0f} MB")

    def export_gif(self):
        if not self.path or getattr(self, "gif_worker", None):
            return
        t_in, t_out = self.timeline.mark_in, self.timeline.mark_out
        if t_out - t_in < 100:
            QMessageBox.warning(self, "mp4trim", "In/out range is empty.")
            return
        if self.discord_limit() <= 10 and t_out - t_in > 60_000:
            if QMessageBox.question(
                    self, "mp4trim",
                    "Selection is over a minute — GIFs that long get huge "
                    "and may not fit Discord even at lowest quality.\n"
                    "Continue anyway?") != QMessageBox.StandardButton.Yes:
                return

        src = Path(self.path)
        dst = src.with_name(f"{src.stem}_clip_{t_in // 1000}-{t_out // 1000}.gif")
        self.player.pause()
        self.btn_gif.setEnabled(False)
        self.btn_gif.setText("Encoding…")
        self.gif_worker = GifWorker(self.path, t_in, t_out, str(dst),
                                    self.discord_limit())
        self.gif_worker.progress.connect(self.statusBar().showMessage)
        self.gif_worker.done.connect(self.gif_done)
        self.gif_worker.failed.connect(self.gif_failed)
        self.gif_worker.start()

    def _gif_reset(self):
        self.gif_worker = None
        self.btn_gif.setEnabled(True)
        self.btn_gif.setText("🎞 GIF for Discord")

    def gif_done(self, path: str, size_mb: float, fits: bool):
        self._gif_reset()
        limit = self.discord_limit()
        if fits:
            copy_file_to_clipboard(path)
            self.statusBar().showMessage(
                f"GIF saved + copied to clipboard ({size_mb:.1f} MB)")
            QMessageBox.information(
                self, "mp4trim",
                f"GIF saved ({size_mb:.1f} MB — fits your "
                f"{limit:.0f} MB Discord limit) and copied to clipboard — "
                f"paste straight into Discord.\n{path}")
        else:
            self.statusBar().showMessage(f"GIF saved but large: {size_mb:.1f} MB")
            QMessageBox.warning(
                self, "mp4trim",
                f"GIF saved, but even at lowest quality it is "
                f"{size_mb:.1f} MB (over your {limit:.0f} MB Discord limit). "
                f"Trim a shorter range for a smaller file.\n{path}")

    def gif_failed(self, err: str):
        self._gif_reset()
        self.statusBar().showMessage("GIF export failed")
        QMessageBox.critical(self, "mp4trim", f"GIF export failed:\n{err}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fallback:
            self._frame_timer.start()

    def refresh(self):
        self.timeline.position = self.position
        self.timeline.update()
        self.lbl_pos.setText(f"{fmt_ms(self.position)} / {fmt_ms(self.duration)}")
        if self.path:
            self.lbl_range.setText(
                f"{fmt_ms(self.timeline.mark_in)}  →  "
                f"{fmt_ms(self.timeline.mark_out)}   "
                f"(keep {fmt_ms(self.timeline.mark_out - self.timeline.mark_in)})")

    def keyPressEvent(self, e):
        step = 10_000 if e.modifiers() & Qt.ShiftModifier else 1_000
        if e.key() == Qt.Key_Space:
            self.play_pause()
        elif e.key() == Qt.Key_Left:
            self.seek(self.position - step)
        elif e.key() == Qt.Key_Right:
            self.seek(self.position + step)
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.trim()
        else:
            super().keyPressEvent(e)

    # ---- trimming ----

    def trim(self):
        if not self.path or self.proc:
            return
        t_in, t_out = self.timeline.mark_in, self.timeline.mark_out
        if t_out - t_in < 100:
            QMessageBox.warning(self, "mp4trim", "In/out range is empty.")
            return

        src = Path(self.path)
        dst = src.with_stem(src.stem + f"_trim_{t_in // 1000}-{t_out // 1000}")
        if dst.exists():
            if QMessageBox.question(self, "mp4trim", f"Overwrite {dst.name}?") \
                    != QMessageBox.StandardButton.Yes:
                return

        args = ["-y", "-ss", f"{t_in / 1000:.3f}", "-to", f"{t_out / 1000:.3f}",
                "-i", str(src), "-map", "0"]
        if self.act_reencode.isChecked():
            args += ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                     "-c:a", "copy", "-c:s", "copy"]
        else:
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        args.append(str(dst))

        self.player.pause()
        self.btn_trim.setEnabled(False)
        self.btn_trim.setText("Trimming…")
        self.statusBar().showMessage(f"Trimming to {dst.name} …")
        self.proc = QProcess(self)
        self.proc.finished.connect(lambda code, _st: self.trim_done(code, dst))
        self.proc.start("ffmpeg", args)

    def trim_done(self, code: int, dst: Path):
        err = bytes(self.proc.readAllStandardError()).decode(errors="replace")
        self.proc = None
        self.btn_trim.setEnabled(True)
        self.btn_trim.setText("✂ Trim MP4")
        if code == 0:
            copy_file_to_clipboard(str(dst))
            self.statusBar().showMessage(f"Saved + copied to clipboard: {dst}")
            QMessageBox.information(
                self, "mp4trim",
                f"Saved and copied to clipboard — paste straight into "
                f"Discord or Explorer.\n{dst}")
        else:
            self.statusBar().showMessage("Trim failed")
            QMessageBox.critical(self, "mp4trim",
                                 f"ffmpeg exited with {code}:\n{err[-1500:]}")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont("Segoe UI Variable Text", 10)
    if not font.exactMatch():
        font = QFont("Segoe UI", 10)
    app.setFont(font)
    app.setStyleSheet(STYLE)
    icon = res_path("icon.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = Trimmer()
    win.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        win.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
