"""Checks for v2.1.0 features: fine scrubbing, folder navigation, OLED theme.

    py -3.12 tests/check_v21.py <clip.mp4>
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MP4TRIM_NO_UPDATE"] = "1"
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import mp4trim as m  # noqa: E402

fails = []


def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg, flush=True)
    if not c:
        fails.append(msg)


app = QApplication(sys.argv)
app.setStyleSheet(m.STYLE)

# ---- fine scrubbing math on a bare Timeline (60 s file, 1000 px wide) ----
tl = m.Timeline()
tl.resize(1000, 88)
tl.reset(60_000)
# the app updates .position from the seeked signal; mirror that here
tl.seeked.connect(lambda ms: setattr(tl, "position", ms))


def press(x, y):
    tl.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def move(x, y):
    tl.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y),
                                  Qt.NoButton, Qt.LeftButton, Qt.NoModifier))


def release():
    tl.mouseReleaseEvent(None)


ms_per_px = 60_000 / 1000  # 60 ms per pixel at full speed

# full speed: 100 px drag = 6000 ms
press(500, 40)
p0 = tl.position
move(600, 40)
check(abs(tl.position - (p0 + 100 * ms_per_px)) <= ms_per_px,
      f"full speed: 100px = {tl.position - p0}ms (~6000)")

# drop to half speed: next 100 px = ~3000 ms
p1 = tl.position
move(700, tl.height() + 45)
p1b = tl.position
check(abs(p1b - p1 - 100 * ms_per_px * 0.5) <= ms_per_px,
      f"half speed: 100px = {p1b - p1}ms (~3000)")
check(tl._rate == 0.5, f"rate is 0.5 ({tl._rate})")

# fine: two 100 px moves at 0.05 = ~600 ms total (3 ms/px, no motion lost)
p2 = tl.position
move(800, tl.height() + 150)
move(900, tl.height() + 150)
p2b = tl.position
check(abs(p2b - p2 - 200 * ms_per_px * 0.05) <= ms_per_px,
      f"fine: 200px = {p2b - p2}ms (~600)")
check(ms_per_px * 0.05 <= 1000 / 60,
      f"fine step {ms_per_px * 0.05:.1f}ms/px is sub-frame at 60fps")

# tier changes never jump the value
p3 = tl.position
move(900, 40)   # back to full speed at same x
check(abs(tl.position - p3) <= 1, f"no jump on tier change ({tl.position - p3}ms)")
release()

# handles use fine scrubbing too
tl.mark_in, tl.mark_out = 0, 60_000
press(tl._ms_to_x(0) + 2, 40)
check(tl._drag == "in", "grabbed in handle")
move(200, tl.height() + 150)
check(0 < tl.mark_in < 12_000 * 0.5, f"in-handle fine drag moved {tl.mark_in}ms (fine, not 12000)")
release()

# badge label appears for slowed tiers
press(500, 40)
move(520, tl.height() + 150)
check(tl._rate == 0.05, "fine tier active")
label = next(l for _, r, l in tl.SCRUB_TIERS if r == tl._rate)
check(label == "fine scrubbing", f"label '{label}'")
release()

# ---- folder navigation on a temp folder of 3 copies ----
src = sys.argv[1]
work = Path(tempfile.mkdtemp(prefix="mp4trim-nav-"))
names = ["a clip.mp4", "b clip.mp4", "c clip.mp4"]
for n in names:
    shutil.copy2(src, work / n)
(work / "not-a-video.txt").write_text("x")

w = m.Trimmer()
w.show()


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


w.load(str(work / "b clip.mp4"))
pump(0.5)
vids, idx = w._siblings()
check([v.name for v in vids] == names and idx == 1,
      f"siblings sorted, current idx {idx}")
check("clip 2 of 3" in w.statusBar().currentMessage(),
      f"status shows position ({w.statusBar().currentMessage()!r})")
w.step_video(1)
pump(0.5)
check(Path(w.info.path).name == "c clip.mp4", f"next -> {Path(w.info.path).name}")
w.step_video(1)
pump(0.2)
check(Path(w.info.path).name == "c clip.mp4", "stays on last")
check("Last video" in w.statusBar().currentMessage(), "end-of-folder notice")
w.step_video(-1)
pump(0.5)
w.step_video(-1)
pump(0.5)
check(Path(w.info.path).name == "a clip.mp4", f"prev twice -> {Path(w.info.path).name}")
check(w.btn_prev_vid.isEnabled() and w.btn_next_vid.isEnabled(), "nav buttons enabled")

# ---- OLED theme: window paints true black ----
img = w.grab().toImage()
c = img.pixelColor(w.width() // 2, w.menuBar().height() + 4)
check(c.red() == 0 and c.green() == 0 and c.blue() == 0,
      f"video area is pure black ({c.name()})")
check("background: #000000" in m.STYLE, "stylesheet uses #000000")

w.close()
pump(0.3)
shutil.rmtree(work, ignore_errors=True)
print("\nV21:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
