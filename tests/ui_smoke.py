"""UI smoke test: drives the real window headlessly-ish and grabs screenshots.

    py -3.12 tests/ui_smoke.py <clip.mp4> [out_dir]

Loads the clip, waits for thumbnails + keyframes, exercises marks, frame
step, zoom, tier switching, fallback preview, and a real lossless trim via
the UI path (progress + completion signal). Writes screenshots to out_dir.
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")
os.environ["MP4TRIM_NO_UPDATE"] = "1"

from PySide6.QtCore import QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import mp4trim as m  # noqa: E402

out = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
out.mkdir(parents=True, exist_ok=True)
# work on a temp copy so exports never land next to the user's real clips
work = Path(tempfile.mkdtemp(prefix="mp4trim-ui-"))
clip = str(work / "sample (2).mp4")
shutil.copy2(sys.argv[1], clip)

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(m.STYLE)
w = m.Trimmer()
w.settings.setValue("recent", [])
w.resize(1240, 820)
w.show()
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_for(pred, sec, what):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    print(f"  timeout waiting for {what}")
    return False


w.load(clip)
wait_for(lambda: w.info is not None, 15, "async load")
check(w.info is not None, "clip loaded")
check(wait_for(lambda: len(w.keyframes) > 0, 20, "keyframes"), "keyframes indexed")
check(wait_for(lambda: len(w.timeline._thumb_ms) >= 35, 60, "thumbs"),
      f"thumbnails ({len(w.timeline._thumb_ms)})")
pump(1.0)
w.grab().save(str(out / "01_loaded.png"))

# marks via the same slots the I/O keys trigger
w.seek(12_345)
pump(0.3)
w.set_in()
check(w.timeline.mark_in == 12_345, f"I sets in-point ({w.timeline.mark_in})")
w.seek(21_000)
pump(0.3)
w.set_out()
check(w.timeline.mark_out == 21_000, f"O sets out-point ({w.timeline.mark_out})")
check("lossless starts" in w.lbl_range.text(), "keyframe warning shown for mid-GOP in")
w.snap_in()
kf = m.keyframe_before(w.keyframes, 12_345)
check(w.timeline.mark_in == kf, f"K snaps to keyframe {kf}")
check("lossless starts" not in w.lbl_range.text(), "warning cleared after snap")

# frame stepping
w.seek(5000)
pump(0.3)
w.step_frames(1)
check(w.position == 5000 + round(1000 / w.info.fps), f"next frame ({w.position})")
w.step_frames(-2)
check(w.position == 5000 - round(1000 / w.info.fps) or
      abs(w.position - (5000 - 1000 / w.info.fps)) <= 1, f"prev frame ({w.position})")

# zoom around the in-point
tl = w.timeline
pos = QPointF(tl._ms_to_x(tl.mark_in), tl.height() / 2)
for _ in range(6):
    ev = QWheelEvent(pos, tl.mapToGlobal(pos), QPointF(0, 0).toPoint(),
                     QPointF(0, 120).toPoint(), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    tl.wheelEvent(ev)
check(tl.zoomed and tl.view1 - tl.view0 < w.info.duration_ms / 3,
      f"wheel zooms ({tl.view0}-{tl.view1})")
check(tl.view0 <= tl.mark_in <= tl.view1, "zoom keeps anchor in view")
pump(0.5)
w.grab().save(str(out / "02_zoomed.png"))
tl.reset_zoom()
check(not tl.zoomed, "zoom reset")

# tiers update the estimate
tier_limits = [mb for _, mb in m.DISCORD_TIERS]
for limit in tier_limits:
    w.combo_tier.setCurrentIndex(tier_limits.index(limit))
    pump(0.05)
    check(f"Discord {limit} MB" in w.lbl_est.text() or "too long" in w.lbl_est.text(),
          f"estimate reflects {limit} MB tier")
check(w.discord_limit() == tier_limits[-1] and
      w.tier_actions[len(tier_limits) - 1][0].isChecked(),
      "combo and menu tier stay in sync")
w.combo_tier.setCurrentIndex(1)

# fallback preview is async and does not block the UI
w.act_ffpreview.setChecked(True)
t = time.time()
for ms in range(0, 20_000, 500):
    w.seek(ms)
    app.processEvents()
block = time.time() - t
check(block < 1.0, f"scrubbing 40 steps in fallback stays responsive ({block:.2f}s)")
check(wait_for(lambda: w.frame_label.pixmap() is not None and
               not w.frame_label.pixmap().isNull(), 10, "fallback frame"),
      "fallback frame arrives")
w.act_ffpreview.setChecked(False)
pump(0.5)

# real lossless trim through the UI job path
w.timeline.mark_in, w.timeline.mark_out = kf, kf + 4000
w.refresh_range()
w.trim()
check(w.job is not None and not w.btn_trim.isEnabled(), "export buttons disabled while busy")
check(wait_for(lambda: w.job is None, 60, "trim"), "trim finished")
outp = w.last_output
check(outp and Path(outp).exists() and Path(outp).name.startswith("sample (2)_trim_"),
      f"trim output exists ({outp and Path(outp).name})")
check(w.statusBar().currentMessage().startswith("✓"), "success shown in status bar")
check(w.btn_reveal.isVisible(), "'Show in folder' offered")
check(app.clipboard().mimeData().hasUrls(), "output copied to clipboard as file")
pump(0.3)
w.grab().save(str(out / "03_after_export.png"))

# same range again must not overwrite: gets a (2) suffix
w.trim()
wait_for(lambda: w.job is None, 60, "trim 2")
check(w.last_output != outp and Path(w.last_output).exists(),
      f"second trim gets unique name ({Path(w.last_output).name})")

# cancel path on a Discord export
w.timeline.mark_in, w.timeline.mark_out = 0, min(40_000, w.info.duration_ms)
w.export_discord()
dst = w.job.dst
# first update arrives after the encoder probe + ffmpeg start; wait for it
wait_for(lambda: w.progress.value() > 0, 15, "first progress update")
check(w.progress.isVisible() and w.progress.value() > 0,
      f"progress bar moving ({w.progress.value() / 10:.0f}%)")
w.grab().save(str(out / "04_exporting.png"))
w.cancel_job()
check(wait_for(lambda: w.job is None, 20, "cancel"), "cancel returns control")
check(w.statusBar().currentMessage() == "Export cancelled", "cancel reported")
check(not Path(dst).exists(), "partial output removed on cancel")

w.close()
shutil.rmtree(work, ignore_errors=True)
print("\nUI SMOKE:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
