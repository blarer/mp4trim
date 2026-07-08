"""mp4trim — minimal MP4 trimmer with a drag-handle timeline.

Preview frames are pulled through ffmpeg (correct colors even for hybrid
DV/HDR10 files that Windows decoders mangle), and the trim itself is a pure
stream copy so Dolby Vision / HDR10 metadata and all tracks survive intact.

Usage:  mp4trim [file.mp4]
Drag the green/red bars inward from each end to choose the kept range.
Keys:   Left/Right = step 1s (Shift = 10s), Enter = trim, Ctrl+O = open
"""

import json
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
HANDLE_GRAB_PX = 10


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
        self.setMinimumHeight(56)
        self.setMouseTracking(True)
        self.duration = 0
        self.position = 0
        self.mark_in = 0
        self.mark_out = 0
        self._drag = None  # None | "in" | "out" | "seek"

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
            self.setCursor(Qt.SizeHorCursor if over in ("in", "out")
                           else Qt.PointingHandCursor)

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

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        bar = QRectF(0, h * 0.3, w, h * 0.4)

        p.fillRect(self.rect(), QColor("#1e1e1e"))
        p.fillRect(bar, QColor("#3a3a3a"))

        if self.duration > 0:
            x_in, x_out = self._ms_to_x(self.mark_in), self._ms_to_x(self.mark_out)

            # trimmed-away ends, dimmed
            p.fillRect(QRectF(0, bar.top(), x_in, bar.height()), QColor("#262626"))
            p.fillRect(QRectF(x_out, bar.top(), w - x_out, bar.height()),
                       QColor("#262626"))
            # kept region
            p.fillRect(QRectF(x_in, bar.top(), max(x_out - x_in, 0), bar.height()),
                       QColor("#2e7d32"))

            # handles: full-height bars with a wider grip tab
            for x, color in ((x_in, "#66bb6a"), (x_out, "#ef5350")):
                p.fillRect(QRectF(x - 2, 2, 4, h - 4), QColor(color))
                p.fillRect(QRectF(x - 6, h / 2 - 9, 12, 18), QColor(color))

            # playhead
            x_pos = self._ms_to_x(self.position)
            p.fillRect(QRectF(x_pos - 1, 0, 2, h), QColor("#e0e0e0"))
        p.end()


class Trimmer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mp4trim")
        self.resize(980, 700)
        self.path: str | None = None
        self.duration = 0
        self.position = 0
        self.proc: QProcess | None = None

        self.preview = QLabel("Open a file (Ctrl+O) or drop one here")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background:#111; color:#888; font-size:14px;")
        self.preview.setMinimumHeight(400)

        self.timeline = Timeline()
        self.timeline.seeked.connect(self.seek)
        self.timeline.range_changed.connect(self.refresh)

        self.lbl_pos = QLabel("-")
        self.lbl_range = QLabel("-")
        self.btn_trim = QPushButton("Trim  [Enter]")
        self.btn_trim.clicked.connect(self.trim)

        row = QHBoxLayout()
        row.addWidget(self.lbl_pos)
        row.addStretch()
        row.addWidget(self.lbl_range)
        row.addStretch()
        row.addWidget(self.btn_trim)

        root = QVBoxLayout()
        root.addWidget(self.preview, stretch=1)
        root.addWidget(self.timeline)
        root.addLayout(row)
        host = QWidget()
        host.setLayout(root)
        self.setCentralWidget(host)
        self.setAcceptDrops(True)
        self._build_menu()

        # debounce frame grabs while scrubbing
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
        m_opts.addAction(self.act_reencode)

        m_help = self.menuBar().addMenu("&Help")
        m_help.addAction(QAction(
            "&About", self,
            triggered=lambda: QMessageBox.about(
                self, "mp4trim",
                "mp4trim 1.0\n\nDrag the green/red bars inward to pick the kept "
                "range, then Trim.\nStream copy by default — hybrid DV/HDR10 "
                "safe.\n\nRequires ffmpeg on PATH.")))

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
        self.refresh()
        self.update_frame()

    # ---- playback position ----

    def seek(self, ms: int):
        if not self.path:
            return
        self.position = min(max(ms, 0), self.duration)
        self.refresh()
        self._frame_timer.start()

    def update_frame(self):
        if not self.path:
            return
        img = grab_frame(self.path, self.position)
        if img:
            self.preview.setPixmap(QPixmap.fromImage(img).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._frame_timer.start()

    def refresh(self):
        self.timeline.position = self.position
        self.timeline.update()
        self.lbl_pos.setText(fmt_ms(self.position) + " / " + fmt_ms(self.duration))
        self.lbl_range.setText(
            f"keep {fmt_ms(self.timeline.mark_in)} → {fmt_ms(self.timeline.mark_out)}"
            f"  ({fmt_ms(self.timeline.mark_out - self.timeline.mark_in)})")

    def keyPressEvent(self, e):
        step = 10_000 if e.modifiers() & Qt.ShiftModifier else 1_000
        if e.key() == Qt.Key_Left:
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

        self.btn_trim.setEnabled(False)
        self.btn_trim.setText("Trimming…")
        self.proc = QProcess(self)
        self.proc.finished.connect(lambda code, _st: self.trim_done(code, dst))
        self.proc.start("ffmpeg", args)

    def trim_done(self, code: int, dst: Path):
        err = bytes(self.proc.readAllStandardError()).decode(errors="replace")
        self.proc = None
        self.btn_trim.setEnabled(True)
        self.btn_trim.setText("Trim  [Enter]")
        if code == 0:
            QMessageBox.information(self, "mp4trim", f"Saved:\n{dst}")
        else:
            QMessageBox.critical(self, "mp4trim",
                                 f"ffmpeg exited with {code}:\n{err[-1500:]}")


def main():
    app = QApplication(sys.argv)
    win = Trimmer()
    win.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        win.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
