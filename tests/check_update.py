"""Updater checks: version compare, feed selection, mock-feed download, UI.

    py -3.12 tests/check_update.py
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MP4TRIM_NO_NVENC"] = ""   # irrelevant here
from PySide6.QtWidgets import QApplication  # noqa: E402

import mp4trim as m  # noqa: E402

fails = []


def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg, flush=True)
    if not c:
        fails.append(msg)


# --- version compare ---
check(m._ver_tuple("v2.10.1") > m._ver_tuple("2.9.9"), "2.10.1 > 2.9.9")
check(m._ver_tuple("2.3.0") == (2, 3, 0), "tuple parse")
check(m._ver_tuple("v2.3") < m._ver_tuple("2.3.1"), "2.3 < 2.3.1")

# --- pick_update rules ---
asset = {"name": "mp4trim-9.9.9-win64.msi", "size": 1234,
         "browser_download_url": "http://x/y.msi"}
feed = {"tag_name": "v9.9.9", "assets": [asset], "body": "notes"}
u = m.pick_update(feed, "2.3.0")
check(u and u["version"] == "9.9.9" and u["size"] == 1234, "newer release picked")
check(m.pick_update(feed, "9.9.9") is None, "same version -> no update")
check(m.pick_update(feed, "10.0.0") is None, "older release -> no update")
check(m.pick_update({**feed, "prerelease": True}, "2.3.0") is None,
      "prerelease skipped")
check(m.pick_update({**feed, "assets": []}, "2.3.0") is None,
      "no MSI asset -> no update")
check(m.pick_update({**feed, "assets": [{**asset, "name": "arm64.msi"}]},
                    "2.3.0") is None, "non-win64 asset skipped")

# --- end-to-end against a local feed + HTTP server serving a fake MSI ---
work = Path(tempfile.mkdtemp(prefix="mp4trim-upd-"))
payload = b"MSI" * 5000
(work / "fake.msi").write_bytes(payload)


class H(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        return str(work / "fake.msi")

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
feed_file = work / "feed.json"
feed_file.write_text(json.dumps({
    "tag_name": "v99.0.0",
    "assets": [{"name": "mp4trim-99.0.0-win64.msi", "size": len(payload),
                "browser_download_url": f"http://127.0.0.1:{port}/fake.msi"}],
    "body": "test release",
}), encoding="utf-8")
os.environ["MP4TRIM_UPDATE_FEED"] = str(feed_file)

app = QApplication(sys.argv)
got = []
chk = m.UpdateChecker(manual=True)
chk.update_ready.connect(lambda v, p: got.append((v, p)))
chk.no_update.connect(lambda s: got.append(("none", s)))
chk.start()
end = time.time() + 20
while not got and time.time() < end:
    app.processEvents()
    time.sleep(0.02)
check(got and got[0][0] == "99.0.0", f"update detected ({got})")
dl = Path(got[0][1])
check(dl.exists() and dl.read_bytes() == payload,
      f"MSI downloaded and byte-identical ({dl.stat().st_size} B)")

# corrupted size -> rejected
got.clear()
feed_file.write_text(feed_file.read_text().replace(str(len(payload)),
                                                   str(len(payload) + 7)),
                     encoding="utf-8")
dl.unlink()
chk2 = m.UpdateChecker(manual=True)
chk2.update_ready.connect(lambda v, p: got.append(("ready", p)))
chk2.no_update.connect(lambda s: got.append(("none", s)))
chk2.start()
end = time.time() + 20
while not got and time.time() < end:
    app.processEvents()
    time.sleep(0.02)
check(got and got[0][0] == "none" and "incomplete" in got[0][1],
      f"size mismatch rejected ({got})")

# --- UI: button appears, apply blocked while a job is 'running' ---
os.environ["MP4TRIM_UPDATE_FEED"] = str(feed_file)
feed_file.write_text(feed_file.read_text().replace(str(len(payload) + 7),
                                                   str(len(payload))),
                     encoding="utf-8")
w = m.Trimmer()
w.show()
w.check_updates(manual=False)
end = time.time() + 20
while not w.btn_update.isVisible() and time.time() < end:
    app.processEvents()
    time.sleep(0.02)
check(w.btn_update.isVisible() and "99.0.0" in w.btn_update.text(),
      f"update button shown ({w.btn_update.text()!r})")
launched = []
import mp4trim_updater
mp4trim_updater.launch_update = lambda p: launched.append(p)  # app calls upd.launch_update
# the job-running path shows a modal box; stub it or the test blocks forever
infos = []
m.QMessageBox.information = staticmethod(lambda *a, **k: infos.append(a))
w.job = object()   # pretend an export is running
w.apply_update()
check(not launched and infos, "apply blocked while a job runs (modal stubbed)")
w.job = None
w.apply_update()
check(launched and launched[0] == w._update_msi, "apply launches installer + closes")
end = time.time() + 5
while w.isVisible() and time.time() < end:
    app.processEvents()
    time.sleep(0.02)
check(not w.isVisible(), "window closed for update")

srv.shutdown()
os.environ.pop("MP4TRIM_UPDATE_FEED")
import shutil  # noqa: E402
shutil.rmtree(work, ignore_errors=True)
print("\nUPDATE:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
