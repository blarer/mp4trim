"""Simulate a fresh PC for the frozen build.

Runs mp4trim.exe's bundled ffmpeg/ffprobe via the same resolver the app uses,
with PATH reduced to System32 only (no scoop ffmpeg, no Python), and checks:
- the app resolves ffmpeg to the bundled copy
- bundled ffmpeg runs with the bundled DLLs only
- a Discord MP4 export works with NVENC disabled (x264 path, non-NVIDIA PC)
- the frozen exe starts, loads a clip and closes with that stripped PATH

    py -3.12 tests/check_portable.py <build_dir> <clip.mp4>
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

build, clip = Path(sys.argv[1]), sys.argv[2]
fails = []


def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg, flush=True)
    if not c:
        fails.append(msg)


clean_env = {k: v for k, v in os.environ.items()
             if k.upper() not in ("PATH", "PYTHONPATH", "PYTHONHOME")}
clean_env["PATH"] = r"C:\Windows\System32;C:\Windows"
clean_env["MP4TRIM_NO_NVENC"] = "1"

ff = build / "ffmpeg" / "ffmpeg.exe"
r = subprocess.run([str(ff), "-hide_banner", "-version"], env=clean_env,
                   capture_output=True, text=True)
check(r.returncode == 0, f"bundled ffmpeg runs with stripped PATH ({r.stdout.splitlines()[0] if r.stdout else r.stderr[:200]})")
r = subprocess.run(["where", "ffmpeg"], env=clean_env, capture_output=True, text=True)
check(r.returncode != 0, "no ffmpeg on the stripped PATH (so the app must use its own)")

# Run the app's own export code inside the frozen lib, with the stripped env.
probe = f"""
import sys, os
sys.frozen = True
sys.executable = {str(build / 'mp4trim.exe')!r}
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
import mp4trim as m
print('TOOL', m.tool('ffmpeg'))
print('ENC', m.h264_encoder())
info = m.probe({clip!r})
out = os.path.join({tempfile.gettempdir()!r}, 'portable_test.mp4')
p, msg, ok = m.export_discord(lambda a, s, l: m.run_ffmpeg(a, s, l), info,
                              0, 8000, out, 10, False)
print('OK', ok, os.path.getsize(p))
os.remove(p)
"""
env2 = dict(clean_env)
env2["PATH"] = clean_env["PATH"] + ";" + str(Path(sys.executable).parent)
r = subprocess.run([sys.executable, "-c", probe], env=env2, capture_output=True,
                   text=True, encoding="utf-8", errors="replace")
out = r.stdout
print(out.strip() or r.stderr[-800:])
check(f"TOOL {build / 'ffmpeg' / 'ffmpeg.exe'}" in out, "app resolves bundled ffmpeg")
check("ENC libx264" in out, "non-NVIDIA path picks libx264")
ok_line = [l for l in out.splitlines() if l.startswith("OK")]
check(ok_line and ok_line[0].split()[1] == "True" and int(ok_line[0].split()[2]) <= 10e6,
      f"x264 Discord export under 10 MB via bundled ffmpeg ({ok_line})")

# The frozen exe itself, stripped PATH, loading a clip.
p = subprocess.Popen([str(build / "mp4trim.exe"), clip], env=clean_env)
time.sleep(7)
alive = p.poll() is None
check(alive, "frozen exe starts and stays up with stripped PATH")
kids = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={p.pid}' | "
     "Select -Expand ExecutablePath"], capture_output=True, text=True).stdout
if alive:
    p.terminate()
    p.wait(10)
print("  children seen:", kids.strip().replace("\n", " | ") or "(none at sample time)")
check("scoop" not in kids.lower(), "exe never launches the scoop ffmpeg")

print("\nPORTABLE:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
