<div align="center">

<img src="icon.png" width="96" alt="mp4trim icon">

# mp4trim

**Drag two handles. Hit Trim, or Discord MP4. Done.**

A fast MP4 trimmer with live playback, a zoomable thumbnail timeline, and
one-click exports that actually fit Discord's upload limit.

![Python](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/GUI-PySide6-41CD52?logo=qt&logoColor=white)
![ffmpeg](https://img.shields.io/badge/engine-ffmpeg-007808)
![platform](https://img.shields.io/badge/platform-Windows-0078D4)

<img src="docs/screenshot.png" width="820" alt="mp4trim main window">

</div>

## Three ways out

| Button | What it does | Size |
| --- | --- | --- |
| **✂ Trim (lossless)** | Stream copy. Every track and all Dolby Vision / HDR10 metadata survive bit-exact. Takes seconds. | Same bitrate as the source. A 67 Mbps OBS clip is ~8 MB per second. |
| **💬 Discord MP4** | Re-encodes to H.264 (NVENC on NVIDIA GPUs, x264 otherwise) at a bitrate computed from your Discord tier. Checks the real size afterwards and retries lower if it overshot. | Always under the limit you picked. |
| **🎞 GIF** | Palette GIF, steps down size/fps until it fits your tier. | Under the limit, or it tells you to pick a shorter range. |

Every export is copied to the clipboard as a file, so Ctrl+V drops it
straight into Discord. **Show in folder** appears in the status bar.

Pick your tier (Free 10 MB, Nitro Basic 50 MB, Nitro 500 MB) in the
**Discord limit** box. It is remembered between runs.

### How Discord MP4 picks quality

The range label shows the plan live as you drag the handles, for example
`Lossless ≈ 415 MB · Discord 50 MB → 1080p60 · 7.0 Mbps ✓`.

1. Budget = 93% of the limit, minus audio (128 kbps, 96 on the 10 MB tier).
2. Video bitrate = budget ÷ length, never above the source's own bitrate.
3. Highest resolution/fps that still looks clean at that bitrate:
   1440p60 → 1080p60 → 1080p30 → 720p60 → 720p30 → 540p30 → 480p30.
4. Encode, measure, and re-encode lower if the file came out too big.

If a selection is too long to look acceptable even at 480p it warns you
first and tells you the longest length that fits.

Measured on a real 1440p60 AV1 OBS clip (51.5 s, 415 MB lossless):

| Tier | Result |
| --- | --- |
| 10 MB | 9.4 MB, 540p30 |
| 50 MB | 47.3 MB, 1080p60 |
| 500 MB | 439 MB, 1440p60 (source bitrate, nothing to cut) |

## Editing

- **Timeline**: thumbnails, time ruler, green in-handle and red
  out-handle. Scroll to zoom around the cursor (Shift+scroll pans,
  double-click resets). The thin strip underneath is always the whole
  file, with your range and the zoom window on it.
- **Keyframe warning**: lossless cuts can only start on a keyframe. If
  your in-point is between keyframes the label warns how much earlier the
  clip will really start; press **K** to snap to it. Or turn on
  **Options → Frame-accurate trim** (re-encodes, drops Dolby Vision).
- **Playback** uses QtMultimedia (hardware decode + audio). If the system
  decoder fails on a file it switches to ffmpeg-decoded previews
  automatically, so colors are always right. HDR is tone-mapped for
  previews, snapshots, GIFs and Discord MP4s.
- **Snapshot**: grab the current frame at native resolution, drag to
  crop, copy to clipboard or save PNG.
- **Multiple audio tracks**: Discord MP4 uses track 1. **Options → Mix all
  audio tracks** mixes them instead (only turn this on if your tracks are
  different, e.g. game and mic, or the audio doubles in volume).

## Keyboard shortcuts

Everything works with buttons. These are faster. **F1** shows this list.

| Key | Action |
| --- | --- |
| `Space` | play / pause |
| `I` / `O` | set in / out at the playhead |
| `Home` / `End` | jump to in / out |
| `,` / `.` | previous / next frame |
| `Left` / `Right` | 1 s back / forward (`Shift` = 10 s) |
| `K` | snap in-point to keyframe |
| `Z` | reset timeline zoom |
| `Enter` | trim (lossless) |
| `D` | Discord MP4 |
| `G` | GIF |
| `S` | snapshot |
| `Ctrl+O` | open (drag & drop and **File → Open Recent** work too) |

Output names never overwrite anything:
`clip_trim_0m14s-0m36s.mp4`, `clip_discord50mb_0m14s-0m36s.mp4`, then
`… (2).mp4` if that exists.

## Install & run

**From source:**

```
pip install PySide6
python mp4trim.py [file.mp4]
```

Requires **ffmpeg / ffprobe on PATH** (not bundled), e.g.
`winget install Gyan.FFmpeg`. The app tells you if they are missing.

**From the installer:** grab `mp4trim-*.msi` from `dist/` (or build it
below). Installs per-user, no admin, Start Menu shortcut included;
upgrades replace the old version in place.

## Build the MSI

```
pip install cx_Freeze
python setup.py bdist_msi
```

## Checks

These run against real clips (they encode video, so they are not a fast
unit suite):

```
python tests/check_exports.py <clip.mp4> ...   # every tier: size, codec, audio, duration
python tests/ui_smoke.py <clip.mp4>           # drives the window: marks, zoom, export, cancel
python tests/quality_compare.py <clip.mp4>    # side-by-side frame + color tags
python tests/make_screenshot.py <clip.mp4>    # regenerates docs/screenshot.png
```

## Project layout

```
mp4trim.py     the whole app: UI, playback, export planning, ffmpeg wiring
setup.py       cx_Freeze build → exe + MSI
make_icon.py   regenerates icon.ico / icon.png
tests/         real-file export checks, UI smoke test, screenshot renderer
docs/          README images
```
