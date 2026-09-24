"""Color/quality spot check: export 10 s at 50 MB, print color tags, and write
a side-by-side (source | export) crop of the same frame to %TEMP%\\qcmp.png."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mp4trim as m  # noqa: E402

src = sys.argv[1]
tmp = Path(os.environ["TEMP"])
out = tmp / "q50.mp4"
info = m.probe(src)
print(m.export_discord(lambda a, s, l: m.run_ffmpeg(a, s, l), info, 15000,
                       25000, str(out), 50, False))
print(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=color_range,color_space,"
                      "color_primaries,color_transfer", "-of", "compact",
                      str(out)], capture_output=True, text=True).stdout)
crop = "scale=1920:1080,crop=960:540:480:270"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "20", "-i", src,
                "-frames:v", "1", "-vf", crop, str(tmp / "qsrc.png")], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "5", "-i", str(out),
                "-frames:v", "1", "-vf", crop, str(tmp / "q50.png")], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp / "qsrc.png"),
                "-i", str(tmp / "q50.png"), "-filter_complex", "hstack",
                str(tmp / "qcmp.png")], check=True)
out.unlink()
