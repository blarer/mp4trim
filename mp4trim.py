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

import bisect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import (
    QMimeData, QPoint, QPointF, QRect, QRectF, QSettings, Qt, QThread, QTimer,
    QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QIcon, QImage, QKeySequence, QLinearGradient,
    QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

APP_VERSION = "2.1.0"
# Discord limits are decimal megabytes; using 1e6 keeps us on the safe side.
DISCORD_TIERS = [("Free · 10 MB", 10), ("Nitro Basic · 50 MB", 50),
                 ("Nitro · 500 MB", 500)]
TARGET_FILL = 0.93          # aim for 93% of the limit, leaves room for drift
MIN_VIDEO_KBPS = 300        # below this the result is not worth sending
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts"}

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
BELOW_NORMAL = 0x00004000 if sys.platform == "win32" else 0
HANDLE_GRAB_PX = 12

# HDR (PQ / HLG) -> SDR bt709. Applied after scaling, so it runs on few pixels.
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")


def res_path(name: str) -> Path:
    base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).parent)
    return base / name


@lru_cache(maxsize=None)
def tool(name: str) -> str:
    """Path to ffmpeg/ffprobe. Prefers the copy bundled next to the app
    (installer ships one in ./ffmpeg), then a local ./ffmpeg folder when run
    from source, then PATH. MP4TRIM_USE_PATH=1 skips the bundled copy."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    if not os.environ.get("MP4TRIM_USE_PATH"):
        for base in (res_path("ffmpeg"), res_path("ffmpeg") / "bin"):
            cand = base / exe
            if cand.is_file():
                return str(cand)
    return shutil.which(name) or name


def have_ffmpeg() -> bool:
    return all(Path(tool(n)).is_file() or shutil.which(tool(n))
               for n in ("ffmpeg", "ffprobe"))


# --------------------------------------------------------------- formatting

def fmt_ms(ms: int) -> str:
    s, ms = divmod(max(0, int(ms)), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_dur(ms: float) -> str:
    """Short human duration: 7.4s, 1:05, 1:02:03."""
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    s = int(ms // 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_mb(mb: float) -> str:
    if mb >= 1000:
        return f"{mb / 1000:.2f} GB"
    return f"{mb:.1f} MB" if mb < 10 else f"{mb:.0f} MB"


def fmt_tag(ms: int) -> str:
    """Filename-safe timestamp: 9m08s."""
    s = int(ms) // 1000
    return f"{s // 60}m{s % 60:02d}s"


def unique_path(p: Path) -> Path:
    """p, or 'name (2).ext', 'name (3).ext' ... whichever does not exist."""
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def output_path(src: str, kind: str, t_in: int, t_out: int, ext: str) -> Path:
    s = Path(src)
    return unique_path(
        s.with_name(f"{s.stem}_{kind}_{fmt_tag(t_in)}-{fmt_tag(t_out)}{ext}"))


def copy_file_to_clipboard(path: str):
    """Put the file itself on the clipboard (paste into Discord uploads it)."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    QApplication.clipboard().setMimeData(mime)


def reveal_in_explorer(path: str):
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(Path(path))])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


# -------------------------------------------------------------------- probe

@dataclass
class MediaInfo:
    path: str
    duration_ms: int
    start_s: float
    width: int
    height: int
    fps: float
    v_codec: str
    pix_fmt: str
    v_kbps: float
    total_kbps: float
    audio_tracks: int
    hdr: bool

    @property
    def ten_bit(self) -> bool:
        return "10" in self.pix_fmt or "p010" in self.pix_fmt


def _rate(s: str | None) -> float:
    try:
        n, d = (s or "0/1").split("/")
        return float(n) / float(d) if float(d) else 0.0
    except ValueError:
        return 0.0


def probe(path: str) -> MediaInfo:
    out = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if out.returncode != 0:
        raise ValueError(out.stderr.strip() or "ffprobe failed")
    data = json.loads(out.stdout)
    fmt, streams = data.get("format", {}), data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"
             and not s.get("disposition", {}).get("attached_pic")]
    if not video:
        raise ValueError("no video stream")
    v = video[0]
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise ValueError("could not read duration")
    size = int(fmt.get("size") or os.path.getsize(path))
    total_kbps = float(fmt.get("bit_rate") or size * 8 / dur) / 1000
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    a_kbps = sum(float(s.get("bit_rate") or 0) for s in audio) / 1000
    v_kbps = float(v.get("bit_rate") or 0) / 1000
    if v_kbps <= 0:
        v_kbps = max(total_kbps - a_kbps, total_kbps * 0.9)
    fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))
    return MediaInfo(
        path=path, duration_ms=int(dur * 1000),
        start_s=float(fmt.get("start_time") or 0),
        width=int(v.get("width") or 0), height=int(v.get("height") or 0),
        fps=fps, v_codec=v.get("codec_name", "?"),
        pix_fmt=v.get("pix_fmt", ""), v_kbps=v_kbps, total_kbps=total_kbps,
        audio_tracks=len(audio),
        hdr=v.get("color_transfer") in ("smpte2084", "arib-std-b67"),
    )


def probe_keyframes(path: str, start_s: float) -> list[int]:
    """Keyframe times in ms relative to the file start (the trim timeline)."""
    out = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW | BELOW_NORMAL,
    )
    kfs = []
    for line in out.stdout.splitlines():
        pts, _, flags = line.partition(",")
        if "K" in flags:
            try:
                kfs.append(max(0, int(round((float(pts) - start_s) * 1000))))
            except ValueError:
                pass
    return sorted(set(kfs))


def keyframe_before(kfs: list[int], ms: int) -> int | None:
    i = bisect.bisect_right(kfs, ms)
    return kfs[i - 1] if i else None


def grab_frame(path: str, ms: int, native: bool = False,
               hdr: bool = False) -> QImage | None:
    vf = [] if native else ["scale=960:-2"]
    if hdr:
        vf += [TONEMAP, "format=yuv420p"]
    out = subprocess.run(
        [tool("ffmpeg"), "-v", "quiet", "-ss", f"{ms / 1000:.3f}", "-i", path,
         "-frames:v", "1", *(["-vf", ",".join(vf)] if vf else []),
         "-f", "image2pipe", "-vcodec", "bmp", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )
    if not out.stdout:
        return None
    img = QImage.fromData(out.stdout, "BMP")
    return None if img.isNull() else img


def grab_thumb(path: str, ms: int, hdr: bool, height: int = 56) -> QImage | None:
    """Fast keyframe-only thumbnail; low priority so it never fights a game."""
    vf = f"scale=-2:{height}" + (f",{TONEMAP},format=yuv420p" if hdr else "")
    out = subprocess.run(
        [tool("ffmpeg"), "-v", "quiet", "-skip_frame", "nokey", "-noaccurate_seek",
         "-ss", f"{ms / 1000:.3f}", "-i", path, "-frames:v", "1",
         "-vf", vf, "-threads", "2", "-f", "image2pipe", "-vcodec", "bmp", "-"],
        capture_output=True, creationflags=CREATE_NO_WINDOW | BELOW_NORMAL,
    )
    if not out.stdout:
        return None
    img = QImage.fromData(out.stdout, "BMP")
    return None if img.isNull() else img


# ------------------------------------------------------- export planning

@dataclass
class DiscordPlan:
    height: int
    fps: int
    v_kbps: int
    a_kbps: int
    ok: bool            # enough bitrate for a watchable result
    max_keep_s: float   # longest selection that still fits at minimum quality


# (short side, fps, video kbps needed for that rung to look good)
RUNGS = [(1440, 60, 14000), (1080, 60, 6500), (1080, 30, 4200),
         (720, 60, 3200), (720, 30, 1800), (540, 30, 1000), (480, 30, 0)]


def audio_kbps_for(limit_mb: float, tracks: int) -> int:
    if tracks == 0:
        return 0
    return 96 if limit_mb <= 10 else 128


def plan_discord(keep_ms: int, limit_mb: float, info: MediaInfo) -> DiscordPlan:
    keep_s = max(keep_ms, 100) / 1000
    a = audio_kbps_for(limit_mb, info.audio_tracks)
    budget = limit_mb * 1e6 * 8 * TARGET_FILL / 1000   # kbit available
    v = budget * 0.985 / keep_s - a                      # ~1.5% mux overhead
    v = min(v, info.v_kbps)                              # never inflate
    short = min(info.width, info.height) or 1080
    src_fps = round(info.fps) if info.fps else 60
    cands: dict[tuple[int, int], float] = {}
    for h, f, need in RUNGS:
        hh = min(h, short) // 2 * 2
        ff = min(f, src_fps)
        need = need * (hh / h) ** 2 * (ff / f)
        cands[(hh, ff)] = min(cands.get((hh, ff), need), need)
    height, fps = next(((h, f) for (h, f), need in cands.items() if v >= need),
                       list(cands)[-1])
    return DiscordPlan(
        height=height, fps=fps, v_kbps=int(max(v, 100)), a_kbps=a,
        ok=v >= MIN_VIDEO_KBPS,
        max_keep_s=budget * 0.985 / (MIN_VIDEO_KBPS + a),
    )


def lossless_mb(keep_ms: int, info: MediaInfo) -> float:
    return info.total_kbps * keep_ms / 1000 / 8 / 1000


# Options each encoder is used with. The probe runs these exact options, so an
# older ffmpeg or GPU driver that rejects one (e.g. -multipass, -tune hq)
# falls back to the CPU encoder instead of failing the export.
PROBE_OPTS = {
    "h264_nvenc": ["-preset", "p6", "-tune", "hq", "-rc", "vbr",
                   "-multipass", "fullres", "-b:v", "2000k", "-maxrate",
                   "2600k", "-bufsize", "4000k", "-spatial-aq", "1",
                   "-rc-lookahead", "32", "-profile:v", "high"],
    "hevc_nvenc": ["-preset", "p6", "-rc", "vbr", "-cq", "18", "-b:v", "0",
                   "-profile:v", "main10", "-pix_fmt", "p010le"],
}


@lru_cache(maxsize=None)
def encoder_works(codec: str) -> bool:
    if os.environ.get("MP4TRIM_NO_NVENC") and "nvenc" in codec:
        return False
    try:
        r = subprocess.run(
            [tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
             "color=s=640x360:r=30:d=0.5", "-c:v", codec,
             *PROBE_OPTS.get(codec, []), "-f", "null", "-"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def h264_encoder() -> str:
    return "h264_nvenc" if encoder_works("h264_nvenc") else "libx264"


def video_chain(info: MediaInfo, height: int, fps: int) -> str:
    f = []
    if fps and info.fps and fps < info.fps - 0.5:
        f.append(f"fps={fps}")
    if height < min(info.width, info.height):
        f.append(f"scale=-2:{height}:flags=lanczos" if info.width >= info.height
                 else f"scale={height}:-2:flags=lanczos")
    if info.hdr:
        f.append(TONEMAP)
    f.append("format=yuv420p")
    return ",".join(f)


def discord_args(info: MediaInfo, t_in: int, t_out: int, dst: str,
                 height: int, fps: int, v_kbps: int, a_kbps: int,
                 mix_audio: bool, encoder: str) -> list[str]:
    graph = f"[0:v:0]{video_chain(info, height, fps)}[v]"
    amap: list[str] = []
    if info.audio_tracks and a_kbps:
        if mix_audio and info.audio_tracks > 1:
            ins = "".join(f"[0:a:{i}]" for i in range(info.audio_tracks))
            graph += f";{ins}amix=inputs={info.audio_tracks}:duration=longest[a]"
            amap = ["-map", "[a]"]
        else:
            amap = ["-map", "0:a:0"]
    args = ["-ss", f"{t_in / 1000:.3f}", "-to", f"{t_out / 1000:.3f}",
            "-i", info.path, "-filter_complex", graph, "-map", "[v]", *amap]
    rate = ["-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.3)}k",
            "-bufsize", f"{v_kbps * 2}k"]
    if encoder == "h264_nvenc":
        args += ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
                 "-rc", "vbr", "-multipass", "fullres", *rate,
                 "-spatial-aq", "1", "-rc-lookahead", "32"]
    else:
        args += ["-c:v", "libx264", "-preset", "medium", *rate]
    args += ["-profile:v", "high"]
    # Always tag BT.709. Untagged H.264 is treated as BT.601 by many players
    # (Discord included), which shifts colors and looks darker/washed out.
    args += ["-colorspace", "bt709", "-color_primaries", "bt709",
             "-color_trc", "bt709", "-color_range", "tv"]
    args += (["-c:a", "aac", "-b:a", f"{a_kbps}k", "-ac", "2"] if amap
             else ["-an"])
    args += ["-sn", "-dn", "-movflags", "+faststart", dst]
    return args


def trim_args(info: MediaInfo, t_in: int, t_out: int, dst: str,
              accurate: bool) -> list[str]:
    args = ["-ss", f"{t_in / 1000:.3f}", "-to", f"{t_out / 1000:.3f}",
            "-i", info.path, "-map", "0"]
    if not accurate:
        return args + ["-c", "copy", "-avoid_negative_ts", "make_zero", dst]
    if info.ten_bit or info.hdr:
        if encoder_works("hevc_nvenc"):
            v = ["-c:v", "hevc_nvenc", "-preset", "p6", "-rc", "vbr",
                 "-cq", "18", "-b:v", "0", "-profile:v", "main10",
                 "-pix_fmt", "p010le"]
        else:
            v = ["-c:v", "libx265", "-crf", "18", "-preset", "medium",
                 "-pix_fmt", "yuv420p10le"]
        v += ["-tag:v", "hvc1"]
    elif encoder_works("h264_nvenc"):
        v = ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr",
             "-cq", "18", "-b:v", "0", "-profile:v", "high"]
    else:
        v = ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
    return args + v + ["-c:a", "copy", "-c:s", "copy", dst]


GIF_LADDER = [(480, 20), (480, 15), (400, 15), (360, 12),
              (320, 12), (280, 10), (240, 10)]
GIF_HQ_LADDER = [(720, 24), (640, 24), (560, 20)]


def gif_args(info: MediaInfo, t_in: int, t_out: int, dst: str,
             width: int, fps: int) -> list[str]:
    width = min(width, info.width or width)
    pre = f"fps={fps},scale={width}:-2:flags=lanczos"
    if info.hdr:
        pre += f",{TONEMAP},format=yuv420p"
    flt = (f"[0:v:0] {pre},split [a][b];[a] palettegen=stats_mode=diff [p];"
           f"[b][p] paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    return ["-ss", f"{t_in / 1000:.3f}", "-to", f"{t_out / 1000:.3f}",
            "-i", info.path, "-filter_complex", flt, "-loop", "0", dst]


# ------------------------------------------------------------ export jobs
# Each export is a function(run) -> (path, message, ok). `run` executes one
# ffmpeg command with progress; tests pass a plain runner, the UI a Job.

def export_discord(run, info: MediaInfo, t_in: int, t_out: int, dst: str,
                   limit_mb: float, mix_audio: bool):
    plan = plan_discord(t_out - t_in, limit_mb, info)
    enc = h264_encoder()
    limit_bytes = limit_mb * 1e6
    v = plan.v_kbps
    size = 0
    for attempt in range(3):
        label = (f"Discord MP4 · {plan.height}p{plan.fps} · {v / 1000:.1f} Mbps"
                 + (f" · retry {attempt}" if attempt else ""))
        run(discord_args(info, t_in, t_out, dst, plan.height, plan.fps, v,
                         plan.a_kbps, mix_audio, enc),
            (t_out - t_in) / 1000, label)
        size = os.path.getsize(dst)
        if size <= limit_bytes:
            return (dst, f"Discord MP4 saved + copied · {size / 1e6:.1f} MB · "
                    f"{plan.height}p{plan.fps}", True)
        v = max(100, int(v * limit_bytes * TARGET_FILL / size))
    return (dst, f"Discord MP4 is {size / 1e6:.1f} MB, still over the "
            f"{limit_mb:g} MB limit. Pick a shorter range.", False)


def export_gif(run, info: MediaInfo, t_in: int, t_out: int, dst: str,
               limit_mb: float):
    ladder = (GIF_HQ_LADDER + GIF_LADDER) if limit_mb > 10 else GIF_LADDER
    limit_bytes = limit_mb * 1e6
    size = 0
    for i, (width, fps) in enumerate(ladder, 1):
        run(gif_args(info, t_in, t_out, dst, width, fps),
            (t_out - t_in) / 1000,
            f"GIF pass {i}/{len(ladder)} · {width}px {fps}fps")
        size = os.path.getsize(dst)
        if size <= limit_bytes:
            return (dst, f"GIF saved + copied · {size / 1e6:.1f} MB · "
                    f"{width}px {fps}fps", True)
    return (dst, f"GIF is {size / 1e6:.1f} MB even at lowest quality, over the "
            f"{limit_mb:g} MB limit. Pick a shorter range.", False)


def export_trim(run, info: MediaInfo, t_in: int, t_out: int, dst: str,
                accurate: bool):
    run(trim_args(info, t_in, t_out, dst, accurate), (t_out - t_in) / 1000,
        "Re-encoding (frame-accurate)" if accurate else "Trimming (lossless)")
    size = os.path.getsize(dst)
    return dst, f"Trimmed + copied · {fmt_mb(size / 1e6)}", True


class Cancelled(Exception):
    pass


def run_ffmpeg(args: list[str], span_s: float, label: str,
               on_progress=None, on_start=None, is_cancelled=lambda: False):
    """Run ffmpeg with machine-readable progress. Raises on failure."""
    if is_cancelled():
        raise Cancelled
    cmd = [tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error",
           "-progress", "pipe:1", "-nostats", *args]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err, text=True,
            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        if on_start:
            on_start(proc)
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key == "out_time_us" and on_progress:
                try:
                    frac = int(val) / 1e6 / max(span_s, 0.001)
                except ValueError:
                    continue
                on_progress(min(max(frac, 0.0), 1.0), label)
        rc = proc.wait()
        if is_cancelled():
            raise Cancelled
        if rc != 0:
            err.seek(0)
            msg = err.read().decode(errors="replace").strip()
            raise RuntimeError(msg[-1500:] or f"ffmpeg exited with {rc}")


class Job(QThread):
    progress = Signal(float, str)
    succeeded = Signal(str, str, bool)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn, dst: str):
        super().__init__()
        self.fn, self.dst = fn, dst
        self._cancel = False
        self._proc: subprocess.Popen | None = None

    def cancel(self):
        self._cancel = True
        p = self._proc
        if p and p.poll() is None:
            p.kill()

    def _run(self, args, span_s, label):
        run_ffmpeg(args, span_s, label, on_progress=self.progress.emit,
                   on_start=lambda p: setattr(self, "_proc", p),
                   is_cancelled=lambda: self._cancel)

    def _drop_partial(self):
        try:
            Path(self.dst).unlink(missing_ok=True)
        except OSError:
            pass

    def run(self):
        try:
            path, msg, ok = self.fn(self._run)
            self.succeeded.emit(path, msg, ok)
        except Cancelled:
            self._drop_partial()
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 - surface anything to the user
            self._drop_partial()
            self.failed.emit(str(e))


# --------------------------------------------------------- background work

class Analyzer(QThread):
    """Keyframe index + timeline thumbnails for one file."""

    keyframes = Signal(int, list)
    thumb = Signal(int, int, QImage)

    def __init__(self, gen: int, info: MediaInfo, count: int = 40):
        super().__init__()
        self.gen, self.info, self.count = gen, info, count
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        info = self.info
        try:
            kfs = probe_keyframes(info.path, info.start_s)
        except Exception:  # noqa: BLE001
            kfs = []
        if self._stop:
            return
        self.keyframes.emit(self.gen, kfs)
        times = [int(info.duration_ms * (i + 0.5) / self.count)
                 for i in range(self.count)]
        ex = ThreadPoolExecutor(max_workers=4)
        futs = {ex.submit(grab_thumb, info.path, t, info.hdr): t for t in times}
        for f in as_completed(futs):
            if self._stop:
                break
            img = f.result()
            if img is not None:
                self.thumb.emit(self.gen, futs[f], img)
        ex.shutdown(wait=True, cancel_futures=True)


class FrameGrabber(QThread):
    """Serves ffmpeg preview frames off the UI thread, newest request wins."""

    ready = Signal(QImage)

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._evt = threading.Event()
        self._req = None
        self._stop = False

    def request(self, path: str, ms: int, hdr: bool):
        with self._lock:
            self._req = (path, ms, hdr)
        self._evt.set()

    def stop(self):
        self._stop = True
        self._evt.set()
        self.wait(3000)

    def run(self):
        while not self._stop:
            self._evt.wait()
            self._evt.clear()
            with self._lock:
                req, self._req = self._req, None
            if req and not self._stop:
                img = grab_frame(req[0], req[1], hdr=req[2])
                if img is not None:
                    self.ready.emit(img)


# ------------------------------------------------------------------- style

STYLE = """
QMainWindow, QDialog { background: #000000; }
QWidget { color: #e8e8ea; font-size: 10pt; }
QMenuBar { background: #000000; padding: 2px; border-bottom: 1px solid #131318; }
QMenuBar::item { padding: 5px 12px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: #1a1a20; }
QMenu { background: #0a0a0d; border: 1px solid #232329; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 28px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #2e7d32; }
QMenu::item:disabled { color: #55555c; }
QMenu::separator { height: 1px; background: #232329; margin: 6px 8px; }
QPushButton {
    background: #101014; border: 1px solid #26262c; border-radius: 8px;
    padding: 7px 14px; font-weight: 600;
}
QPushButton:hover { background: #1a1a20; border-color: #3a3a44; }
QPushButton:pressed { background: #08080a; }
QPushButton:disabled { color: #55555c; background: #0a0a0d; border-color: #18181d; }
QPushButton#accent { background: #2e7d32; border-color: #3a9440; color: #f2fff2; }
QPushButton#accent:hover { background: #37953c; }
QPushButton#accent:disabled { background: #122415; color: #5c7a60; }
QPushButton#discord { background: #4752c4; border-color: #5865f2; color: #f0f2ff; }
QPushButton#discord:hover { background: #5865f2; }
QPushButton#discord:disabled { background: #181b3a; color: #5c6190; }
QPushButton#tp { min-width: 36px; max-width: 36px; padding: 6px 0; font-size: 11pt; }
QPushButton#nav { min-width: 30px; max-width: 30px; padding: 6px 0; font-size: 11pt; }
QPushButton#play {
    min-width: 44px; max-width: 44px; min-height: 30px; padding: 4px 0;
    font-size: 13pt; border-radius: 19px; background: #e8e8ea; color: #000000;
    border: none;
}
QPushButton#play:hover { background: #ffffff; }
QPushButton#play:disabled { background: #1d1d23; color: #55555c; }
QPushButton#mark { padding: 6px 10px; font-weight: 600; }
QPushButton#link {
    background: transparent; border: none; color: #8ab4f8; padding: 2px 6px;
}
QPushButton#link:hover { text-decoration: underline; }
QPushButton#cancel { padding: 2px 10px; }
QComboBox {
    background: #101014; border: 1px solid #26262c; border-radius: 8px;
    padding: 6px 10px; min-width: 150px; font-weight: 600;
}
QComboBox:hover { border-color: #3a3a44; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #0a0a0d; border: 1px solid #232329; outline: none;
    selection-background-color: #4752c4; padding: 4px;
}
QProgressBar {
    background: #101014; border: none; border-radius: 4px;
    max-height: 8px; min-width: 180px;
}
QProgressBar::chunk { background: #5865f2; border-radius: 4px; }
QToolTip { background: #0a0a0d; color: #e8e8ea; border: 1px solid #232329; padding: 4px; }
QLabel#time { font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 13pt; color: #f0f0f2; }
QLabel#dur { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #6a6a74; }
QLabel#range { font-family: 'Cascadia Mono', 'Consolas', monospace; color: #8fd694; }
QLabel#est { color: #a0a0a8; }
QLabel#muted { color: #8a8a92; }
QLabel#drop { color: #6a6a74; font-size: 13pt; background: #000000; }
QStatusBar { background: #000000; color: #8a8a92; border-top: 1px solid #131318; }
QStatusBar::item { border: none; }
"""

SHORTCUTS_HELP = """<table cellspacing=6>
<tr><td><b>Space</b></td><td>play / pause</td></tr>
<tr><td><b>PgUp</b> / <b>PgDn</b></td><td>previous / next video in the folder</td></tr>
<tr><td><b>I</b> / <b>O</b></td><td>set in / out at the playhead</td></tr>
<tr><td><b>Home</b> / <b>End</b></td><td>jump to in / out</td></tr>
<tr><td><b>,</b> / <b>.</b></td><td>previous / next frame</td></tr>
<tr><td><b>Left</b> / <b>Right</b></td><td>1 s back / forward (Shift = 10 s)</td></tr>
<tr><td><b>K</b></td><td>snap in-point to the keyframe (exact lossless start)</td></tr>
<tr><td><b>Mouse wheel</b></td><td>zoom the timeline (Shift+wheel pans, double-click resets)</td></tr>
<tr><td><b>Drag below the timeline</b></td><td>fine scrubbing: further down = slower (½, ¼, fine)</td></tr>
<tr><td><b>Enter</b></td><td>trim (lossless)</td></tr>
<tr><td><b>D</b></td><td>export Discord MP4</td></tr>
<tr><td><b>G</b></td><td>export GIF</td></tr>
<tr><td><b>S</b></td><td>snapshot / crop current frame</td></tr>
<tr><td><b>Ctrl+O</b></td><td>open (drag &amp; drop works too)</td></tr>
</table>"""


# ---------------------------------------------------------------- timeline

class Timeline(QWidget):
    """Zoomable timeline: thumbnails, ruler, draggable in/out handles.

    Drag the green handle right to cut the beginning, the red handle left to
    cut the end. Click elsewhere to scrub. Wheel zooms around the cursor,
    Shift+wheel pans, double-click resets the zoom.
    """

    seeked = Signal(int)
    range_changed = Signal()

    RULER = 18
    OVERVIEW = 8
    # Apple-style fine scrubbing: dragging further below the bar drops the
    # horizontal sensitivity through tiers. (min px below bar, rate, label)
    SCRUB_TIERS = [(140, 0.05, "fine scrubbing"), (90, 0.25, "¼ speed"),
                   (40, 0.5, "½ speed"), (0, 1.0, "")]

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(88)
        self.setMouseTracking(True)
        self.reset(0)

    def reset(self, duration: int):
        self.duration = duration
        self.position = 0
        self.mark_in = 0
        self.mark_out = duration
        self.view0, self.view1 = 0, duration
        self._thumb_ms: list[int] = []
        self._thumb_img: list[QImage] = []
        self._drag = None   # None | "in" | "out" | "seek"
        self._hover = None  # None | "in" | "out"
        self._rate = 1.0            # active scrub sensitivity
        self._anchor_x = 0.0        # x where the current rate segment began
        self._anchor_ms = 0.0       # target value at that x
        self._last_x = 0.0          # previous mouse x during a drag
        self.update()

    def add_thumb(self, ms: int, img: QImage):
        i = bisect.bisect(self._thumb_ms, ms)
        self._thumb_ms.insert(i, ms)
        self._thumb_img.insert(i, img)
        self.update()

    # --- coordinates ---
    @property
    def zoomed(self) -> bool:
        return self.duration > 0 and (self.view1 - self.view0) < self.duration

    def _span(self) -> float:
        return max(self.view1 - self.view0, 1)

    def _x_to_ms(self, x: float) -> int:
        if self.duration <= 0 or self.width() <= 0:
            return 0
        frac = min(max(x / self.width(), 0.0), 1.0)
        return int(self.view0 + frac * self._span())

    def _ms_to_x(self, ms: float) -> float:
        return (ms - self.view0) / self._span() * self.width()

    def _set_view(self, v0: float, span: float):
        span = min(max(span, min(1500, self.duration)), self.duration)
        v0 = min(max(v0, 0), self.duration - span)
        self.view0, self.view1 = int(v0), int(v0 + span)
        self.update()

    def reset_zoom(self):
        self._set_view(0, self.duration)

    def follow(self, ms: int):
        """Keep the playhead visible while zoomed."""
        if self.zoomed and not (self.view0 <= ms <= self.view1):
            self._set_view(ms - self._span() * 0.1, self._span())

    # --- interaction ---
    def _hit(self, x: float) -> str:
        if self.duration <= 0:
            return "seek"
        xi, xo = self._ms_to_x(self.mark_in), self._ms_to_x(self.mark_out)
        di, do = abs(x - xi), abs(x - xo)
        if di <= HANDLE_GRAB_PX and do <= HANDLE_GRAB_PX:
            # handles overlap: left half grabs in, right half grabs out
            return "in" if x <= (xi + xo) / 2 else "out"
        if di <= HANDLE_GRAB_PX:
            return "in"
        if do <= HANDLE_GRAB_PX:
            return "out"
        return "seek"

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._drag = self._hit(e.position().x())
        self._apply_drag(e.position().x())
        # anchor for fine scrubbing at the (possibly jumped-to) value
        self._rate = 1.0
        self._anchor_x = e.position().x()
        self._last_x = e.position().x()
        self._anchor_ms = float(self._drag_value())

    def mouseDoubleClickEvent(self, e):
        if self._hit(e.position().x()) == "seek":
            self.reset_zoom()

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag:
            self._fine_drag(x, e.position().y())
        else:
            over = self._hit(x)
            hover = over if over in ("in", "out") else None
            if hover != self._hover:
                self._hover = hover
                self.update()
            self.setCursor(Qt.SizeHorCursor if hover else Qt.PointingHandCursor)
            if self.duration > 0:
                self.setToolTip(fmt_ms(self._x_to_ms(x)))

    def leaveEvent(self, _):
        if self._hover:
            self._hover = None
            self.update()

    def mouseReleaseEvent(self, _):
        self._drag = None
        self._rate = 1.0
        self.update()

    def wheelEvent(self, e):
        if self.duration <= 0:
            return
        d = e.angleDelta()
        steps = (d.y() or d.x()) / 120
        span = self._span()
        if e.modifiers() & Qt.ShiftModifier or (d.x() and not d.y()):
            self._set_view(self.view0 - steps * span * 0.15, span)
        else:
            anchor = self._x_to_ms(e.position().x())
            ratio = (anchor - self.view0) / span
            new_span = span * (0.8 ** steps)
            new_span = min(max(new_span, min(1500, self.duration)), self.duration)
            self._set_view(anchor - ratio * new_span, new_span)
        e.accept()

    def _apply_drag(self, x: float):
        ms = self._x_to_ms(x)
        self._set_drag_value(ms)

    def _drag_value(self) -> int:
        return {"in": self.mark_in, "out": self.mark_out,
                "seek": self.position}.get(self._drag, self.position)

    def _set_drag_value(self, ms: float):
        ms = int(ms)
        if self._drag == "in":
            self.mark_in = max(0, min(ms, self.mark_out - 100))
            self.range_changed.emit()
            self.seeked.emit(self.mark_in)
        elif self._drag == "out":
            self.mark_out = min(self.duration, max(ms, self.mark_in + 100))
            self.range_changed.emit()
            self.seeked.emit(self.mark_out)
        else:
            self.seeked.emit(max(0, min(ms, self.duration)))
        self.update()

    def _rate_for(self, y: float) -> tuple[float, str]:
        """Scrub sensitivity from vertical distance below the bar."""
        below = y - (self.height() - self.OVERVIEW)
        for min_px, rate, label in self.SCRUB_TIERS:
            if below >= min_px:
                return rate, label
        return 1.0, ""

    def _fine_drag(self, x: float, y: float):
        """Relative scrubbing, Apple style: T = T_anchor + ΔX·rate·(ms/px).

        On a tier change the anchor rebases at the previous x, so motion in
        the same event still counts at the new rate and returning to full
        speed never snaps the value to the absolute cursor position.
        """
        rate, _ = self._rate_for(y)
        if rate != self._rate:
            self._anchor_x = self._last_x
            self._anchor_ms = float(self._drag_value())
            self._rate = rate
        ms_per_px = self._span() / max(self.width(), 1)
        self._set_drag_value(self._anchor_ms + (x - self._anchor_x)
                             * ms_per_px * rate)
        self._last_x = x
    # --- painting ---
    def _draw_handle(self, p: QPainter, x: float, bar: QRectF, color: str,
                     active: bool):
        c = QColor(color)
        if active:
            c = c.lighter(125)
        p.fillRect(QRectF(x - 1.5, bar.top() - 3, 3, bar.height() + 6), c)
        tab = QRectF(x - 7, bar.center().y() - 14, 14, 28)
        path = QPainterPath()
        path.addRoundedRect(tab, 5, 5)
        p.fillPath(path, c)
        p.setPen(QColor(0, 0, 0, 110))
        cy = bar.center().y()
        for dy in (-5, 0, 5):
            p.drawLine(QPointF(x - 2.5, cy + dy), QPointF(x + 2.5, cy + dy))
        p.setPen(Qt.NoPen)

    def _draw_ruler(self, p: QPainter, w: int):
        span = self._span()
        px_per_ms = w / span
        steps = [100, 200, 500, 1000, 2000, 5000, 10_000, 15_000, 30_000,
                 60_000, 120_000, 300_000, 600_000, 1_800_000, 3_600_000]
        step = next((s for s in steps if s * px_per_ms >= 80), steps[-1])
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        t = (self.view0 // step + 1) * step if self.view0 % step else self.view0
        while t <= self.view1:
            x = self._ms_to_x(t)
            p.setPen(QColor("#4a4a52"))
            p.drawLine(QPointF(x, self.RULER - 5), QPointF(x, self.RULER - 1))
            s, ms = divmod(int(t), 1000)
            m, s = divmod(s, 60)
            h, m = divmod(m, 60)
            label = (f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")
            if step < 1000:
                label += f".{ms // 100}"
            p.setPen(QColor("#7a7a82"))
            p.drawText(QPointF(x + 3, self.RULER - 6), label)
            t += step
        p.setPen(Qt.NoPen)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#000000"))
        bar = QRectF(0, self.RULER, w, h - self.RULER - self.OVERVIEW - 2)
        track = QPainterPath()
        track.addRoundedRect(bar, 6, 6)
        p.fillPath(track, QColor("#101014"))

        if self.duration <= 0:
            p.end()
            return

        self._draw_ruler(p, w)
        x_in, x_out = self._ms_to_x(self.mark_in), self._ms_to_x(self.mark_out)
        p.save()
        p.setClipPath(track)
        if self._thumb_ms:
            # filmstrip: fixed-width tiles, each shows the thumbnail nearest
            # to the time at its centre (so zooming never repeats one image)
            first = self._thumb_img[0]
            iw = max(first.width() * bar.height() / max(first.height(), 1), 8)
            x = 0.0
            while x < w:
                t = self._x_to_ms(x + iw / 2)
                i = bisect.bisect(self._thumb_ms, t)
                if i == len(self._thumb_ms) or (
                        i > 0 and t - self._thumb_ms[i - 1]
                        < self._thumb_ms[i] - t):
                    i -= 1
                p.drawImage(QRectF(x, bar.top(), iw, bar.height()),
                            self._thumb_img[max(i, 0)])
                x += iw
        else:
            grad = QLinearGradient(0, bar.top(), 0, bar.bottom())
            grad.setColorAt(0, QColor("#3a9440"))
            grad.setColorAt(1, QColor("#256b2a"))
            p.fillRect(QRectF(x_in, bar.top(), max(x_out - x_in, 0),
                              bar.height()), grad)
        # dim what gets cut away
        shade = QColor(0, 0, 0, 215)
        p.fillRect(QRectF(0, bar.top(), max(x_in, 0), bar.height()), shade)
        p.fillRect(QRectF(x_out, bar.top(), max(w - x_out, 0), bar.height()),
                   shade)
        p.restore()
        # kept-range frame
        pen = QPen(QColor("#66bb6a"), 2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(x_in, bar.top() + 1, max(x_out - x_in, 0),
                          bar.height() - 2))
        p.setPen(Qt.NoPen)

        # playhead
        x_pos = self._ms_to_x(self.position)
        if -6 <= x_pos <= w + 6:
            p.fillRect(QRectF(x_pos - 1, self.RULER - 4, 2,
                              bar.height() + 6), QColor("#f0f0f2"))
            p.setBrush(QColor("#f0f0f2"))
            y = self.RULER - 4
            p.drawPolygon(QPolygonF([QPointF(x_pos - 5, y - 6),
                                     QPointF(x_pos + 5, y - 6),
                                     QPointF(x_pos, y + 1)]))

        self._draw_handle(p, x_in, bar, "#66bb6a",
                          self._hover == "in" or self._drag == "in")
        self._draw_handle(p, x_out, bar, "#ef5350",
                          self._hover == "out" or self._drag == "out")

        # fine-scrub tier badge above the playhead while dragging slowed
        if self._drag and self._rate < 1.0:
            _, label = next(((r, l) for _, r, l in self.SCRUB_TIERS
                             if r == self._rate), (1.0, ""))
            if label:
                f = QFont(self.font())
                f.setPointSizeF(8.5)
                f.setBold(True)
                p.setFont(f)
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(label) + 16
                bx = min(max(self._ms_to_x(self._drag_value()) - tw / 2, 4),
                         w - tw - 4)
                badge = QRectF(bx, bar.top() + 4, tw, fm.height() + 6)
                path = QPainterPath()
                path.addRoundedRect(badge, 6, 6)
                p.fillPath(path, QColor(0, 0, 0, 230))
                p.setBrush(Qt.NoBrush)  # playhead brush would fill the pill
                p.setPen(QPen(QColor("#5865f2"), 1))
                p.drawPath(path)
                p.setPen(QColor("#e8e8ea"))
                p.drawText(badge, Qt.AlignCenter, label)
                p.setPen(Qt.NoPen)

        # overview strip: whole file, kept range, and the zoom window
        oy = h - self.OVERVIEW + 2
        full = QRectF(0, oy, w, 4)
        p.fillRect(full, QColor("#2a2a2f"))
        k = w / self.duration
        p.fillRect(QRectF(self.mark_in * k, oy, (self.mark_out - self.mark_in) * k,
                          4), QColor("#2e6b32"))
        if self.zoomed:
            p.fillRect(QRectF(self.view0 * k, oy, max((self.view1 - self.view0) * k,
                                                      2), 4),
                       QColor(240, 240, 242, 150))
        p.fillRect(QRectF(self.position * k - 1, oy - 1, 2, 6), QColor("#f0f0f2"))
        p.end()


# ---------------------------------------------------------------- snapshot

class SnapshotView(QWidget):
    """Shows a frame fit-to-window; drag a rectangle to select a crop."""

    def __init__(self, image: QImage):
        super().__init__()
        self.image = image
        self.sel: QRect | None = None
        self._anchor: QPoint | None = None
        self.setMinimumSize(560, 340)
        self.setCursor(Qt.CrossCursor)

    def _fit(self):
        iw, ih = self.image.width(), self.image.height()
        s = min(self.width() / iw, self.height() / ih)
        dw, dh = iw * s, ih * s
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2,
                      dw, dh), s

    def _to_img(self, pos) -> QPoint:
        r, s = self._fit()
        x = (pos.x() - r.left()) / s
        y = (pos.y() - r.top()) / s
        return QPoint(int(min(max(x, 0), self.image.width())),
                      int(min(max(y, 0), self.image.height())))

    def mousePressEvent(self, e):
        self._anchor = self._to_img(e.position())
        self.sel = None
        self.update()

    def mouseMoveEvent(self, e):
        if self._anchor is not None:
            self.sel = QRect(self._anchor, self._to_img(e.position())).normalized()
            self.update()

    def mouseReleaseEvent(self, _):
        self._anchor = None
        if self.sel and (self.sel.width() < 4 or self.sel.height() < 4):
            self.sel = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0d0d0f"))
        r, s = self._fit()
        p.drawImage(r, self.image)
        if self.sel:
            sv = QRectF(r.left() + self.sel.x() * s, r.top() + self.sel.y() * s,
                        self.sel.width() * s, self.sel.height() * s)
            shade = QPainterPath()
            shade.addRect(QRectF(self.rect()))
            shade.addRect(sv)
            p.fillPath(shade, QColor(0, 0, 0, 150))
            p.setPen(QColor("#66bb6a"))
            p.drawRect(sv)
            p.drawText(sv.adjusted(6, 4, 0, 0), Qt.AlignTop | Qt.AlignLeft,
                       f"{self.sel.width()}×{self.sel.height()}")
        p.end()


class SnapshotDialog(QDialog):
    def __init__(self, image: QImage, default_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Snapshot · drag to crop")
        self.resize(900, 580)
        self.default_path = default_path
        self.view = SnapshotView(image)

        self.info = QLabel(f"{image.width()}×{image.height()} · "
                           "drag to crop, or copy the full frame")
        btn_copy = QPushButton("Copy to clipboard")
        btn_copy.setObjectName("accent")
        btn_copy.clicked.connect(self.copy)
        btn_save = QPushButton("Save PNG…")
        btn_save.clicked.connect(self.save)
        btn_reset = QPushButton("Reset crop")
        btn_reset.clicked.connect(self.reset)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self.info)
        row.addStretch()
        for b in (btn_reset, btn_save, btn_copy, btn_close):
            row.addWidget(b)

        root = QVBoxLayout(self)
        root.addWidget(self.view, stretch=1)
        root.addLayout(row)

    def cropped(self) -> QImage:
        v = self.view
        return v.image.copy(v.sel) if v.sel else v.image

    def copy(self):
        img = self.cropped()
        QApplication.clipboard().setImage(img)
        self.info.setText(f"Copied {img.width()}×{img.height()} to clipboard")

    def save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save snapshot", self.default_path, "PNG (*.png)")
        if path:
            img = self.cropped()
            img.save(path, "PNG")
            self.info.setText(f"Saved {img.width()}×{img.height()} → "
                              f"{Path(path).name}")

    def reset(self):
        self.view.sel = None
        self.view.update()
        img = self.view.image
        self.info.setText(f"{img.width()}×{img.height()} · "
                          "drag to crop, or copy the full frame")


# ------------------------------------------------------------- main window

class Trimmer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("mp4trim")
        self.setMinimumSize(980, 620)
        self.settings = QSettings("mp4trim", "mp4trim")
        geo = self.settings.value("geometry")
        if geo is None or not self.restoreGeometry(geo):
            self.resize(1240, 820)

        self.info: MediaInfo | None = None
        self.position = 0
        self.keyframes: list[int] = []
        self.job: Job | None = None
        self.last_output: str | None = None
        self.fallback = False
        self._prime = False
        self._gen = 0
        self._analyzer: Analyzer | None = None
        self._threads: list[QThread] = []   # keep stopped threads alive

        # --- playback ---
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setFocusPolicy(Qt.NoFocus)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self.on_player_pos)
        self.player.playbackStateChanged.connect(self.on_play_state)
        self.player.errorOccurred.connect(self.on_player_error)

        self.frame_label = QLabel("Drop a video here  ·  or press Ctrl+O")
        self.frame_label.setObjectName("drop")
        self.frame_label.setAlignment(Qt.AlignCenter)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.video)        # 0 = live playback
        self.stack.addWidget(self.frame_label)  # 1 = ffmpeg preview / empty
        self.stack.setCurrentIndex(1)
        self.stack.setMinimumHeight(360)

        self.grabber = FrameGrabber()
        self.grabber.ready.connect(self._show_frame)
        self.grabber.start()
        self._frame_timer = QTimer(self, singleShot=True, interval=60)
        self._frame_timer.timeout.connect(self.update_frame)

        # --- timeline ---
        self.timeline = Timeline()
        self.timeline.seeked.connect(self.seek)
        self.timeline.range_changed.connect(self.refresh_range)

        # --- transport row ---
        def btn(text, tip, slot, name=None):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            b.setFocusPolicy(Qt.NoFocus)
            if name:
                b.setObjectName(name)
            return b

        self.btn_go_in = btn("⇤", "Go to in-point (Home)", self.go_in, "tp")
        self.btn_prev_vid = btn("«", "Previous video in this folder (PgUp)",
                                lambda: self.step_video(-1), "nav")
        self.btn_next_vid = btn("»", "Next video in this folder (PgDn)",
                                lambda: self.step_video(1), "nav")
        self.btn_prev = btn("◂", "Previous frame  ( , )",
                            lambda: self.step_frames(-1), "tp")
        self.btn_play = btn("▶", "Play / pause (Space)", self.play_pause, "play")
        self.btn_next = btn("▸", "Next frame  ( . )",
                            lambda: self.step_frames(1), "tp")
        self.btn_go_out = btn("⇥", "Go to out-point (End)", self.go_out, "tp")
        self.btn_set_in = btn("[ In", "Set in-point at playhead (I)",
                              self.set_in, "mark")
        self.btn_set_out = btn("Out ]", "Set out-point at playhead (O)",
                               self.set_out, "mark")
        self.lbl_time = QLabel("0:00:00.000")
        self.lbl_time.setObjectName("time")
        self.lbl_dur = QLabel("/ 0:00:00.000")
        self.lbl_dur.setObjectName("dur")
        self.lbl_range = QLabel("")
        self.lbl_range.setObjectName("range")
        self.lbl_range.setTextFormat(Qt.RichText)
        self.lbl_range.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_est = QLabel("")
        self.lbl_est.setObjectName("est")
        self.lbl_est.setTextFormat(Qt.RichText)
        self.lbl_est.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row_a = QHBoxLayout()
        row_a.setContentsMargins(12, 6, 12, 4)
        row_a.setSpacing(6)
        row_a.addWidget(self.btn_prev_vid)
        row_a.addWidget(self.btn_next_vid)
        row_a.addSpacing(10)
        for w in (self.btn_go_in, self.btn_prev, self.btn_play, self.btn_next,
                  self.btn_go_out):
            row_a.addWidget(w)
        row_a.addSpacing(10)
        row_a.addWidget(self.btn_set_in)
        row_a.addWidget(self.btn_set_out)
        row_a.addSpacing(14)
        row_a.addWidget(self.lbl_time)
        row_a.addWidget(self.lbl_dur)
        row_a.addStretch()
        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        info_col.addWidget(self.lbl_range)
        info_col.addWidget(self.lbl_est)
        row_a.addLayout(info_col)

        # --- export row ---
        self.btn_open = btn("📂 Open", "Open a video (Ctrl+O)", self.open_dialog)
        lbl_tier = QLabel("Discord limit")
        lbl_tier.setObjectName("muted")
        self.combo_tier = QComboBox()
        self.combo_tier.setFocusPolicy(Qt.NoFocus)
        for name, mb in DISCORD_TIERS:
            self.combo_tier.addItem(name, mb)
        self.combo_tier.setToolTip("Target size for Discord MP4 and GIF exports")
        self.combo_tier.currentIndexChanged.connect(
            lambda i: self.set_discord_limit(self.combo_tier.itemData(i)))
        self.btn_snap = btn("📷 Snapshot", "Grab + crop the current frame (S)",
                            self.snapshot)
        self.btn_gif = btn("🎞 GIF", "Animated GIF sized for your Discord "
                           "limit (G)", self.export_gif)
        self.btn_discord = btn("💬 Discord MP4",
                               "Re-encode to fit your Discord limit (D)",
                               self.export_discord, "discord")
        self.btn_trim = btn("✂ Trim (lossless)",
                            "Stream copy, original quality and size (Enter)",
                            self.trim, "accent")

        row_b = QHBoxLayout()
        row_b.setContentsMargins(12, 4, 12, 12)
        row_b.setSpacing(8)
        row_b.addWidget(self.btn_open)
        row_b.addStretch()
        row_b.addWidget(lbl_tier)
        row_b.addWidget(self.combo_tier)
        row_b.addSpacing(8)
        for w in (self.btn_snap, self.btn_gif, self.btn_discord, self.btn_trim):
            row_b.addWidget(w)

        root = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.stack, stretch=1)
        tl_wrap = QHBoxLayout()
        tl_wrap.setContentsMargins(12, 8, 12, 0)
        tl_wrap.addWidget(self.timeline)
        root.addLayout(tl_wrap)
        root.addLayout(row_a)
        root.addLayout(row_b)
        host = QWidget()
        host.setLayout(root)
        self.setCentralWidget(host)
        self.setAcceptDrops(True)

        # --- status bar: progress, cancel, show-in-folder ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.btn_cancel = btn("Cancel", "Stop the running export",
                              self.cancel_job, "cancel")
        self.btn_cancel.hide()
        self.btn_reveal = btn("Show in folder", "Open the export's folder",
                              self.reveal_last, "link")
        self.btn_reveal.hide()
        sb = self.statusBar()
        sb.addPermanentWidget(self.btn_reveal)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.btn_cancel)

        self._build_menu()
        saved = float(self.settings.value("discord_limit", 10.0))
        self.set_discord_limit(saved if saved in [m for _, m in DISCORD_TIERS]
                               else 10.0)
        self.update_controls()
        if have_ffmpeg():
            sb.showMessage("Ready · drop a video to start")
        else:
            sb.showMessage("ffmpeg not found · install it: winget install Gyan.FFmpeg")
            QTimer.singleShot(0, self._warn_no_ffmpeg)

    # ---------------------------------------------------------------- menu

    def _act(self, text, slot, shortcut=None, checkable=False):
        a = QAction(text, self, checkable=checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(slot)
        return a

    def _build_menu(self):
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        m_file.addAction(self._act("&Open…", self.open_dialog, QKeySequence.Open))
        self.m_recent = m_file.addMenu("Open &Recent")
        self._rebuild_recent()
        m_file.addSeparator()
        self.act_trim = self._act("&Trim (lossless)", self.trim, "Return")
        self.act_trim.setShortcuts([QKeySequence("Return"), QKeySequence("Enter"),
                                    QKeySequence("Ctrl+E")])
        m_file.addAction(self.act_trim)
        m_file.addAction(self._act("Export &Discord MP4", self.export_discord, "D"))
        m_file.addAction(self._act("Export &GIF", self.export_gif, "G"))
        m_file.addAction(self._act("&Snapshot / Crop…", self.snapshot, "S"))
        m_file.addSeparator()
        m_file.addAction(self._act("Show last export in folder", self.reveal_last))
        m_file.addSeparator()
        m_file.addAction(self._act("E&xit", self.close, QKeySequence.Quit))

        m_edit = mb.addMenu("&Marks")
        m_edit.addAction(self._act("Set &in-point at playhead", self.set_in, "I"))
        m_edit.addAction(self._act("Set &out-point at playhead", self.set_out, "O"))
        m_edit.addAction(self._act("Snap in-point to &keyframe", self.snap_in, "K"))
        m_edit.addSeparator()
        m_edit.addAction(self._act("Go to in-point", self.go_in, "Home"))
        m_edit.addAction(self._act("Go to out-point", self.go_out, "End"))
        m_edit.addAction(self._act("Previous frame", lambda: self.step_frames(-1), ","))
        m_edit.addAction(self._act("Next frame", lambda: self.step_frames(1), "."))
        m_edit.addSeparator()
        m_edit.addAction(self._act("Reset timeline &zoom", self.timeline.reset_zoom, "Z"))
        m_edit.addAction(self._act("Reset in/out to whole file", self.reset_marks, "Ctrl+R"))
        m_edit.addSeparator()
        m_edit.addAction(self._act("&Previous video in folder",
                                   lambda: self.step_video(-1), "PgUp"))
        m_edit.addAction(self._act("&Next video in folder",
                                   lambda: self.step_video(1), "PgDown"))

        # playback keys that live on the window, not in a menu
        for text, slot, key in [
            ("Play/pause", self.play_pause, "Space"),
            ("Back 1s", lambda: self.seek(self.position - 1000), "Left"),
            ("Forward 1s", lambda: self.seek(self.position + 1000), "Right"),
            ("Back 10s", lambda: self.seek(self.position - 10_000), "Shift+Left"),
            ("Forward 10s", lambda: self.seek(self.position + 10_000), "Shift+Right"),
        ]:
            self.addAction(self._act(text, slot, key))

        m_opts = mb.addMenu("&Options")
        self.act_reencode = self._act(
            "Frame-accurate trim (re-encode, drops Dolby Vision)",
            lambda _: self.refresh_range(), checkable=True)
        self.act_mix = self._act(
            "Mix all audio tracks in Discord MP4", self._save_mix, checkable=True)
        self.act_mix.setChecked(self.settings.value("mix_audio", False, bool))
        self.act_ffpreview = self._act(
            "Force ffmpeg preview (no playback)", self.on_force_fallback,
            checkable=True)
        m_opts.addActions([self.act_reencode, self.act_mix, self.act_ffpreview])
        m_opts.addSeparator()
        m_tier = m_opts.addMenu("Discord upload limit")
        self.tier_actions = []
        for name, mbytes in DISCORD_TIERS:
            act = QAction(name, self, checkable=True)
            act.triggered.connect(lambda _=False, v=mbytes: self.set_discord_limit(v))
            m_tier.addAction(act)
            self.tier_actions.append((act, mbytes))

        m_help = mb.addMenu("&Help")
        m_help.addAction(self._act("&Keyboard shortcuts", self.show_shortcuts, "F1"))
        m_help.addAction(self._act("&About", self.show_about))

    def _rebuild_recent(self):
        self.m_recent.clear()
        recent = [p for p in self._recent() if Path(p).exists()]
        for p in recent:
            self.m_recent.addAction(
                self._act(Path(p).name, lambda _=False, x=p: self.load(x)))
        if not recent:
            a = self.m_recent.addAction("(none)")
            a.setEnabled(False)
        else:
            self.m_recent.addSeparator()
            self.m_recent.addAction(self._act("Clear list", self._clear_recent))

    def _recent(self) -> list[str]:
        v = self.settings.value("recent", [])
        return [v] if isinstance(v, str) else list(v or [])

    def _push_recent(self, path: str):
        rec = [p for p in self._recent() if os.path.normcase(p) != os.path.normcase(path)]
        self.settings.setValue("recent", [path, *rec][:10])
        self._rebuild_recent()

    def _clear_recent(self):
        self.settings.setValue("recent", [])
        self._rebuild_recent()

    def _save_mix(self, on: bool):
        self.settings.setValue("mix_audio", on)

    def show_shortcuts(self):
        QMessageBox.information(self, "Keyboard shortcuts", SHORTCUTS_HELP)

    def show_about(self):
        QMessageBox.about(
            self, "mp4trim",
            f"<b>mp4trim {APP_VERSION}</b><br><br>"
            "Drag the green/red handles to pick the kept range.<br>"
            "<b>Trim</b> is lossless (stream copy, Dolby Vision / HDR safe).<br>"
            "<b>Discord MP4</b> re-encodes to fit your upload limit.<br><br>"
            f"ffmpeg: {tool('ffmpeg')}<br>"
            f"Encoder: {h264_encoder()}")

    def _warn_no_ffmpeg(self):
        QMessageBox.warning(
            self, "mp4trim",
            "ffmpeg / ffprobe were not found. The installer ships them, so "
            "reinstalling mp4trim fixes this.\n\n"
            "Running from source? Run  python fetch_ffmpeg.py  or\n"
            "    winget install Gyan.FFmpeg")

    # ------------------------------------------------------- file handling

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        vids = [f for f in files if Path(f).suffix.lower() in VIDEO_EXTS]
        if vids or files:
            self.load((vids or files)[0])

    def open_dialog(self):
        start = self.settings.value("last_dir", "") or str(Path.home() / "Videos")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", start,
            "Video (*.mp4 *.mkv *.mov *.m4v *.webm *.avi *.ts);;All files (*)")
        if path:
            self.load(path)

    def load(self, path: str):
        if not have_ffmpeg():
            self._warn_no_ffmpeg()
            return
        try:
            info = probe(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "mp4trim", f"Could not open this file:\n{e}")
            return
        self.info = info
        self.position = 0
        self.keyframes = []
        self.timeline.reset(info.duration_ms)
        self.settings.setValue("last_dir", str(Path(path).parent))
        self._push_recent(path)
        self.setWindowTitle(f"mp4trim · {Path(path).name}")
        self._start_analyzer()

        self.fallback = self.act_ffpreview.isChecked()
        if self.fallback:
            self.enter_fallback(silent=True)
        else:
            self.stack.setCurrentIndex(0)
            self._prime = True
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()  # paused on first frame via on_player_pos
            hdr = " · HDR" if info.hdr else ""
            vids, idx = self._siblings()
            pos = f" · clip {idx + 1} of {len(vids)}" if idx >= 0 else ""
            self.statusBar().showMessage(
                f"{Path(path).name} · {info.width}×{info.height} "
                f"{info.fps:.0f}fps {info.v_codec.upper()}{hdr} · "
                f"{info.total_kbps / 1000:.0f} Mbps{pos}")
        self.update_controls()
        self.refresh_pos()
        self.refresh_range()

    def _start_analyzer(self):
        if self._analyzer:
            self._analyzer.stop()
        self._gen += 1
        a = Analyzer(self._gen, self.info)
        a.keyframes.connect(self._on_keyframes)
        a.thumb.connect(self._on_thumb)
        a.finished.connect(lambda a=a: self._threads.remove(a)
                           if a in self._threads else None)
        self._threads.append(a)
        self._analyzer = a
        a.start()

    def _on_keyframes(self, gen: int, kfs: list):
        if gen == self._gen:
            self.keyframes = kfs
            self.refresh_range()

    def _on_thumb(self, gen: int, ms: int, img: QImage):
        if gen == self._gen:
            self.timeline.add_thumb(ms, img)

    # ---------------------------------------------------- folder navigation

    def _siblings(self) -> tuple[list[Path], int]:
        """Videos in the open file's folder (name order) + index of current."""
        if not self.info:
            return [], -1
        cur = Path(self.info.path)
        try:
            vids = sorted((p for p in cur.parent.iterdir()
                           if p.suffix.lower() in VIDEO_EXTS and p.is_file()),
                          key=lambda p: p.name.lower())
        except OSError:
            return [], -1
        me = os.path.normcase(str(cur))
        idx = next((i for i, p in enumerate(vids)
                    if os.path.normcase(str(p)) == me), -1)
        return vids, idx

    def step_video(self, delta: int):
        vids, idx = self._siblings()
        if not vids or self.job:
            return
        if idx < 0:
            idx = 0 if delta > 0 else len(vids) - 1
            self.load(str(vids[idx]))
            return
        j = idx + delta
        if not 0 <= j < len(vids):
            self.statusBar().showMessage(
                "Last video in this folder" if delta > 0
                else "First video in this folder")
            return
        self.load(str(vids[j]))

    # ------------------------------------------------- playback / preview

    @property
    def path(self) -> str | None:
        return self.info.path if self.info else None

    def play_pause(self):
        if not self.info or self.fallback:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.position >= self.timeline.mark_out - 50:
                self.seek(self.timeline.mark_in)
            self.player.play()

    def on_play_state(self, state):
        self.btn_play.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def on_player_pos(self, ms: int):
        if self._prime:
            self._prime = False
            self.player.pause()
        self.position = ms
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.timeline.follow(ms)
        self.refresh_pos()

    def on_player_error(self, _err, msg: str):
        if not self.fallback and self.info:
            self.enter_fallback(silent=False, detail=msg)

    def on_force_fallback(self, on: bool):
        if self.info:
            if on:
                self.enter_fallback(silent=True)
            else:
                pos = self.position
                self.fallback = False
                self.stack.setCurrentIndex(0)
                self.player.setSource(QUrl.fromLocalFile(self.info.path))
                self.player.setPosition(pos)
                self.update_controls()

    def enter_fallback(self, silent: bool, detail: str = ""):
        self.fallback = True
        self.player.stop()
        self.player.setSource(QUrl())
        self.stack.setCurrentIndex(1)
        self.btn_play.setText("▶")
        note = "ffmpeg preview mode · scrub the timeline (no live playback)"
        if not silent:
            note = "System decoder failed; " + note + (f"  [{detail}]" if detail else "")
        self.statusBar().showMessage(note)
        self.update_controls()
        self.update_frame()

    def seek(self, ms: int):
        if not self.info:
            return
        self.position = int(min(max(ms, 0), self.info.duration_ms))
        if self.fallback:
            self._frame_timer.start()
        else:
            self.player.setPosition(self.position)
        self.timeline.follow(self.position)
        self.refresh_pos()

    def step_frames(self, n: int):
        if not self.info:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        self.seek(round(self.position + n * 1000 / (self.info.fps or 30)))

    def update_frame(self):
        if self.info and self.fallback:
            self.grabber.request(self.info.path, self.position, self.info.hdr)

    def _show_frame(self, img: QImage):
        if self.fallback:
            self.frame_label.setPixmap(QPixmap.fromImage(img).scaled(
                self.frame_label.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fallback:
            self._frame_timer.start()

    # ------------------------------------------------------------- marks

    def set_in(self):
        if self.info:
            self.timeline.mark_in = max(0, min(self.position,
                                               self.timeline.mark_out - 100))
            self.timeline.update()
            self.refresh_range()

    def set_out(self):
        if self.info:
            self.timeline.mark_out = min(self.info.duration_ms,
                                         max(self.position,
                                             self.timeline.mark_in + 100))
            self.timeline.update()
            self.refresh_range()

    def snap_in(self):
        kf = keyframe_before(self.keyframes, self.timeline.mark_in)
        if kf is not None:
            self.timeline.mark_in = kf
            self.seek(kf)
            self.timeline.update()
            self.refresh_range()

    def go_in(self):
        self.seek(self.timeline.mark_in)

    def go_out(self):
        self.seek(self.timeline.mark_out)

    def reset_marks(self):
        if self.info:
            self.timeline.mark_in, self.timeline.mark_out = 0, self.info.duration_ms
            self.timeline.update()
            self.refresh_range()

    # ------------------------------------------------------------ display

    def update_controls(self):
        loaded = self.info is not None
        busy = self.job is not None
        for b in (self.btn_go_in, self.btn_prev, self.btn_next, self.btn_go_out,
                  self.btn_set_in, self.btn_set_out):
            b.setEnabled(loaded)
        for b in (self.btn_prev_vid, self.btn_next_vid):
            b.setEnabled(loaded and not busy)
        self.btn_play.setEnabled(loaded and not self.fallback)
        for b in (self.btn_snap, self.btn_gif, self.btn_discord, self.btn_trim):
            b.setEnabled(loaded and not busy)

    def refresh_pos(self):
        self.timeline.position = self.position
        self.timeline.update()
        self.lbl_time.setText(fmt_ms(self.position))
        self.lbl_dur.setText(f"/ {fmt_ms(self.info.duration_ms if self.info else 0)}")

    def refresh_range(self):
        if not self.info:
            self.lbl_range.setText("")
            self.lbl_est.setText("")
            return
        t_in, t_out = self.timeline.mark_in, self.timeline.mark_out
        keep = t_out - t_in
        text = (f"IN {fmt_ms(t_in)}  OUT {fmt_ms(t_out)}  "
                f"KEEP <b>{fmt_dur(keep)}</b>")
        if not self.act_reencode.isChecked() and self.keyframes:
            kf = keyframe_before(self.keyframes, t_in)
            if kf is not None and t_in - kf > 50:
                text += (f"  <span style='color:#ffb74d'>⚠ lossless starts "
                         f"{(t_in - kf) / 1000:.1f}s early · K snaps</span>")
        self.lbl_range.setText(text)

        limit = self.discord_limit()
        lossless = lossless_mb(keep, self.info)
        plan = plan_discord(keep, limit, self.info)
        parts = [f"Lossless ≈ {fmt_mb(lossless)}"]
        if plan.ok:
            parts.append(
                f"<span style='color:#9aa4ff'>Discord {limit:g} MB → "
                f"{plan.height}p{plan.fps} · {plan.v_kbps / 1000:.1f} Mbps ✓</span>")
        else:
            parts.append(
                f"<span style='color:#ef9a9a'>too long for {limit:g} MB · "
                f"max ≈ {fmt_dur(plan.max_keep_s * 1000)}</span>")
        self.lbl_est.setText("  ·  ".join(parts))

    # ------------------------------------------------------------ exports

    def discord_limit(self) -> float:
        return float(self.settings.value("discord_limit", 10.0))

    def set_discord_limit(self, mb: float):
        mb = float(mb)
        self.settings.setValue("discord_limit", mb)
        for act, v in self.tier_actions:
            act.setChecked(v == mb)
        i = self.combo_tier.findData(int(mb))
        if i >= 0 and i != self.combo_tier.currentIndex():
            self.combo_tier.blockSignals(True)
            self.combo_tier.setCurrentIndex(i)
            self.combo_tier.blockSignals(False)
        self.refresh_range()

    def _range_ok(self) -> bool:
        if not self.info or self.job:
            return False
        if self.timeline.mark_out - self.timeline.mark_in < 100:
            QMessageBox.warning(self, "mp4trim", "In/out range is empty.")
            return False
        return True

    def _start_job(self, fn, dst: Path):
        self.player.pause()
        self.job = Job(fn, str(dst))
        self.job.progress.connect(self._on_progress)
        self.job.succeeded.connect(self._on_done)
        self.job.failed.connect(self._on_failed)
        self.job.cancelled.connect(self._on_cancelled)
        self.progress.setValue(0)
        self.progress.show()
        self.btn_cancel.show()
        self.btn_reveal.hide()
        self.update_controls()
        self.job.start()

    def _finish_job(self):
        if self.job:
            self.job.wait(2000)
        self.job = None
        self.progress.hide()
        self.btn_cancel.hide()
        self.update_controls()

    def _on_progress(self, frac: float, label: str):
        self.progress.setValue(int(frac * 1000))
        self.statusBar().showMessage(f"{label} · {frac * 100:.0f}%")

    def _on_done(self, path: str, msg: str, ok: bool):
        self._finish_job()
        self.last_output = path
        self.btn_reveal.show()
        if ok:
            copy_file_to_clipboard(path)
            self.statusBar().showMessage(f"✓ {msg}  ·  {Path(path).name}")
        else:
            self.statusBar().showMessage(f"⚠ {msg}")
            QMessageBox.warning(self, "mp4trim", f"{msg}\n\n{path}")

    def _on_failed(self, err: str):
        self._finish_job()
        self.statusBar().showMessage("Export failed")
        QMessageBox.critical(self, "mp4trim", f"Export failed:\n{err}")

    def _on_cancelled(self):
        self._finish_job()
        self.statusBar().showMessage("Export cancelled")

    def cancel_job(self):
        if self.job:
            self.statusBar().showMessage("Cancelling…")
            self.job.cancel()

    def reveal_last(self):
        if self.last_output and Path(self.last_output).exists():
            reveal_in_explorer(self.last_output)

    def trim(self):
        if not self._range_ok():
            return
        info, t_in, t_out = self.info, self.timeline.mark_in, self.timeline.mark_out
        accurate = self.act_reencode.isChecked()
        dst = output_path(info.path, "trim", t_in, t_out, Path(info.path).suffix)
        self._start_job(lambda run: export_trim(run, info, t_in, t_out, str(dst),
                                                accurate), dst)

    def export_discord(self):
        if not self._range_ok():
            return
        info, t_in, t_out = self.info, self.timeline.mark_in, self.timeline.mark_out
        limit = self.discord_limit()
        plan = plan_discord(t_out - t_in, limit, info)
        if not plan.ok:
            if QMessageBox.question(
                    self, "mp4trim",
                    f"{fmt_dur(t_out - t_in)} is too long for {limit:g} MB. It "
                    f"would be very blurry. Up to about "
                    f"{fmt_dur(plan.max_keep_s * 1000)} fits.\n\n"
                    "Export anyway?") != QMessageBox.StandardButton.Yes:
                return
        mix = self.act_mix.isChecked()
        dst = output_path(info.path, f"discord{limit:g}mb", t_in, t_out, ".mp4")
        self._start_job(lambda run: export_discord(run, info, t_in, t_out,
                                                   str(dst), limit, mix), dst)

    def export_gif(self):
        if not self._range_ok():
            return
        info, t_in, t_out = self.info, self.timeline.mark_in, self.timeline.mark_out
        limit = self.discord_limit()
        if limit <= 10 and t_out - t_in > 60_000:
            if QMessageBox.question(
                    self, "mp4trim",
                    "Selection is over a minute. GIFs that long get huge and "
                    "may not fit Discord even at lowest quality.\n"
                    "Tip: Discord MP4 looks far better at this length.\n\n"
                    "Continue anyway?") != QMessageBox.StandardButton.Yes:
                return
        dst = output_path(info.path, "clip", t_in, t_out, ".gif")
        self._start_job(lambda run: export_gif(run, info, t_in, t_out, str(dst),
                                               limit), dst)

    def snapshot(self):
        if not self.info:
            return
        self.player.pause()
        img = grab_frame(self.info.path, self.position, native=True,
                         hdr=self.info.hdr)
        if img is None:
            QMessageBox.warning(self, "mp4trim", "Could not grab this frame.")
            return
        src = Path(self.info.path)
        default = str(src.with_name(f"{src.stem}_{fmt_tag(self.position)}.png"))
        SnapshotDialog(img, default, self).exec()

    # ------------------------------------------------------------ closing

    def closeEvent(self, e):
        if self.job:
            if QMessageBox.question(
                    self, "mp4trim", "An export is running. Cancel it and quit?"
            ) != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.job.cancel()
            self.job.wait(5000)
        self.settings.setValue("geometry", self.saveGeometry())
        if self._analyzer:
            self._analyzer.stop()
        for t in list(self._threads):
            t.wait(3000)
        self.grabber.stop()
        self.player.stop()
        super().closeEvent(e)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2:]))
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFont("Segoe UI Variable Text", 10)
    if not font.exactMatch():
        font = QFont("Segoe UI", 10)
    app.setFont(font)
    app.setStyleSheet(STYLE)
    icon = res_path("icon.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = Trimmer()
    win.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        win.load(sys.argv[1])
    sys.exit(app.exec())


def selftest(argv: list[str]) -> int:
    """Headless end-to-end check of the installed app on this machine.

        mp4trim.exe --selftest <out.json> [clip.mp4]

    Uses the same code paths as the buttons. Without a clip it synthesizes a
    1440p60 test video with the bundled ffmpeg. Writes a JSON report (a GUI
    exe has no console) and returns 0 only if every check passed.
    """
    report_path = Path(argv[0]) if argv else Path(tempfile.gettempdir()) / "mp4trim-selftest.json"
    rep: dict = {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
                 "checks": {}}
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        rep["checks"][name] = {"ok": bool(cond), "detail": detail}
        ok = ok and bool(cond)

    work = Path(tempfile.mkdtemp(prefix="mp4trim-selftest-"))
    try:
        rep["ffmpeg"], rep["ffprobe"] = tool("ffmpeg"), tool("ffprobe")
        chk("ffmpeg_found", have_ffmpeg(), rep["ffmpeg"])
        chk("ffmpeg_bundled", Path(rep["ffmpeg"]).parent == res_path("ffmpeg")
            or not rep["frozen"], rep["ffmpeg"])
        rep["encoder"] = h264_encoder()
        run = lambda a, s, l: run_ffmpeg(a, s, l)  # noqa: E731
        clip = argv[1] if len(argv) > 1 else None
        if not clip:
            clip = str(work / "synthetic (2).mp4")
            run(["-f", "lavfi", "-i", "testsrc2=s=2560x1440:r=60:d=20",
                 "-f", "lavfi", "-i", "sine=f=440:d=20", "-c:v", "libx264",
                 "-preset", "ultrafast", "-b:v", "60M", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", clip], 20, "synth")
        info = probe(clip)
        rep["source"] = {"w": info.width, "h": info.height, "fps": info.fps,
                         "mb": os.path.getsize(clip) / 1e6,
                         "sec": info.duration_ms / 1000}
        kfs = probe_keyframes(clip, info.start_s)
        chk("keyframes", len(kfs) > 0, f"{len(kfs)} keyframes")
        chk("thumbnail", grab_thumb(clip, 2000, info.hdr) is not None)
        results = {}
        for limit in (10, 50):
            dst = str(output_path(clip, f"discord{limit}mb", 0, info.duration_ms, ".mp4"))
            _, msg, good = export_discord(run, info, 0, info.duration_ms, dst,
                                          limit, False)
            size = os.path.getsize(dst) / 1e6
            v = probe(dst)
            results[limit] = {"mb": round(size, 2), "h": min(v.width, v.height),
                              "codec": v.v_codec, "msg": msg}
            chk(f"discord_{limit}mb", good and size <= limit and v.v_codec == "h264",
                f"{size:.2f} MB {v.v_codec} {v.width}x{v.height}")
        rep["discord"] = results
        dst = str(output_path(clip, "trim", 1000, 6000, ".mp4"))
        export_trim(run, info, 1000, 6000, dst, False)
        chk("lossless_trim", probe(dst).v_codec == info.v_codec, dst)
        dst = str(output_path(clip, "clip", 1000, 4000, ".gif"))
        _, msg, good = export_gif(run, info, 1000, 4000, dst, 10)
        chk("gif_10mb", good, msg)
    except Exception as e:  # noqa: BLE001
        chk("exception", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    rep["ok"] = ok
    report_path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
