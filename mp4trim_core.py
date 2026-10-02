"""mp4trim core: probing, planning, ffmpeg invocation, export logic.

No Qt widgets here (QImage/QMimeData only), so it is import-light and
testable headless. The UI lives in mp4trim_widgets / mp4trim_app."""

import bisect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

APP_VERSION = "3.0.0"
UPDATE_REPO = "blarer/mp4trim"   # GitHub repo the auto-updater watches
# Discord limits are decimal megabytes; using 1e6 keeps us on the safe side.
DISCORD_TIERS = [("Free · 20 MB", 20), ("Nitro Basic · 50 MB", 50),
                 ("Nitro · 1 GB", 1000)]
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
    return 96 if limit_mb <= 20 else 128


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
# older ffmpeg or GPU driver that rejects one (e.g. -tune hq)
# falls back to the CPU encoder instead of failing the export.
PROBE_OPTS = {
    "h264_nvenc": ["-preset", "p5", "-rc", "vbr",
                   "-b:v", "2000k", "-maxrate",
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
        args += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                 *rate, "-spatial-aq", "1", "-rc-lookahead", "32"]
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
    ladder = (GIF_HQ_LADDER + GIF_LADDER) if limit_mb > 20 else GIF_LADDER
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


