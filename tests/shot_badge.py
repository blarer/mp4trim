"""Grab a screenshot of the fine-scrub badge during a simulated drag."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import mp4trim as m  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(m.STYLE)
w = m.Trimmer()
w.resize(1240, 800)
w.show()
w.act_ffpreview.setChecked(True)
w.load(sys.argv[1])
end = time.time() + 60
while len(w.timeline._thumb_ms) < 40 and time.time() < end:
    app.processEvents()
    time.sleep(0.05)
tl = w.timeline
x = tl._ms_to_x(tl.duration * 0.4)
tl.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, 40),
                               Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
tl.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x + 30, tl.height() + 150),
                              Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
for _ in range(30):
    app.processEvents()
    time.sleep(0.02)
assert tl._rate == 0.05, tl._rate
tl.grab().save(sys.argv[2])
print("badge rate", tl._rate, "->", sys.argv[2])
w.close()
