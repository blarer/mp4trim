"""v3.0 acceptance on the INSTALLED exe with real OS input.

Launches %LOCALAPPDATA%\\mp4trim\\mp4trim.exe on a temp copy of a real clip,
then drives it with real cursor moves and key events:
  1. move mouse over the window -> screenshot: glass panels visible
  2. hands off for 3.5 s        -> screenshot: panels faded (video-only)
  3. move again, press M        -> mute state only observable via UI; capture
  4. press D                    -> real Discord export appears next to the clip
Captures with the bundled ffmpeg gdigrab so a human can verify the look.
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


work = Path(tempfile.mkdtemp(prefix="mp4trim-a30-"))
clip = work / "glass accept.mp4"
shutil.copy2(sys.argv[1], clip)

p = subprocess.Popen([str(exe), str(clip)])
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def title(h):
    buf = ctypes.create_unicode_buffer(256)
    u32.GetWindowTextW(h, buf, 256)
    return buf.value


hwnd = None
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
    if found and "glass accept" in title(found[0]):
        hwnd = found[0]
        break
check(hwnd is not None, f"window up ({hwnd and title(hwnd)})")
time.sleep(6)   # thumbnails etc.

u32.keybd_event(0x12, 0, 0, 0)
u32.SetForegroundWindow(hwnd)
u32.keybd_event(0x12, 0, 2, 0)
time.sleep(0.4)
check(u32.GetForegroundWindow() == hwnd, "foreground")

r = wt.RECT()
u32.GetWindowRect(hwnd, ctypes.byref(r))
cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2


def shot(name):
    subprocess.run([str(ff), "-v", "error", "-y", "-f", "gdigrab", "-i",
                    f"title={title(hwnd)}", "-frames:v", "1",
                    str(out_dir / name)], timeout=30)


def brightness_bottom(path):
    """Mean luma of the bottom fifth of a png via ffmpeg signalstats."""
    rr = subprocess.run([str(ff), "-v", "error", "-i", str(path), "-vf",
                         "crop=iw:ih/5:0:4*ih/5,signalstats,metadata=print:file=-",
                         "-f", "null", "-"], capture_output=True, text=True, timeout=30)
    for line in rr.stdout.splitlines():
        if "YAVG" in line:
            return float(line.split("=")[-1])
    return -1.0


# 1. real mouse move -> panels in
for dx in range(-120, 121, 24):
    u32.SetCursorPos(cx + dx, cy)
    time.sleep(0.03)
time.sleep(0.5)
shot("31_panels_in.png")
b_in = brightness_bottom(out_dir / "31_panels_in.png")

# 2. park the cursor OUTSIDE the window so idle kicks in cleanly
u32.SetCursorPos(r.right + 60, cy)
time.sleep(3.5)
shot("32_panels_faded.png")
b_out = brightness_bottom(out_dir / "32_panels_faded.png")
# with panels gone the bottom shows (darker letterbox/video) vs glass+thumbnails
print(f"bottom-strip luma: panels {b_in:.1f} vs faded {b_out:.1f}")
check(b_in != b_out and abs(b_in - b_out) > 2.0,
      "bottom chrome visibly changes between active and idle")

# 3. move again -> panels return; capture
u32.SetCursorPos(cx, cy + 100)
for dx in range(0, 80, 16):
    u32.SetCursorPos(cx + dx, cy + 100)
    time.sleep(0.03)
time.sleep(0.5)
shot("33_panels_back.png")
b_back = brightness_bottom(out_dir / "33_panels_back.png")
check(abs(b_back - b_in) < max(3.0, abs(b_in - b_out) / 2),
      f"panels return on activity (luma {b_back:.1f} ~ {b_in:.1f})")


def key(vk):
    u32.keybd_event(vk, 0, 0, 0)
    u32.keybd_event(vk, 0, 2, 0)


# 4. real D keypress -> Discord export lands next to the temp clip
key(0x44)
t0 = time.time()
outp = None
while time.time() - t0 < 180:
    time.sleep(1)
    c = list(work.glob("*discord*mb_*.mp4"))
    busy = subprocess.run(["tasklist", "/fi", "imagename eq ffmpeg.exe"],
                          capture_output=True, text=True).stdout.count("ffmpeg.exe")
    if c and busy <= 0:
        outp = c[0]
        break
check(outp is not None, "D produced a Discord export")
if outp:
    mb = outp.stat().st_size / 1e6
    print(f"export: {outp.name} {mb:.2f} MB in ~{time.time()-t0:.0f}s")
    check(mb <= 50, f"fits 50 MB tier ({mb:.2f})")

u32.PostMessageW(hwnd, 0x0010, 0, 0)
try:
    p.wait(10)
except subprocess.TimeoutExpired:
    p.kill()
shutil.rmtree(work, ignore_errors=True)
print("\nACCEPT30:", "ALL PASS" if not fails else f"{len(fails)} FAILURES",
      "· screenshots in", out_dir)
sys.exit(1 if fails else 0)
