"""Real-path updater acceptance: the actual GitHub API and the actual release.

Runs UpdateChecker with APP_VERSION patched to an older version so it must
discover the real v2.3.0 release, download the real MSI from GitHub, and the
bytes must match the local dist build. (v<=2.2.x binaries have no updater,
so in-place update works for 2.3.0+ installs going forward.)
"""
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication  # noqa: E402

import mp4trim as m  # noqa: E402

os.environ.pop("MP4TRIM_UPDATE_FEED", None)
os.environ.pop("MP4TRIM_NO_UPDATE", None)
m.APP_VERSION = "2.3.0"   # pretend we are the previous version

app = QApplication(sys.argv)
got = []
u = m.UpdateChecker(manual=True)
u.update_ready.connect(lambda v, p: got.append(("ready", v, p)))
u.no_update.connect(lambda s: got.append(("none", s)))
u.start()
t0 = time.time()
while not got and time.time() - t0 < 300:
    app.processEvents()
    time.sleep(0.05)
print("result:", got[0][:2] if got else "timeout", f"in {time.time()-t0:.0f}s")
if not got or got[0][0] != "ready":
    sys.exit("FAIL: real GitHub release not detected")
ver, path = got[0][1], Path(got[0][2])
h = hashlib.sha256(path.read_bytes()).hexdigest()
local = Path(__file__).resolve().parent.parent / "dist" / f"mp4trim-{ver}-win64.msi"
h2 = hashlib.sha256(local.read_bytes()).hexdigest()
print(f"downloaded v{ver}: {path.stat().st_size} bytes, sha match: {h == h2}")
sys.exit(0 if ver == "2.4.0" and h == h2 else 1)
