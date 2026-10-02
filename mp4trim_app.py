"""Main window (Trimmer), app entry point, and --selftest."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import (Property, QEvent, QObject, QSettings, QSizeF,
                            Qt, QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QColor, QFont, QIcon, QImage,
                           QKeySequence, QPixmap)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog,
                               QGraphicsScene, QGraphicsView,
                               QMenuBar, QStatusBar,
                               QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QSlider, QStackedWidget,
                               QVBoxLayout, QWidget)

import mp4trim_updater as upd
from mp4trim_core import *  # noqa: F401,F403
from mp4trim_updater import UpdateChecker
from mp4trim_workers import (Analyzer, FrameGrabber, Job, ProbeWorker,
                             ScrubEngine, SnapGrabber)
from mp4trim_widgets import (SHORTCUTS_HELP, STYLE, SnapshotDialog,
                             SnapshotView, Timeline)

# GlassPanel / AutoHider land in mp4trim_widgets; until then, develop against
# minimal local stand-ins that implement the same contract (no animation).
try:
    from mp4trim_widgets import AutoHider, GlassPanel
    GLASS_IS_STUB = False
except ImportError:
    GLASS_IS_STUB = True

    class GlassPanel(QWidget):
        """Fallback stand-in for mp4trim_widgets.GlassPanel (no fade)."""
        visibility_changed = Signal(bool)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._pinned = False
            lay = QVBoxLayout(self)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(6)
            self.setAutoFillBackground(True)

        def fade_in(self, ms: int = 160):
            if not self.isVisible():
                self.show()
                self.visibility_changed.emit(True)

        def fade_out(self, ms: int = 260):
            if self._pinned:
                return
            if self.isVisible():
                self.hide()
                self.visibility_changed.emit(False)

        def _get_pinned(self) -> bool:
            return self._pinned

        def _set_pinned(self, v: bool):
            self._pinned = bool(v)

        pinned = Property(bool, _get_pinned, _set_pinned)

    class AutoHider(QObject):
        """Fallback stand-in for mp4trim_widgets.AutoHider (never hides)."""

        def __init__(self, window, panels, idle_ms=2000, cursor_target=None):
            super().__init__(window)
            self.panels = list(panels)
            self.idle_ms = idle_ms
            self.cursor_target = cursor_target
            self.hide_cursor_when_idle = True
            self._enabled = False

        def set_enabled(self, on: bool):
            self._enabled = bool(on)
            if not on:
                self.poke()

        def poke(self):
            for p in self.panels:
                p.fade_in()


class _EdgeResizer(QObject):
    """Frameless windows lose their resize border; give it back: within
    BORDER px of an edge show the resize cursor and hand the drag to the
    OS (startSystemResize keeps Aero snap etc.)."""
    BORDER = 6

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._cursor_set = False

    def _edges(self, gp):
        w = self.win
        if w.isMaximized() or w.isFullScreen():
            return Qt.Edges()
        g = w.frameGeometry()
        b = self.BORDER
        x, y = gp.x(), gp.y()
        if not (g.left() - 1 <= x <= g.right() + 1
                and g.top() - 1 <= y <= g.bottom() + 1):
            return Qt.Edges()
        e = Qt.Edges()
        if x <= g.left() + b:
            e |= Qt.LeftEdge
        elif x >= g.right() - b:
            e |= Qt.RightEdge
        if y <= g.top() + b:
            e |= Qt.TopEdge
        elif y >= g.bottom() - b:
            e |= Qt.BottomEdge
        return e

    @staticmethod
    def _cursor(e):
        if e in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            return Qt.SizeFDiagCursor
        if e in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            return Qt.SizeBDiagCursor
        if e & (Qt.LeftEdge | Qt.RightEdge):
            return Qt.SizeHorCursor
        return Qt.SizeVerCursor

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t not in (QEvent.MouseMove, QEvent.MouseButtonPress):
            return False
        if not isinstance(obj, QWidget) or obj.window() is not self.win:
            return False
        e = self._edges(ev.globalPosition().toPoint())
        if t == QEvent.MouseMove:
            if e and not ev.buttons():
                if not self._cursor_set:
                    QApplication.setOverrideCursor(self._cursor(e))
                    self._cursor_set = True
                else:
                    QApplication.changeOverrideCursor(self._cursor(e))
            elif self._cursor_set:
                QApplication.restoreOverrideCursor()
                self._cursor_set = False
            return False
        if e and ev.button() == Qt.LeftButton:
            h = self.win.windowHandle()
            if h is not None:
                h.startSystemResize(e)
                return True
        return False


class _VideoView(QGraphicsView):
    """Borderless graphics view that keeps the video item filling it.

    QVideoWidget is a native win32 child window that Windows composites
    over sibling Qt widgets, hiding the glass panels. QGraphicsVideoItem
    renders through Qt's scene graph instead, so normal stacking works.
    """

    def __init__(self, scene, item):
        super().__init__(scene)
        self._item = item
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor(0, 0, 0))
        self.setRenderHints(self.renderHints())
        item.nativeSizeChanged.connect(lambda *_: self._fit())

    def _fit(self):
        vp = self.viewport().size()
        self._item.setSize(QSizeF(vp.width(), vp.height()))
        self.setSceneRect(0, 0, vp.width(), vp.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class _TierCombo(QComboBox):
    """QComboBox that reports when its popup is open (used to pin panels)."""
    popup_open = Signal(bool)

    def showPopup(self):
        self.popup_open.emit(True)
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        self.popup_open.emit(False)


class _FlexWidget(QWidget):
    """Container whose sizeHint().width() is always 0.

    Placed in an HBoxLayout with stretch > 0 so it expands to fill
    remaining space without inflating the parent layout's sizeHint.
    """

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setWidth(0)
        return sh

    def minimumSizeHint(self):
        sh = super().minimumSizeHint()
        sh.setWidth(0)
        return sh


# ------------------------------------------------------------- main window

class Trimmer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mp4trim")
        # no native title bar: window controls live in the top glass panel
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setMinimumSize(640, 360)
        # menu + status bar are overlay widgets inside the glass panels so
        # the video owns the whole client area (no bars, no letterbox)
        self._menu_bar = QMenuBar()
        self._menu_bar.setNativeMenuBar(False)
        self._status_bar = QStatusBar()
        self._status_bar.setSizeGripEnabled(False)
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
        self._scene = QGraphicsScene(self)
        self._video_item = QGraphicsVideoItem()
        self._scene.addItem(self._video_item)
        self.video = _VideoView(self._scene, self._video_item)
        self.video.setFocusPolicy(Qt.NoFocus)
        self.player.setVideoOutput(self._video_item)
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

        # --- volume ---
        self.btn_mute = btn("🔊", "Mute / unmute (M)", self.toggle_mute, "glass")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setObjectName("vol")
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(110)
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.setToolTip("Volume")
        self.vol.valueChanged.connect(self._on_volume)
        self.vol_slider = self.vol   # contract alias (tests, future UI work)
        vol0 = int(self.settings.value("volume", 100))
        muted0 = self.settings.value("muted", False, bool)
        self.vol.setValue(min(max(vol0, 0), 100))
        self.audio.setVolume(self.vol.value() / 100)
        self.audio.setMuted(muted0)
        self._sync_mute_icon()

        # --- export controls ---
        self.btn_open = btn("📂 Open", "Open a video (Ctrl+O)", self.open_dialog)
        self.combo_tier = _TierCombo()
        self.combo_tier.setFocusPolicy(Qt.NoFocus)
        for name, mb in DISCORD_TIERS:
            self.combo_tier.addItem(name, mb)
        self.combo_tier.setToolTip("Target size for Discord MP4 and GIF exports")
        self.combo_tier.currentIndexChanged.connect(
            lambda i: self.set_discord_limit(self.combo_tier.itemData(i)))
        self._combo_open = False
        self._menu_open = False
        self.combo_tier.popup_open.connect(self._on_combo_popup)
        self.btn_snap = btn("📷 Snap", "Grab + crop the current frame (S)",
                            self.snapshot)
        self.btn_gif = btn("🎞 GIF", "Animated GIF sized for your Discord "
                           "limit (G)", self.export_gif)
        self.btn_discord = btn("💬 Discord",
                               "Discord MP4: re-encode to fit your limit (D)",
                               self.export_discord, "discord")
        self.btn_trim = btn("✂ Trim",
                            "Lossless trim: stream copy, original quality (Enter)",
                            self.trim, "accent")

        # --- edge-to-edge video with floating glass panels on top ---
        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack, stretch=1)
        host = QWidget()
        host.setLayout(root)
        self.setCentralWidget(host)
        self.setAcceptDrops(True)

        # bottom panel: timeline on its own line, then transport | export row
        self.bottom_panel = GlassPanel(host)
        bl = QVBoxLayout()
        bl.setSpacing(6)
        if hasattr(self.bottom_panel, "set_content_layout"):
            self.bottom_panel.set_content_layout(bl)
        else:   # stub fallback
            old = self.bottom_panel.layout()
            if old is not None:
                QWidget().setLayout(old)
            bl.setContentsMargins(14, 10, 14, 10)
            self.bottom_panel.setLayout(bl)
        bl.addWidget(self.timeline)
        # row 1: transport · marks · volume · time ········ range/estimate
        row = QHBoxLayout()
        row.setSpacing(6)
        for w in (self.btn_go_in, self.btn_prev, self.btn_play, self.btn_next,
                  self.btn_go_out):
            row.addWidget(w)
        row.addSpacing(8)
        row.addWidget(self.btn_set_in)
        row.addWidget(self.btn_set_out)
        row.addSpacing(10)
        row.addWidget(self.btn_mute)
        row.addWidget(self.vol)
        row.addSpacing(10)
        row.addWidget(self.lbl_time)
        row.addWidget(self.lbl_dur)
        # wrap range/est labels in a _FlexWidget so their preferred width
        # doesn't inflate the bottom panel's sizeHint (they clip naturally)
        self._info_flex = _FlexWidget()
        info_col = QVBoxLayout(self._info_flex)
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(2)
        info_col.addWidget(self.lbl_range)
        info_col.addWidget(self.lbl_est)
        row.addWidget(self._info_flex, 1)   # stretch = 1
        bl.addLayout(row)
        # row 2: exports, right-aligned ····· tier · snap · gif · discord · trim
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addStretch()
        row2.addWidget(self.combo_tier)
        row2.addSpacing(6)
        for w in (self.btn_snap, self.btn_gif, self.btn_discord, self.btn_trim):
            row2.addWidget(w)
        bl.addLayout(row2)
        bl.addWidget(self._status_bar)

        # top panel: open / folder nav / clip name / update
        self.top_panel = GlassPanel(host)
        self.lbl_clip = QLabel("")
        self.lbl_clip.setObjectName("muted")
        self.lbl_clip.setMinimumWidth(0)
        self.lbl_clip.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._lbl_clip_full = ""   # unelided text; elided version set via _elide_clip()
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        top_row.addWidget(self._menu_bar)
        top_row.addSpacing(6)
        top_row.addWidget(self.btn_open)
        top_row.addSpacing(8)
        top_row.addWidget(self.btn_prev_vid)
        top_row.addWidget(self.btn_next_vid)
        top_row.addSpacing(10)
        top_row.addWidget(self.lbl_clip)
        top_row.addStretch()
        if hasattr(self.top_panel, "set_content_layout"):
            self.top_panel.set_content_layout(top_row)
        else:   # stub fallback
            old = self.top_panel.layout()
            if old is not None:
                QWidget().setLayout(old)
            top_row.setContentsMargins(14, 8, 14, 8)
            self.top_panel.setLayout(top_row)
        self._top_row = top_row

        # real-mouse support: without tracking, Qt emits no MouseMove
        # while no button is held, so the AutoHider would never see
        # activity; hover events need WA_Hover.
        for w in (self, host, self.stack, self.video, self.frame_label,
                  self.timeline):
            w.setMouseTracking(True)
            w.setAttribute(Qt.WA_Hover, True)

        # single/double click on the video toggles playback
        for w in (self.stack, self.video, self.video.viewport(),
                  self.frame_label):
            w.installEventFilter(self)
        host.installEventFilter(self)   # relayout panels when host resizes

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
        self._menu_bar.setMinimumWidth(self._menu_bar.sizeHint().width())
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
        self._top_row.addWidget(self.btn_update)
        self._top_row.addSpacing(8)
        self.btn_min = btn("–", "Minimize", self.showMinimized, "winbtn")
        self.btn_max = btn("□", "Maximize / restore (double-click the bar)",
                           self.toggle_maximize, "winbtn")
        self.btn_close = btn("✕", "Close", self.close, "winclose")
        for b_ in (self.btn_min, self.btn_max, self.btn_close):
            self._top_row.addWidget(b_)
        # drag the top panel to move; edges resize
        self.top_panel.installEventFilter(self)
        self._menu_bar.installEventFilter(self)
        self.lbl_clip.installEventFilter(self)
        self._edges = _EdgeResizer(self)
        QApplication.instance().installEventFilter(self._edges)
        QTimer.singleShot(3000, lambda: self.check_updates(manual=False))
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(24 * 60 * 60 * 1000)
        self._update_timer.timeout.connect(
            lambda: self.check_updates(manual=False))
        self._update_timer.start()

        # --- auto-hide chrome (panels + menu bar + status bar) ---
        self.hider = AutoHider(self, [self.top_panel, self.bottom_panel],
                               idle_ms=2000, cursor_target=self.video)
        self.hider.hide_cursor_when_idle = True
        self.auto_hider = self.hider   # alias used by tests
        self.hider.set_enabled(False)   # nothing loaded: keep chrome visible
        self.bottom_panel.visibility_changed.connect(self._on_chrome_visible)
        self._wire_menu_pins()

        # fullscreen + mute shortcuts
        self.addAction(self._act("Fullscreen", self.toggle_fullscreen, "F11"))
        self.addAction(self._act("Exit fullscreen", self._esc_pressed, "Esc"))
        self.addAction(self._act("Mute", self.toggle_mute, "M"))

        QTimer.singleShot(0, self._relayout_panels)

    # ------------------------------------------- glass panels / auto-hide

    def _wire_menu_pins(self):
        """Pin panels + keep chrome up while any menu is open."""
        for m in self.menuBar().findChildren(QMenu):
            m.aboutToShow.connect(lambda: self._set_menu_open(True))
            m.aboutToHide.connect(lambda: self._set_menu_open(False))

    def _set_menu_open(self, on: bool):
        self._menu_open = on
        self._update_pins()

    def _on_combo_popup(self, on: bool):
        self._combo_open = on
        self._update_pins()

    def _update_pins(self):
        busy = self.job is not None
        pin_bottom = busy or self._combo_open or self._menu_open
        self.bottom_panel.pinned = pin_bottom
        self.top_panel.pinned = self._menu_open
        if pin_bottom or self._menu_open:
            self.hider.poke()

    def _on_chrome_visible(self, visible: bool):
        """Menu + status bar follow the bottom panel's visibility."""
        pinned_sb = (self.job is not None or self._update_msi is not None
                     or self._menu_open)
        self._menu_bar.setVisible(True)
        self._status_bar.setVisible(True)

    def _place_panel(self, panel, x: int, y: int, w: int, h: int):
        # The real GlassPanel slides via a QPropertyAnimation on 'pos' whose
        # end value was captured before this relayout; stop it and resync the
        # base position or the animation drags the panel back to (0, 0).
        slide = getattr(panel, "_slide", None)
        if slide is not None:
            slide.stop()
        panel.setGeometry(x, y, w, h)
        if getattr(panel, "_base_pos", None) is not None:
            panel._base_pos = panel.pos()

    def _elide_clip(self, avail_px: int):
        """Set lbl_clip text, elided to avail_px with Qt.ElideMiddle."""
        if not self._lbl_clip_full:
            self.lbl_clip.setText("")
            return
        fm = self.lbl_clip.fontMetrics()
        elided = fm.elidedText(self._lbl_clip_full, Qt.ElideMiddle,
                               max(avail_px, 20))
        self.lbl_clip.setText(elided)

    def _apply_tier(self, host_width: int):
        """Switch between full / compact / minimal layout tiers based on host_width.

        Tiers
        -----
        full    >= 1100 px  all labels visible, vol_slider 110 px
        compact  820-1099   shorten mark buttons, hide lbl_dur,
                            narrow vol_slider to 70 px
        minimal  < 820      hide vol_slider (keep btn_mute), hide lbl_range,
                            shorten export buttons to emoji only
        """
        if host_width >= 1100:
            tier = "full"
        elif host_width >= 820:
            tier = "compact"
        else:
            tier = "minimal"

        if getattr(self, "_current_tier", None) == tier:
            return  # nothing to do

        self._current_tier = tier

        # Reset max-width constraints (previous tiers may have set them)
        self.lbl_range.setMaximumWidth(16777215)
        self.lbl_est.setMaximumWidth(16777215)

        if tier == "full":
            self.btn_set_in.setText("[ In")
            self.btn_set_out.setText("Out ]")
            self.lbl_dur.show()
            self.vol.setFixedWidth(110)
            self.vol.show()
            self.btn_mute.show()
            self.lbl_range.show()
            self.lbl_est.show()
            # max widths will be set by _relayout_panels based on bw
            self.btn_snap.setText("📷 Snap")
            self.btn_gif.setText("🎞 GIF")
            self.btn_discord.setText("💬 Discord")
            self.btn_trim.setText("✂ Trim")
            self.btn_open.setText("📂 Open")

        elif tier == "compact":
            self.btn_set_in.setText("[")
            self.btn_set_out.setText("]")
            self.lbl_dur.hide()
            self.vol.setFixedWidth(70)
            self.vol.show()
            self.btn_mute.show()
            self.lbl_range.show()
            self.lbl_est.show()
            # max widths will be set by _relayout_panels based on bw
            self.btn_snap.setText("📷 Snap")
            self.btn_gif.setText("🎞 GIF")
            self.btn_discord.setText("💬 Discord")
            self.btn_trim.setText("✂ Trim")
            self.btn_open.setText("📂 Open")

        else:  # minimal
            self.btn_set_in.setText("[")
            self.btn_set_out.setText("]")
            self.lbl_dur.hide()
            self.vol.hide()
            self.btn_mute.show()
            self.lbl_range.hide()
            self.lbl_est.show()
            self.btn_snap.setText("📷")
            self.btn_gif.setText("🎞")
            self.btn_discord.setText("💬")
            self.btn_trim.setText("✂")
            self.btn_open.setText("📂")

    def _relayout_panels(self):
        host = self.centralWidget()
        if host is None or not hasattr(self, "bottom_panel"):
            return
        hw, hh = host.width(), host.height()
        m = 16
        bw = min(1240, hw - 2 * m)

        # apply responsive tier FIRST (changes widget visibility / sizes)
        self._apply_tier(hw)

        # _FlexWidget (info_flex) reports sizeHint().width() == 0, so
        # lbl_range / lbl_est never inflate the bottom panel's sizeHint.
        # No manual max-width caps needed; the layout stretch gives them
        # exactly the leftover space.

        # activate layouts so sizeHints reflect the new tier
        for pan in (self.top_panel, self.bottom_panel):
            lay = pan.layout()
            if lay is not None:
                lay.activate()

        bh = self.bottom_panel.sizeHint().height()
        self._place_panel(self.bottom_panel, (hw - bw) // 2, hh - bh - m, bw, bh)

        # top panel: lbl_clip gets whatever space is left after fixed widgets
        # estimate fixed widget widths to compute clip label budget
        _menu_w = self._menu_bar.sizeHint().width()
        _open_w = self.btn_open.sizeHint().width()
        _nav_w = (self.btn_prev_vid.sizeHint().width()
                  + self.btn_next_vid.sizeHint().width())
        _wctl_w = (self.btn_min.sizeHint().width()
                   + self.btn_max.sizeHint().width()
                   + self.btn_close.sizeHint().width())
        _upd_w = (self.btn_update.sizeHint().width()
                  if self.btn_update.isVisible() else 0)
        # spacings: 6+6+8+10+8 (from top_row layout, approximate)
        _spacing = 6 + 6 + 8 + 10 + 8 + 6 * 3 + 14 * 2  # margins + spacings
        _clip_budget = (hw - 2 * m
                        - _menu_w - _open_w - _nav_w - _wctl_w
                        - _upd_w - _spacing - 20)
        self._elide_clip(max(_clip_budget, 20))

        # re-activate after elision may change sizeHint
        lay = self.top_panel.layout()
        if lay is not None:
            lay.activate()

        tw = min(max(self.top_panel.sizeHint().width(),
                     self.top_panel.minimumSizeHint().width(), 520),
                 hw - 2 * m)   # top panel capped at host_width - 32
        th = self.top_panel.sizeHint().height()
        self._place_panel(self.top_panel, (hw - tw) // 2, m, tw, th)
        self.bottom_panel.raise_()
        self.top_panel.raise_()

    # ------------------------------------------------------------- volume

    def _on_volume(self, v: int):
        self.audio.setVolume(v / 100)
        self.settings.setValue("volume", int(v))

    def toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())
        self.settings.setValue("muted", self.audio.isMuted())
        self._sync_mute_icon()

    def _sync_mute_icon(self):
        self.btn_mute.setText("🔇" if self.audio.isMuted() else "🔊")

    # --------------------------------------------------------- fullscreen

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def fit_to_video(self):
        """Resize the window to the clip's aspect ratio (no black bars),
        as big as fits in 90% of the screen, centred on its current spot."""
        if not self.info or self.isFullScreen() or self.isMaximized():
            return
        vw, vh = self.info.width, self.info.height
        if not vw or not vh:
            return
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        s = min(scr.width() * 0.9 / vw, scr.height() * 0.9 / vh, 1.0)
        w, h = round(vw * s), round(vh * s)
        if w < 640 or h < 360:   # tiny/odd clips: grow to the minimum
            s = max(640 / vw, 360 / vh)
            w, h = round(vw * s), round(vh * s)
        c = self.geometry().center()
        x = min(max(c.x() - w // 2, scr.left()), scr.right() - w)
        y = min(max(c.y() - h // 2, scr.top()), scr.bottom() - h)
        self.setGeometry(x, y, w, h)

    def _esc_pressed(self):
        if self.isFullScreen():
            self.showNormal()

    def keyPressEvent(self, e):
        # Menu bar hides with the chrome, and Qt disables shortcuts of
        # actions in hidden menus; match keys against menu shortcuts
        # manually so D/G/S/I/O etc. keep working while the UI is
        # faded out. Unconditional: if this handler receives the key at
        # all, Qt's native shortcut resolution already declined it.
        if True:
            seq = QKeySequence(int(e.modifiers().value) | e.key())
            for top in self._menu_bar.actions():
                menu = top.menu()
                if menu is None:
                    continue
                for act in menu.actions():
                    if act.isEnabled() and any(
                            s.matches(seq) == QKeySequence.ExactMatch
                            for s in act.shortcuts()):
                        act.trigger()
                        e.accept()
                        return
        super().keyPressEvent(e)

    def eventFilter(self, obj, ev):
        host = self.centralWidget()
        if obj is host and ev.type() == QEvent.Resize:
            self._relayout_panels()
        elif obj in (self.top_panel, self.lbl_clip) or (
                obj is self._menu_bar and ev.type() in (
                    QEvent.MouseButtonPress, QEvent.MouseButtonDblClick)
                and self._menu_bar.actionAt(ev.position().toPoint()) is None):
            if ev.type() == QEvent.MouseButtonDblClick \
                    and ev.button() == Qt.LeftButton:
                self.toggle_maximize()
                return True
            if ev.type() == QEvent.MouseButtonPress \
                    and ev.button() == Qt.LeftButton:
                h = self.windowHandle()
                if h is not None and not self.isFullScreen():
                    h.startSystemMove()
                return True
        elif obj in (self.stack, self.video, self.video.viewport(),
                     self.frame_label):
            # single AND double click both play/pause; the second click of a
            # double arrives as DblClick, which we swallow so the pair counts
            # as one toggle, not two.
            if ev.type() == QEvent.MouseButtonPress \
                    and ev.button() == Qt.LeftButton:
                self.hider.poke()
                self.play_pause()
                return True
            if ev.type() == QEvent.MouseButtonDblClick \
                    and ev.button() == Qt.LeftButton:
                return True
        return super().eventFilter(obj, ev)

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
        self.statusBar().show()
        self._relayout_panels()
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

    def menuBar(self):
        return self._menu_bar

    def statusBar(self):
        return self._status_bar

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
        vids, idx = self._siblings()
        cpos = f"  ·  clip {idx + 1} of {len(vids)}" if idx >= 0 else ""
        self._lbl_clip_full = f"{Path(path).name}{cpos}"
        self.lbl_clip.setText(self._lbl_clip_full)
        self.hider.set_enabled(True)
        self.fit_to_video()
        self._relayout_panels()
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
        text = (f"IN {fmt_dur(t_in)} · OUT {fmt_dur(t_out)} · "
                f"KEEP <b>{fmt_dur(keep)}</b>&nbsp;")
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
        self.statusBar().show()
        self._update_pins()
        self.hider.poke()
        self.update_controls()
        self.job.start()

    def _finish_job(self):
        if self.job:
            self.job.wait(2000)
        self.job = None
        self.progress.hide()
        self.btn_cancel.hide()
        self._update_pins()
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
        # invalidate in-flight async probes/snapshots; their slots
        # would otherwise touch widgets that Qt is tearing down
        self._load_gen += 1

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


