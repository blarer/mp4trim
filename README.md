# mp4trim

Minimal MP4 trimmer with a drag-handle timeline. Built for hybrid
DV/HDR10 files: preview frames are decoded by ffmpeg (no green/purple
tint from Windows decoders) and trimming is a pure stream copy, so
Dolby Vision metadata and all tracks survive untouched.

![timeline] Drag the green bar inward from the start and the red bar
inward from the end, then hit **Trim**. Output lands next to the source
as `name_trim_START-END.mp4`.

## Requirements

- Python 3.11+ with PySide6
- ffmpeg / ffprobe on PATH

## Run

```
python mp4trim.py [file.mp4]
```

Keys: `Left/Right` step 1 s (`Shift` = 10 s), `Enter` trim, `Ctrl+O` open.
Drag & drop also works.

## Notes

- Default trim is stream copy: instant, lossless, but cuts snap to the
  keyframe at/before the in-point (a few seconds of slop possible).
- Options → Frame-accurate re-encodes video with x264 CRF 18 — exact
  cuts, but drops Dolby Vision metadata.

## Build MSI

```
python -m pip install cx_Freeze
python setup.py bdist_msi
```

Installer appears in `dist/`. Installs per-user (no admin) with a Start
Menu shortcut. ffmpeg is not bundled — install it separately.
