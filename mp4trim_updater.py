"""Auto-update from GitHub releases. Reads core.APP_VERSION dynamically
so tests can patch mp4trim_core.APP_VERSION."""

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from PySide6.QtCore import QThread, Signal

import mp4trim_core as core

# ---------------------------------------------------------------- updater

def _ver_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def pick_update(feed: dict, current: str) -> dict | None:
    """From a GitHub 'latest release' API response, the MSI to update to.

    Returns {"version", "url", "size", "notes"} or None if current is up to
    date, the release is a draft/prerelease, or it carries no MSI asset.
    """
    if not isinstance(feed, dict) or feed.get("draft") or feed.get("prerelease"):
        return None
    tag = str(feed.get("tag_name") or "")
    if not tag or _ver_tuple(tag) <= _ver_tuple(current):
        return None
    asset = next((a for a in feed.get("assets", ())
                  if str(a.get("name", "")).lower().endswith(".msi")
                  and "win64" in str(a.get("name", "")).lower()), None)
    if not asset:
        return None
    sha = re.search(r"\b[0-9a-f]{64}\b", str(feed.get("body") or ""),
                    re.IGNORECASE)
    return {"version": tag.lstrip("v"),
            "url": asset["browser_download_url"],
            "size": int(asset.get("size") or 0),
            "sha256": sha.group(0).lower() if sha else None,
            "notes": str(feed.get("body") or "")[:2000]}


class UpdateChecker(QThread):
    """Checks GitHub for a newer release and downloads the MSI.

    Runs once at startup (and on demand from the Help menu). Never interrupts:
    when the MSI is downloaded and size-verified it just offers a button.
    MP4TRIM_NO_UPDATE=1 disables it; MP4TRIM_UPDATE_FEED points the check at
    a local JSON file for tests.
    """

    update_ready = Signal(str, str)   # version, path to downloaded MSI
    no_update = Signal(str)           # status message (for manual checks)

    def __init__(self, manual: bool = False):
        super().__init__()
        self.manual = manual

    def run(self):
        import json as _json
        import urllib.request
        feed_src = os.environ.get("MP4TRIM_UPDATE_FEED")
        # an explicit test feed overrides the kill switch (run_all sets it)
        if os.environ.get("MP4TRIM_NO_UPDATE") and not feed_src:
            return
        try:
            if feed_src:
                feed = _json.loads(Path(feed_src).read_text(encoding="utf-8"))
            else:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{core.UPDATE_REPO}/releases/latest",
                    headers={"User-Agent": f"mp4trim/{core.APP_VERSION}",
                             "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    feed = _json.loads(r.read().decode("utf-8"))
            upd = pick_update(feed, core.APP_VERSION)
            if not upd:
                self.no_update.emit(f"mp4trim {core.APP_VERSION} is up to date")
                return
            dst = Path(tempfile.gettempdir()) / f"mp4trim-{upd['version']}-update.msi"
            if not (dst.exists() and upd["size"]
                    and dst.stat().st_size == upd["size"]):
                tmp = dst.with_suffix(".part")
                urllib.request.urlretrieve(upd["url"], tmp)
                if upd["size"] and tmp.stat().st_size != upd["size"]:
                    tmp.unlink(missing_ok=True)
                    self.no_update.emit("Update download was incomplete")
                    return
                tmp.replace(dst)
            if upd.get("sha256"):
                digest = hashlib.sha256(dst.read_bytes()).hexdigest()
                if digest != upd["sha256"]:
                    dst.unlink(missing_ok=True)
                    self.no_update.emit("Update failed verification")
                    return
            self.update_ready.emit(upd["version"], str(dst))
        except Exception as e:  # noqa: BLE001 - updates must never crash the app
            if self.manual:
                self.no_update.emit(f"Update check failed: {e}")


def launch_update(msi_path: str):
    """Hand the MSI to msiexec and exit. Per-user install, silent-ish."""
    subprocess.Popen(
        ["msiexec", "/i", msi_path, "/passive", "/norestart"],
        creationflags=0x00000008)   # DETACHED_PROCESS: survives our exit


