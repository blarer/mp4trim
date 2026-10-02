"""Main window (Trimmer), app entry point, and --selftest."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QAction, QFont, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

import mp4trim_updater as upd
from mp4trim_core import *  # noqa: F401,F403
from mp4trim_updater import UpdateChecker
from mp4trim_workers import (Analyzer, FrameGrabber, Job, ProbeWorker,
                             ScrubEngine, SnapGrabber)
from mp4trim_widgets import (SHORTCUTS_HELP, STYLE, SnapshotDialog,
                             SnapshotView, Timeline)

# ------------------------------------------------------------- main window

class Trimmer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mp4trim")
        self.setMinimumSize(980, 620)
        self.settings = QSettings("mp4trim", "mp4trim")
        geo = self.settings.value("geometry")
        if geo is None or not self.restoreGeometry(geo):
            self.resize(1240, 820)

        self.info: MediaInfo | None = None
        self.position = 0
        self.keyframes: list[int] = []
        self.job: Job | None = None
        self.last_output: str | None = None
        self.fallback = False
        self._prime = False
        self._gen = 0
        self._analyzer: Analyzer | None = None
        self._load_gen = 0
        self._prober: ProbeWorker | None = None
        self._snapper: SnapGrabber | None = None
        self._threads: list[QThread] = []   # keep stopped threads alive

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

        self.frame_label = QLabel("Drop a video here  ·  or press Ctrl+O")
        self.frame_label.setObjectName("drop")
        self.frame_label.setAlignment(Qt.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.video)        # 0 = live playback
        self.stack.addWidget(self.frame_label)  # 1 = ffmpeg preview / empty
        self.stack.setCurrentIndex(1)
        self.stack.setMinimumHeight(360)

        self.grabber = FrameGrabber()
        self.grabber.ready.connect(self._show_frame)
        self.grabber.start()
        self._frame_timer = QTimer(self, singleShot=True, interval=60)
        self._frame_timer.timeout.connect(self.update_frame)

        # frame-exact scrubbing (GOP cache). During a drag the preview stack
        # switches to frame_label and shows engine frames; the media player
        # is only seeked once, on release.
        self.scrub = ScrubEngine()
        self.scrub.frame_ready.connect(self._on_scrub_frame)
        self.scrub.start()
        self._scrubbing = False

        # --- timeline ---
        self.timeline = Timeline()
        self.timeline.seeked.connect(self.seek)
        self.timeline.range_changed.connect(self.refresh_range)
        self.timeline.drag_state.connect(self._on_drag_state)

        # --- transport row ---
        def btn(text, tip, slot, name=None):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            b.setFocusPolicy(Qt.NoFocus)
            if name:
                b.setObjectName(name)
            return b

        self.btn_go_in = btn("⇤", "Go to in-point (Home)", self.go_in, "tp")
        self.btn_prev_vid = btn("«", "Previous video in this folder (PgUp)",
                                lambda: self.step_video(-1), "nav")
        self.btn_next_vid = btn("»", "Next video in this folder (PgDn)",
                                lambda: self.step_video(1), "nav")
        self.btn_prev = btn("◂", "Previous frame  ( , )",
                            lambda: self.step_frames(-1), "tp")
        self.btn_play = btn("▶", "Play / pause (Space)", self.play_pause, "play")
        self.btn_next = btn("▸", "Next frame  ( . )",
                            lambda: self.step_frames(1), "tp")
        self.btn_go_out = btn("⇥", "Go to out-point (End)", self.go_out, "tp")
        self.btn_set_in = btn("[ In", "Set in-point at playhead (I)",
                              self.set_in, "mark")
        self.btn_set_out = btn("Out ]", "Set out-point at playhead (O)",
                               self.set_out, "mark")
        self.lbl_time = QLabel("0:00:00.000")
        self.lbl_time.setObjectName("time")
        self.lbl_dur = QLabel("/ 0:00:00.000")
        self.lbl_dur.setObjectName("dur")
        self.lbl_range = QLabel("")
        self.lbl_range.setObjectName("range")
        self.lbl_range.setTextFormat(Qt.RichText)
        self.lbl_range.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_est = QLabel("")
        self.lbl_est.setObjectName("est")
        self.lbl_est.setTextFormat(Qt.RichText)
        self.lbl_est.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row_a = QHBoxLayout()
        row_a.setContentsMargins(12, 6, 12, 4)
        row_a.setSpacing(6)
        row_a.addWidget(self.btn_prev_vid)
        row_a.addWidget(self.btn_next_vid)
        row_a.addSpacing(10)
        for w in (self.btn_go_in, self.btn_prev, self.btn_play, self.btn_next,
                  self.btn_go_out):
            row_a.addWidget(w)
        row_a.addSpacing(10)
        row_a.addWidget(self.btn_set_in)
        row_a.addWidget(self.btn_set_out)
        row_a.addSpacing(14)
        row_a.addWidget(self.lbl_time)
        row_a.addWidget(self.lbl_dur)
        row_a.addStretch()
        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        info_col.addWidget(self.lbl_range)
        info_col.addWidget(self.lbl_est)
        row_a.addLayout(info_col)

        # --- export row ---
        self.btn_open = btn("📂 Open", "Open a video (Ctrl+O)", self.open_dialog)
        lbl_tier = QLabel("Discord limit")
        lbl_tier.setObjectName("muted")
        self.combo_tier = QComboBox()
        self.combo_tier.setFocusPolicy(Qt.NoFocus)
        for name, mb in DISCORD_TIERS:
            self.combo_tier.addItem(name, mb)
        self.combo_tier.setToolTip("Target size for Discord MP4 and GIF exports")
        self.combo_tier.currentIndexChanged.connect(
            lambda i: self.set_discord_limit(self.combo_tier.itemData(i)))
        self.btn_snap = btn("📷 Snapshot", "Grab + crop the current frame (S)",
                            self.snapshot)
        self.btn_gif = btn("🎞 GIF", "Animated GIF sized for your Discord "
                           "limit (G)", self.export_gif)
        self.btn_discord = btn("💬 Discord MP4",
                               "Re-encode to fit your Discord limit (D)",
                               self.export_discord, "discord")
        self.btn_trim = btn("✂ Trim (lossless)",
                            "Stream copy, original quality and size (Enter)",
                            self.trim, "accent")

        row_b = QHBoxLayout()
        row_b.setContentsMargins(12, 4, 12, 12)
        row_b.setSpacing(8)
        row_b.addWidget(self.btn_open)
        row_b.addStretch()
        row_b.addWidget(lbl_tier)
        row_b.addWidget(self.combo_tier)
        row_b.addSpacing(8)
        for w in (self.btn_snap, self.btn_gif, self.btn_discord, self.btn_trim):
            row_b.addWidget(w)

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack, stretch=1)
        tl_wrap = QHBoxLayout()
        tl_wrap.setContentsMargins(12, 8, 12, 0)
        tl_wrap.addWidget(self.timeline)
        root.addLayout(tl_wrap)
        root.addLayout(row_a)
        root.addLayout(row_b)
        host = QWidget()
        host.setLayout(root)
        self.setCentralWidget(host)
        self.setAcceptDrops(True)

        # --- status bar: progress, cancel, show-in-folder ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.btn_cancel = btn("Cancel", "Stop the running export",
                              self.cancel_job, "cancel")
        self.btn_cancel.hide()
        self.btn_reveal = btn("Show in folder", "Open the export's folder",
                              self.reveal_last, "link")
        self.btn_reveal.hide()
        sb = self.statusBar()
        sb.addPermanentWidget(self.btn_reveal)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.btn_cancel)

        self._build_menu()
        saved = float(self.settings.value("discord_limit", 20.0))
        saved = {10.0: 20.0, 500.0: 1000.0}.get(saved, saved)  # Aug 2026 bump
        self.set_discord_limit(saved if saved in [m for _, m in DISCORD_TIERS]
                               else 20.0)
        self.update_controls()
        if have_ffmpeg():
            sb.showMessage("Ready · drop a video to start")
        else:
            sb.showMessage("ffmpeg not found · install it: winget install Gyan.FFmpeg")
            QTimer.singleShot(0, self._warn_no_ffmpeg)

        # auto-update: quiet startup check 3 s after launch
        self._updater: UpdateChecker | None = None
        self._update_msi: str | None = None
        self.btn_update = btn("", "Install the downloaded update and restart "
                              "mp4trim", self.apply_update, "accent")
        self.btn_update.hide()
        sb.addPermanentWidget(self.btn_update)
        QTimer.singleShot(3000, lambda: self.check_updates(manual=False))
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(24 * 60 * 60 * 1000)
        self._update_timer.timeout.connect(
            lambda: self.check_updates(manual=False))
        self._update_timer.start()

    # ------------------------------------------------------------ updates

    def check_updates(self, manual: bool = True):
        if self._updater and self._updater.isRunning():
            return
        u = UpdateChecker(manual=manual)
        u.update_ready.connect(self._on_update_ready)
        if manual:
            u.no_update.connect(self.statusBar().showMessage)
        u.finished.connect(lambda: None)
        self._updater = u
        if manual:
            self.statusBar().showMessage("Checking for updates…")
        u.start()

    def _on_update_ready(self, version: str, msi: str):
        self._update_msi = msi
        self.btn_update.setText(f"⬆ Update to {version}")
        self.btn_update.show()
        self.statusBar().showMessage(
            f"mp4trim {version} is ready to install (you have {APP_VERSION})")

    def apply_update(self):
        if not self._update_msi or not Path(self._update_msi).exists():
            return
        if self.job:
            QMessageBox.information(
                self, "mp4trim", "An export is running. The update will "
                "install after you finish or cancel it.")
            return
        upd.launch_update(self._update_msi)
        self.close()

    # ---------------------------------------------------------------- menu

    def _act(self, text, slot, shortcut=None, checkable=False):
        a = QAction(text, self, checkable=checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(slot)
        return a

    def _build_menu(self):
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        m_file.addAction(self._act("&Open…", self.open_dialog, QKeySequence.Open))
        self.m_recent = m_file.addMenu("Open &Recent")
        self._rebuild_recent()
        m_file.addSeparator()
        self.act_trim = self._act("&Trim (lossless)", self.trim, "Return")
        self.act_trim.setShortcuts([QKeySequence("Return"), QKeySequence("Enter"),
                                    QKeySequence("Ctrl+E")])
        m_file.addAction(self.act_trim)
        m_file.addAction(self._act("Export &Discord MP4", self.export_discord, "D"))
        m_file.addAction(self._act("Export &GIF", self.export_gif, "G"))
        m_file.addAction(self._act("&Snapshot / Crop…", self.snapshot, "S"))
        m_file.addSeparator()
        m_file.addAction(self._act("Show last export in folder", self.reveal_last))
        m_file.addSeparator()
        m_file.addAction(self._act("E&xit", self.close, QKeySequence.Quit))

        m_edit = mb.addMenu("&Marks")
        m_edit.addAction(self._act("Set &in-point at playhead", self.set_in, "I"))
        m_edit.addAction(self._act("Set &out-point at playhead", self.set_out, "O"))
        m_edit.addAction(self._act("Snap in-point to &keyframe", self.snap_in, "K"))
        m_edit.addSeparator()
        m_edit.addAction(self._act("Go to in-point", self.go_in, "Home"))
        m_edit.addAction(self._act("Go to out-point", self.go_out, "End"))
        m_edit.addAction(self._act("Previous frame", lambda: self.step_frames(-1), ","))
        m_edit.addAction(self._act("Next frame", lambda: self.step_frames(1), "."))
        m_edit.addSeparator()
        m_edit.addAction(self._act("Reset timeline &zoom", self.timeline.reset_zoom, "Z"))
        m_edit.addAction(self._act("Reset in/out to whole file", self.reset_marks, "Ctrl+R"))
        m_edit.addSeparator()
        m_edit.addAction(self._act("&Previous video in folder",
                                   lambda: self.step_video(-1), "PgUp"))
        m_edit.addAction(self._act("&Next video in folder",
                                   lambda: self.step_video(1), "PgDown"))

        # playback keys that live on the window, not in a menu
        for text, slot, key in [
            ("Play/pause", self.play_pause, "Space"),
            ("Back 1s", lambda: self.seek(self.position - 1000), "Left"),
            ("Forward 1s", lambda: self.seek(self.position + 1000), "Right"),
            ("Back 10s", lambda: self.seek(self.position - 10_000), "Shift+Left"),
            ("Forward 10s", lambda: self.seek(self.position + 10_000), "Shift+Right"),
        ]:
            self.addAction(self._act(text, slot, key))

        m_opts = mb.addMenu("&Options")
        self.act_reencode = self._act(
            "Frame-accurate trim (re-encode, drops Dolby Vision)",
            lambda _: self.refresh_range(), checkable=True)
        self.act_mix = self._act(
            "Mix all audio tracks in Discord MP4", self._save_mix, checkable=True)
        self.act_mix.setChecked(self.settings.value("mix_audio", False, bool))
        self.act_ffpreview = self._act(
            "Force ffmpeg preview (no playback)", self.on_force_fallback,
            checkable=True)
        m_opts.addActions([self.act_reencode, self.act_mix, self.act_ffpreview])
        m_opts.addSeparator()
        m_tier = m_opts.addMenu("Discord upload limit")
        self.tier_actions = []
        for name, mbytes in DISCORD_TIERS:
            act = QAction(name, self, checkable=True)
            act.triggered.connect(lambda _=False, v=mbytes: self.set_discord_limit(v))
            m_tier.addAction(act)
            self.tier_actions.append((act, mbytes))

        m_help = mb.addMenu("&Help")
        m_help.addAction(self._act("&Keyboard shortcuts", self.show_shortcuts, "F1"))
        m_help.addAction(self._act("Check for &updates", self.check_updates))
        m_help.addAction(self._act("&About", self.show_about))

    def _rebuild_recent(self):
        self.m_recent.clear()
        recent = [p for p in self._recent() if Path(p).exists()]
        for p in recent:
            self.m_recent.addAction(
                self._act(Path(p).name, lambda _=False, x=p: self.load(x)))
        if not recent:
            a = self.m_recent.addAction("(none)")
            a.setEnabled(False)
        else:
            self.m_recent.addSeparator()
            self.m_recent.addAction(self._act("Clear list", self._clear_recent))

    def _recent(self) -> list[str]:
        v = self.settings.value("recent", [])
        return [v] if isinstance(v, str) else list(v or [])

    def _push_recent(self, path: str):
        rec = [p for p in self._recent() if os.path.normcase(p) != os.path.normcase(path)]
        self.settings.setValue("recent", [path, *rec][:10])
        self._rebuild_recent()

    def _clear_recent(self):
        self.settings.setValue("recent", [])
        self._rebuild_recent()

    def _save_mix(self, on: bool):
        self.settings.setValue("mix_audio", on)

    def show_shortcuts(self):
        QMessageBox.information(self, "Keyboard shortcuts", SHORTCUTS_HELP)

    def show_about(self):
        QMessageBox.about(
            self, "mp4trim",
            f"<b>mp4trim {APP_VERSION}</b><br><br>"
            "Drag the green/red handles to pick the kept range.<br>"
            "<b>Trim</b> is lossless (stream copy, Dolby Vision / HDR safe).<br>"
            "<b>Discord MP4</b> re-encodes to fit your upload limit.<br><br>"
            f"ffmpeg: {tool('ffmpeg')}<br>"
            f"Encoder: {h264_encoder()}")

    def _warn_no_ffmpeg(self):
        QMessageBox.warning(
            self, "mp4trim",
            "ffmpeg / ffprobe were not found. The installer ships them, so "
            "reinstalling mp4trim fixes this.\n\n"
            "Running from source? Run  python fetch_ffmpeg.py  or\n"
            "    winget install Gyan.FFmpeg")

    # ------------------------------------------------------- file handling

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        vids = [f for f in files if Path(f).suffix.lower() in VIDEO_EXTS]
        if vids or files:
            self.load((vids or files)[0])

    def open_dialog(self):
        start = self.settings.value("last_dir", "") or str(Path.home() / "Videos")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", start,
            "Video (*.mp4 *.mkv *.mov *.m4v *.webm *.avi *.ts);;All files (*)")
        if path:
            self.load(path)

    def load(self, path: str):
        if not have_ffmpeg():
            self._warn_no_ffmpeg()
            return
        self._load_gen += 1
        self.statusBar().showMessage(f"Loading {Path(path).name}…")
        w = ProbeWorker(self._load_gen, path)
        w.ok.connect(self._on_probe_ok)
        w.fail.connect(self._on_probe_fail)
        w.finished.connect(lambda w=w: self._threads.remove(w)
                           if w in self._threads else None)
        self._threads.append(w)
        self._prober = w
        w.start()

    def _on_probe_ok(self, gen: int, path: str, info):
        if gen != self._load_gen:
            return
        self._finish_load(path, info)

    def _on_probe_fail(self, gen: int, path: str, err: str):
        if gen != self._load_gen:
            return
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "mp4trim", f"Could not open this file:\n{err}")

    def _finish_load(self, path: str, info: MediaInfo):
        self.info = info
        self.position = 0
        self.keyframes = []
        self._scrubbing = False
        self.scrub.set_source(None, [])
        self.frame_label.clear()
        self.frame_label.setText("")
        self.timeline.reset(info.duration_ms)
        self.settings.setValue("last_dir", str(Path(path).parent))
        self._push_recent(path)
        self.setWindowTitle(f"mp4trim · {Path(path).name}")
        self._start_analyzer()

        self.fallback = self.act_ffpreview.isChecked()
        if self.fallback:
            self.enter_fallback(silent=True)
        else:
            self.stack.setCurrentIndex(0)
            self._prime = True
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()  # paused on first frame via on_player_pos
            hdr = " · HDR" if info.hdr else ""
            vids, idx = self._siblings()
            pos = f" · clip {idx + 1} of {len(vids)}" if idx >= 0 else ""
            self.statusBar().showMessage(
                f"{Path(path).name} · {info.width}×{info.height} "
                f"{info.fps:.0f}fps {info.v_codec.upper()}{hdr} · "
                f"{info.total_kbps / 1000:.0f} Mbps{pos}")
        self.update_controls()
        self.refresh_pos()
        self.refresh_range()

    def _start_analyzer(self):
        if self._analyzer:
            self._analyzer.stop()
        self._gen += 1
        a = Analyzer(self._gen, self.info)
        a.keyframes.connect(self._on_keyframes)
        a.thumb.connect(self._on_thumb)
        a.finished.connect(lambda a=a: self._threads.remove(a)
                           if a in self._threads else None)
        self._threads.append(a)
        self._analyzer = a
        a.start()

    def _on_keyframes(self, gen: int, kfs: list):
        if gen == self._gen:
            self.keyframes = kfs
            self.scrub.set_source(self.info, kfs)
            self.refresh_range()

    def _on_thumb(self, gen: int, ms: int, img: QImage):
        if gen == self._gen:
            self.timeline.add_thumb(ms, img)

    # ---------------------------------------------------- folder navigation

    def _siblings(self) -> tuple[list[Path], int]:
        """Videos in the open file's folder (name order) + index of current."""
        if not self.info:
            return [], -1
        cur = Path(self.info.path)
        try:
            vids = sorted((p for p in cur.parent.iterdir()
                           if p.suffix.lower() in VIDEO_EXTS and p.is_file()),
                          key=lambda p: p.name.lower())
        except OSError:
            return [], -1
        me = os.path.normcase(str(cur))
        idx = next((i for i, p in enumerate(vids)
                    if os.path.normcase(str(p)) == me), -1)
        return vids, idx

    def step_video(self, delta: int):
        vids, idx = self._siblings()
        if not vids or self.job:
            return
        if idx < 0:
            idx = 0 if delta > 0 else len(vids) - 1
            self.load(str(vids[idx]))
            return
        j = idx + delta
        if not 0 <= j < len(vids):
            self.statusBar().showMessage(
                "Last video in this folder" if delta > 0
                else "First video in this folder")
            return
        self.load(str(vids[j]))

    # ------------------------------------------------- playback / preview

    @property
    def path(self) -> str | None:
        return self.info.path if self.info else None

    def play_pause(self):
        if not self.info or self.fallback:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.position >= self.timeline.mark_out - 50:
                self.seek(self.timeline.mark_in)
            self.player.play()

    def on_play_state(self, state):
        self.btn_play.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def on_player_pos(self, ms: int):
        if self._prime:
            self._prime = False
            self.player.pause()
        self.position = ms
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.timeline.follow(ms)
        self.refresh_pos()

    def on_player_error(self, _err, msg: str):
        if not self.fallback and self.info:
            self.enter_fallback(silent=False, detail=msg)

    def on_force_fallback(self, on: bool):
        if self.info:
            if on:
                self.enter_fallback(silent=True)
            else:
                pos = self.position
                self.fallback = False
                self.stack.setCurrentIndex(0)
                self.player.setSource(QUrl.fromLocalFile(self.info.path))
                self.player.setPosition(pos)
                self.update_controls()

    def enter_fallback(self, silent: bool, detail: str = ""):
        self.fallback = True
        self.player.stop()
        self.player.setSource(QUrl())
        self.stack.setCurrentIndex(1)
        self.btn_play.setText("▶")
        note = "ffmpeg preview mode · scrub the timeline (no live playback)"
        if not silent:
            note = "System decoder failed; " + note + (f"  [{detail}]" if detail else "")
        self.statusBar().showMessage(note)
        self.update_controls()
        self.update_frame()

    def seek(self, ms: int):
        if not self.info:
            return
        self.position = int(min(max(ms, 0), self.info.duration_ms))
        if self._scrubbing and self.scrub.ready:
            # frame-exact path: serve from the GOP cache, never the player
            self.scrub.request(self.position)
        elif self.fallback:
            self._frame_timer.start()
        else:
            self.player.setPosition(self.position)
        self.timeline.follow(self.position)
        self.refresh_pos()

    # ---- frame-exact scrubbing ----

    def _on_drag_state(self, dragging: bool):
        if not self.info:
            return
        if dragging:
            self._scrubbing = True
            if not self.fallback:
                if self.player.playbackState() == QMediaPlayer.PlayingState:
                    self.player.pause()
                if self.scrub.ready:
                    self.stack.setCurrentIndex(1)   # engine frames here
            if self.scrub.ready:
                self.scrub.request(self.position)
            elif self.fallback:
                self._frame_timer.start()
        else:
            self._scrubbing = False
            if self.fallback:
                self._frame_timer.start()
            else:
                # one real seek so playback resumes exactly here
                self.player.setPosition(self.position)
                self.stack.setCurrentIndex(0)

    def _on_scrub_frame(self, ms: int, img: QImage):
        # drop frames that arrive after the playhead moved a lot further
        if abs(ms - self.position) > 1000:
            return
        if self._scrubbing or self.fallback:
            self.frame_label.setPixmap(QPixmap.fromImage(img).scaled(
                self.frame_label.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def step_frames(self, n: int):
        if not self.info:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        self.seek(round(self.position + n * 1000 / (self.info.fps or 30)))

    def update_frame(self):
        if self.info and self.fallback:
            # prefer the cache in fallback mode too; miss = async ffmpeg grab
            if not (self.scrub.ready and self.scrub.request(self.position)):
                self.grabber.request(self.info.path, self.position,
                                     self.info.hdr)

    def _show_frame(self, img: QImage):
        if self.fallback:
            self.frame_label.setPixmap(QPixmap.fromImage(img).scaled(
                self.frame_label.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fallback:
            self._frame_timer.start()

    # ------------------------------------------------------------- marks

    def set_in(self):
        if self.info:
            self.timeline.mark_in = max(0, min(self.position,
                                               self.timeline.mark_out - 100))
            self.timeline.update()
            self.refresh_range()

    def set_out(self):
        if self.info:
            self.timeline.mark_out = min(self.info.duration_ms,
                                         max(self.position,
                                             self.timeline.mark_in + 100))
            self.timeline.update()
            self.refresh_range()

    def snap_in(self):
        kf = keyframe_before(self.keyframes, self.timeline.mark_in)
        if kf is not None:
            self.timeline.mark_in = kf
            self.seek(kf)
            self.timeline.update()
            self.refresh_range()

    def go_in(self):
        self.seek(self.timeline.mark_in)

    def go_out(self):
        self.seek(self.timeline.mark_out)

    def reset_marks(self):
        if self.info:
            self.timeline.mark_in, self.timeline.mark_out = 0, self.info.duration_ms
            self.timeline.update()
            self.refresh_range()

    # ------------------------------------------------------------ display

    def update_controls(self):
        loaded = self.info is not None
        busy = self.job is not None
        for b in (self.btn_go_in, self.btn_prev, self.btn_next, self.btn_go_out,
                  self.btn_set_in, self.btn_set_out):
            b.setEnabled(loaded)
        for b in (self.btn_prev_vid, self.btn_next_vid):
            b.setEnabled(loaded and not busy)
        self.btn_play.setEnabled(loaded and not self.fallback)
        for b in (self.btn_snap, self.btn_gif, self.btn_discord, self.btn_trim):
            b.setEnabled(loaded and not busy)

    def refresh_pos(self):
        self.timeline.position = self.position
        self.timeline.update()
        self.lbl_time.setText(fmt_ms(self.position))
        self.lbl_dur.setText(f"/ {fmt_ms(self.info.duration_ms if self.info else 0)}")

    def refresh_range(self):
        if not self.info:
            self.lbl_range.setText("")
            self.lbl_est.setText("")
            return
        t_in, t_out = self.timeline.mark_in, self.timeline.mark_out
        keep = t_out - t_in
        text = (f"IN {fmt_ms(t_in)}  OUT {fmt_ms(t_out)}  "
                f"KEEP <b>{fmt_dur(keep)}</b>")
        if not self.act_reencode.isChecked() and self.keyframes:
            kf = keyframe_before(self.keyframes, t_in)
            if kf is not None and t_in - kf > 50:
                text += (f"  <span style='color:#ffb74d'>⚠ lossless starts "
                         f"{(t_in - kf) / 1000:.1f}s early · K snaps</span>")
        self.lbl_range.setText(text)

        limit = self.discord_limit()
        lossless = lossless_mb(keep, self.info)
        plan = plan_discord(keep, limit, self.info)
        parts = [f"Lossless ≈ {fmt_mb(lossless)}"]
        if plan.ok:
            parts.append(
                f"<span style='color:#9aa4ff'>Discord {limit:g} MB → "
                f"{plan.height}p{plan.fps} · {plan.v_kbps / 1000:.1f} Mbps ✓</span>")
        else:
            parts.append(
                f"<span style='color:#ef9a9a'>too long for {limit:g} MB · "
                f"max ≈ {fmt_dur(plan.max_keep_s * 1000)}</span>")
        self.lbl_est.setText("  ·  ".join(parts))

    # ------------------------------------------------------------ exports

    def discord_limit(self) -> float:
        return float(self.settings.value("discord_limit", 20.0))

    def set_discord_limit(self, mb: float):
        mb = float(mb)
        self.settings.setValue("discord_limit", mb)
        for act, v in self.tier_actions:
            act.setChecked(v == mb)
        i = self.combo_tier.findData(int(mb))
        if i >= 0 and i != self.combo_tier.currentIndex():
            self.combo_tier.blockSignals(True)
            self.combo_tier.setCurrentIndex(i)
            self.combo_tier.blockSignals(False)
        self.refresh_range()

    def _range_ok(self) -> bool:
        if not self.info or self.job:
            return False
        if self.timeline.mark_out - self.timeline.mark_in < 100:
            QMessageBox.warning(self, "mp4trim", "In/out range is empty.")
            return False
        return True

    def _start_job(self, fn, dst: Path):
        self.player.pause()
        self.job = Job(fn, str(dst))
        self.job.progress.connect(self._on_progress)
        self.job.succeeded.connect(self._on_done)
        self.job.failed.connect(self._on_failed)
        self.job.cancelled.connect(self._on_cancelled)
        self.progress.setValue(0)
        self.progress.show()
        self.btn_cancel.show()
        self.btn_reveal.hide()
        self.update_controls()
        self.job.start()

    def _finish_job(self):
        if self.job:
            self.job.wait(2000)
        self.job = None
        self.progress.hide()
        self.btn_cancel.hide()
        self.update_controls()

    def _on_progress(self, frac: float, label: str):
        self.progress.setValue(int(frac * 1000))
        self.statusBar().showMessage(f"{label} · {frac * 100:.0f}%")

    def _on_done(self, path: str, msg: str, ok: bool):
        self._finish_job()
        self.last_output = path
        self.btn_reveal.show()
        if ok:
            copy_file_to_clipboard(path)
            self.statusBar().showMessage(f"✓ {msg}  ·  {Path(path).name}")
        else:
            self.statusBar().showMessage(f"⚠ {msg}")
            QMessageBox.warning(self, "mp4trim", f"{msg}\n\n{path}")

    def _on_failed(self, err: str):
        self._finish_job()
        self.statusBar().showMessage("Export failed")
        QMessageBox.critical(self, "mp4trim", f"Export failed:\n{err}")

    def _on_cancelled(self):
        self._finish_job()
        self.statusBar().showMessage("Export cancelled")

    def cancel_job(self):
        if self.job:
            self.statusBar().showMessage("Cancelling…")
            self.job.cancel()

    def reveal_last(self):
        if self.last_output and Path(self.last_output).exists():
            reveal_in_explorer(self.last_output)

    def trim(self):
        if not self._range_ok():
            return
        info, t_in, t_out = self.info, self.timeline.mark_in, self.timeline.mark_out
        accurate = self.act_reencode.isChecked()
        dst = output_path(info.path, "trim", t_in, t_out, Path(info.path).suffix)
        self._start_job(lambda run: export_trim(run, info, t_in, t_out, str(dst),
                                                accurate), dst)

    def export_discord(self):
        if not self._range_ok():
            return
        info, t_in, t_out = self.info, self.timeline.mark_in, self.timeline.mark_out
        limit = self.discord_limit()
        plan = plan_discord(t_out - t_in, limit, info)
        if not plan.ok:
            if QMessageBox.question(
                    self, "mp4trim",
                    f"{fmt_dur(t_out - t_in)} is too long for {limit:g} MB. It "
                    f"would be very blurry. Up to about "
                    f"{fmt_dur(plan.max_keep_s * 1000)} fits.\n\n"
                    "Export anyway?") != QMessageBox.StandardButton.Yes:
                return
        mix = self.act_mix.isChecked()
        dst = output_path(info.path, f"discord{limit:g}mb", t_in, t_out, ".mp4")
        self._start_job(lambda run: export_discord(run, info, t_in, t_out,
                                                   str(dst), limit, mix), dst)

    def export_gif(self):
        if not self._range_ok():
            return
        info, t_in, t_out = self.info, self.timeline.mark_in, self.timeline.mark_out
        limit = self.discord_limit()
        if limit <= 20 and t_out - t_in > 60_000:
            if QMessageBox.question(
                    self, "mp4trim",
                    "Selection is over a minute. GIFs that long get huge and "
                    "may not fit Discord even at lowest quality.\n"
                    "Tip: Discord MP4 looks far better at this length.\n\n"
                    "Continue anyway?") != QMessageBox.StandardButton.Yes:
                return
        dst = output_path(info.path, "clip", t_in, t_out, ".gif")
        self._start_job(lambda run: export_gif(run, info, t_in, t_out, str(dst),
                                               limit), dst)

    def snapshot(self):
        if not self.info:
            return
        self.player.pause()
        if self._snapper and self._snapper.isRunning():
            return
        path, ms = self.info.path, self.position
        QApplication.setOverrideCursor(Qt.WaitCursor)
        w = SnapGrabber(path, ms, self.info.hdr)
        w.done.connect(lambda img, p=path, m=ms: self._snap_done(img, p, m))
        w.finished.connect(lambda w=w: self._threads.remove(w)
                           if w in self._threads else None)
        self._threads.append(w)
        self._snapper = w
        w.start()

    def _snap_done(self, img, path: str, ms: int):
        QApplication.restoreOverrideCursor()
        if not self.info or self.info.path != path:
            return   # a different file was loaded meanwhile
        if img is None:
            QMessageBox.warning(self, "mp4trim", "Could not grab this frame.")
            return
        src = Path(path)
        default = str(src.with_name(f"{src.stem}_{fmt_tag(ms)}.png"))
        SnapshotDialog(img, default, self).exec()

    # ------------------------------------------------------------ closing

    def closeEvent(self, e):
        if self.job:
            if QMessageBox.question(
                    self, "mp4trim", "An export is running. Cancel it and quit?"
            ) != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.job.cancel()
            self.job.wait(5000)
        self.settings.setValue("geometry", self.saveGeometry())
        if self._analyzer:
            self._analyzer.stop()
        if self._updater is not None and self._updater.isRunning():
            self._updater.wait(2000)
        for t in list(self._threads):
            t.wait(3000)
        self.grabber.stop()
        self.scrub.stop()
        self.player.stop()
        super().closeEvent(e)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2:]))
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


def selftest(argv: list[str]) -> int:
    """Headless end-to-end check of the installed app on this machine.

        mp4trim.exe --selftest <out.json> [clip.mp4]

    Uses the same code paths as the buttons. Without a clip it synthesizes a
    1440p60 test video with the bundled ffmpeg. Writes a JSON report (a GUI
    exe has no console) and returns 0 only if every check passed.
    """
    report_path = Path(argv[0]) if argv else Path(tempfile.gettempdir()) / "mp4trim-selftest.json"
    rep: dict = {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
                 "checks": {}}
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        rep["checks"][name] = {"ok": bool(cond), "detail": detail}
        ok = ok and bool(cond)

    work = Path(tempfile.mkdtemp(prefix="mp4trim-selftest-"))
    try:
        rep["ffmpeg"], rep["ffprobe"] = tool("ffmpeg"), tool("ffprobe")
        chk("ffmpeg_found", have_ffmpeg(), rep["ffmpeg"])
        chk("ffmpeg_bundled", Path(rep["ffmpeg"]).parent == res_path("ffmpeg")
            or not rep["frozen"], rep["ffmpeg"])
        rep["encoder"] = h264_encoder()
        run = lambda a, s, l: run_ffmpeg(a, s, l)  # noqa: E731
        clip = argv[1] if len(argv) > 1 else None
        if not clip:
            clip = str(work / "synthetic (2).mp4")
            run(["-f", "lavfi", "-i", "testsrc2=s=2560x1440:r=60:d=20",
                 "-f", "lavfi", "-i", "sine=f=440:d=20", "-c:v", "libx264",
                 "-preset", "ultrafast", "-b:v", "60M", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", clip], 20, "synth")
        info = probe(clip)
        rep["source"] = {"w": info.width, "h": info.height, "fps": info.fps,
                         "mb": os.path.getsize(clip) / 1e6,
                         "sec": info.duration_ms / 1000}
        kfs = probe_keyframes(clip, info.start_s)
        chk("keyframes", len(kfs) > 0, f"{len(kfs)} keyframes")
        chk("thumbnail", grab_thumb(clip, 2000, info.hdr) is not None)
        results = {}
        for limit in (10, 50):
            dst = str(output_path(clip, f"discord{limit}mb", 0, info.duration_ms, ".mp4"))
            _, msg, good = export_discord(run, info, 0, info.duration_ms, dst,
                                          limit, False)
            size = os.path.getsize(dst) / 1e6
            v = probe(dst)
            results[limit] = {"mb": round(size, 2), "h": min(v.width, v.height),
                              "codec": v.v_codec, "msg": msg}
            chk(f"discord_{limit}mb", good and size <= limit and v.v_codec == "h264",
                f"{size:.2f} MB {v.v_codec} {v.width}x{v.height}")
        rep["discord"] = results
        dst = str(output_path(clip, "trim", 1000, 6000, ".mp4"))
        export_trim(run, info, 1000, 6000, dst, False)
        chk("lossless_trim", probe(dst).v_codec == info.v_codec, dst)
        dst = str(output_path(clip, "clip", 1000, 4000, ".gif"))
        _, msg, good = export_gif(run, info, 1000, 4000, dst, 10)
        chk("gif_10mb", good, msg)
    except Exception as e:  # noqa: BLE001
        chk("exception", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    rep["ok"] = ok
    report_path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    return 0 if ok else 1


