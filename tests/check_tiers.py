import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mp4trim as m  # noqa: E402

src = sys.argv[1]
info = m.probe(src)
tmp = tempfile.mkdtemp(prefix="mp4trim-tiers-")
fails = 0
for limit in (20, 50, 1000):
    dst = os.path.join(tmp, f"t{limit}.mp4")
    p, msg, ok = m.export_discord(lambda a, s, l: m.run_ffmpeg(a, s, l),
                                  info, 0, info.duration_ms, dst, limit, False)
    size = os.path.getsize(dst) / 1e6
    good = ok and size <= limit
    fails += not good
    print(("PASS" if good else "FAIL"),
          f"{limit} MB tier -> {size:.2f} MB · {msg}")
    os.remove(dst)
os.rmdir(tmp)
sys.exit(1 if fails else 0)
