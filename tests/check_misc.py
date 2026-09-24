"""Checks the paths the main suites skip: GIF export, frame-accurate trim,
overlapping-handle grab, snapshot grab.

    py -3.12 tests/check_misc.py <clip.mp4>
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication  # noqa: E402

import mp4trim as m  # noqa: E402

fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


labels = []


def run(args, span, label):
    labels.append(label)
    m.run_ffmpeg(args, span, label)


src = sys.argv[1]
info = m.probe(src)
tmp = Path(tempfile.mkdtemp(prefix="mp4trim-misc-"))

# GIF at 10 MB: fits, and pass label counts the ladder actually used
gif = tmp / "a.gif"
path, msg, ok = m.export_gif(run, info, 20_000, 26_000, str(gif), 10)
size = os.path.getsize(gif) / 1e6
check(ok and size <= 10, f"GIF 6 s fits 10 MB ({size:.2f} MB, {msg})")
check(all(f"/{len(m.GIF_LADDER)} " in l for l in labels),
      f"10 MB GIF pass labels use ladder of {len(m.GIF_LADDER)}: {labels}")
labels.clear()
path, msg, ok = m.export_gif(run, info, 20_000, 23_000, str(tmp / "b.gif"), 50)
check(ok and labels[0].startswith(f"GIF pass 1/{len(m.GIF_HQ_LADDER) + len(m.GIF_LADDER)}"),
      f"50 MB GIF label counts HQ+normal ladder ({labels[0]})")

# frame-accurate trim: re-encoded on NVENC, starts exactly at the in-point
acc = tmp / "acc.mp4"
t_in = 12_345  # deliberately between keyframes (GOP is 2 s)
m.export_trim(run, info, t_in, t_in + 3000, str(acc), True)
p = json.loads(subprocess.run(
    ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
     "-show_format", str(acc)], capture_output=True, text=True).stdout)
v = [s for s in p["streams"] if s["codec_type"] == "video"][0]
dur = float(p["format"]["duration"])
check(v["codec_name"] == "h264", f"frame-accurate uses h264 ({v['codec_name']})")
check(abs(dur - 3.0) < 0.1, f"frame-accurate duration exact ({dur:.3f}s)")
check(len([s for s in p["streams"] if s["codec_type"] == "audio"]) == info.audio_tracks,
      "frame-accurate keeps all audio tracks")
lossless = tmp / "ll.mp4"
m.export_trim(run, info, t_in, t_in + 3000, str(lossless), False)
dl = float(json.loads(subprocess.run(
    ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
     str(lossless)], capture_output=True, text=True).stdout)["format"]["duration"])
check(dl > dur + 0.2, f"lossless from mid-GOP really starts early ({dl:.2f}s vs {dur:.2f}s)"
      " -> keyframe warning is warranted")

# overlapping handles: both still grabbable
app = QApplication(sys.argv)
tl = m.Timeline()
tl.resize(1000, 88)
tl.reset(60_000)
tl.mark_in, tl.mark_out = 30_000, 30_100   # 1.7 px apart
x = tl._ms_to_x(30_050)
check(tl._hit(x - 3) == "in" and tl._hit(x + 3) == "out",
      "overlapping handles: left half grabs in, right half grabs out")

# snapshot grab: native resolution
img = m.grab_frame(src, 10_000, native=True, hdr=info.hdr)
check(img is not None and img.width() == info.width,
      f"snapshot native {img.width() if img else None}x{img.height() if img else None}")

for f in tmp.iterdir():
    f.unlink()
tmp.rmdir()
print("\nMISC:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
