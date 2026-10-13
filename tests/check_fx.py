"""Checks for the keyframe FX model (mp4trim_fx) plus real ffmpeg renders
and (when landed) the core/fxwin integrations.

    py -3.12 tests/check_fx.py <clip.mp4>

mp4trim_fx.py is a committed contract, so its checks are hard asserts.
Integration pieces (fx= kwargs in mp4trim_core, mp4trim_fxwin.FxWindow)
may not exist yet and are reported as SKIP, like tests/check_glass.py.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MP4TRIM_NO_UPDATE"] = "1"
os.environ.setdefault("MP4TRIM_SETTINGS", os.path.join(
    tempfile.mkdtemp(prefix="mp4trim-testset-"), "settings.ini"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import mp4trim_fx as fx  # noqa: E402
import mp4trim_core as core  # noqa: E402

fails = []
skips = []


def check(c, msg):
    print(("PASS " if c else "FAIL ") + msg, flush=True)
    if not c:
        fails.append(msg)


def skip(msg):
    print("SKIP " + msg, flush=True)
    skips.append(msg)


if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(2)

real_clip = sys.argv[1]
work = Path(tempfile.mkdtemp(prefix="mp4trim-fx-"))

FFMPEG = core.tool("ffmpeg")
FFPROBE = core.tool("ffprobe")
NOWIN = getattr(core, "CREATE_NO_WINDOW", 0)


def run_cmd(args, timeout=300):
    return subprocess.run(args, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          creationflags=NOWIN)


def probe_duration_ms(path):
    r = run_cmd([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip()) * 1000.0
    except ValueError:
        return -1.0


def probe_resolution(path):
    r = run_cmd([FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0",
                 str(path)])
    try:
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return 0, 0


# =====================================================================
# A. Pure model (hard asserts)
# =====================================================================
print("--- A. pure model ---")

# --- interpolation: linear midpoint ---
t = fx.FxTrack("speed")
t.set_kf(0, 1.0, "linear")
t.set_kf(1000, 2.0, "linear")
check(abs(t.value_at(500) - 1.5) < 1e-9, "linear interp midpoint 1.0->2.0 @500 == 1.5")

# --- hold ---
t = fx.FxTrack("speed")
t.set_kf(0, 1.0, "hold")
t.set_kf(1000, 2.0, "linear")
check(t.value_at(999) == 1.0, "hold interp keeps left value until next kf")
check(t.value_at(1000) == 2.0, "hold interp jumps at next kf")

# --- ease monotonicity ---
t = fx.FxTrack("speed")
t.set_kf(0, 0.5, "ease")
t.set_kf(1000, 2.0, "linear")
vals = [t.value_at(ms) for ms in range(0, 1001, 50)]
mono = all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))
check(mono, "ease interp is monotonic for ascending keyframes")
check(abs(vals[0] - 0.5) < 1e-9 and abs(vals[-1] - 2.0) < 1e-9,
      "ease interp hits exact endpoints")
# ease midpoint == linear midpoint (smoothstep(0.5)==0.5)
check(abs(t.value_at(500) - 1.25) < 1e-9, "ease midpoint equals linear midpoint")

# --- edge clamp ---
check(t.value_at(-500) == 0.5, "value_at before first kf clamps to first")
check(t.value_at(5000) == 2.0, "value_at after last kf clamps to last")
empty = fx.FxTrack("speed")
check(empty.value_at(0) == 1.0, "empty speed track samples 1.0")
check(fx.FxTrack("crop").value_at(0) is None, "empty crop track samples None")

# --- value clamping 0.25 / 4.0 ---
t = fx.FxTrack("speed")
t.set_kf(0, 0.01)
t.set_kf(100, 99.0)
check(t.value_at(0) == fx.SPEED_MIN == 0.25, "speed clamps to SPEED_MIN 0.25")
check(t.value_at(100) == fx.SPEED_MAX == 4.0, "speed clamps to SPEED_MAX 4.0")

# --- set_kf replace semantics ---
t = fx.FxTrack("speed")
t.set_kf(500, 1.0, "linear")
t.set_kf(500, 2.0, "hold")
check(len(t.kfs) == 1, "set_kf at same ms replaces (one kf)")
check(t.kfs[0].value == 2.0 and t.kfs[0].interp == "hold",
      "set_kf replace keeps the latest value/interp")
t.set_kf(100, 3.0, "bogus-interp")
check(t.kfs[0].ms == 100 and t.kfs[0].interp == "linear",
      "set_kf sorts and coerces bad interp to linear")

# --- speed_segments: constant track -> 1 segment ---
t = fx.FxTrack("speed")
t.set_kf(0, 2.0)
segs = fx.speed_segments(t, 0, 4000)
check(segs == [(0, 4000, 2.0)], f"constant track -> single segment {segs}")
segs = fx.speed_segments(fx.FxTrack("speed"), 0, 4000)
check(segs == [(0, 4000, 1.0)], "empty track -> single unity segment")
check(fx.speed_segments(t, 4000, 4000) == [], "t_out <= t_in -> no segments")

# --- ramp subdivision respects MAX_SEGMENTS with many keyframes ---
t = fx.FxTrack("speed")
for i in range(40):
    t.set_kf(i * 1000, 0.25 + (i % 7) * 0.55, "linear")
segs = fx.speed_segments(t, 0, 39000)
check(1 < len(segs) <= fx.MAX_SEGMENTS,
      f"ramp subdivision capped at MAX_SEGMENTS ({len(segs)} <= {fx.MAX_SEGMENTS})")
check(segs[0][0] == 0 and segs[-1][1] == 39000 and
      all(a[1] == b[0] for a, b in zip(segs, segs[1:])),
      "segments are contiguous and cover [t_in, t_out]")

# --- adjacent equal speeds merged ---
t = fx.FxTrack("speed")
t.set_kf(0, 2.0, "hold")
t.set_kf(1000, 2.0, "hold")
t.set_kf(2000, 1.0, "hold")
segs = fx.speed_segments(t, 0, 3000)
check(segs == [(0, 2000, 2.0), (2000, 3000, 1.0)],
      f"adjacent equal-speed segments merged {segs}")

# --- output_duration_ms, hand-computed ---
t = fx.FxTrack("speed")
t.set_kf(0, 1.0, "hold")
t.set_kf(2000, 0.5, "hold")
# 2000ms @1.0 -> 2000ms wall; 2000ms @0.5 -> 4000ms wall
check(fx.output_duration_ms(t, 0, 4000) == 6000,
      "output_duration_ms hold 1.0->0.5 over 4s == 6000ms")

# --- atempo_chain ---
for speed in (0.25, 0.3, 1.0, 2.0, 4.0):
    chain = fx.atempo_chain(speed)
    factors = [float(x) for x in re.findall(r"atempo=([0-9.eE+-]+)", chain)]
    check(len(factors) >= 1, f"atempo_chain({speed}) non-empty: {chain}")
    in_range = all(0.5 - 1e-9 <= f <= 2.0 + 1e-9 for f in factors)
    check(in_range, f"atempo_chain({speed}) factors all within 0.5..2.0")
    prod = 1.0
    for f in factors:
        prod *= f
    check(abs(prod - speed) < 1e-6,
          f"atempo_chain({speed}) product {prod:.8f} == speed")

# --- build_speed_graph ---
g, v, a = fx.build_speed_graph("vc", "0:a:0", [(0, 4000, 1.0)], 0)
check(g == "" and v == "vc" and a == "0:a:0",
      "build_speed_graph no-op for single unity segment")
g, v, a = fx.build_speed_graph("vc", None, [], 0)
check(g == "" and v == "vc" and a is None, "build_speed_graph no-op for empty segments")

three = [(0, 1000, 2.0), (1000, 2000, 0.5), (2000, 3000, 1.0)]
g, v, a = fx.build_speed_graph("vc", "0:a:0", three, 0)
check("concat=n=3:v=1:a=1" in g, "3 segments -> concat=n=3 with audio")
check("split=3" in g and "asplit=3" in g, "graph splits video and audio 3 ways")
check(v == "vfx" and a == "afx", "graph output labels vfx/afx")
check("volume=0" not in g and "minterpolate" not in g,
      "no mute/minterpolate by default")

g, v, a = fx.build_speed_graph("vc", None, three, 0)
check("concat=n=3:v=1:a=0" in g and a is None, "video-only concat when ain is None")

g, _, _ = fx.build_speed_graph("vc", "0:a:0", three, 0, mute_slowed=True)
check(g.count("volume=0") == 1, "mute_slowed adds volume=0 only to the slowed segment")
# it must be in the slowed audio chain
slowed_chain = [p for p in g.split(";") if "volume=0" in p]
check(len(slowed_chain) == 1 and "atrim=start=1.000:end=2.000" in slowed_chain[0],
      "volume=0 lives in the <1x audio chain")

g, _, _ = fx.build_speed_graph("vc", "0:a:0", three, 0, hq_fps=60)
check(g.count("minterpolate") == 1, "hq_fps adds minterpolate only to the slowed segment")
mi_chain = [p for p in g.split(";") if "minterpolate" in p]
check(len(mi_chain) == 1 and "trim=start=1.000:end=2.000" in mi_chain[0],
      "minterpolate lives in the <1x video chain")

# --- crop_filter ---
check(fx.crop_filter(fx.FxTrack("crop"), 0, 4000, 320, 180) == "",
      "crop_filter empty track -> empty string")

c = fx.FxTrack("crop")
c.set_kf(0, (10, 20, 321, 181))
s = fx.crop_filter(c, 0, 4000, 320, 180)
check(s.startswith("crop=320:180:10:20"), f"constant crop -> static even-dim crop ({s})")
check("eval=frame" not in s, "constant crop does not use eval=frame")
check("scale=320:180" in s, "constant crop scales to requested out size")

c = fx.FxTrack("crop")
c.set_kf(0, (0, 0, 320, 180), "linear")
c.set_kf(4000, (300, 160, 320, 180), "linear")
s = fx.crop_filter(c, 0, 4000, 320, 180)
check("eval=frame" in s, "animated crop uses eval=frame scaling")
check("floor((" in s and ")/2)*2" in s, "animated crop floors dims to even")
check("scale=320:180" in s, "animated crop scales to requested out size")

# --- FxState / sidecar roundtrip ---
st = fx.FxState()
check(not st.active, "fresh FxState inactive")
st.speed.set_kf(0, 1.0, "hold")
st.speed.set_kf(2000, 0.5, "hold")
st.crop.set_kf(0, (0, 0, 320, 180), "ease")
st.mute_slowed = True
st.hq_interp = True
check(st.active, "FxState active with keyframes")
check(st.output_duration_ms(0, 4000) == 6000, "FxState.output_duration_ms delegates")

sclip = str(work / "sidecar.mp4")
Path(sclip).write_bytes(b"")
fx.save_fx(sclip, st)
sp = fx.sidecar_path(sclip)
check(sp.exists(), "save_fx writes sidecar for active state")
st2 = fx.load_fx(sclip)
check(st2.to_json() == st.to_json(), "sidecar save/load roundtrip identical")
check(st2.mute_slowed and st2.hq_interp, "flags survive roundtrip")
st2.speed.clear()
st2.crop.clear()
fx.save_fx(sclip, st2)
check(not sp.exists(), "save_fx deletes sidecar when state empty")
check(not fx.load_fx(sclip).active, "load_fx on missing sidecar -> inactive state")

# =====================================================================
# B. Real renders
# =====================================================================
print("--- B. real renders ---")

synth = work / "synth.mp4"
r = run_cmd([FFMPEG, "-hide_banner", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc2=duration=4:size=640x360:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(synth)])
check(r.returncode == 0 and synth.exists(), "synthetic 4s 640x360 clip rendered")
if r.returncode != 0:
    print(r.stderr[-800:])

if synth.exists():
    # (1) speed ramp render: hold 1.0 then 0.5 from midpoint
    t = fx.FxTrack("speed")
    t.set_kf(0, 1.0, "hold")
    t.set_kf(2000, 0.5, "hold")
    segs = fx.speed_segments(t, 0, 4000)
    expect_ms = fx.output_duration_ms(t, 0, 4000)
    graph, vout, aout = fx.build_speed_graph("0:v:0", "0:a:0", segs, 0)
    check(graph != "" and "concat" in graph, "speed ramp graph built")
    ramp_out = work / "ramp.mp4"
    r = run_cmd([FFMPEG, "-hide_banner", "-y", "-v", "error",
                 "-i", str(synth), "-filter_complex", graph,
                 "-map", f"[{vout}]", "-map", f"[{aout}]",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-c:a", "aac", str(ramp_out)])
    check(r.returncode == 0, "speed ramp encode succeeded")
    if r.returncode != 0:
        print(r.stderr[-800:])
    else:
        dur = probe_duration_ms(ramp_out)
        check(abs(dur - expect_ms) <= 150,
              f"ramp output duration {dur:.0f}ms within 150ms of predicted {expect_ms}ms")

    # (2) animated crop pan render
    c = fx.FxTrack("crop")
    c.set_kf(0, (0, 0, 320, 180), "linear")
    c.set_kf(4000, (320, 180, 320, 180), "linear")
    flt = fx.crop_filter(c, 0, 4000, 320, 180)
    crop_out = work / "crop.mp4"
    r = run_cmd([FFMPEG, "-hide_banner", "-y", "-v", "error",
                 "-i", str(synth), "-filter_complex", f"[0:v:0]{flt}[v]",
                 "-map", "[v]", "-an",
                 "-c:v", "libx264", "-preset", "ultrafast", str(crop_out)])
    check(r.returncode == 0, "animated crop encode succeeded")
    if r.returncode != 0:
        print(r.stderr[-800:])
    else:
        dur = probe_duration_ms(crop_out)
        check(abs(dur - 4000) <= 150,
              f"crop render duration unchanged ({dur:.0f}ms ~ 4000ms)")
        rw, rh = probe_resolution(crop_out)
        check((rw, rh) == (320, 180),
              f"crop render resolution {rw}x{rh} == requested 320x180")

        # (3) the crop window actually moves: frames at 0.2s vs 3.5s differ
        def gray_frame(src, at_s, dst):
            rr = run_cmd([FFMPEG, "-hide_banner", "-y", "-v", "error",
                          "-ss", f"{at_s}", "-i", str(src), "-frames:v", "1",
                          "-pix_fmt", "gray", "-f", "rawvideo", str(dst)])
            return rr.returncode == 0 and Path(dst).exists()

        f1, f2 = work / "f1.raw", work / "f2.raw"
        ok1 = gray_frame(crop_out, 0.2, f1)
        ok2 = gray_frame(crop_out, 3.5, f2)
        check(ok1 and ok2, "grabbed gray frames at 0.2s and 3.5s")
        if ok1 and ok2:
            b1, b2 = f1.read_bytes(), f2.read_bytes()
            check(len(b1) == len(b2) == 320 * 180,
                  f"raw frames are 320x180 gray ({len(b1)}, {len(b2)} bytes)")
            if len(b1) == len(b2) and b1:
                diff = sum(abs(x - y) for x, y in zip(b1, b2)) / len(b1)
                check(diff > 10.0,
                      f"crop window moved: mean abs pixel diff {diff:.1f} > 10")

# =====================================================================
# C. Integration (SKIP if not landed)
# =====================================================================
print("--- C. integration ---")

def has_fx_kwarg(func):
    try:
        return "fx" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False

ramp_state = fx.FxState()
ramp_state.speed.set_kf(0, 1.0, "hold")
ramp_state.speed.set_kf(1500, 0.5, "hold")

info = None
try:
    info = core.probe(real_clip)
except Exception as e:  # noqa: BLE001
    skip(f"probe of real clip failed ({e}); core integration checks skipped")

if info is not None and has_fx_kwarg(core.discord_args):
    dst = str(work / "fxcmd.mp4")
    try:
        args = core.discord_args(info, 0, 3000, dst, 720, 30, 2000, 96,
                                 False, "libx264", fx=ramp_state)
        joined = " ".join(str(a) for a in args)
        check("concat" in joined, "discord_args(fx=ramp) command contains concat")
        # output-duration-based reasoning must not crash
        _ = ramp_state.output_duration_ms(0, 3000)
        check(True, "fx output-duration reasoning does not crash")
    except Exception as e:  # noqa: BLE001
        check(False, f"discord_args with fx raised: {e!r}")
else:
    skip("discord_args fx= kwarg not landed yet")

if info is not None and has_fx_kwarg(core.export_discord):
    dur = getattr(info, "duration_ms", 0) or 0
    if dur < 3500:
        skip("real clip too short for fx export check")
    else:
        t_in, t_out = 0, 3000
        dst = str(work / "fxexport.mp4")

        def runner(args, span_s, label):
            print(f"  [export] {label}", flush=True)
            core.run_ffmpeg(args, span_s, label)

        try:
            path, msg, ok = core.export_discord(
                runner, info, t_in, t_out, dst, 20.0, False, fx=ramp_state)
            check(ok and os.path.exists(path), f"fx discord export ok: {msg}")
            pred = ramp_state.output_duration_ms(t_in, t_out)
            got = probe_duration_ms(path)
            check(abs(got - pred) <= 250,
                  f"fx export duration {got:.0f}ms within 250ms of predicted {pred}ms")
            size = os.path.getsize(path)
            check(size < 20e6, f"fx export under 20MB tier ({size / 1e6:.1f} MB)")
        except Exception as e:  # noqa: BLE001
            check(False, f"export_discord with fx raised: {e!r}")
else:
    skip("export_discord fx= kwarg not landed yet")

if info is not None and has_fx_kwarg(core.gif_args):
    try:
        args = core.gif_args(info, 0, 2000, str(work / "fx.gif"), 320, 12,
                             fx=ramp_state)
        check(isinstance(args, list) and args, "gif_args accepts fx kwarg")
    except Exception as e:  # noqa: BLE001
        check(False, f"gif_args with fx raised: {e!r}")
else:
    skip("gif_args fx= kwarg not landed yet")

if hasattr(core, "trim_fx_args"):
    check(callable(core.trim_fx_args), "trim_fx_args exists and is callable")
else:
    skip("trim_fx_args not landed yet")

# --- FxWindow ---
try:
    import mp4trim_fxwin as fxwin  # noqa: E402
except Exception as e:  # noqa: BLE001
    fxwin = None
    skip(f"mp4trim_fxwin not landed yet ({type(e).__name__})")

if fxwin is not None:
    if not hasattr(fxwin, "FxWindow"):
        skip("mp4trim_fxwin has no FxWindow yet")
    else:
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance() or QApplication([sys.argv[0]])
            win = None
            for ctor in ((fx.FxState(), 60_000, (1920, 1080)), (),
                         (fx.FxState(),)):
                try:
                    win = fxwin.FxWindow(*ctor)
                    break
                except TypeError:
                    continue
            if win is None:
                skip("FxWindow constructor signature not recognized")
            else:
                for name in ("set_position", "set_range", "set_frame", "changed"):
                    check(hasattr(win, name), f"FxWindow.{name} exists")
                win.close()
        except Exception as e:  # noqa: BLE001
            check(False, f"FxWindow construction raised: {e!r}")

import shutil  # noqa: E402
shutil.rmtree(work, ignore_errors=True)
print(f"\nFX: {len(skips)} skipped,", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(1 if fails else 0)
