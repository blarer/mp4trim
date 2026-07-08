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

from PySide6.QtCore import QProcess, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QColor, QFont, QImage, QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPixmap, QPolygonF,
)
from PySide6.QtCore import QPointF
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

APP_VERSION = "1.1.0"
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
QLabel#mono { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #b8b8c0; }
QLabel#range { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #8fd694; }
QLabel#drop { color: #7a7a82; font-size: 12pt; background: #0d0d0f; }
QStatusBar { background: #1b1b1e; color: #8a8a92; }
"""


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


def grab_frame(path: str, ms: int) -> QImage | None:
    out = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-ss", f"{ms / 1000:.3f}", "-i", path,
         "-frames:v", "1", "-vf", "scale=960:-2", "-f", "image2pipe",
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


class Trimmer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mp4trim")
        self.resize(1020, 720)
        self.path: str | None = None
        self.duration = 0
        self.position = 0
        self.proc: QProcess | None = None
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

        self.lbl_pos = QLabel("--:--:--.---")
        self.lbl_pos.setObjectName("mono")
        self.lbl_range = QLabel("")
        self.lbl_range.setObjectName("range")
        self.btn_trim = QPushButton("Trim")
        self.btn_trim.setObjectName("accent")
        self.btn_trim.clicked.connect(self.trim)
        self.btn_trim.setFocusPolicy(Qt.NoFocus)

        row = QHBoxLayout()
        row.setContentsMargins(12, 4, 12, 10)
        row.setSpacing(12)
        row.addWidget(self.btn_play)
        row.addWidget(self.lbl_pos)
        row.addStretch()
        row.addWidget(self.lbl_range)
        row.addStretch()
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
        act_exit = QAction("E&xit", self, shortcut=QKeySequence.Quit,
                           triggered=self.close)
        m_file.addActions([act_open, act_trim])
        m_file.addSeparator()
        m_file.addAction(act_exit)

        m_opts = self.menuBar().addMenu("&Options")
        self.act_reencode = QAction(
            "Frame-accurate (re-encode — drops DV metadata)", self, checkable=True)
        self.act_ffpreview = QAction(
            "Force ffmpeg preview (no playback)", self, checkable=True,
            toggled=self.on_force_fallback)
        m_opts.addActions([self.act_reencode, self.act_ffpreview])

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
        self.btn_trim.setText("Trim")
        if code == 0:
            self.statusBar().showMessage(f"Saved: {dst}")
            QMessageBox.information(self, "mp4trim", f"Saved:\n{dst}")
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
    win = Trimmer()
    win.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        win.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
