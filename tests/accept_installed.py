"""Drive the INSTALLED app from outside via UI Automation-free Win32 input:
launch, find its top-level window by PID, force it foreground, post 'D'
key messages to it, then wait for the Discord MP4 and inspect it."""
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

exe = Path(os.environ["LOCALAPPDATA"]) / "mp4trim" / "mp4trim.exe"
src = sys.argv[1]
work = Path(tempfile.mkdtemp(prefix="mp4trim-accept-"))
clip = work / "rb Sep 23 08.47 PM (2).mp4"
shutil.copy2(src, clip)

u32 = ctypes.windll.user32
p = subprocess.Popen([str(exe), str(clip)])
hwnd = None
EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
for _ in range(40):
    time.sleep(0.5)
    found = []

    def cb(h, _):
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == p.pid and u32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(h, buf, 256)
            if buf.value.startswith("mp4trim"):
                found.append((h, buf.value))
        return True
    u32.EnumWindows(EnumProc(cb), 0)
    if found and "(2).mp4" in found[0][1]:
        hwnd = found[0][0]
        break
print("window:", hwnd and found[0][1])
time.sleep(4)  # let playback prime + analyzer start
# foreground via the Alt trick, then a real keyboard event
u32.keybd_event(0x12, 0, 0, 0)
u32.SetForegroundWindow(hwnd)
u32.keybd_event(0x12, 0, 2, 0)
time.sleep(0.5)
print("foreground ok:", u32.GetForegroundWindow() == hwnd)
u32.keybd_event(0x44, 0, 0, 0)       # 'D'
u32.keybd_event(0x44, 0, 2, 0)
t0 = time.time()
out = None
while time.time() - t0 < 150:
    time.sleep(1)
    c = list(work.glob("*_discord*mb_*.mp4"))
    busy = subprocess.run(["tasklist", "/fi", "imagename eq ffmpeg.exe"],
                          capture_output=True, text=True).stdout.count("ffmpeg.exe")
    if c and not busy:
        out = c[0]
        break
if out:
    info = json.loads(subprocess.run(
        [str(exe.parent / "ffmpeg" / "ffprobe.exe"), "-v", "error",
         "-print_format", "json", "-show_streams", "-show_format", str(out)],
        capture_output=True, text=True).stdout)
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    print(f"output: {out.name}")
    print(f"size: {out.stat().st_size / 1e6:.2f} MB (source {clip.stat().st_size / 1e6:.0f} MB) "
          f"in ~{time.time() - t0:.0f}s, {v['codec_name']} {v['width']}x{v['height']}, "
          f"{len(info['streams']) - 1} audio, {float(info['format']['duration']):.1f}s")
else:
    print("RESULT: no Discord MP4 within 150 s")
u32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
try:
    p.wait(10)
except subprocess.TimeoutExpired:
    p.kill()
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if out else 1)
