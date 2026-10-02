"""mp4trim - MP4 trimmer with playback, a zoomable thumbnail timeline, and
Discord-ready exports.

Exports
  Trim (lossless)  pure stream copy. Every track and all Dolby Vision / HDR10
                   metadata survive bit-exact. Size = source bitrate x length.
  Discord MP4      re-encodes (H.264, NVENC when available) to a bitrate
                   budget computed from the chosen Discord tier, then verifies
                   the real size and retries lower if it overshoots.
  GIF              palette GIF, steps down size/fps until it fits the tier.

Usage:  mp4trim [file.mp4]
Keys:   Space play/pause   I / O set in/out   Home / End go to in/out
        , / . frame step   Left/Right 1 s (Shift 10 s)   K snap in to keyframe
        Enter trim   D Discord MP4   G GIF   S snapshot   wheel on timeline zoom
"""

# Facade: the implementation lives in mp4trim_core / mp4trim_workers /
# mp4trim_updater / mp4trim_widgets / mp4trim_app. This module re-exports
# everything so `import mp4trim as m` keeps working for tests and tools.

from mp4trim_core import *  # noqa: F401,F403
from mp4trim_core import _rate  # noqa: F401
from mp4trim_workers import (Analyzer, FrameGrabber, Job, ProbeWorker,  # noqa: F401
                             ScrubEngine, SnapGrabber)
from mp4trim_updater import (_ver_tuple, launch_update, pick_update,  # noqa: F401
                             UpdateChecker)
from mp4trim_widgets import (SHORTCUTS_HELP, STYLE, SnapshotDialog,  # noqa: F401
                             SnapshotView, Timeline)
from mp4trim_app import Trimmer, main, selftest  # noqa: F401
from PySide6.QtWidgets import QMessageBox  # noqa: F401  (tests stub it)

if __name__ == "__main__":
    main()
