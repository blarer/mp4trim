"""Scrub engine checks: exactness, latency, cache behavior, UI integration.

    py -3.12 tests/check_scrub.py <clip.mp4>
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
src = sys.argv[1]
info = m.probe(src)
kfs = m.probe_keyframes(src, info.start_s)
frame_ms = 1000 / info.fps

eng = m.ScrubEngine()
got: list[tuple[int, object]] = []
eng.frame_ready.connect(lambda ms, img: got.append((ms, img)))
eng.start()
eng.set_source(info, kfs)


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def ask(ms, sec=10):
    """Request a frame and wait for it (handles sync cache-hit emits)."""
    n = len(got)
    eng.request(ms)
    end = time.time() + sec
    while time.time() < end:
        if len(got) > n:
            return got[-1]
        app.processEvents()
        time.sleep(0.005)
    return None


# 1. cold request: first frame of a GOP arrives fast (streamed decode)
t0 = time.time()
r = ask(10_050)
cold = time.time() - t0
check(r is not None, f"cold frame arrives ({cold * 1000:.0f} ms)")
check(cold < 1.0, f"cold latency < 1 s ({cold * 1000:.0f} ms)")

# 2. let the GOP finish, then every frame in it is a sync cache hit
pump(3.0)
import bisect  # noqa: E402
base = kfs[bisect.bisect_right(kfs, 10_050) - 1]   # keyframe of that GOP
hits = 0
t0 = time.time()
req = [int(base + i * frame_ms) for i in range(60)]
for ms in req:
    hits += eng.request(ms)
sync = (time.time() - t0) / len(req)
check(hits == len(req), f"{hits}/{len(req)} sync cache hits after warm-up")
check(sync < 0.005, f"cache hit {sync * 1000:.2f} ms avg")

# 3. frames are exact and distinct: engine frame == ffmpeg's own frame grab
got.clear()
mid_kf = kfs[len(kfs) // 2]
# aim at frame centres so floor() maps each time to the intended frame
probe_pts = [mid_kf + int((k + 0.5) * frame_ms) for k in (3, 4, 30)]
imgs = {}
for ms in probe_pts:
    # wait until the request is a synchronous cache hit -> exact frame
    end = time.time() + 15
    while not eng.request(ms) and time.time() < end:
        pump(0.05)
    r = got[-1] if got else None
    check(r is not None and r[0] == ms, f"exact frame served for {ms}ms")
    imgs[ms] = r[1]
    got.clear()
pump(0.2)


def diff(a, b):
    """Mean abs difference over a 64x64 sample grid."""
    w = min(a.width(), b.width())
    h = min(a.height(), b.height())
    total = 0
    for yy in range(0, h, max(h // 64, 1)):
        for xx in range(0, w, max(w // 64, 1)):
            ca, cb = a.pixelColor(xx, yy), b.pixelColor(xx, yy)
            total += (abs(ca.red() - cb.red()) + abs(ca.green() - cb.green())
                      + abs(ca.blue() - cb.blue()))
    return total / (64 * 64 * 3)


# adjacent frames differ, and the engine frame matches an independent decode.
# ref uses a different scaler, so there is a noise floor; identity means the
# ref is closer to the claimed frame than to its neighbour or a far frame.
d_adj = diff(imgs[probe_pts[0]], imgs[probe_pts[1]])
check(d_adj > 0.3, f"adjacent frames distinct (mean diff {d_adj:.2f})")
ref = m.grab_frame(src, probe_pts[1], hdr=info.hdr)
ref = ref.scaled(imgs[probe_pts[1]].size())
d_ref = diff(imgs[probe_pts[1]], ref)
d_prev = diff(imgs[probe_pts[0]], ref)
d_far = diff(imgs[probe_pts[2]], ref)
check(d_ref < d_prev and d_ref < d_far,
      f"engine frame matches reference decode (ref {d_ref:.2f} < prev "
      f"{d_prev:.2f}, far {d_far:.2f})")

# 4. simulated fast drag across GOP borders: every request eventually lands
got.clear()
t0 = time.time()
span = [int(20_000 + i * 120) for i in range(50)]   # 6 s sweep
for ms in span:
    eng.request(ms)
    pump(0.016)                                      # ~60 Hz mouse
pump(4.0)
latest = [g for g in got if g[0] == span[-1]]
check(len(got) >= 20, f"{len(got)} frames shown during 50-step sweep")
check(latest, "final position's exact frame delivered")

# 5. memory stays bounded
eng._lock.acquire()
n_frames = sum(len(v) for v in eng._cache.values())
if eng._cache:
    any_gop = next(iter(eng._cache.values()))
    fb = any_gop[0].width() * any_gop[0].height() * 3 if any_gop else 0
else:
    fb = 0
eng._lock.release()
mb = n_frames * fb / 1_048_576
check(mb <= eng.CACHE_MB * 1.2, f"cache {mb:.0f} MB <= cap {eng.CACHE_MB}")
eng.stop()

# 6. UI integration: dragging shows engine frames without touching the player
w = m.Trimmer()
w.show()
w.load(src)
end = time.time() + 30
while not w.scrub.ready and time.time() < end:
    pump(0.1)
check(w.scrub.ready, "engine armed after load (keyframes)")
pump(1.0)
tl = w.timeline
x = tl._ms_to_x(min(15_000, info.duration_ms // 2))
tl.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, 40),
                               Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
check(w._scrubbing, "drag enters scrub mode")
check(w.stack.currentIndex() == 1, "preview switched to frame view")
seeks = []
orig = w.player.setPosition
w.player.setPosition = lambda ms: seeks.append(ms)
for i in range(1, 25):
    tl.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x + i * 6, 40),
                                  Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    pump(0.016)
pump(1.5)
check(not seeks, f"player never seeked during drag ({len(seeks)})")
check(w.frame_label.pixmap() and not w.frame_label.pixmap().isNull(),
      "engine frame visible mid-drag")
tl.mouseReleaseEvent(None)
pump(0.3)
check(not w._scrubbing and len(seeks) == 1 and seeks[0] == w.position,
      f"release: one real seek to {w.position} ({seeks})")
check(w.stack.currentIndex() == 0, "preview back to live playback")
w.player.setPosition = orig
w.close()
pump(0.3)

print("\nSCRUB:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
