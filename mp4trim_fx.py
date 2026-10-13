"""Keyframe FX model: speed ramps and animated crop/pan.

Pure Python + string building (no Qt, no subprocess), so it is unit-testable
headless. Consumed by:
  - mp4trim_core.py   -> composes these graphs into export ffmpeg commands
  - mp4trim_fxwin.py  -> the FX editor window edits FxState/FxTrack
  - mp4trim_app.py    -> persistence (sidecar) + estimate label

Conventions
-----------
* All keyframe times are SOURCE milliseconds (same clock as Timeline marks).
* Speed values are factors: 0.25 .. 4.0  (25% .. 400%).
* Crop values are rects (x, y, w, h) in SOURCE pixels.
* Filter expressions use t in SECONDS RELATIVE TO t_in, matching ffmpeg
  behaviour when the export seeks with -ss before -i (timestamps reset).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

SPEED_MIN = 0.25
SPEED_MAX = 4.0
MAX_SEGMENTS = 64          # hard cap on concat segments per export
SUBDIV_MS = 250            # target step when sampling ramped (non-hold) spans

INTERPS = ("hold", "linear", "ease")


def _ease(u: float) -> float:
    """Smoothstep 0..1 -> 0..1."""
    return u * u * (3.0 - 2.0 * u)


@dataclass
class Keyframe:
    ms: int
    value: object                    # float (speed) or (x, y, w, h) tuple
    interp: str = "linear"           # how to blend TOWARDS the next keyframe


class FxTrack:
    """Sorted keyframe list with interpolation."""

    def __init__(self, kind: str):
        assert kind in ("speed", "crop")
        self.kind = kind
        self.kfs: list[Keyframe] = []

    # --- editing ---
    def set_kf(self, ms: int, value, interp: str = "linear"):
        """Add or replace the keyframe at `ms`."""
        if self.kind == "speed":
            value = min(max(float(value), SPEED_MIN), SPEED_MAX)
        else:
            value = tuple(float(v) for v in value)
        if interp not in INTERPS:
            interp = "linear"
        self.remove_kf(ms)
        self.kfs.append(Keyframe(int(ms), value, interp))
        self.kfs.sort(key=lambda k: k.ms)

    def remove_kf(self, ms: int):
        self.kfs = [k for k in self.kfs if k.ms != int(ms)]

    def clear(self):
        self.kfs = []

    def __bool__(self):
        return bool(self.kfs)

    # --- sampling ---
    def value_at(self, ms: float):
        """Interpolated value at source time `ms`."""
        kfs = self.kfs
        if not kfs:
            return 1.0 if self.kind == "speed" else None
        if ms <= kfs[0].ms:
            return kfs[0].value
        if ms >= kfs[-1].ms:
            return kfs[-1].value
        for a, b in zip(kfs, kfs[1:]):
            if a.ms <= ms <= b.ms:
                if a.interp == "hold" or b.ms == a.ms:
                    return a.value
                u = (ms - a.ms) / (b.ms - a.ms)
                if a.interp == "ease":
                    u = _ease(u)
                if self.kind == "speed":
                    return a.value + (b.value - a.value) * u
                return tuple(va + (vb - va) * u
                             for va, vb in zip(a.value, b.value))
        return kfs[-1].value   # unreachable

    def is_constant(self, t_in: int, t_out: int) -> bool:
        """True when the track does not change inside [t_in, t_out]."""
        if not self.kfs:
            return True
        vals = {}
        probes = [t_in, t_out] + [k.ms for k in self.kfs
                                  if t_in <= k.ms <= t_out]
        for p in probes:
            vals[p] = self.value_at(p)
        first = next(iter(vals.values()))
        return all(v == first for v in vals.values())


# ------------------------------------------------------------- speed track

def speed_segments(track: FxTrack, t_in: int, t_out: int,
                   max_segments: int = MAX_SEGMENTS
                   ) -> list[tuple[int, int, float]]:
    """Approximate the speed curve over [t_in, t_out] as constant-speed
    segments [(src_start_ms, src_end_ms, speed), ...].

    Hold spans produce one segment; linear/ease spans are subdivided
    (midpoint-sampled) at ~SUBDIV_MS steps, coarsened to respect the cap."""
    if t_out <= t_in:
        return []
    if not track or track.is_constant(t_in, t_out):
        s = track.value_at((t_in + t_out) / 2) if track else 1.0
        return [(t_in, t_out, round(float(s), 4))]

    # boundaries: clip edges + keyframes inside the range
    cuts = sorted({t_in, t_out, *(k.ms for k in track.kfs
                                  if t_in < k.ms < t_out)})
    spans = list(zip(cuts, cuts[1:]))

    # how finely can we subdivide while respecting the cap?
    ramped = [(a, b) for a, b in spans if not _span_is_hold(track, a, b)]
    budget = max(max_segments - (len(spans) - len(ramped)), len(ramped))
    step = SUBDIV_MS
    while ramped and sum(max(1, math.ceil((b - a) / step))
                         for a, b in ramped) > budget:
        step *= 2

    segs: list[tuple[int, int, float]] = []
    for a, b in spans:
        if _span_is_hold(track, a, b):
            segs.append((a, b, round(float(track.value_at(a)), 4)))
            continue
        n = max(1, math.ceil((b - a) / step))
        for i in range(n):
            sa = a + (b - a) * i // n
            sb = a + (b - a) * (i + 1) // n
            if sb > sa:
                segs.append((sa, sb,
                             round(float(track.value_at((sa + sb) / 2)), 4)))
    # merge neighbours with identical speed
    merged = [segs[0]]
    for s in segs[1:]:
        if abs(s[2] - merged[-1][2]) < 1e-6:
            merged[-1] = (merged[-1][0], s[1], merged[-1][2])
        else:
            merged.append(s)
    return merged


def _span_is_hold(track: FxTrack, a: int, b: int) -> bool:
    va, vb = track.value_at(a), track.value_at((a + b) / 2)
    ve = track.value_at(b - 1e-6)
    return va == vb == ve


def output_duration_ms(track: FxTrack, t_in: int, t_out: int) -> int:
    """Wall-clock duration of the export after speed ramping."""
    return round(sum((b - a) / s for a, b, s in
                     speed_segments(track, t_in, t_out)))


def atempo_chain(speed: float) -> str:
    """atempo only accepts 0.5..2.0 per instance; chain for 0.25..4.0."""
    parts = []
    f = float(speed)
    while f > 2.0 + 1e-9:
        parts.append(2.0)
        f /= 2.0
    while f < 0.5 - 1e-9:
        parts.append(0.5)
        f /= 0.5
    if abs(f - 1.0) > 1e-6 or not parts:
        parts.append(f)
    return ",".join(f"atempo={p:.6g}" for p in parts)


def build_speed_graph(vin: str, ain: str | None,
                      segments: list[tuple[int, int, float]], t_in: int,
                      mute_slowed: bool = False,
                      hq_fps: int = 0) -> tuple[str, str, str | None]:
    """Filter-graph string for the speed ramp.

    vin/ain: input pad labels WITHOUT brackets (e.g. "vc", "0:a:0").
    t_in: export in-point (ms); segment times are made relative to it,
          matching -ss input seeking.
    mute_slowed: silence audio in segments with speed < 1.
    hq_fps: when > 0, run minterpolate at this fps on slowed segments
            (motion-interpolated HQ slow-mo; very slow to encode).

    Returns (graph, vout_label, aout_label) with labels without brackets.
    `graph` is "" when there is exactly one segment at speed 1.0 (no-op)."""
    n = len(segments)
    if n == 0 or (n == 1 and abs(segments[0][2] - 1.0) < 1e-6):
        return "", vin, ain
    want_audio = ain is not None
    chains = []
    for i, (a, b, s) in enumerate(segments):
        sa, sb = (a - t_in) / 1000.0, (b - t_in) / 1000.0
        vflt = (f"trim=start={sa:.3f}:end={sb:.3f},"
                f"setpts=(PTS-STARTPTS)/{s:.6g}")
        if hq_fps and s < 1.0 - 1e-6:
            vflt += (f",minterpolate=fps={hq_fps}:mi_mode=mci:"
                     f"mc_mode=aobmc:me_mode=bidir:vsbmc=1")
        chains.append(f"[{vin}s{i}]{vflt}[v{i}]")
        if want_audio:
            aflt = (f"atrim=start={sa:.3f}:end={sb:.3f},"
                    f"asetpts=PTS-STARTPTS,{atempo_chain(s)}")
            if mute_slowed and s < 1.0 - 1e-6:
                aflt += ",volume=0"
            chains.append(f"[{ain}s{i}]{aflt}[a{i}]")
    vsplit = f"[{vin}]split={n}" + "".join(f"[{vin}s{i}]" for i in range(n))
    parts = [vsplit]
    if want_audio:
        parts.append(f"[{ain}]asplit={n}"
                     + "".join(f"[{ain}s{i}]" for i in range(n)))
    parts += chains
    if want_audio:
        inter = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{inter}concat=n={n}:v=1:a=1[vfx][afx]")
        return ";".join(parts), "vfx", "afx"
    inter = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{inter}concat=n={n}:v=1:a=0[vfx]")
    return ";".join(parts), "vfx", None


# -------------------------------------------------------------- crop track

def _crop_pieces(track: FxTrack, t_in: int, t_out: int
                 ) -> list[tuple[float, float, tuple, tuple]]:
    """Linear pieces [(t0_s, t1_s, rect0, rect1)] relative to t_in, with
    eased spans pre-sampled into short linear steps."""
    cuts = sorted({t_in, t_out, *(k.ms for k in track.kfs
                                  if t_in < k.ms < t_out)})
    pieces = []
    for a, b in zip(cuts, cuts[1:]):
        # find governing keyframe to know the interp towards b
        left = None
        for k in track.kfs:
            if k.ms <= a:
                left = k
        interp = left.interp if left else "linear"
        if interp == "ease" and track.value_at(a) != track.value_at(b):
            n = min(8, max(2, math.ceil((b - a) / 500)))
            for i in range(n):
                sa = a + (b - a) * i / n
                sb = a + (b - a) * (i + 1) / n
                pieces.append(((sa - t_in) / 1000, (sb - t_in) / 1000,
                               track.value_at(sa), track.value_at(sb)))
        else:
            ra = track.value_at(a)
            rb = ra if interp == "hold" else track.value_at(b)
            pieces.append(((a - t_in) / 1000, (b - t_in) / 1000, ra, rb))
    return pieces


def _piecewise_expr(pieces, comp: int) -> str:
    """Nested-if ffmpeg expression for component `comp` of the rect."""
    expr = f"{pieces[-1][3][comp]:.2f}"            # after the last piece
    for t0, t1, r0, r1 in reversed(pieces):
        v0, v1 = r0[comp], r1[comp]
        if abs(v1 - v0) < 1e-6 or t1 <= t0:
            seg = f"{v0:.2f}"
        else:
            seg = (f"({v0:.2f}+({v1 - v0:.2f})*"
                   f"(t-{t0:.3f})/{t1 - t0:.3f})")
        expr = f"if(lt(t\\,{t1:.3f})\\,{seg}\\,{expr})"
    return expr


def crop_filter(track: FxTrack, t_in: int, t_out: int,
                out_w: int, out_h: int) -> str:
    """`crop=...,scale=...` chain for an animated crop window, scaling to a
    constant out_w x out_h so the encoder sees a fixed frame size.

    Returns "" when the track is empty. For a constant rect a static crop
    is emitted (cheaper)."""
    if not track:
        return ""
    if track.is_constant(t_in, t_out):
        x, y, w, h = track.value_at(t_in)
        w, h = int(w) // 2 * 2, int(h) // 2 * 2
        return (f"crop={w}:{h}:{int(x)}:{int(y)},"
                f"scale={out_w}:{out_h}:flags=lanczos,setsar=1")
    pieces = _crop_pieces(track, t_in, t_out)
    ex = {c: _piecewise_expr(pieces, i)
          for i, c in enumerate("xywh")}
    # even dims keep yuv420p happy
    w = f"floor(({ex['w']})/2)*2"
    h = f"floor(({ex['h']})/2)*2"
    return (f"crop=w='{w}':h='{h}':x='{ex['x']}':y='{ex['y']}',"
            f"scale={out_w}:{out_h}:flags=lanczos:eval=frame,setsar=1")


# ---------------------------------------------------------------- FxState

@dataclass
class FxState:
    """Everything the FX editor edits, persisted as a sidecar JSON."""
    speed: FxTrack = field(default_factory=lambda: FxTrack("speed"))
    crop: FxTrack = field(default_factory=lambda: FxTrack("crop"))
    mute_slowed: bool = False
    hq_interp: bool = False          # minterpolate on slowed segments

    @property
    def active(self) -> bool:
        return bool(self.speed) or bool(self.crop)

    def output_duration_ms(self, t_in: int, t_out: int) -> int:
        return output_duration_ms(self.speed, t_in, t_out)

    # --- persistence ---
    def to_json(self) -> dict:
        def dump(tr):
            return [{"ms": k.ms, "value": k.value, "interp": k.interp}
                    for k in tr.kfs]
        return {"version": 1, "speed": dump(self.speed),
                "crop": dump(self.crop), "mute_slowed": self.mute_slowed,
                "hq_interp": self.hq_interp}

    @classmethod
    def from_json(cls, data: dict) -> "FxState":
        st = cls()
        for kind in ("speed", "crop"):
            tr = getattr(st, kind)
            for k in data.get(kind, []):
                try:
                    tr.set_kf(k["ms"], k["value"], k.get("interp", "linear"))
                except (KeyError, TypeError, ValueError):
                    continue
        st.mute_slowed = bool(data.get("mute_slowed", False))
        st.hq_interp = bool(data.get("hq_interp", False))
        return st


def sidecar_path(clip: str) -> Path:
    return Path(str(clip) + ".fx.json")


def load_fx(clip: str) -> FxState:
    p = sidecar_path(clip)
    try:
        return FxState.from_json(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return FxState()


def save_fx(clip: str, fx: FxState):
    p = sidecar_path(clip)
    if not fx.active:
        try:
            p.unlink()
        except OSError:
            pass
        return
    p.write_text(json.dumps(fx.to_json(), indent=1), encoding="utf-8")
