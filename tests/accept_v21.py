"""Acceptance for v2.1.0 on the INSTALLED app with real OS input.

- launches %LOCALAPPDATA%\\mp4trim\\mp4trim.exe on a temp folder of 3 clips
- real PgDn / PgUp keypresses -> window title changes to the next/prev clip
- real mouse drag along the bar vs. deep below it (SetCursorPos + mouse_event)
- captures the window with the bundled ffmpeg (gdigrab) mid-drag so the
  fine-scrub badge and playhead position can be inspected

    py -3.12 tests/accept_v21.py <clip.mp4> <out_dir>
"""
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

u32 = ctypes.windll.user32
u32.SetProcessDPIAware()
exe = Path(os.environ["LOCALAPPDATA"]) / "mp4trim" / "mp4trim.exe"
ff = exe.parent / "ffmpeg" / "ffmpeg.exe"
out_dir = Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
fails = []


def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg, flush=True)
    if not c:
        fails.append(msg)


work = Path(tempfile.mkdtemp(prefix="mp4trim-a21-"))
for n in ("a clip.mp4", "b clip.mp4", "c clip.mp4"):
    shutil.copy2(sys.argv[1], work / n)

p = subprocess.Popen([str(exe), str(work / "b clip.mp4")])
hwnd = None
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def title(h):
    buf = ctypes.create_unicode_buffer(256)
    u32.GetWindowTextW(h, buf, 256)
    return buf.value


for _ in range(60):
    time.sleep(0.5)
    found = []

    def cb(h, _):
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == p.pid and u32.IsWindowVisible(h) and title(h).startswith("mp4trim"):
            found.append(h)
        return True
    u32.EnumWindows(EnumProc(cb), 0)
    if found and "b clip" in title(found[0]):
        hwnd = found[0]
        break
check(hwnd is not None, f"window up ({hwnd and title(hwnd)})")
time.sleep(6)  # thumbnails + first frame

# foreground
u32.keybd_event(0x12, 0, 0, 0)
u32.SetForegroundWindow(hwnd)
u32.keybd_event(0x12, 0, 2, 0)
time.sleep(0.5)
check(u32.GetForegroundWindow() == hwnd, "foreground")


def key(vk):
    u32.keybd_event(vk, 0, 0, 0)
    u32.keybd_event(vk, 0, 2, 0)


def shot(name):
    subprocess.run([str(ff), "-v", "error", "-y", "-f", "gdigrab", "-i",
                    f"title={title(hwnd)}", "-frames:v", "1",
                    str(out_dir / name)], timeout=30)


# --- real PgDn / PgUp folder navigation ---
key(0x22)  # PgDn
t0 = time.time()
while "c clip" not in title(hwnd) and time.time() - t0 < 15:
    time.sleep(0.3)
check("c clip" in title(hwnd), f"PgDn -> next clip ({title(hwnd)})")
time.sleep(3)
key(0x21)  # PgUp
t0 = time.time()
while "b clip" not in title(hwnd) and time.time() - t0 < 15:
    time.sleep(0.3)
check("b clip" in title(hwnd), f"PgUp -> prev clip ({title(hwnd)})")
time.sleep(5)

# --- real mouse drags on the timeline ---
r = wt.RECT()
u32.GetWindowRect(hwnd, ctypes.byref(r))
bar_y = r.bottom - 172          # mid-height of the timeline bar
x0 = r.left + int((r.right - r.left) * 0.30)


def drag_to(points):
    xs, ys = points[0]
    u32.SetCursorPos(xs, ys)
    time.sleep(0.2)
    u32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    time.sleep(0.2)
    for x, y in points[1:]:
        u32.SetCursorPos(x, y)
        time.sleep(0.03)


# full speed: 250 px along the bar
drag_to([(x0, bar_y)] + [(x0 + d, bar_y) for d in range(10, 251, 10)])
time.sleep(0.4)
shot("11_fullspeed_drag.png")
u32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
time.sleep(0.5)

# fine: press, pull 160 px down, then 250 px right
drag_to([(x0, bar_y)] + [(x0, bar_y + d) for d in range(20, 161, 20)]
        + [(x0 + d, bar_y + 160) for d in range(10, 251, 10)])
time.sleep(0.4)
shot("12_fine_drag.png")
u32.mouse_event(0x0004, 0, 0, 0, 0)
time.sleep(0.5)

u32.PostMessageW(hwnd, 0x0010, 0, 0)
try:
    p.wait(10)
except subprocess.TimeoutExpired:
    p.kill()
shutil.rmtree(work, ignore_errors=True)
print("\nACCEPT21:", "ALL PASS" if not fails else f"{len(fails)} FAILURES",
      "· inspect", out_dir)
sys.exit(1 if fails else 0)
