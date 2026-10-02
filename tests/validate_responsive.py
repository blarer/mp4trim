"""Throwaway validation script for responsive panel layout.

Opens Trimmer at widths 640, 820, 1100, 1620, loads a clip,
pokes the auto-hider, and asserts:
  - both panels' sizeHint fits their geometry
  - no widget's x+width exceeds panel width

Usage:
    set PYTHONIOENCODING=utf-8
    py -3.12 tests/validate_responsive.py <clip.mp4>
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")
os.environ["MP4TRIM_NO_UPDATE"] = "1"

from PySide6.QtWidgets import QApplication
import mp4trim as m

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(2)

clip = sys.argv[1]

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(m.STYLE)

fails = []


def check(cond, msg):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {msg}", flush=True)
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
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    print(f"  timeout waiting for {what}")
    return False


def check_panels_fit(w, width_label):
    """Assert sizeHints fit geometry and no widget overflows its panel."""
    host = w.centralWidget()
    top = w.top_panel
    bot = w.bottom_panel

    # sizeHint must fit allocated geometry
    top_sh = top.sizeHint()
    bot_sh = bot.sizeHint()
    check(top_sh.width() <= top.width() + 2,
          f"[{width_label}] top sizeHint.w {top_sh.width()} <= panel.w {top.width()}")
    check(bot_sh.width() <= bot.width() + 2,
          f"[{width_label}] bot sizeHint.w {bot_sh.width()} <= panel.w {bot.width()}")

    # no child widget of bottom panel should overflow right edge
    bot_w = bot.width()
    for child in bot.findChildren(__import__("PySide6.QtWidgets", fromlist=["QWidget"]).QWidget):
        if not child.isVisible():
            continue
        if child.parent() is not bot:
            # only direct children; layout intermediaries may report odd positions
            continue
        right = child.x() + child.width()
        if right > bot_w + 4:  # 4px tolerance for border/shadow
            check(False, f"[{width_label}] {child.__class__.__name__}({child.objectName()!r}) "
                         f"right={right} > panel.w={bot_w}")

    # top panel: btn_close must be visible and fit
    btn_close = w.btn_close
    right = btn_close.mapTo(top, btn_close.rect().topRight()).x() + 1
    check(right <= top.width() + 4,
          f"[{width_label}] btn_close right={right} <= top.w={top.width()}")


w = m.Trimmer()
w.show()

w.load(clip)
check(wait_for(lambda: w.info is not None, 20, "clip load"), "clip loaded")
pump(0.5)

WIDTHS = [640, 820, 1100, 1620]

for width in WIDTHS:
    height = max(360, round(width * 9 / 16))
    w.resize(width, height)
    pump(0.3)

    # poke the hider so panels are visible
    if hasattr(w, "hider"):
        w.hider.poke()
    pump(0.2)

    # force relayout
    w._relayout_panels()
    pump(0.1)

    check_panels_fit(w, str(width))

    # verify tier attribute
    tier = getattr(w, "_current_tier", None)
    expected_tier = "full" if width >= 1100 else ("compact" if width >= 820 else "minimal")
    check(tier == expected_tier,
          f"[{width}] tier={tier!r} expected={expected_tier!r}")

    # verify visible widgets per tier
    if tier == "full":
        check(w.lbl_dur.isVisible(), f"[{width}] lbl_dur visible in full tier")
        check(w.vol_slider.isVisible(), f"[{width}] vol_slider visible in full tier")
        check(w.lbl_range.isVisible(), f"[{width}] lbl_range visible in full tier")
        check(w.btn_set_in.text() == "[ In",
              f"[{width}] btn_set_in text={w.btn_set_in.text()!r}")
    elif tier == "compact":
        check(not w.lbl_dur.isVisible(), f"[{width}] lbl_dur hidden in compact tier")
        check(w.vol_slider.isVisible(), f"[{width}] vol_slider visible in compact tier")
        check(w.vol_slider.width() == 70, f"[{width}] vol_slider.w={w.vol_slider.width()} in compact")
        check(w.btn_set_in.text() == "[", f"[{width}] btn_set_in=[")
    else:  # minimal
        check(not w.lbl_dur.isVisible(), f"[{width}] lbl_dur hidden in minimal")
        check(not w.vol_slider.isVisible(), f"[{width}] vol_slider hidden in minimal")
        check(w.btn_mute.isVisible(), f"[{width}] btn_mute visible in minimal")
        check(not w.lbl_range.isVisible(), f"[{width}] lbl_range hidden in minimal")
        check(w.lbl_est.isVisible(), f"[{width}] lbl_est visible in minimal")

w.close()
pump(0.2)

print()
print("=" * 50)
if fails:
    print(f"FAIL  {len(fails)} assertion(s) failed:")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL PASS")
    sys.exit(0)
