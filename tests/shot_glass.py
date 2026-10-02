"""Visual check of the glass UI using the exact main() setup path."""
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\blare\projects\mp4trim")
sys.path.insert(0, str(ROOT))
import os
os.environ["MP4TRIM_NO_UPDATE"] = "1"
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

import mp4trim as m

app = QApplication(sys.argv)
app.setStyle("Fusion")
font = QFont("Segoe UI Variable Text", 10)
if not font.exactMatch():
    font = QFont("Segoe UI", 10)
app.setFont(font)
app.setStyleSheet(m.STYLE)
w = m.Trimmer()
w.resize(1240, 800)
w.show()
w.act_ffpreview.setChecked(True)
w.load(sys.argv[1])


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


end = time.time() + 60
while (w.info is None or len(w.timeline._thumb_ms) < 40) and time.time() < end:
    pump(0.1)
w.seek(22_500)
w.timeline.mark_in, w.timeline.mark_out = 14_010, 36_000
w.refresh_range()
# keep chrome up for the shot
hider = getattr(w, "auto_hider", None)
print("hider:", type(hider).__name__ if hider else None)
if hider:
    hider.poke()
    hider.set_enabled(False)
pump(2.0)
for p in ("bottom_panel", "top_panel"):
    pan = getattr(w, p, None)
    print(p, "exists" if pan else "MISSING",
          "visible" if pan and pan.isVisible() else "", pan.geometry() if pan else "")
w.grab().save(str(Path(sys.argv[2])))
print("saved", sys.argv[2])
w.close()
