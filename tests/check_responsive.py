"""Validation script for responsive panel layout.

Opens Trimmer at widths 640, 760, 900, 1200, 1620 (heights per 16:9),
loads a clip, pokes w.hider, processes events, then for each panel asserts:
  - panel.sizeHint().width() <= panel.width()
  - for every visible child widget: geometry().right() <= panel.width()
  - no two sibling visible widgets' geometries intersect

Prints a per-width PASS/FAIL table.

Usage:
    set PYTHONIOENCODING=utf-8
    py -3.12 tests/check_responsive.py <clip.mp4>
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia*=false")
os.environ["MP4TRIM_NO_UPDATE"] = "1"

from PySide6.QtWidgets import QApplication, QWidget
import mp4trim as m

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(2)

clip = sys.argv[1]

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(m.STYLE)

fails = []
results = {}  # width -> list of (pass, msg)


def check(width, cond, msg):
    tag = "PASS" if cond else "FAIL"
    print(f"  {tag} {msg}", flush=True)
    results.setdefault(width, []).append((cond, msg))
    if not cond:
        fails.append(f"[{width}] {msg}")


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


def rects_intersect(r1, r2):
    """True if two QRects overlap (non-zero intersection area)."""
    if r1.right() <= r2.left() or r2.right() <= r1.left():
        return False
    if r1.bottom() <= r2.top() or r2.bottom() <= r1.top():
        return False
    return True


def visible_direct_children(panel):
    """Yield (name, geometry) of visible direct-child QWidgets."""
    for child in panel.children():
        if not isinstance(child, QWidget):
            continue
        if not child.isVisible():
            continue
        # GlassPanel: children are on _surface; check both levels
        parent = child.parent()
        surface = getattr(panel, "_surface", None)
        if parent is not panel and parent is not surface:
            continue
        name = f"{child.__class__.__name__}({child.objectName()!r})"
        # map geometry to panel coordinates
        geo = child.geometry()
        if parent is surface and surface is not panel:
            # offset by surface position inside panel
            sp = surface.pos()
            geo.translate(sp.x(), sp.y())
        yield name, geo


def check_panel(panel, panel_name, width):
    """Run all assertions for one panel at a given width."""
    pw = panel.width()
    sh = panel.sizeHint()

    # 1. sizeHint().width() <= panel.width()
    check(width, sh.width() <= pw + 2,
          f"{panel_name} sizeHint.w {sh.width()} <= panel.w {pw}")

    # 2. every visible child: right() <= panel width
    children = list(visible_direct_children(panel))
    for name, geo in children:
        right = geo.right() + 1  # Qt right() is inclusive
        check(width, right <= pw + 4,
              f"{panel_name}/{name} right={right} <= panel.w={pw}")

    # 3. no two sibling visible widgets' geometries intersect
    for i, (n1, g1) in enumerate(children):
        for j, (n2, g2) in enumerate(children):
            if j <= i:
                continue
            if rects_intersect(g1, g2):
                # only flag if overlap area > 2px (ignore sub-pixel rounding)
                inter_w = min(g1.right(), g2.right()) - max(g1.left(), g2.left())
                inter_h = min(g1.bottom(), g2.bottom()) - max(g1.top(), g2.top())
                if inter_w > 2 and inter_h > 2:
                    check(width, False,
                          f"{panel_name} overlap: {n1} {g1} vs {n2} {g2}")


# ---------- main ----------

w = m.Trimmer()
w.show()

w.load(clip)
check("init", wait_for(lambda: w.info is not None, 20, "clip load"), "clip loaded")
pump(0.5)

WIDTHS = [640, 760, 900, 1200, 1620]

for width in WIDTHS:
    height = max(360, round(width * 9 / 16))
    print(f"\n--- {width}x{height} ---", flush=True)
    w.resize(width, height)
    pump(0.3)

    # poke the hider so panels are visible
    if hasattr(w, "hider"):
        w.hider.poke()
    pump(0.2)

    # force relayout
    w._relayout_panels()
    pump(0.1)

    # check both panels
    check_panel(w.top_panel, "top", width)
    check_panel(w.bottom_panel, "bot", width)

    # verify tier
    tier = getattr(w, "_current_tier", None)
    if width >= 1100:
        expected = "full"
    elif width >= 820:
        expected = "compact"
    else:
        expected = "minimal"
    check(width, tier == expected, f"tier={tier!r} expected={expected!r}")

w.close()
pump(0.2)

# ---------- summary table ----------

print()
print("=" * 60)
header = f"{'width':<8} {'checks':<8} {'passed':<8} {'result':<8}"
print(header)
print("-" * 60)
all_ok = True
for width in ["init"] + WIDTHS:
    items = results.get(width, [])
    total = len(items)
    passed = sum(1 for ok, _ in items if ok)
    ok = passed == total
    all_ok = all_ok and ok
    tag = "PASS" if ok else "FAIL"
    print(f"{str(width):<8} {total:<8} {passed:<8} {tag:<8}")
print("-" * 60)
print(f"{'OVERALL':<8} {'':8} {'':8} {'PASS' if all_ok else 'FAIL'}")
print("=" * 60)

if fails:
    print(f"\n{len(fails)} failure(s):")
    for f in fails:
        print(f"  - {f}")

sys.exit(0 if all_ok else 1)
