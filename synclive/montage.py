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
    """Aperçu du son de tout le fichier en tâche de fond, pour CHAQUE piste son : niveau (dB) par
    tranche de 50 ms, disponible au fur et à mesure (`tracks[k]`, `levels` = piste affichée,
    `done_until`). Une seule lecture du fichier pour toutes les pistes."""

    STEP = 0.05

    def __init__(self, media: Media, on_progress: Callable[[], None] | None = None):
        super().__init__(daemon=True)
        self.media = media
        n = max(1, int(math.ceil(media.duration / self.STEP)) + 1)
        self.tracks: list[np.ndarray] = [np.full(n, -120.0, np.float32) for _ in range(max(1, len(media.audio)))]
        self.shown = 0                           # piste dessinée dans la timeline
        self.done_until = media.start
        self.finished = False
        self.on_progress = on_progress
        self._halt = False
        self._go = threading.Event()             # en pause pendant un export (il garde le processeur)
        self._go.set()

    @property
    def levels(self) -> np.ndarray:
        return self.tracks[min(self.shown, len(self.tracks) - 1)]

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
            self.finished = True
            return
        try:
            with av.open(self.media.path) as c:
                auds = list(c.streams.audio)
                for a in auds:
                    a.thread_type = "AUTO"
                idx = {a.index: k for k, a in enumerate(auds)}
                last = 0.0
                for pkt in c.demux(*auds):
                    self._go.wait()
                    if self._halt:
                        return
                    k = idx.get(pkt.stream.index)
                    if k is None:
                        continue
                    try:
                        frames = pkt.decode()
                    except av.error.InvalidDataError:
                        continue
                    lv = self.tracks[k]
                    for fr in frames:
                        if fr.time is None:
                            continue
                        x = fr.to_ndarray()
                        peak = float(np.abs(x).max()) if x.size else 0.0
                        if fr.format.name.startswith("s16"):
                            peak /= 32768.0
                        elif fr.format.name.startswith("s32"):
                            peak /= 2147483648.0
                        i = int((float(fr.time) - self.media.start) / self.STEP)
                        if 0 <= i < lv.size:
                            db = 20.0 * math.log10(max(peak, 1e-6))
                            if db > lv[i]:
                                lv[i] = db
                        if k == 0:
                            self.done_until = float(fr.time)
                    if self.on_progress and self.done_until - last > 60.0:
                        last = self.done_until
                        self.on_progress()
        except Exception:
            pass
        self.done_until = self.media.end
        self.finished = True


def _peak_db(fr) -> float:
    x = fr.to_ndarray()
    peak = float(np.abs(x).max()) if x.size else 0.0
    if fr.format.name.startswith("s16"):
        peak /= 32768.0
    elif fr.format.name.startswith("s32"):
        peak /= 2147483648.0
    return 20.0 * math.log10(max(peak, 1e-6))


class LevelReader(threading.Thread):
    """Niveau de CHAQUE piste son autour de la tête de lecture, pour le vumètre, là où l'aperçu en
    tâche de fond n'est pas encore arrivé. Lit en avance (≈ 1 s) à partir de l'instant demandé et
    ne saute que si l'on s'est déplacé ; tranches de 50 ms comme l'aperçu. `level(k, t)` : None
    tant que l'instant n'est pas lu."""

    STEP = Overview.STEP
    AHEAD = 1.0

    def __init__(self, media: Media):
        super().__init__(daemon=True)
        self.media = media
        n = max(1, int(math.ceil(media.duration / self.STEP)) + 1)
        self.tracks: list[np.ndarray] = [np.full(n, -120.0, np.float32) for _ in range(max(1, len(media.audio)))]
        self.read_from = self.read_until = -1.0   # passage lu d'un seul tenant (toutes les pistes)
        self._cond = threading.Condition()
        self._target: float | None = None
        self._halt = False

    def level(self, k: int, t: float) -> float | None:
        if not (self.read_from <= t <= self.read_until) or k >= len(self.tracks):
            return None
        i = int((t - self.media.start) / self.STEP)
        return float(self.tracks[k][i]) if 0 <= i < self.tracks[k].size else None

    def request(self, t: float) -> None:
        with self._cond:
            self._target = t
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._halt = True
            self._cond.notify()

    def _wanted(self) -> float | None:
        """Instant à lire, ou None s'il n'y a rien à faire (déjà lu assez loin)."""
        t = self._target
        if t is None or self.read_from <= t and t + self.AHEAD <= self.read_until:
            return None
        if self.read_until >= self.media.end - 0.05 and self.read_from <= t:
            return None                             # lu jusqu'au bout du fichier
        return t

    def run(self) -> None:
        if not self.media.audio:
            return
        try:
            c = av.open(self.media.path)
        except Exception:
            return
        auds = list(c.streams.audio)
        idx = {a.index: k for k, a in enumerate(auds)}
        last = [-1.0] * len(auds)                   # dernier instant lu de chaque piste
        it = None
        try:
            while True:
                with self._cond:
                    while not self._halt and self._wanted() is None:
                        self._cond.wait(0.5)
                    if self._halt:
                        return
                    t = self._wanted()
                if it is None or not (self.read_from <= t <= self.read_until + 0.5):
                    c.seek(int(max(0.0, t - 0.3) * 1_000_000), backward=True)   # on s'est déplacé
                    it = c.demux(*auds)
                    last = [-1.0] * len(auds)
                    self.read_from = self.read_until = -1.0
                start = None
                goal = t + 2 * self.AHEAD
                try:
                    for pkt in it:
                        k = idx.get(pkt.stream.index)
                        if k is None:
                            continue
                        try:
                            frames = pkt.decode()
                        except av.error.InvalidDataError:
                            continue
                        for fr in frames:
                            if fr.time is None:
                                continue
                            ft = float(fr.time)
                            j = int((ft - self.media.start) / self.STEP)
                            lv = self.tracks[k]
                            if 0 <= j < lv.size:
                                db = _peak_db(fr)
                                same = last[k] >= 0 and int((last[k] - self.media.start) / self.STEP) == j
                                lv[j] = max(lv[j], db) if same else db
                            last[k] = ft
                        lo = min(last)
                        if lo >= 0:
                            if start is None:
                                start = max(last)
                                if self.read_from < 0:
                                    self.read_from = start
                            self.read_until = lo
                        if self._halt:
                            return
                        if lo >= goal or (self._target is not None and abs(self._target - t) > self.AHEAD):
                            break
                    else:
                        self.read_until = self.media.end          # fin du fichier
                except Exception:
                    it = None
        finally:
            c.close()
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


def _norm_segments(segments) -> list[tuple[int, float, float]]:
    """Morceaux (fichier, début, fin) ; les anciens (début, fin) désignent le 1er fichier."""
    out = []
    for seg in segments:
        s, a, b = (0, seg[0], seg[1]) if len(seg) == 2 else seg
        if b - a > 1e-3:
            out.append((int(s), float(a), float(b)))
    return out


def _signature(path: str) -> tuple:
    """Ce qui doit être identique pour mettre des fichiers bout à bout sans réencoder."""
    with av.open(path) as c:
        vids = [s for s in c.streams.video if not L._is_cover(s)]
        v = tuple((s.codec_context.name, s.codec_context.width, s.codec_context.height,
                   bytes(s.codec_context.extradata or b"")) for s in vids)
        a = tuple((s.codec_context.name, s.codec_context.sample_rate, s.codec_context.channels,
                   bytes(s.codec_context.extradata or b"")) for s in c.streams.audio)
    return v, a


def incompatibility(medias: list[Media]) -> str | None:
    """None si les fichiers peuvent être mis bout à bout en recopie, sinon la raison (en clair)."""
    if len(medias) < 2:
        return None
    ref = _signature(medias[0].path)
    for m in medias[1:]:
        sig = _signature(m.path)
        if sig == ref:
            continue
        name = Path(m.path).name
        (rv, ra), (v, a) = ref, sig
        if len(rv) != len(v) or len(ra) != len(a):
            return f"{name} n'a pas le même nombre de pistes vidéo / son que le 1er fichier"
        for x, y in zip(rv, v):
            if x[:3] != y[:3]:
                return (f"{name} : vidéo {y[0]} {y[1]}×{y[2]}, le 1er fichier : {x[0]} {x[1]}×{x[2]} — "
                        "pour les mettre bout à bout sans perte, il faut le même format")
            if x[3] != y[3]:
                return (f"{name} : vidéo encodée avec d'autres réglages que le 1er fichier — pour les mettre "
                        "bout à bout sans perte, il faut les mêmes réglages d'encodage")
        for x, y in zip(ra, a):
            if x[:3] != y[:3]:
                return (f"{name} : son {y[0]} {y[1] // 1000} kHz {y[2]} canaux, le 1er fichier : {x[0]} "
                        f"{x[1] // 1000} kHz {x[2]} canaux")
            if x[3] != y[3]:
                return f"{name} : son encodé avec d'autres réglages que le 1er fichier"
    return None


def export(medias, segments, dst: str | os.PathLike, progress: ProgressCb | None = None,
           cancel: CancelCb | None = None, exact_audio: bool = False) -> Path:
    """Écrit le montage. `medias` : un fichier ou la liste des fichiers du montage ; `segments` :
    (fichier, début, fin) dans l'ordre de sortie ((début, fin) pour un seul fichier). Vidéo
    recopiée (sans perte). Son recopié (rapide, synchro à ½ paquet près) ou, avec exact_audio et un
    seul fichier, recalculé à l'échantillon près (plus lent)."""
    if isinstance(medias, Media):
        medias = [medias]
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    segs = _norm_segments(segments)
    used = sorted({s for s, _a, _b in segs})
    if exact_audio:
        if len(used) > 1:
            raise ValueError("Son recalculé : un seul fichier à la fois (plusieurs fichiers : son recopié)")
        m = medias[used[0]]
        info = _info_for_exact(m)
        L.normalize(m.path, dst, info, L.Target(level=False), progress, cancel, [(a, b) for _s, a, b in segs])
        return dst
    why = incompatibility([medias[i] for i in used])
    if why:
        raise ValueError(why)
    tmp = dst.with_name(dst.stem + ".partiel" + dst.suffix)
    try:
        _copy_segments(medias, segs, tmp, progress, cancel)
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


def _copy_segments(medias: list[Media], segments: list[tuple[int, float, float]], tmp: Path, progress,
                   cancel) -> None:
    """Recopie des morceaux (de un ou plusieurs fichiers de même format) bout à bout. Les instants
    sont recalculés en secondes puis dans la base de temps de la sortie : des fichiers différents
    s'enchaînent sans saut. Son : voir la docstring du module (écart < ½ paquet, sans cumul)."""
    first = medias[segments[0][0]]
    inputs: dict[int, object] = {}

    def opened(s: int):
        if s not in inputs:
            inputs[s] = av.open(medias[s].path)
        return inputs[s]

    try:
        ref = opened(segments[0][0])
        fmt = {".mov": "mov", ".m4v": "ipod", ".3gp": "3gp", ".mp4": "mp4"}.get(tmp.suffix.lower())
        out = av.open(str(tmp), "w", format=fmt)
        try:
            out.metadata.update(ref.metadata)
        except Exception:
            pass
        ovids, oauds = [], []                       # pistes de sortie, dans l'ordre des pistes d'entrée
        for s in ref.streams:
            if s.type == "video" and not L._is_cover(s):
                ov = out.add_stream_from_template(s)
                try:
                    ov.time_base = s.time_base
                    if s.average_rate:
                        ov.codec_context.framerate = s.average_rate
                        ov.codec_context.time_base = s.time_base
                except Exception:
                    pass
                ovids.append(ov)
            elif s.type == "audio":
                oa = out.add_stream_from_template(s)
                try:
                    oa.time_base = s.time_base
                    oa.metadata.update(s.metadata)
                except Exception:
                    pass
                oauds.append(oa)
        total = sum(b - a for _s, a, b in segments) or 1.0
        off = first.start                           # le fichier écrit garde son instant de départ
        a_end: dict[int, float | None] = {k: None for k in range(len(oauds))}   # fin du son écrit (s)
        last_p, last_f, eps = 0.0, -1.0, 1e-4
        for src, sa, sb in segments:
            inp = opened(src)
            vids = [s for s in inp.streams.video if not L._is_cover(s)][:len(ovids)]
            auds = list(inp.streams.audio)[:len(oauds)]
            vmap = {s.index: ovids[k] for k, s in enumerate(vids)}
            amap = {s.index: k for k, s in enumerate(auds)}
            streams = vids + auds
            _seek(inp, sa - 1.0)
            vstate = {si: "wait" for si in vmap}
            adone = {si: False for si in amap}
            seg_shift: dict[int, float | None] = {si: None for si in amap}
            for pkt in inp.demux(*streams):
                if cancel and cancel():
                    raise InterruptedError
                si = pkt.stream.index
                if not pkt.size:
                    continue
                tb = pkt.time_base
                t = float(pkt.pts * tb) if pkt.pts is not None else None
                if si in vmap:
                    if vstate[si] != "done":
                        if vstate[si] == "wait":
                            if not (pkt.is_keyframe and t is not None and t >= sa - eps):
                                continue
                            vstate[si] = "on"
                        if pkt.is_keyframe and t is not None and t >= sb - eps:
                            vstate[si] = "done"
                        else:
                            ost = vmap[si]
                            otb = ost.time_base
                            if pkt.pts is not None:
                                pkt.pts = int(round((off + t - sa) / otb))
                            if pkt.dts is not None:
                                pkt.dts = int(round((off + float(pkt.dts * tb) - sa) / otb))
                            pkt.time_base = otb
                            pkt.stream = ost
                            out.mux(pkt)
                elif si in amap and not adone[si] and t is not None:
                    k = amap[si]
                    d = float((pkt.duration or 0) * tb) or 0.02
                    prev = a_end[k]
                    seg_end = off + (sb - sa)              # fin du morceau dans le fichier écrit
                    if seg_shift[si] is None:
                        # 1er paquet du morceau : celui dont la place (synchro sur la vidéo) tombe au
                        # plus près de la fin du son déjà écrit, sans trou ni chevauchement
                        want = off + (t - sa)
                        if prev is None:
                            if want < off - 1e-6:
                                continue
                            place = want
                        else:
                            if want < prev - d / 2 - 1e-6:
                                continue
                            place = prev
                        seg_shift[si] = place - t
                    place = t + seg_shift[si]
                    if prev is not None and place < prev - 1e-6:
                        continue
                    if place + d / 2 > seg_end + 1e-6:
                        adone[si] = True
                        continue
                    ost = oauds[k]
                    otb = ost.time_base
                    pkt.pts = int(round(place / otb))
                    pkt.dts = pkt.pts
                    pkt.duration = int(round(d / otb))
                    pkt.time_base = otb
                    pkt.stream = ost
                    out.mux(pkt)
                    a_end[k] = place + d
                if all(v == "done" for v in vstate.values()) and all(adone.values()):
                    break
                if progress and t is not None and si in vmap:
                    now = time.perf_counter()
                    if now - last_p > 0.2:                 # 5 fois par seconde suffit (pas à chaque paquet)
                        last_p = now
                        f = min(0.999, (off - first.start + min(max(t - sa, 0.0), sb - sa)) / total)
                        if f > last_f:                     # jamais en arrière (ordre de décodage)
                            last_f = f
                            progress(f)
            off += sb - sa
        out.close()
    finally:
        for c in inputs.values():
            c.close()
    if progress:
        progress(1.0)


def _seek(inp, t: float) -> None:
    """Saut au plus près avant t ; si le fichier ne le permet pas (FLV sans index…), on repart
    du début (plus lent, mais juste)."""
    try:
        inp.seek(int(max(0.0, t) * 1_000_000), backward=True, any_frame=False)
    except Exception:
        inp.seek(0, any_frame=True)


# --- repérage des pauses (écrans d'attente, de pause, image figée) ------------------------
@dataclass
class Pause:
    start: float          # image clé où commence le passage (instant du fichier)
    end: float            # image clé où il se termine
    kind: str             # "fixe" (image qui ne bouge pas) ou "pareil" (comme l'écran d'exemple)
    quiet: bool = False   # son très faible pendant le passage

    @property
    def duration(self) -> float:
        return self.end - self.start


TW, TH = 64, 36       # vignettes (niveaux de gris) des images clés
STILL_DIFF = 4.0      # écart moyen (sur 255) entre deux images clés voisines : en dessous, image fixe
LIKE_DIFF = 7.0       # écart à l'image d'exemple la plus proche : en dessous, « même écran »


@dataclass
class KeyThumbs:
    """Vignettes de toutes les images clés d'un fichier (calculées une fois, en tâche de fond)."""
    times: np.ndarray                 # instants (s)
    thumbs: np.ndarray                # (n, TH, TW) uint8


def key_thumbs(media: Media, progress: ProgressCb | None = None, cancel: CancelCb | None = None) -> KeyThumbs:
    """Images clés seulement (une toutes les 1 à 2 s), réduites en 64×36 : 3 h en ~1 min 30."""
    times: list[float] = []
    thumbs: list[np.ndarray] = []
    if media.has_video:
        with av.open(media.path) as c:
            vs = next(s for s in c.streams.video if not L._is_cover(s))
            vs.thread_type = "AUTO"
            vs.codec_context.skip_frame = "NONKEY"          # images clés seulement
            last = time.perf_counter()
            for f in c.decode(vs):
                if cancel and cancel():
                    raise InterruptedError
                if f.time is None:
                    continue
                times.append(float(f.time))
                thumbs.append(f.reformat(width=TW, height=TH, format="gray").to_ndarray())
                now = time.perf_counter()
                if progress and now - last > 0.25:
                    last = now
                    progress(min(0.999, (float(f.time) - media.start) / max(1e-6, media.duration)))
    if progress:
        progress(1.0)
    arr = np.stack(thumbs) if thumbs else np.zeros((0, TH, TW), np.uint8)
    return KeyThumbs(np.asarray(times, np.float64), arr)


def _runs(flags: np.ndarray, times: np.ndarray, min_duration: float, kind: str, gap: int = 2) -> list[Pause]:
    """Suites d'images clés marquées (avec au plus `gap` images non marquées de suite au milieu :
    bruit d'encodage, logo qui clignote) d'au moins `min_duration` s."""
    out: list[Pause] = []
    n = flags.size
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        miss = 0
        k = i + 1
        while k < n:
            if flags[k]:
                j, miss = k, 0
            else:
                miss += 1
                if miss > gap:
                    break
            k += 1
        a, b = float(times[i]), float(times[j])
        if b - a >= min_duration:
            out.append(Pause(a, b, kind))
        i = j + 1
    return out


def _quiet(p: Pause, media: Media, overview: "Overview | None") -> bool:
    if overview is None or overview.done_until < p.end:
        return False
    lo = int((p.start - media.start) / overview.STEP)
    hi = int((p.end - media.start) / overview.STEP)
    seg = overview.levels[max(0, lo):max(lo + 1, hi)]
    return bool(seg.size) and float(np.percentile(seg, 95)) < -45.0


def detect_still(kt: KeyThumbs, media: Media, min_duration: float = 20.0,
                 overview: "Overview | None" = None) -> list[Pause]:
    """Écrans fixes ou quasi fixes (attente, pause, mire, image figée, noir) d'au moins
    `min_duration` s. Les bornes sont des images clés : couper dessus reste sans perte, et on
    garde la dernière / première image qui bouge."""
    if kt.times.size < 3:
        return []
    th = kt.thumbs.astype(np.int16)
    d = np.abs(th[1:] - th[:-1]).mean(axis=(1, 2))
    still = np.zeros(kt.times.size, bool)
    s = d < STILL_DIFF                       # la paire (i, i+1) est identique
    still[:-1] |= s
    still[1:] |= s
    out = _runs(still, kt.times, min_duration, "fixe", gap=0)     # pas de tolérance : ne rogne pas le live
    for p in out:
        p.quiet = _quiet(p, media, overview)
    return out


def reference_thumbs(media: Media, t: float, half: float = 15.0, fps: float = 4.0) -> np.ndarray:
    """Empreinte d'un écran d'exemple : vignettes à `fps` images/s sur ±`half` s autour de t (une
    animation en boucle est ainsi reconnue à n'importe quel moment de la boucle)."""
    t0, t1 = max(media.start, t - half), min(media.end, t + half)
    out: list[np.ndarray] = []
    with av.open(media.path) as c:
        vs = next(s for s in c.streams.video if not L._is_cover(s))
        vs.thread_type = "AUTO"
        c.seek(int(max(0.0, t0) / vs.time_base), stream=vs, backward=True)
        nxt = t0
        for f in c.decode(vs):
            if f.time is None:
                continue
            ft = float(f.time)
            if ft > t1:
                break
            if ft + 1e-6 >= nxt:
                out.append(f.reformat(width=TW, height=TH, format="gray").to_ndarray())
                nxt = ft + 1.0 / fps
    return np.stack(out) if out else np.zeros((0, TH, TW), np.uint8)


def detect_like(kt: KeyThumbs, media: Media, ref: np.ndarray, min_duration: float = 20.0,
                overview: "Overview | None" = None) -> list[Pause]:
    """Tous les passages qui ressemblent à l'écran d'exemple (même animation, même habillage),
    d'au moins `min_duration` s."""
    if kt.times.size == 0 or ref.shape[0] == 0:
        return []
    K = kt.thumbs.reshape(kt.thumbs.shape[0], -1).astype(np.float32)
    R = ref.reshape(ref.shape[0], -1).astype(np.float32)
    best = np.full(K.shape[0], np.inf, np.float32)
    for k0 in range(0, K.shape[0], 2048):                # par paquets : peu de mémoire même sur 6 h
        blk = K[k0:k0 + 2048]
        dist = np.abs(blk[:, None, :] - R[None, :, :]).mean(axis=2)
        best[k0:k0 + 2048] = dist.min(axis=1)
    # seuil adapté au fichier : le live est loin de l'exemple (écart typique = la médiane), un écran
    # d'attente animé en est proche même s'il bouge (≈ 15 sur une animation rapide, 0 si fixe)
    thr = float(np.clip(0.35 * np.median(best), LIKE_DIFF, 30.0))
    out = _runs(best < thr, kt.times, min_duration, "pareil", gap=2)
    for p in out:
        p.quiet = _quiet(p, media, overview)
    return out


ACTIVE_DB = -45.0      # piste « active » au-dessus de ce niveau (crête par tranche de 50 ms)


def track_activity(overview: "Overview", k: int) -> np.ndarray:
    """Piste k active ou non, par seconde (au moins un pic au-dessus de ACTIVE_DB dans la seconde)."""
    lv = overview.tracks[k]
    per = int(round(1.0 / overview.STEP))
    n = lv.size // per
    return (lv[:n * per].reshape(n, per).max(axis=1) > ACTIVE_DB) if n else np.zeros(0, bool)


def suggest_pause_track(overview: "Overview", min_duration: float = 20.0) -> int | None:
    """La piste qui a le profil « musique des pauses » : muette la plupart du temps, active par
    longs blocs. None si aucune (ou une seule piste)."""
    if len(overview.tracks) < 2:
        return None
    best, best_score = None, 0.0
    for k in range(1, len(overview.tracks)):
        act = track_activity(overview, k)
        if act.size == 0:
            continue
        frac = float(act.mean())
        if not (0.005 <= frac <= 0.6):
            continue
        runs = _runs(act, np.arange(act.size, dtype=np.float64), min_duration, "piste", gap=3)
        if not runs:
            continue
        cover = sum(r.duration for r in runs) / max(1.0, act.sum())     # activité groupée en longs blocs
        if cover > best_score:
            best, best_score = k, cover
    return best if best_score >= 0.6 else None


def detect_track(overview: "Overview", media: Media, k: int, keys: np.ndarray, min_duration: float = 20.0
                 ) -> list[Pause]:
    """Passages où la piste k (par exemple la musique des pauses) est active pendant au moins
    `min_duration` s. Les bornes sont ramenées sur les images clés À L'INTÉRIEUR du passage : on
    coupe sans perte et sans rogner le live."""
    act = track_activity(overview, k)
    if act.size == 0:
        return []
    secs = media.start + np.arange(act.size, dtype=np.float64)
    out = []
    for r in _runs(act, secs, min_duration, "piste", gap=3):
        # activité mesurée par seconde (et le codec étale un peu le début du son) : 1 s de marge
        # à l'intérieur avant de caler sur les images clés — on garde un peu de pause plutôt que
        # de couper du live
        a = r.start + 1.0 if r.start > media.start + 0.5 else r.start      # rien à protéger au début
        b = r.end if r.end + 1.0 < media.end - 0.5 else r.end + 1.0          # ni à la fin du fichier
        if keys.size:
            i = int(np.searchsorted(keys, a - 1e-6))
            j = int(np.searchsorted(keys, b + 1e-6)) - 1
            if i >= keys.size or j < 0 or keys[j] - keys[i] < min_duration:
                continue
            a, b = float(keys[i]), float(keys[j])
        out.append(Pause(a, b, "piste"))
    return out


def merge_pauses(*groups: list[Pause]) -> list[Pause]:
    """Réunit des passages repérés de plusieurs façons (ceux qui se chevauchent n'en font qu'un)."""
    allp = sorted((p for g in groups for p in g), key=lambda p: p.start)
    out: list[Pause] = []
    for p in allp:
        if out and p.start <= out[-1].end + 1e-3:
            q = out[-1]
            q.end = max(q.end, p.end)
            q.kind = "+".join(sorted(set(q.kind.split("+")) | set(p.kind.split("+"))))   # toutes les raisons
            q.quiet = q.quiet and p.quiet
        else:
            out.append(Pause(p.start, p.end, p.kind, p.quiet))
    return out


# --- enregistrement automatique du montage ------------------------------------------------
PROJECT_VERSION = 1


def projects_dir() -> Path:
    """Montages enregistrés (SYNCLIVE_PROJECTS_DIR : un autre dossier, pour les tests)."""
    if os.environ.get("SYNCLIVE_PROJECTS_DIR"):
        return Path(os.environ["SYNCLIVE_PROJECTS_DIR"])
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "SyncLive" / "montages"


def _file_id(path: str) -> dict:
    st = os.stat(path)
    return {"path": str(Path(path).resolve()), "size": st.st_size, "mtime": int(st.st_mtime)}


def project_file(first_path: str) -> Path:
    """Montage enregistré pour ce fichier (le 1er du montage) : un fichier par vidéo, retrouvé
    tant que la vidéo n'a pas changé (même chemin, taille et date)."""
    import hashlib
    fid = _file_id(first_path)
    key = hashlib.sha1(f"{fid['path'].lower()}|{fid['size']}|{fid['mtime']}".encode()).hexdigest()[:16]
    return projects_dir() / f"{key}.json"


def save_project(paths: list[str], state: dict) -> Path | None:
    """Écrit l'état du montage (atomique : un plantage pendant l'écriture ne l'abîme pas)."""
    import json
    try:
        f = project_file(paths[0])
        f.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": PROJECT_VERSION, "saved": time.time(), "files": [_file_id(p) for p in paths], **state}
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
        return f
    except OSError:
        return None


def load_project(first_path: str) -> dict | None:
    """Montage enregistré pour cette vidéo, si tous ses fichiers sont encore là, inchangés."""
    import json
    try:
        f = project_file(first_path)
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("version") != PROJECT_VERSION:
            return None
        for fid in data.get("files", []):
            if not os.path.exists(fid["path"]) or _file_id(fid["path"]) != fid:
                return None
        return data
    except (OSError, ValueError, KeyError):
        return None


def forget_project(first_path: str) -> None:
    try:
        project_file(first_path).unlink()
    except OSError:
        pass


def recent_projects(limit: int = 6) -> list[dict]:
    """Derniers montages enregistrés (les plus récents d'abord), dont les fichiers existent encore."""
    import json
    out = []
    try:
        files = sorted(projects_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("files") and os.path.exists(data["files"][0]["path"]):
                out.append(data)
        except (OSError, ValueError, KeyError):
            continue
        if len(out) >= limit:
            break
    return out
