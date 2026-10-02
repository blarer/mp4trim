"""Render docs/screenshot.png from the live UI.

    py -3.12 tests/make_screenshot.py <clip.mp4>

Uses ffmpeg preview mode so the video area shows a real frame (a
QVideoWidget cannot be grabbed off-screen).
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MP4TRIM_NO_UPDATE"] = "1"
from PySide6.QtWidgets import QApplication  # noqa: E402

import mp4trim as m  # noqa: E402

work = Path(tempfile.mkdtemp(prefix="mp4trim-shot-"))
clip = work / "rb Sep 23 08.47 PM.mp4"
shutil.copy2(sys.argv[1], clip)

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(m.STYLE)
w = m.Trimmer()
w.resize(1240, 800)
w.show()
w.act_ffpreview.setChecked(True)
w.load(str(clip))
if hasattr(w, "auto_hider"):  # keep glass panels visible in the screenshot
    w.auto_hider.set_enabled(False)


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


end = time.time() + 60
while len(w.timeline._thumb_ms) < 40 and time.time() < end:
    pump(0.1)
w.combo_tier.setCurrentIndex(1)
w.timeline.mark_in, w.timeline.mark_out = 14_010, 36_000
w.seek(22_500)
w.refresh_range()
pump(3.0)
w.grab().save(str(ROOT / "docs" / "screenshot.png"))
w.close()
shutil.rmtree(work, ignore_errors=True)
print("wrote docs/screenshot.png")
