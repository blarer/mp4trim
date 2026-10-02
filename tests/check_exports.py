"""End-to-end export checks against real files. Not a unit-test suite: it
encodes real video, so run it by hand:

    py -3.12 tests/check_exports.py <clip.mp4> [more clips...]

For each clip and Discord tier it runs the same export_discord() the app
uses, then verifies with ffprobe: size under the limit, H.264 + AAC, one
audio track, expected resolution. Also checks lossless trim keeps codecs.
Outputs go to a temp folder and are deleted afterwards.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mp4trim as m  # noqa: E402


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", path], capture_output=True, text=True)
    return json.loads(out.stdout)


def plain_run(args, span_s, label):
    print(f"      {label}")
    m.run_ffmpeg(args, span_s, label)


failures = []


def check(cond, msg):
    print(("    PASS " if cond else "    FAIL ") + msg)
    if not cond:
        failures.append(msg)


def discord_case(info, t_in, t_out, limit, tmp, mix=False):
    dst = str(Path(tmp) / f"d_{limit}_{t_in}_{t_out}_{int(mix)}.mp4")
    plan = m.plan_discord(t_out - t_in, limit, info)
    print(f"  Discord {limit} MB · keep {m.fmt_dur(t_out - t_in)} · plan "
          f"{plan.height}p{plan.fps} {plan.v_kbps}k ok={plan.ok}")
    t = time.time()
    path, msg, ok = m.export_discord(plain_run, info, t_in, t_out, dst, limit, mix)
    size = os.path.getsize(path)
    p = ffprobe(path)
    v = [s for s in p["streams"] if s["codec_type"] == "video"][0]
    a = [s for s in p["streams"] if s["codec_type"] == "audio"]
    dur = float(p["format"]["duration"])
    print(f"    -> {size / 1e6:.2f} MB ({size / (limit * 1e6) * 100:.0f}% of "
          f"limit) {v['width']}x{v['height']} in {time.time() - t:.1f}s")
    check(ok and size <= limit * 1e6, f"under {limit} MB ({size / 1e6:.2f})")
    check(v["codec_name"] == "h264", f"h264 video ({v['codec_name']})")
    check(len(a) == (1 if info.audio_tracks else 0), f"{len(a)} audio track")
    check(min(v["width"], v["height"]) == plan.height,
          f"height {plan.height}")
    check(abs(dur - (t_out - t_in) / 1000) < 0.5,
          f"duration {dur:.2f}s ~ {(t_out - t_in) / 1000:.2f}s")
    return size


def main(clips):
    tmp = tempfile.mkdtemp(prefix="mp4trim-check-")
    print("encoder:", m.h264_encoder())
    for clip in clips:
        info = m.probe(clip)
        print(f"\n{Path(clip).name}: {info.width}x{info.height} "
              f"{info.fps:.0f}fps {info.v_codec} {info.total_kbps / 1000:.0f} Mbps "
              f"{info.duration_ms / 1000:.1f}s audio={info.audio_tracks}")
        full = (0, info.duration_ms)
        limits = [mb for _, mb in m.DISCORD_TIERS]
        for limit in limits:
            plan = m.plan_discord(full[1] - full[0], limit, info)
            if plan.ok:
                discord_case(info, *full, limit, tmp)
            else:
                print(f"  Discord {limit} MB: full clip too long "
                      f"(max {m.fmt_dur(plan.max_keep_s * 1000)}), "
                      "checking the planner refuses it")
                check(not plan.ok, "planner flags too-long selection")
                cut = int(plan.max_keep_s * 1000 * 0.9)
                discord_case(info, 0, cut, limit, tmp)
        if info.audio_tracks > 1:
            discord_case(info, 0, min(8000, info.duration_ms), limits[1],
                         tmp, mix=True)

        # a short clip at the big tier must not exceed the source bitrate
        short = min(3000, info.duration_ms)
        size = discord_case(info, 0, short, limits[-1], tmp)
        src_rate = info.total_kbps * short / 8
        check(size <= src_rate * 1000 * 1.15,
              "short clip not inflated past source bitrate")

        dst = str(Path(tmp) / "lossless.mp4")
        m.export_trim(plain_run, info, 1000, min(6000, info.duration_ms), dst,
                      False)
        p, src = ffprobe(dst), ffprobe(clip)
        check([s["codec_name"] for s in p["streams"]] ==
              [s["codec_name"] for s in src["streams"]],
              "lossless trim keeps every stream + codec")

    for f in Path(tmp).iterdir():
        f.unlink()
    os.rmdir(tmp)
    print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILURES'}")
    for f in failures:
        print("  -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
