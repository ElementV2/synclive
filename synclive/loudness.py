# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalisation du niveau sonore (régie : des médias de toutes provenances, au même volume).

Mesure selon ITU-R BS.1770-4 / EBU R128 : niveau intégré (LUFS, double fenêtrage −70 LUFS /
−10 LU), plage de dynamique (LRA, EBU Tech 3342) et crête vraie (dBTP, suréchantillonnage ×4).

Correction : un gain linéaire amène chaque fichier à la cible (la dynamique ne change pas). Si ce
gain ferait dépasser la crête maximale, un limiteur de crête vraie (anticipation, canaux liés)
abaisse seulement ces passages. La vidéo et les autres pistes sont recopiées sans réencodage ; le
son est réencodé dans le même format, au même instant de départ : la synchro est intacte."""
from __future__ import annotations

import math
import os
from fractions import Fraction
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import av
import numpy as np
from scipy.signal import firwin, resample_poly, sosfilt

ProgressCb = Callable[[float], None]
CancelCb = Callable[[], bool]

MEDIA_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mxf", ".ts", ".mts", ".m2ts", ".mpg",
             ".mpeg", ".flv", ".wmv", ".3gp", ".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".aac",
             ".ogg", ".opus", ".wma"}

BLOCK = 64                 # échantillons par bloc du limiteur (≈ 1,3 ms à 48 kHz)
ATTACK_DB_PER_S = 2000.0   # le gain descend au plus vite de 2 dB/ms, avant la crête (anticipation)
RELEASE_DB_PER_S = 30.0    # et remonte doucement après (30 dB/s)
TP_MARGIN_DB = 0.1         # marge sous la crête maximale (arrondis de l'encodeur)
MAX_LIMIT_DB = 6.0         # réduction maximale du limiteur : au-delà le son serait écrasé


@dataclass
class Target:
    lufs: float = -23.0          # niveau intégré visé
    peak: float = -1.0           # crête vraie maximale (dBTP)
    limiter: bool = True         # sinon : gain plafonné pour ne pas dépasser la crête
    level: bool = True           # False : montage seul, le niveau n'est pas touché


PRESETS: list[tuple[str, Target]] = [
    ("EBU R128 — diffusion TV : −23 LUFS, crête −1 dBTP", Target(-23.0, -1.0)),
    ("Plateau, live, streaming : −16 LUFS, crête −1 dBTP", Target(-16.0, -1.0)),
    ("Web, réseaux sociaux : −14 LUFS, crête −1 dBTP", Target(-14.0, -1.0)),
]


@dataclass
class TrackLoudness:
    index: int                    # n° de la piste audio dans le fichier (0 = première)
    rate: int
    channels: int
    layout: str
    codec: str
    integrated: float             # LUFS (-inf : silence)
    lra: float                    # LU
    true_peak: float              # dBTP (-inf : silence)
    duration: float               # s de son
    block_peaks: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))
    sub_power: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0))   # par 100 ms, pondérée
    block_energy: np.ndarray = field(repr=False, default_factory=lambda: np.zeros(0, np.float32))  # par bloc
    start: float = 0.0            # instant du premier échantillon (s, timeline du fichier)

    @property
    def silent(self) -> bool:
        return not math.isfinite(self.integrated)


@dataclass
class FileLoudness:
    path: str
    duration: float
    has_video: bool
    tracks: list[TrackLoudness]
    keyframes: list[float] = field(default_factory=list)    # images clés de la vidéo (s)
    start: float = 0.0                                       # début du fichier (s)
    video_end: float = 0.0                                   # fin de la dernière image vidéo (s)

    @property
    def main(self) -> TrackLoudness | None:
        return self.tracks[0] if self.tracks else None


@dataclass
class Plan:
    """Ce qui sera appliqué à une piste."""
    gain_db: float                # gain linéaire
    limited_db: float             # réduction maximale du limiteur (0 : aucune)
    expected_lufs: float          # niveau attendu après (un peu sous la cible si le limiteur agit)
    expected_peak: float
    note: str = ""


# --- K-weighting (BS.1770), coefficients pour n'importe quelle fréquence ------------------
def k_weighting_sos(rate: int) -> np.ndarray:
    f0, g, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10.0 ** (g / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    s1 = [(vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0,
          1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    a0 = 1.0 + k / q + k * k
    s2 = [1.0, -2.0, 1.0, 1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    return np.array([s1, s2], dtype=np.float64)


def channel_weights(names: list[str]) -> np.ndarray:
    """BS.1770 : avant 1, surround 1,41, LFE exclu."""
    w = []
    for n in names:
        u = n.upper()
        if u == "LFE" or u.startswith("LFE"):
            w.append(0.0)
        elif u in ("SL", "SR", "BL", "BR", "LS", "RS", "SIDE_LEFT", "SIDE_RIGHT", "BACK_LEFT", "BACK_RIGHT"):
            w.append(1.41)
        else:
            w.append(1.0)
    return np.array(w, dtype=np.float64)


def _oversampling(rate: int) -> int:
    return 4 if rate < 96000 else (2 if rate < 192000 else 1)


def _lufs(power: float) -> float:
    return -0.691 + 10.0 * math.log10(power) if power > 0 else -math.inf


class Meter:
    """Mesure en continu (morceau par morceau) : niveau intégré, LRA, crête vraie, et crête de
    chaque bloc du limiteur (suréchantillonnée, canaux liés)."""

    def __init__(self, rate: int, weights: np.ndarray):
        self.rate = rate
        self.w = weights
        self.nch = len(weights)
        self.sos = k_weighting_sos(rate)
        self.zi = np.zeros((self.sos.shape[0], self.nch, 2))    # (sections, canaux, 2)
        self.sub = int(round(rate * 0.1))            # sous-blocs de 100 ms
        self._acc = np.zeros(self.nch)
        self._acc_n = 0
        self.subblocks: list[np.ndarray] = []        # énergie moyenne par canal, par 100 ms
        self.os = _oversampling(rate)
        self._buf = np.zeros((self.nch, 0), np.float64)    # échantillons en attente (crête vraie)
        self._ctx = 0                                       # dont : contexte déjà mesuré
        self.tp = 0.0
        self._bp: list[np.ndarray] = []
        self._bacc = np.zeros(0, np.float32)
        self._be: list[np.ndarray] = []
        self._eacc = np.zeros(0)
        self.samples = 0
        # filtre du suréchantillonnage (le même que resample_poly), calculé une seule fois
        self._h = firwin(2 * 10 * self.os + 1, 1.0 / self.os, window=("kaiser", 5.0)) if self.os > 1 else None
        self._pending: list[np.ndarray] = []
        self._pending_n = 0

    def add(self, x: np.ndarray) -> None:
        """x : (canaux, n) en float, ±1 = pleine échelle. Regroupé par ~1 s avant calcul (les
        images audio font ~20 ms : les traiter une par une coûtait surtout en appels)."""
        if x.size == 0:
            return
        self._pending.append(x)
        self._pending_n += x.shape[1]
        if self._pending_n >= self.rate:
            self._drain()

    def _drain(self) -> None:
        if not self._pending:
            return
        x = self._pending[0] if len(self._pending) == 1 else np.concatenate(self._pending, axis=1)
        self._pending, self._pending_n = [], 0
        self._process(x)

    def _process(self, x: np.ndarray) -> None:
        x = x.astype(np.float64, copy=False)
        n = x.shape[1]
        self.samples += n
        # niveau : K-weighting puis énergie par sous-bloc de 100 ms
        y, self.zi = sosfilt(self.sos, x, axis=1, zi=self.zi)
        sq = y * y
        # énergie pondérée par bloc du limiteur (pour estimer le niveau après limitation)
        wsq = (sq * self.w[:, None]).sum(axis=0)
        eb = np.concatenate([self._eacc, wsq]) if self._eacc.size else wsq
        ne = eb.size // BLOCK
        if ne:
            self._be.append(eb[:ne * BLOCK].reshape(ne, BLOCK).sum(axis=1).astype(np.float32))
        self._eacc = eb[ne * BLOCK:]
        pos = 0
        while pos < n:
            take = min(self.sub - self._acc_n, n - pos)
            self._acc += sq[:, pos:pos + take].sum(axis=1)
            self._acc_n += take
            pos += take
            if self._acc_n == self.sub:
                self.subblocks.append(self._acc / self.sub)
                self._acc = np.zeros(self.nch)
                self._acc_n = 0
        # crête vraie : suréchantillonnage ; les dernières images attendent le morceau suivant
        # (le filtre a besoin du signal de part et d'autre, sinon il « sonne » aux bords)
        buf = np.concatenate([self._buf, x], axis=1) if self._buf.size else x
        end = buf.shape[1] - self.HOLD
        if end > self._ctx:
            self._emit(self._peaks(buf)[self._ctx:end])
            k = max(0, end - self.CTX)
            self._buf, self._ctx = buf[:, k:], end - k
        else:
            self._buf = buf

    CTX = 64       # échantillons de contexte avant
    HOLD = 64      # et après (retenus jusqu'au morceau suivant)

    HOT_DB = 9.0       # suréchantillonné seulement à moins de 9 dB de la crête maximale
    EST = 10 ** (3.0 / 20)   # ailleurs : borne prudente (crête des échantillons + 3 dB)

    def _peaks(self, buf: np.ndarray) -> np.ndarray:
        """Crête par échantillon d'origine, canaux liés. La crête vraie (suréchantillonnage) n'est
        calculée que près des pics : un passage à plus de 9 dB sous la crête maximale ne peut pas
        dépasser le plafond, même limité de 6 dB ; il garde une borne prudente (+3 dB)."""
        sp = np.abs(buf).max(axis=0)
        if self.os <= 1:
            return sp.astype(np.float32)
        thr = max(self.tp, float(sp.max())) * 10 ** (-self.HOT_DB / 20)
        hot = sp >= thr
        per = (sp * self.EST).astype(np.float32)
        if not hot.any():
            return per
        if hot.mean() > 0.3:                          # son dense et fort : tout le morceau
            idx = [(0, buf.shape[1])]
        else:                                         # zones chaudes, élargies de 64 échantillons
            k = np.flatnonzero(hot)
            brk = np.flatnonzero(np.diff(k) > 128)
            starts = np.concatenate([[k[0]], k[brk + 1]])
            ends = np.concatenate([k[brk], [k[-1]]])
            idx = [(max(0, int(a) - 64), min(buf.shape[1], int(b) + 65)) for a, b in zip(starts, ends)]
        for a, b in idx:
            lo, hi = max(0, a - 64), min(buf.shape[1], b + 64)     # contexte du filtre
            seg = buf[:, lo:hi]
            up = np.abs(resample_poly(seg, self.os, 1, axis=1, window=self._h))
            tp = up.reshape(self.nch, hi - lo, self.os).max(axis=2).max(axis=0)
            per[a:b] = np.maximum(tp[a - lo:b - lo], sp[a:b])
        return per

    def _emit(self, per: np.ndarray) -> None:
        if per.size == 0:
            return
        self.tp = max(self.tp, float(per.max()))
        buf = np.concatenate([self._bacc, per]) if self._bacc.size else per
        nb = buf.size // BLOCK
        if nb:
            self._bp.append(buf[:nb * BLOCK].reshape(nb, BLOCK).max(axis=1))
        self._bacc = buf[nb * BLOCK:]

    def _flush(self) -> None:
        if self._buf.size and self._buf.shape[1] > self._ctx:
            self._emit(self._peaks(self._buf)[self._ctx:])
        self._buf = np.zeros((self.nch, 0))
        self._ctx = 0

    def result(self) -> tuple[float, float, float, np.ndarray]:
        """(niveau intégré LUFS, LRA LU, crête vraie dBTP, crêtes par bloc)."""
        self._drain()
        self._flush()
        bp = list(self._bp)
        if self._bacc.size:
            bp.append(np.array([self._bacc.max()], np.float32))
        block_peaks = np.concatenate(bp) if bp else np.zeros(0, np.float32)
        tp = 20.0 * math.log10(self.tp) if self.tp > 0 else -math.inf
        sb = np.array(self.subblocks) if self.subblocks else np.zeros((0, self.nch))
        sp = sb @ self.w if len(sb) else np.zeros(0)
        be = list(self._be)
        if self._eacc.size:
            be.append(np.array([self._eacc.sum()], np.float32))
        self.block_energy = np.concatenate(be) if be else np.zeros(0, np.float32)
        return integrated_from(sp), self._lra(sb), tp, block_peaks, sp

    def _blocks(self, sb: np.ndarray, size: int, hop: int) -> np.ndarray:
        if len(sb) < size:
            return np.zeros(0)
        cs = np.vstack([np.zeros((1, self.nch)), np.cumsum(sb, axis=0)])
        starts = np.arange(0, len(sb) - size + 1, hop)
        z = (cs[starts + size] - cs[starts]) / size            # énergie moyenne par canal
        return z @ self.w

    def _lra(self, sb: np.ndarray) -> float:
        return lra_from(sb @ self.w if len(sb) else np.zeros(0))


def lra_from(sub_power: np.ndarray) -> float:
    """Plage de dynamique (EBU Tech 3342) à partir des énergies pondérées par 100 ms : fenêtres de
    3 s toutes les 100 ms, seuils −70 LUFS et −20 LU, écart entre les centiles 10 et 95."""
    if sub_power.size < 30:
        return 0.0
    cs = np.concatenate([[0.0], np.cumsum(sub_power)])
    p = (cs[30:] - cs[:-30]) / 30.0
    with np.errstate(divide="ignore"):
        l = -0.691 + 10.0 * np.log10(p)
    keep = l > -70.0
    p, l = p[keep], l[keep]
    if p.size == 0:
        return 0.0
    rel = _lufs(float(p.mean())) - 20.0
    l = l[l > rel]
    if l.size < 2:
        return 0.0
    return float(np.percentile(l, 95) - np.percentile(l, 10))


def integrated_from(sub_power: np.ndarray) -> float:
    """Niveau intégré BS.1770 à partir des énergies pondérées par 100 ms : blocs de 400 ms
    (recouvrement 75 %), seuil absolu −70 LUFS puis relatif −10 LU."""
    if sub_power.size < 4:
        return -math.inf
    cs = np.concatenate([[0.0], np.cumsum(sub_power)])
    p = (cs[4:] - cs[:-4]) / 4.0
    with np.errstate(divide="ignore"):
        l = -0.691 + 10.0 * np.log10(p)
    p = p[l > -70.0]
    if p.size == 0:
        return -math.inf
    rel = _lufs(float(p.mean())) - 10.0
    with np.errstate(divide="ignore"):
        l = -0.691 + 10.0 * np.log10(p)
    p = p[l > rel]
    return _lufs(float(p.mean())) if p.size else -math.inf


# --- lecture ---------------------------------------------------------------------------
def _layout_names(layout) -> list[str]:
    try:
        return [ch.name for ch in layout.channels]
    except Exception:
        return [f"c{i}" for i in range(layout.nb_channels)]


def _frames(container, stream):
    """Images audio d'une piste en float planaire (canaux, n), à sa fréquence d'origine."""
    cc = stream.codec_context
    res = av.AudioResampler(format="fltp", layout=cc.layout, rate=cc.sample_rate)
    for frame in container.decode(stream):
        for out in res.resample(frame):
            yield frame, out.to_ndarray()
    for out in res.resample(None):
        yield None, out.to_ndarray()


def _duration(c) -> float:
    if c.duration:
        return c.duration / 1_000_000
    best = 0.0
    for s in c.streams:
        if s.duration and s.time_base:
            best = max(best, float(s.duration * s.time_base))
    return best


def measure(path: str, progress: ProgressCb | None = None, cancel: CancelCb | None = None) -> FileLoudness:
    """Mesure toutes les pistes audio du fichier et relève les images clés de la vidéo, en une
    seule lecture du fichier."""
    with av.open(path) as c:
        dur = _duration(c)
        vids = [s for s in c.streams.video if not _is_cover(s)]
        has_video = bool(vids)
        start = c.start_time / 1_000_000 if c.start_time is not None else 0.0
        auds = list(c.streams.audio)
        keys: list[float] = []
        vend = 0.0
        st = {}
        for k, a in enumerate(auds):
            a.thread_type = "AUTO"
            cc = a.codec_context
            names = _layout_names(cc.layout)
            st[a.index] = dict(k=k, cc=cc, names=names, m=Meter(cc.sample_rate, channel_weights(names)),
                               res=av.AudioResampler(format="fltp", layout=cc.layout, rate=cc.sample_rate), t0=None)
        last = 0.0
        for pkt in c.demux(*auds, *vids[:1]):
            if cancel and cancel():
                raise InterruptedError
            if pkt.stream.type == "video":
                if pkt.pts is not None and pkt.size:
                    t = float(pkt.pts * pkt.time_base)
                    if pkt.is_keyframe:
                        keys.append(t)
                    vend = max(vend, t + float((pkt.duration or 0) * pkt.time_base))
                continue
            x = st[pkt.stream.index]
            try:
                frames = pkt.decode()
            except av.error.InvalidDataError:
                continue                            # paquet abîmé : on passe
            for frame in frames:
                if x["t0"] is None and frame.time is not None:
                    x["t0"] = float(frame.time)
                for out in x["res"].resample(frame):
                    x["m"].add(out.to_ndarray())
                if progress and dur > 0 and frame.time is not None and frame.time - last > 0.5:
                    last = float(frame.time)
                    progress(min(1.0, (last - start) / dur))
        tracks = []
        for a in auds:
            x = st[a.index]
            for out in x["res"].resample(None):
                x["m"].add(out.to_ndarray())
            m, cc = x["m"], x["cc"]
            il, lra, tp, bp, sp = m.result()
            tracks.append(TrackLoudness(x["k"], cc.sample_rate, len(x["names"]), cc.layout.name, cc.name,
                                        il, lra, tp, m.samples / cc.sample_rate, bp, sp, m.block_energy,
                                        x["t0"] if x["t0"] is not None else start))
    if progress:
        progress(1.0)
    return FileLoudness(path, dur, has_video, tracks, sorted(set(keys)), start, vend)


def _scan_video(path: str, cancel: CancelCb | None = None) -> tuple[list[float], float]:
    """Images clés de la première vidéo et fin de sa dernière image (paquets, sans décodage)."""
    out, vend = [], 0.0
    with av.open(path) as c:
        vs = [s for s in c.streams.video if not _is_cover(s)]
        if not vs:
            return [], 0.0
        for pkt in c.demux(vs[0]):
            if cancel and cancel():
                raise InterruptedError
            if pkt.pts is not None and pkt.size:
                t = float(pkt.pts * pkt.time_base)
                if pkt.is_keyframe:
                    out.append(t)
                vend = max(vend, t + float((pkt.duration or 0) * pkt.time_base))
    return sorted(set(out)), vend


# --- montage : morceaux gardés, dans l'ordre de sortie -------------------------------------
Segment = tuple[float, float]


def file_end(info: FileLoudness) -> float:
    """Fin du fichier : la plus tardive de la durée annoncée et de la dernière image vidéo (dans
    certains fichiers la vidéo dépasse un peu la durée du conteneur)."""
    return max(info.start + info.duration, info.video_end)


def snap(info: FileLoudness, t: float) -> float:
    """Point de coupe possible le plus proche : une image clé (copie sans perte), ou n'importe
    quel instant pour un fichier son seul."""
    end = file_end(info)
    if not info.has_video or not info.keyframes:
        return float(min(max(t, info.start), end))
    cand = info.keyframes + [end]
    return float(min(cand, key=lambda k: abs(k - t)))


def full(info: FileLoudness) -> list[Segment]:
    return [(info.start, file_end(info))]


def is_full(info: FileLoudness, segs: list[Segment] | None) -> bool:
    if not segs:
        return True
    return len(segs) == 1 and abs(segs[0][0] - info.start) < 1e-3 and abs(segs[0][1] - file_end(info)) < 1e-3


def kept(segs: list[Segment]) -> float:
    return float(sum(b - a for a, b in segs))


def restrict(track: TrackLoudness, segs: list[Segment] | None) -> TrackLoudness:
    """La même piste réduite aux morceaux gardés (niveau, dynamique, crête recalculés à partir
    de la mesure : pas de nouvelle lecture du fichier)."""
    if not segs or track.silent:
        return track
    sub = int(round(track.rate * 0.1))
    sp, bp, be = [], [], []
    for a, b in segs:
        ia = max(0, int(round((a - track.start) * track.rate)))
        ib = max(ia, int(round((b - track.start) * track.rate)))
        sp.append(track.sub_power[ia // sub:max(ia // sub, ib // sub)])
        bp.append(track.block_peaks[ia // BLOCK:-(-ib // BLOCK)])
        be.append(track.block_energy[ia // BLOCK:-(-ib // BLOCK)])
    spc = np.concatenate(sp) if sp else np.zeros(0)
    bpc = np.concatenate(bp) if bp else np.zeros(0, np.float32)
    tp = float(bpc.max()) if bpc.size else 0.0
    return replace(track, integrated=integrated_from(spc), lra=lra_from(spc),
                   true_peak=20.0 * math.log10(tp) if tp > 0 else -math.inf,
                   duration=kept(segs), block_peaks=bpc, sub_power=spc,
                   block_energy=np.concatenate(be) if be else np.zeros(0, np.float32))


def _is_cover(s) -> bool:
    try:
        return bool(s.disposition & av.stream.Disposition.attached_pic)
    except Exception:
        return False


# --- correction --------------------------------------------------------------------------
def gain_envelope(block_peaks: np.ndarray, gain_db: float, ceiling_db: float, rate: int) -> np.ndarray:
    """Gain (linéaire) au début de chaque bloc et à la fin du dernier : gain linéaire, abaissé
    par le limiteur là où la crête dépasserait le plafond. Toujours ≤ au gain nécessaire dans
    chaque bloc (anticipation : le gain descend avant la crête, remonte doucement après)."""
    n = block_peaks.size
    g = 10.0 ** (gain_db / 20.0)
    ceil = 10.0 ** ((ceiling_db - TP_MARGIN_DB) / 20.0)
    with np.errstate(divide="ignore"):
        need_db = np.minimum(0.0, 20.0 * np.log10(ceil / np.maximum(block_peaks.astype(np.float64) * g, 1e-12)))
    env = need_db.copy()
    if env.size and env.min() < 0.0:
        att = ATTACK_DB_PER_S * BLOCK / rate
        rel = RELEASE_DB_PER_S * BLOCK / rate
        env = _ramps(env, att, rel)
    # aux frontières : le plus petit des deux blocs voisins (l'interpolation reste sous le besoin)
    b = np.empty(n + 1)
    if n:
        b[0] = env[0]
        b[n] = env[-1]
        b[1:n] = np.minimum(env[:-1], env[1:])
    else:
        b[:] = 0.0
    return g * 10.0 ** (b / 20.0)


def _ramps(env: np.ndarray, att: float, rel: float) -> np.ndarray:
    """En dB : le gain descend avant une crête (au plus `att` dB par bloc) et remonte après (au
    plus `rel` dB par bloc), sans jamais dépasser le besoin. Calcul direct : minimum d'une pente
    vers l'arrière (suffixe) puis vers l'avant (préfixe)."""
    i = np.arange(env.size, dtype=np.float64)
    back = np.minimum.accumulate((env + att * i)[::-1])[::-1] - att * i
    return np.minimum.accumulate(back - rel * i) + rel * i


def _after(track: TrackLoudness, gain_db: float, ceiling_db: float) -> tuple[float, float]:
    """(niveau intégré estimé après correction, réduction maximale du limiteur en dB)."""
    env = gain_envelope(track.block_peaks, gain_db, ceiling_db, track.rate)
    g = 10.0 ** (gain_db / 20.0)
    rel = env / g                                           # part du limiteur (≤ 1)
    red = -20.0 * math.log10(max(float(rel.min()), 1e-12)) if rel.size else 0.0
    sp, be = track.sub_power, track.block_energy
    if sp.size == 0 or be.size == 0:
        return track.integrated + gain_db, red
    # énergie de chaque bloc × gain du limiteur au carré sur ce bloc, regroupée par 100 ms
    nb = min(be.size, rel.size - 1)
    r2 = (rel[:nb] ** 2 + rel[1:nb + 1] ** 2) / 2.0
    post = be[:nb].astype(np.float64) * r2
    pre = be[:nb].astype(np.float64)
    per_sub = track.rate * 0.1 / BLOCK
    edges = np.minimum(np.round(np.arange(sp.size + 1) * per_sub).astype(int), nb)
    cpost = np.concatenate([[0.0], np.cumsum(post)])
    cpre = np.concatenate([[0.0], np.cumsum(pre)])
    num = cpost[edges[1:]] - cpost[edges[:-1]]
    den = cpre[edges[1:]] - cpre[edges[:-1]]
    k = np.where(den > 0, num / np.maximum(den, 1e-30), 1.0)
    return integrated_from(sp * (g * g) * k), red


def _fr(v: float) -> str:
    """Nombre à la française : −21,8."""
    return f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",").replace("-", "−")


def plan(track: TrackLoudness, target: Target) -> Plan:
    if not target.level:
        return Plan(0.0, 0.0, track.integrated, track.true_peak, "niveau d'origine (montage seul)")
    if track.silent:
        return Plan(0.0, 0.0, track.integrated, track.true_peak, "silence : rien à corriger")
    gain = target.lufs - track.integrated
    peak_after = track.true_peak + gain
    ceil = target.peak - TP_MARGIN_DB
    if peak_after <= ceil:
        return Plan(gain, 0.0, target.lufs, peak_after)
    if not target.limiter:
        g2 = ceil - track.true_peak
        return Plan(g2, 0.0, track.integrated + g2, ceil,
                    f"Monte jusqu'à {_fr(track.integrated + g2)} LUFS au lieu de {_fr(target.lufs)} : plus "
                    "fort, ses pics dépasseraient la crête max. Le limiteur de crête permet de monter plus haut.")
    # limiteur : on cherche le gain qui donne la cible APRÈS limitation (sinon le son finit plus
    # bas), sans dépasser MAX_LIMIT_DB de réduction
    lo, hi = ceil - track.true_peak, gain + MAX_LIMIT_DB + 6.0    # sans limitation … beaucoup
    lvl_hi, red_hi = _after(track, hi, target.peak)
    if lvl_hi < target.lufs or red_hi > MAX_LIMIT_DB:
        for _ in range(14):                     # le plus fort gain encore acceptable (à 0,001 dB)
            mid = (lo + hi) / 2
            lvl, red = _after(track, mid, target.peak)
            if lvl <= target.lufs and red <= MAX_LIMIT_DB:
                lo = mid
            else:
                hi = mid
        g = lo
    else:
        for _ in range(14):                     # le gain qui donne exactement la cible
            mid = (lo + hi) / 2
            if _after(track, mid, target.peak)[0] < target.lufs:
                lo = mid
            else:
                hi = mid
        g = (lo + hi) / 2
    lvl, red = _after(track, g, target.peak)
    note = f"limiteur : jusqu'à −{red:.1f} dB sur les crêtes" if red >= 0.05 else ""
    if lvl < target.lufs - 0.25:
        note = (f"Monte jusqu'à {_fr(lvl)} LUFS au lieu de {_fr(target.lufs)} : ce fichier a des pics très "
                f"forts (claps, coups, cris). Pour aller plus haut il faudrait les écraser de plus de "
                f"{MAX_LIMIT_DB:.0f} dB, ce qui s'entendrait. Pour que tous les fichiers soient au même "
                f"niveau, « Cible commune » baisse la cible à ce que tout le lot peut atteindre.")
    return Plan(g, max(0.0, red), lvl, ceil, note)


def achievable(track: TrackLoudness, target: Target) -> float:
    """Niveau le plus haut que cette piste peut atteindre sans dépasser la crête maximale (avec
    au plus MAX_LIMIT_DB de limitation si le limiteur est actif)."""
    if track.silent:
        return -math.inf
    ceil = target.peak - TP_MARGIN_DB
    g = ceil - track.true_peak
    if not target.limiter:
        return track.integrated + g
    return _after(track, g + MAX_LIMIT_DB, target.peak)[0]


def common_target(tracks: list[TrackLoudness], target: Target) -> float | None:
    """Cible la plus haute que TOUS les fichiers atteignent (arrondie au dixième vers le bas)."""
    vals = [achievable(t, target) for t in tracks if not t.silent]
    if not vals:
        return None
    return math.floor(min(vals) * 10.0) / 10.0


def _encoder_for(src_codec: str, ext: str, bit_rate: int, channels: int) -> tuple[str, int | None]:
    """Encodeur du son écrit : la même famille de codec que la source (PCM reste PCM, etc.)."""
    ext, c, br = ext.lower(), src_codec, bit_rate or 0
    if c.startswith("pcm_") or c in ("flac", "alac"):
        return c, None
    if c in ("mp3", "mp3float"):
        return "libmp3lame", max(br, 320_000)
    if c in ("mp2", "mp2float"):
        return "mp2", max(br, 384_000)
    if c == "opus":
        return "libopus", max(br, 64_000 * max(1, channels))
    if c in ("ac3", "eac3"):
        return "ac3", max(br, 448_000)
    if c in ("wmav1", "wmav2", "wmapro"):
        return "wmav2", max(br, 192_000)
    if c == "vorbis":
        return "vorbis", None
    # source d'un autre codec : celui qui va avec le conteneur
    if ext in (".ogg", ".oga", ".opus", ".webm"):
        return "libopus", 64_000 * max(1, channels)
    if ext in (".wma", ".wmv", ".asf"):
        return "wmav2", 192_000
    if ext in (".mpg", ".mpeg", ".vob"):
        return "mp2", 384_000
    if ext == ".mp3":
        return "libmp3lame", 320_000
    if ext == ".wav":
        return "pcm_s16le", None
    if ext in (".aif", ".aiff"):
        return "pcm_s16be", None
    # AAC : au moins le débit d'origine, et 256 kb/s en stéréo (96 kb/s par canal au-delà)
    return "aac", max(br, 256_000 if channels <= 2 else 96_000 * channels)


FALLBACK = ("aac", "mp2", "pcm_s16le", "libmp3lame", "ac3")    # si le conteneur refuse l'encodeur


def output_path(src: str, out_dir: str | None, target: Target) -> Path:
    p = Path(src)
    folder = Path(out_dir) if out_dir else p.parent / "normalisé"
    # ex. « clip_norm-23LUFS.mp4 » ; montage seul : « clip_montage.mp4 »
    tag = f"norm{target.lufs:g}LUFS" if target.level else "montage"
    base = folder / f"{p.stem}_{tag}{p.suffix}"
    k = 2
    out = base
    while out.exists():
        out = base.with_name(f"{p.stem}_{tag} ({k}){p.suffix}")
        k += 1
    return out


FADE_S = 0.003      # fondu aux raccords d'un montage (évite les clics), 3 ms


def plans_for(info: FileLoudness, target: Target, segs: list[Segment] | None = None) -> list[Plan]:
    """Ce qui sera appliqué à chaque piste, mesurée sur les seuls morceaux gardés."""
    cut = None if is_full(info, segs) else segs
    return [plan(restrict(t, cut), target) for t in info.tracks]


def normalize(src: str, dst: str | os.PathLike, info: FileLoudness, target: Target,
              progress: ProgressCb | None = None, cancel: CancelCb | None = None,
              segments: list[Segment] | None = None) -> list[Plan]:
    """Écrit `dst` : vidéo et autres pistes recopiées telles quelles (sans réencodage), chaque
    piste audio corrigée. `segments` : morceaux gardés (instants du fichier), dans l'ordre de
    sortie ; leurs bords vidéo sont des images clés (voir `snap`).

    Montage : le son des morceaux est d'abord relu et mesuré exactement tel qu'il sera écrit
    (raccords compris), puis écrit avec le gain et le limiteur calculés sur cette mesure."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    segments = [(a, b) for a, b in (segments or []) if b - a > 1e-3] or None   # morceaux vides : ignorés
    cut = None if is_full(info, segments) else list(segments)
    if cut and target.level:
        # le niveau sera corrigé : le son du montage est d'abord mesuré tel qu'il sera écrit
        tracks = _measure_segments(src, info, cut, (lambda f: progress(0.3 * f)) if progress else None, cancel)
        prog = (lambda f: progress(0.3 + 0.7 * f)) if progress else None
    else:
        tracks = info.tracks
        prog = progress
    plans = [plan(t, target) for t in tracks]
    tmp = dst.with_name(dst.stem + ".partiel" + dst.suffix)
    tries = [None] + list(FALLBACK)
    for i, enc in enumerate(tries):
        try:
            _write(src, tmp, info, tracks, target, plans, prog, cancel, cut, enc)
            os.replace(tmp, dst)
            return plans
        except (InterruptedError, KeyboardInterrupt):
            _unlink(tmp)
            raise
        except Exception:
            _unlink(tmp)
            # le conteneur refuse peut-être cet encodeur de son : on essaie le suivant
            if i == len(tries) - 1:
                raise
    return plans


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def normalize_checked(src: str, dst: str | os.PathLike, info: FileLoudness, target: Target,
                      progress: ProgressCb | None = None, cancel: CancelCb | None = None,
                      segments: list[Segment] | None = None) -> tuple[list[Plan], FileLoudness]:
    """Normalise puis remesure le fichier écrit. Un encodeur avec perte (AAC, MP3…) peut faire
    remonter la crête de quelques dixièmes : si elle dépasse le plafond, on refait avec un
    plafond abaissé d'autant (deux reprises au plus)."""
    t = target
    exp0 = None
    for attempt in range(3):
        p1 = (lambda f: progress(0.8 * f)) if progress and attempt == 0 else None
        plans = normalize(src, dst, info, t, p1, cancel, segments)
        if exp0 is None and plans:
            exp0 = plans[0].expected_lufs
        p2 = (lambda f: progress(0.8 + 0.2 * f)) if progress else None
        after = measure(str(dst), p2, cancel)
        if not target.level:
            break
        excess = max((a.true_peak - target.peak for a in after.tracks if not a.silent), default=-1.0)
        main = after.main
        # l'encodeur (AAC surtout) coupe un peu les aigus : le niveau écrit peut perdre quelques dixièmes
        err = (exp0 - main.integrated) if exp0 is not None and main is not None and not main.silent else 0.0
        if excess <= 0.05 and abs(err) <= 0.15:
            break
        if excess > 0.05:
            t = replace(t, peak=t.peak - excess - 0.1)
        if abs(err) > 0.15 and exp0 >= target.lufs - 0.25:
            t = replace(t, lufs=t.lufs + err)
    return plans, after


def _open_out(inp, tmp, info, tracks, target, plans, force_enc=None):
    fmt = None
    if tmp.suffix.lower() in (".mov", ".m4v", ".mp4", ".3gp"):
        fmt = {".mov": "mov", ".m4v": "ipod", ".3gp": "3gp"}.get(tmp.suffix.lower(), "mp4")
    out = av.open(str(tmp), "w", format=fmt)
    try:
        out.metadata.update(inp.metadata)
    except Exception:
        pass
    copy: dict[int, object] = {}
    audio: dict[int, dict] = {}
    for s in inp.streams:
        if s.type == "audio":
            k = list(inp.streams.audio).index(s)
            cc = s.codec_context
            pl = plans[k]
            tr = tracks[k]
            enc_name, br = _encoder_for(cc.name, tmp.suffix, cc.bit_rate or s.bit_rate or 0, tr.channels)
            if force_enc:
                enc_name, br = force_enc, (max(cc.bit_rate or 0, 256_000) if force_enc not in ("pcm_s16le",) else None)
            os_ = out.add_stream(enc_name, rate=cc.sample_rate)
            if enc_name == "vorbis":
                os_.codec_context.options = {"strict": "experimental"}   # encodeur Vorbis intégré
            os_.layout = cc.layout
            if br:
                os_.bit_rate = br
            try:
                os_.metadata.update(s.metadata)
            except Exception:
                pass
            # enveloppe du limiteur calculée sur le son exactement tel qu'il est écrit (fichier
            # entier, ou montage mesuré à part) : elle se lit à la position de sortie
            use_env = target.level and target.limiter and pl.limited_db > 0
            env = gain_envelope(tr.block_peaks, pl.gain_db, target.peak, cc.sample_rate) if use_env else None
            audio[s.index] = dict(k=k, out=os_, env=env, g=10.0 ** (pl.gain_db / 20.0), n=0,
                                  start=None, rate=cc.sample_rate, layout=cc.layout, t0=info.tracks[k].start,
                                  res=av.AudioResampler(format="fltp", layout=cc.layout, rate=cc.sample_rate),
                                  enc_res=None, sent=0, hold=None, fade_in=0)
        elif s.type == "video" and not _is_cover(s):
            try:
                ov = out.add_stream_from_template(s)
            except Exception:
                continue
            try:                                    # cadence et base de temps (MXF les exige)
                ov.time_base = s.time_base
                if s.average_rate:
                    ov.codec_context.framerate = s.average_rate
                    ov.codec_context.time_base = s.time_base
            except Exception:
                pass
            copy[s.index] = ov
    return out, copy, audio


def _write(src, tmp, info, tracks, target, plans, progress, cancel, segments=None, force_enc=None):
    with av.open(src) as inp:
        out, copy, audio = _open_out(inp, tmp, info, tracks, target, plans, force_enc)
        streams = [s for s in inp.streams if s.index in copy or s.index in audio]
        if segments:
            sink = lambda a, y: _encode(out, a, y)              # noqa: E731
            _walk_segments(inp, streams, copy, audio, segments, sink, out.mux, progress, cancel, info.start)
        else:
            dur = info.duration
            for pkt in inp.demux(*streams):
                if cancel and cancel():
                    raise InterruptedError
                si = pkt.stream.index
                if si in copy:
                    if pkt.size == 0:          # paquet vide de fin de flux (les premiers paquets
                        continue               # d'un MKV n'ont parfois pas de dts : on les garde)
                    pkt.stream = copy[si]
                    out.mux(pkt)
                    continue
                a = audio[si]
                for frame in pkt.decode():
                    if a["start"] is None and frame.pts is not None and frame.time_base is not None:
                        a["start"] = int(round(float(frame.pts * frame.time_base) * a["rate"]))
                    for f2 in a["res"].resample(frame):
                        _encode(out, a, f2.to_ndarray())
                    if progress and dur > 0 and frame.time is not None:
                        progress(min(0.999, (frame.time - info.start) / dur))
            for si, a in audio.items():
                for f2 in a["res"].resample(None):
                    _encode(out, a, f2.to_ndarray())
        for si, a in audio.items():
            _encode(out, a, None)
        out.close()
    if progress:
        progress(1.0)


def _measure_segments(src, info, segments, progress, cancel) -> list[TrackLoudness]:
    """Mesure le son du montage tel qu'il sera écrit (mêmes sauts, mêmes raccords)."""
    with av.open(src) as inp:
        audio, meters = {}, {}
        for s in inp.streams.audio:
            k = list(inp.streams.audio).index(s)
            cc = s.codec_context
            names = _layout_names(cc.layout)
            meters[s.index] = Meter(cc.sample_rate, channel_weights(names))
            audio[s.index] = dict(k=k, rate=cc.sample_rate, layout=cc.layout, t0=info.tracks[k].start,
                                  res=av.AudioResampler(format="fltp", layout=cc.layout, rate=cc.sample_rate),
                                  hold=None, fade_in=0)
        streams = list(inp.streams.audio)
        sink = lambda a, y: meters[[i for i, x in audio.items() if x is a][0]].add(y)    # noqa: E731
        _walk_segments(inp, streams, {}, audio, segments, sink, None, progress, cancel, info.start)
        out = []
        for si, a in audio.items():
            m = meters[si]
            tr = info.tracks[a["k"]]
            il, lra, tp, bp, sp = m.result()
            out.append(replace(tr, integrated=il, lra=lra, true_peak=tp, duration=m.samples / m.rate,
                               block_peaks=bp, sub_power=sp, block_energy=m.block_energy, start=0.0))
        return sorted(out, key=lambda t: t.index)


def _walk_segments(inp, streams, copy, audio, segments, sink, mux, progress, cancel, start0: float = 0.0):
    """Parcourt les morceaux dans l'ordre de sortie : vidéo recopiée paquet par paquet de l'image
    clé a jusqu'à l'image clé b (horodatages décalés bout à bout), son coupé à l'échantillon sur
    les mêmes instants, fondus de 3 ms aux raccords. `sink(a, échantillons)` reçoit le son brut
    (avant gain) dans l'ordre de sortie."""
    total = kept(segments) or 1.0
    last_p = [-1.0]
    off = start0                  # début du morceau dans le fichier écrit (s) : le fichier garde
    eps = 1e-4                    # son instant de départ d'origine (MP3 : 0,023 s, TS…)
    for a in audio.values():
        a["start"] = int(round(start0 * a["rate"]))
    for sa, sb in segments:
        inp.seek(int(max(0.0, sa - 1.0) * 1_000_000), backward=True, any_frame=False)
        vstate = {si: "wait" for si in copy}
        shift = {si: 0 for si in copy}
        astate = {}
        for si, a in audio.items():
            r = a["rate"]
            astate[si] = dict(ia=int(round((sa - a["t0"]) * r)), ib=int(round((sb - a["t0"]) * r)),
                              got=0, done=False)
            a["fade_in"] = int(round(FADE_S * r))
        for pkt in inp.demux(*streams):
            if cancel and cancel():
                raise InterruptedError
            si = pkt.stream.index
            t = None
            if si in copy:
                if pkt.size == 0 or vstate[si] == "done":
                    continue
                tb = pkt.time_base
                t = float(pkt.pts * tb) if pkt.pts is not None else None
                if vstate[si] == "wait":
                    if not (pkt.is_keyframe and t is not None and t >= sa - eps):
                        continue
                    vstate[si] = "on"
                    shift[si] = int(round((off - sa) / tb))
                if pkt.is_keyframe and t is not None and t >= sb - eps:
                    vstate[si] = "done"
                    continue
                if pkt.pts is not None:
                    pkt.pts += shift[si]
                if pkt.dts is not None:
                    pkt.dts += shift[si]
                pkt.stream = copy[si]
                mux(pkt)
            elif si in audio:
                a, st = audio[si], astate[si]
                if st["done"]:
                    continue
                try:
                    decoded = pkt.decode()
                except av.error.InvalidDataError:
                    continue                          # paquet abîmé (souvent juste après un saut)
                for frame in decoded:
                    if frame.pts is None:
                        continue
                    f0 = int(round((float(frame.pts * frame.time_base) - a["t0"]) * a["rate"]))
                    for f2 in a["res"].resample(frame):
                        arr = f2.to_ndarray()
                        n = arr.shape[1]
                        pos = st["ia"] + st["got"]
                        if f0 > pos and pos < st["ib"]:
                            # trou dans le son : silence, pour garder la synchro
                            gap = min(f0, st["ib"]) - pos
                            _seg_audio(a, st, np.zeros((arr.shape[0], gap), np.float32), sink)
                            pos = st["ia"] + st["got"]
                        lo = max(pos, f0)                  # sans recouvrement
                        hi = min(st["ib"], f0 + n)
                        if hi > lo:
                            _seg_audio(a, st, arr[:, lo - f0:hi - f0], sink)
                        f0 += n
                    if st["ia"] + st["got"] >= st["ib"]:
                        st["done"] = True
                        break
            if all(v == "done" for v in vstate.values()) and all(x["done"] for x in astate.values()):
                break
            if progress and si in copy and t is not None:
                # avancement d'après l'instant d'ORIGINE du paquet (t, lu avant le recalage) ;
                # jamais en arrière (la vidéo vient dans l'ordre de décodage)
                f = min(0.999, (off - start0 + min(max(t - sa, 0.0), sb - sa)) / total)
                if f > last_p[0] + 0.002:
                    last_p[0] = f
                    progress(f)
        # fin du morceau : son complété par du silence si le fichier s'arrête avant, fondu de sortie
        for si, a in audio.items():
            st = astate[si]
            miss = st["ib"] - st["ia"] - st["got"]
            if miss > 0:
                _seg_audio(a, st, np.zeros((len(a["layout"].channels), miss), np.float32), sink)
            _seg_flush(a, sink)
        off += sb - sa


def _seg_audio(a, st, arr, sink):
    """Échantillons d'un morceau : fondu d'entrée, puis envoi en retenant les derniers (fondu de
    sortie du morceau)."""
    n = arr.shape[1]
    if n == 0:
        return
    y = arr.astype(np.float64)
    fi = a["fade_in"]
    if st["got"] < fi:
        k = np.arange(st["got"], st["got"] + n, dtype=np.float64)
        y = y * np.minimum(1.0, (k + 0.5) / fi)[None, :]
    st["got"] += n
    hold = a["hold"]
    y = np.concatenate([hold, y], axis=1) if hold is not None and hold.size else y
    keep = int(round(FADE_S * a["rate"]))
    if y.shape[1] > keep:
        sink(a, y[:, :-keep])
        a["hold"] = y[:, -keep:]
    else:
        a["hold"] = y


def _seg_flush(a, sink) -> None:
    """Fin d'un morceau : fondu de sortie sur les échantillons retenus."""
    y = a["hold"]
    a["hold"] = None
    if y is None or y.size == 0:
        return
    n = y.shape[1]
    sink(a, y * ((n - np.arange(n) - 0.5) / n)[None, :])


def _encode(out, a, arr):
    """Gain (et limiteur, lu à la position de sortie) puis encodage ; arr None : fin du flux."""
    os_ = a["out"]
    cc = os_.codec_context
    if a["enc_res"] is None:
        fs = cc.frame_size if cc.frame_size and cc.frame_size > 0 else None
        a["enc_res"] = av.AudioResampler(format=cc.format.name, layout=a["layout"], rate=a["rate"], frame_size=fs)
    if arr is not None:
        n = arr.shape[1]
        s0 = a["n"]
        if a["env"] is None:
            y = arr * a["g"]
        else:
            env = a["env"]
            idx = np.arange(s0, s0 + n, dtype=np.float64) / BLOCK
            g = np.interp(idx, np.arange(env.size, dtype=np.float64), env)
            y = arr * g[None, :]
        a["n"] += n
        y = np.clip(y, -1.0, 1.0).astype(np.float32)
        fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(y), format="fltp", layout=a["layout"])
        fr.sample_rate = a["rate"]
        frames = a["enc_res"].resample(fr)
    else:
        frames = a["enc_res"].resample(None)
    for f in frames:
        f.pts = (a["start"] or 0) + a["sent"]
        f.time_base = Fraction(1, a["rate"])
        a["sent"] += f.samples
        for p in os_.encode(f):
            out.mux(p)
    if arr is None:
        for p in os_.encode(None):
            out.mux(p)
