# SPDX-License-Identifier: GPL-3.0-or-later
"""Montage sans perte de longs fichiers (un live entier : 3, 4, 6 h…) : enlever le début, la fin,
les écrans d'attente ou de pause, réordonner des morceaux.

Rapide de bout en bout :
- ouverture immédiate : rien n'est lu en entier (les images clés sont cherchées à la demande,
  quelques ms chacune) ; l'aperçu du son se calcule en tâche de fond ;
- export à la vitesse du disque : vidéo ET son recopiés tels quels (aucun réencodage).

Son recopié : il ne peut être coupé qu'entre ses paquets (≈ 21 ms en AAC). À chaque raccord on
prend les paquets qui recouvrent le morceau au plus juste, et la synchro de chaque morceau est
remise à zéro sur la vidéo : l'écart reste sous ½ paquet (≈ 10 ms) et ne s'accumule pas.
Pour une synchro à l'échantillon, `export(..., exact_audio=True)` recalcule le son (plus lent)."""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import numpy as np

from . import loudness as L

ProgressCb = Callable[[float], None]
CancelCb = Callable[[], bool]
Segment = tuple[float, float]

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mxf", ".ts", ".mts", ".m2ts", ".mpg",
             ".mpeg", ".flv", ".wmv", ".3gp"}


@dataclass
class Media:
    """Ce qu'il faut pour monter un fichier, lu en quelques ms (sans parcourir le fichier)."""
    path: str
    duration: float
    start: float
    has_video: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio: list[dict] = field(default_factory=list)      # {rate, channels, codec, start}
    size: int = 0

    @property
    def end(self) -> float:
        return self.start + self.duration


def open_media(path: str) -> Media:
    with av.open(path) as c:
        dur = c.duration / 1_000_000 if c.duration else 0.0
        start = c.start_time / 1_000_000 if c.start_time is not None else 0.0
        vs = next((s for s in c.streams.video if not L._is_cover(s)), None)
        m = Media(path, dur, start, vs is not None, size=os.path.getsize(path))
        if vs is not None:
            cc = vs.codec_context
            m.width, m.height, m.video_codec = cc.width, cc.height, cc.name
            m.fps = float(vs.average_rate or 0)
            if not dur and vs.duration and vs.time_base:
                m.duration = float(vs.duration * vs.time_base)
        for a in c.streams.audio:
            cc = a.codec_context
            st = float(a.start_time * a.time_base) if a.start_time is not None and a.time_base else start
            m.audio.append(dict(rate=cc.sample_rate, channels=cc.channels, codec=cc.name, start=st,
                                layout=cc.layout.name))
    return m


class KeyIndex:
    """Images clés trouvées à la demande (saut puis lecture de quelques paquets, sans décodage) :
    l'image clé avant et après un instant, en quelques ms même dans un fichier de 6 h."""

    def __init__(self, media: Media):
        self.media = media
        self._lock = threading.Lock()
        self._c = av.open(media.path) if media.has_video else None
        self._vs = next((s for s in self._c.streams.video if not L._is_cover(s)), None) if self._c else None
        self.known: set[float] = set()
        self._cache: dict[int, tuple[float | None, float | None]] = {}

    def close(self) -> None:
        with self._lock:
            if self._c is not None:
                self._c.close()
                self._c = None

    def around(self, t: float) -> tuple[float | None, float | None]:
        """(image clé ≤ t, image clé > t) ; None si aucune (début / fin du fichier)."""
        if self._vs is None:
            return None, None
        key = int(round(t * 1000))
        if key in self._cache:
            return self._cache[key]
        with self._lock:
            before = after = None
            try:
                self._c.seek(int(max(0.0, t) / self._vs.time_base), stream=self._vs, backward=True, any_frame=False)
                n = 0
                for pk in self._c.demux(self._vs):
                    if pk.pts is None or not pk.size:
                        continue
                    tt = float(pk.pts * pk.time_base)
                    if pk.is_keyframe:
                        self.known.add(tt)
                        if tt <= t + 1e-4:
                            before = tt
                        else:
                            after = tt
                            break
                    n += 1
                    if n > 3000:                    # GOP démesuré : on s'arrête
                        break
            except Exception:
                pass
        self._cache[key] = (before, after)
        return before, after

    def snap(self, t: float) -> float:
        """Point de coupe possible le plus proche (image clé, ou fin du fichier)."""
        if self._vs is None:
            return float(min(max(t, self.media.start), self.media.end))
        b, a = self.around(t)
        cand = [x for x in (b, a, self.media.end) if x is not None]
        if not cand:
            return float(t)
        return float(min(cand, key=lambda k: abs(k - t)))

    def step(self, t: float, d: int) -> float:
        """Image clé précédente (d < 0) ou suivante (d > 0)."""
        if self._vs is None:
            return t + d
        if d > 0:
            _b, a = self.around(t + 1e-3)
            return a if a is not None else self.media.end
        b, _a = self.around(t - 1e-3)
        if b is not None and b >= t - 1e-3:
            b, _a = self.around(b - 1e-3)
        return b if b is not None else self.media.start


class Overview(threading.Thread):
    """Aperçu du son de tout le fichier en tâche de fond : niveau (dB) par tranche de 50 ms,
    disponible au fur et à mesure (`levels`, `done_until`)."""

    STEP = 0.05

    def __init__(self, media: Media, on_progress: Callable[[], None] | None = None):
        super().__init__(daemon=True)
        self.media = media
        n = max(1, int(math.ceil(media.duration / self.STEP)) + 1)
        self.levels = np.full(n, -120.0, np.float32)
        self.done_until = media.start
        self.on_progress = on_progress
        self._halt = False
        self._go = threading.Event()             # en pause pendant un export (il garde le processeur)
        self._go.set()

    def stop(self) -> None:
        self._halt = True
        self._go.set()

    def pause(self) -> None:
        self._go.clear()

    def resume(self) -> None:
        self._go.set()

    def run(self) -> None:
        if not self.media.audio:
            self.done_until = self.media.end
            return
        try:
            with av.open(self.media.path) as c:
                a = c.streams.audio[0]
                a.thread_type = "AUTO"
                last = 0.0
                for pkt in c.demux(a):
                    self._go.wait()
                    if self._halt:
                        return
                    try:
                        frames = pkt.decode()
                    except av.error.InvalidDataError:
                        continue
                    for fr in frames:
                        if fr.time is None:
                            continue
                        x = fr.to_ndarray()
                        if x.ndim > 1:
                            x = np.abs(x).max(axis=0) if x.shape[0] < x.shape[1] else np.abs(x).max(axis=1)
                        else:
                            x = np.abs(x)
                        peak = float(x.max()) if x.size else 0.0
                        if fr.format.name.startswith("s16"):
                            peak /= 32768.0
                        elif fr.format.name.startswith("s32"):
                            peak /= 2147483648.0
                        i = int((float(fr.time) - self.media.start) / self.STEP)
                        if 0 <= i < self.levels.size:
                            db = 20.0 * math.log10(max(peak, 1e-6))
                            if db > self.levels[i]:
                                self.levels[i] = db
                        self.done_until = float(fr.time)
                        if self.on_progress and self.done_until - last > 60.0:
                            last = self.done_until
                            self.on_progress()
        except Exception:
            pass
        self.done_until = self.media.end
        if self.on_progress:
            self.on_progress()


def output_path(src: str, out_dir: str | None = None) -> Path:
    p = Path(src)
    folder = Path(out_dir) if out_dir else p.parent
    base = folder / f"{p.stem}_montage{p.suffix}"
    out, k = base, 2
    while out.exists():
        out = base.with_name(f"{p.stem}_montage ({k}){p.suffix}")
        k += 1
    return out


def export(media: Media, segments: list[Segment], dst: str | os.PathLike, progress: ProgressCb | None = None,
           cancel: CancelCb | None = None, exact_audio: bool = False) -> Path:
    """Écrit le montage. Vidéo recopiée (sans perte). Son recopié (rapide, synchro à ½ paquet
    près) ou, avec exact_audio, recalculé à l'échantillon près (plus lent)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    segments = [(a, b) for a, b in segments if b - a > 1e-3]
    if exact_audio:
        info = _info_for_exact(media)
        L.normalize(media.path, dst, info, L.Target(level=False), progress, cancel, segments)
        return dst
    tmp = dst.with_name(dst.stem + ".partiel" + dst.suffix)
    try:
        _copy_segments(media, segments, tmp, progress, cancel)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return dst


def _info_for_exact(media: Media) -> L.FileLoudness:
    """Ce dont l'écriture au son recalculé a besoin, sans mesurer le fichier (niveau gardé)."""
    tracks = []
    with av.open(media.path) as c:
        for k, a in enumerate(c.streams.audio):
            cc = a.codec_context
            t0 = None
            try:
                for fr in c.decode(a):           # instant du 1er échantillon décodé (comme la mesure)
                    t0 = float(fr.time) if fr.time is not None else None
                    break
            except Exception:
                pass
            c.seek(0)
            tracks.append(L.TrackLoudness(k, cc.sample_rate, cc.channels, cc.layout.name, cc.name,
                                          float("nan"), 0.0, float("nan"), media.duration,
                                          start=t0 if t0 is not None else media.start))
    return L.FileLoudness(media.path, media.duration, media.has_video, tracks, [], media.start, media.end)


def _copy_segments(media: Media, segments: list[Segment], tmp: Path, progress, cancel) -> None:
    with av.open(media.path) as inp:
        fmt = {".mov": "mov", ".m4v": "ipod", ".3gp": "3gp", ".mp4": "mp4"}.get(tmp.suffix.lower())
        out = av.open(str(tmp), "w", format=fmt)
        try:
            out.metadata.update(inp.metadata)
        except Exception:
            pass
        vmap, amap = {}, {}
        for s in inp.streams:
            if s.type == "video" and not L._is_cover(s):
                ov = out.add_stream_from_template(s)
                try:
                    ov.time_base = s.time_base
                    if s.average_rate:
                        ov.codec_context.framerate = s.average_rate
                        ov.codec_context.time_base = s.time_base
                except Exception:
                    pass
                vmap[s.index] = ov
            elif s.type == "audio":
                oa = out.add_stream_from_template(s)
                try:
                    oa.time_base = s.time_base
                    oa.metadata.update(s.metadata)
                except Exception:
                    pass
                amap[s.index] = oa
        streams = [s for s in inp.streams if s.index in vmap or s.index in amap]
        total = sum(b - a for a, b in segments) or 1.0
        off = media.start                           # le fichier écrit garde son instant de départ
        a_end = {si: None for si in amap}           # fin du son déjà écrit, par piste (s)
        last_p = 0.0
        last_f = -1.0
        eps = 1e-4
        for sa, sb in segments:
            _seek(inp, sa - 1.0)
            vstate = {si: "wait" for si in vmap}
            vshift = {si: 0 for si in vmap}
            adone = {si: False for si in amap}
            seg_audio_start = {si: None for si in amap}
            for pkt in inp.demux(*streams):
                if cancel and cancel():
                    raise InterruptedError
                si = pkt.stream.index
                if not pkt.size:
                    continue
                tb = pkt.time_base
                t = float(pkt.pts * tb) if pkt.pts is not None else None
                if si in vmap:
                    if vstate[si] == "done":
                        pass
                    else:
                        if vstate[si] == "wait":
                            if not (pkt.is_keyframe and t is not None and t >= sa - eps):
                                continue
                            vstate[si] = "on"
                            vshift[si] = int(round((off - sa) / tb))
                        if pkt.is_keyframe and t is not None and t >= sb - eps:
                            vstate[si] = "done"
                        else:
                            if pkt.pts is not None:
                                pkt.pts += vshift[si]
                            if pkt.dts is not None:
                                pkt.dts += vshift[si]
                            pkt.stream = vmap[si]
                            out.mux(pkt)
                elif si in amap and not adone[si] and t is not None:
                    d = float((pkt.duration or 0) * tb) or 0.02
                    prev = a_end[si]
                    seg_end = off + (sb - sa)              # fin du morceau dans le fichier écrit
                    if seg_audio_start[si] is None:
                        # 1er paquet du morceau : celui dont la place (synchro sur la vidéo) tombe
                        # au plus près de la fin du son déjà écrit, sans le chevaucher
                        want = off + (t - sa)
                        if prev is None:
                            if want < off - 1e-6:               # 1er morceau : rien avant son début
                                continue
                            place = want
                        else:
                            # sans trou ni chevauchement (un lecteur joue le son d'un trait) : il
                            # reprend exactement à prev ; on prend le paquet dont la place idéale
                            # en est la plus proche (écart ≤ ½ paquet, recalé à chaque raccord)
                            if want < prev - d / 2 - 1e-6:
                                continue
                            place = prev
                        seg_audio_start[si] = place - t       # décalage du morceau (s)
                    # dans le morceau : le minutage d'origine des paquets est gardé (trous compris)
                    place = t + seg_audio_start[si]
                    if prev is not None and place < prev - 1e-6:
                        continue                            # chevaucherait : on saute ce paquet
                    if place + d / 2 > seg_end + 1e-6:     # au-delà de la fin du morceau
                        adone[si] = True
                        continue
                    ts = int(round(place / tb))
                    shift = ts - pkt.pts
                    pkt.pts = ts
                    if pkt.dts is not None:
                        pkt.dts += shift
                    pkt.stream = amap[si]
                    out.mux(pkt)
                    a_end[si] = place + d
                if all(v == "done" for v in vstate.values()) and all(adone.values()):
                    break
                if progress and t is not None and si in vmap:
                    now = time.perf_counter()
                    if now - last_p > 0.2:                 # 5 fois par seconde suffit (pas à chaque paquet)
                        last_p = now
                        f = min(0.999, (off - media.start + min(max(t - sa, 0.0), sb - sa)) / total)
                        if f > last_f:                     # jamais en arrière (ordre de décodage)
                            last_f = f
                            progress(f)
            off += sb - sa
        out.close()
    if progress:
        progress(1.0)


def _seek(inp, t: float) -> None:
    """Saut au plus près avant t ; si le fichier ne le permet pas (FLV sans index…), on repart
    du début (plus lent, mais juste)."""
    try:
        inp.seek(int(max(0.0, t) * 1_000_000), backward=True, any_frame=False)
    except Exception:
        inp.seek(0, any_frame=True)
