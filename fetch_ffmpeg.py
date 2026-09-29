"""Download the ffmpeg build that gets bundled into the installer.

    python fetch_ffmpeg.py

Puts ffmpeg.exe, ffprobe.exe and their DLLs in ./ffmpeg (git-ignored).
Uses BtbN's GPL shared build of the pinned release line: it has h264_nvenc,
libx264, libx265, zscale and everything else mp4trim calls, at ~200 MB
instead of ~450 MB for the static build. When running from source, mp4trim
also prefers ./ffmpeg over whatever is on PATH.
"""

import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
       "ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip")
DEST = Path(__file__).parent / "ffmpeg"


def main():
    print(f"downloading {URL}")
    data = urllib.request.urlopen(URL, timeout=300).read()
    print(f"  {len(data) / 1e6:.0f} MB")
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            p = Path(name)
            keep = (p.parent.name == "bin" and p.suffix.lower() in (".exe", ".dll")
                    and p.name.lower() != "ffplay.exe") or p.name == "LICENSE.txt"
            if keep and not name.endswith("/"):
                (DEST / p.name).write_bytes(z.read(name))
    names = sorted(p.name for p in DEST.iterdir())
    print("  ->", ", ".join(names))
    if "ffmpeg.exe" not in names or "ffprobe.exe" not in names:
        sys.exit("download did not contain ffmpeg.exe / ffprobe.exe")


if __name__ == "__main__":
    main()
