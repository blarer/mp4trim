"""Background QThreads: export jobs, thumbnails, probing, scrub engine."""

import bisect
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from mp4trim_core import (CREATE_NO_WINDOW, TONEMAP, Cancelled, MediaInfo,
                          grab_frame, grab_thumb, probe, probe_keyframes,
                          run_ffmpeg, tool)

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


class SnapGrabber(QThread):
    """One-shot native-resolution frame grab off the UI thread."""

    done = Signal(object)   # QImage | None

    def __init__(self, path: str, ms: int, hdr: bool):
        super().__init__()
        self.path, self.ms, self.hdr = path, ms, hdr

    def run(self):
        try:
            img = grab_frame(self.path, self.ms, native=True, hdr=self.hdr)
        except Exception:  # noqa: BLE001
            img = None
        self.done.emit(img)


class ProbeWorker(QThread):
    """Runs probe() off the UI thread so rapid clip flipping stays smooth."""

    ok = Signal(int, str, object)    # gen, path, MediaInfo
    fail = Signal(int, str, str)     # gen, path, error text

    def __init__(self, gen: int, path: str):
        super().__init__()
        self.gen, self.path = gen, path

    def run(self):
        try:
            info = probe(self.path)
        except Exception as e:  # noqa: BLE001
            self.fail.emit(self.gen, self.path, str(e))
            return
        self.ok.emit(self.gen, self.path, info)


class ScrubEngine(QThread):
    """Frame-exact scrubbing: decodes whole GOPs into RAM once, then serves
    any frame in them instantly.

    Seeking the media player for every mouse move forces a keyframe seek plus
    a decode of up to a full GOP per request, which lags far behind the bar.
    Instead, the first request inside a GOP starts one ffmpeg process that
    decodes that GOP front-to-back into an in-memory frame list (streamed, so
    early frames are usable while later ones still decode), and every further
    request in the GOP is a plain list lookup. An LRU keeps the last few GOPs
    (~{cap} MB). The decoder is aborted when the target moves to a different,
    non-adjacent GOP.
    """

    frame_ready = Signal(int, QImage)   # (ms requested, frame)

    WIDTH = 768
    CACHE_MB = 420
    MAX_GOP_FRAMES = 600
    # Derived byte budget; computed once so the hot evict path avoids multiply.
    CACHE_BYTES = CACHE_MB * 1_048_576

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._evt = threading.Event()
        self._stop = False
        self._proc: subprocess.Popen | None = None
        self._info: MediaInfo | None = None
        self._kfs: list[int] = []
        self._req_ms: int | None = None
        self._cache: dict[int, list[QImage]] = {}   # kf index -> frames
        self._complete: set[int] = set()
        self._lru: list[int] = []
        self._decoding: int | None = None

    # ---- API (UI thread) ----
    def set_source(self, info: MediaInfo | None, kfs: list[int]):
        with self._lock:
            self._info, self._kfs = info, list(kfs)
            for frames in self._cache.values():
                del frames[:]   # release QImage refs immediately on source change
            self._cache.clear()
            self._complete.clear()
            self._lru.clear()
            self._req_ms = None
        self._kill_proc()
        self._evt.set()

    @property
    def ready(self) -> bool:
        return self._info is not None and len(self._kfs) > 0

    def request(self, ms: int) -> bool:
        """Ask for the frame at ms. Returns True if served from cache now."""
        with self._lock:
            info = self._info
            if info is None or not self._kfs:
                return False
            gi, fi = self._locate(ms)
            frames = self._cache.get(gi)
            if frames is not None and fi < len(frames):
                img = frames[fi]
                self._touch(gi)
                self._req_ms = None
                exact = True
            else:
                # provisional: show the nearest decoded frame of this GOP
                # right away so fast sweeps stay live; the exact frame is
                # emitted by the decoder as soon as it reaches it
                img = frames[-1] if frames else None
                exact = False
                self._req_ms = ms
        if img is not None:
            self.frame_ready.emit(ms, img)
        if exact:
            return True
        self._evt.set()
        return False

    def stop(self):
        self._stop = True
        self._evt.set()
        self._kill_proc()
        self.wait(4000)

    # ---- internals ----
    def _locate(self, ms: int) -> tuple[int, int]:
        """(keyframe index, frame offset inside that GOP) for a time."""
        info, kfs = self._info, self._kfs
        gi = max(bisect.bisect_right(kfs, ms) - 1, 0)
        fps = info.fps or 60.0
        fi = max(int((ms - kfs[gi]) * fps / 1000 + 1e-6), 0)
        return gi, min(fi, self.MAX_GOP_FRAMES - 1)

    def _gop_span(self, gi: int) -> tuple[int, int]:
        kfs, info = self._kfs, self._info
        start = kfs[gi]
        end = kfs[gi + 1] if gi + 1 < len(kfs) else info.duration_ms
        return start, end

    def _touch(self, gi: int):
        if gi in self._lru:
            self._lru.remove(gi)
        self._lru.append(gi)

    def _evict(self, frame_bytes: int):
        total = sum(len(v) for v in self._cache.values()) * frame_bytes
        while total > self.CACHE_BYTES and len(self._lru) > 1:
            old = self._lru.pop(0)
            evicted = self._cache.pop(old, None)
            if evicted is not None:
                total -= len(evicted) * frame_bytes
                del evicted[:]   # release QImage refs immediately
                del evicted
            self._complete.discard(old)

    def _kill_proc(self):
        p = self._proc
        if p and p.poll() is None:
            p.kill()

    def run(self):
        while not self._stop:
            self._evt.wait()
            self._evt.clear()
            if self._stop:
                break
            with self._lock:
                info, req = self._info, self._req_ms
                if info is None or not self._kfs or req is None:
                    continue
                gi, _ = self._locate(req)
                if gi in self._complete:
                    self._req_ms = None
                    continue
            self._decode_gop(gi)

    def _decode_gop(self, gi: int):
        with self._lock:
            info = self._info
            start, end = self._gop_span(gi)
            fps = info.fps or 60.0
            n = min(max(int(round((end - start) * fps / 1000)), 1),
                    self.MAX_GOP_FRAMES)
            w = min(self.WIDTH, info.width or self.WIDTH)
            w -= w % 2
            h = max(int(round((info.height or 1) * w / (info.width or 1) / 2))
                    * 2, 2)
            frames = self._cache.setdefault(gi, [])
            self._touch(gi)
            self._evict(w * h * 3)
            done = len(frames)
            hdr, path, start_s = info.hdr, info.path, info.start_s
            self._decoding = gi
        if done >= n:
            with self._lock:
                self._complete.add(gi)
            return
        vf = f"scale={w}:{h}"
        if hdr:
            vf += f",{TONEMAP}"
        seek = (start + done * 1000 / fps) / 1000 + start_s
        cmd = [tool("ffmpeg"), "-v", "error", "-hwaccel", "auto",
               "-ss", f"{seek:.4f}", "-i", path, "-frames:v", str(n - done),
               "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=CREATE_NO_WINDOW)
        except OSError:
            return
        self._proc = proc
        frame_bytes = w * h * 3
        try:
            while not self._stop:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                img = QImage(buf, w, h, w * 3, QImage.Format_RGB888).copy()
                emit = None
                abort = False
                with self._lock:
                    frames.append(img)
                    req = self._req_ms
                    if req is not None:
                        rgi, rfi = self._locate(req)
                        if rgi == gi and rfi < len(frames):
                            emit = (req, frames[rfi])
                            self._req_ms = None
                        elif rgi != gi:
                            # target left this GOP: keep partial, go decode it
                            abort = True
                if emit:
                    self.frame_ready.emit(*emit)
                if abort:
                    proc.kill()
                    return
                if len(frames) >= n:
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            self._proc = None
        emit = None
        with self._lock:
            if len(frames) >= n:
                self._complete.add(gi)
                # serve a request that waited on the tail of this GOP
                req = self._req_ms
                if req is not None:
                    rgi, rfi = self._locate(req)
                    if rgi == gi:
                        emit = (req, frames[min(rfi, len(frames) - 1)])
                        self._req_ms = None
        if emit:
            self.frame_ready.emit(*emit)


