# SPDX-License-Identifier: GPL-3.0-or-later
"""Détection des claps dans l'audio, précision ~1 ms.

Un clap = transitoire très bref, riche en aigus : montée < 10 ms, décroissance rapide.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import percentile_filter, uniform_filter1d
from scipy.signal import butter, find_peaks, sosfiltfilt


@dataclass
class AudioClap:
    t: float          # début d'attaque (s, timeline conteneur)
    peak_t: float
    snr_db: float
    level: float      # niveau crête relatif (0-1, par rapport au clap le plus fort)
    freq: float | None = None   # fréquence d'un bip tonal (mire SyncLive), None pour un clap


def _envelope(sig: np.ndarray, rate: int) -> np.ndarray:
    sos = butter(4, 1000.0, btype="highpass", fs=rate, output="sos")
    y = sosfiltfilt(sos, sig.astype(np.float64)).astype(np.float32)  # phase nulle : pas de retard
    win = max(1, int(rate * 0.001))
    p = uniform_filter1d(y * y, size=win, mode="nearest")
    return np.sqrt(np.maximum(p, 0.0))  # arrondis float32 : jamais de racine de négatif


def _tone_freq(sig: np.ndarray, rate: int, i0: int) -> float | None:
    """Fréquence d'un bip s'il est tonal (une raie nette), sinon None (clap = bruit large bande)."""
    a, b = i0 + int(0.001 * rate), i0 + int(0.031 * rate)
    if b > sig.size:
        return None
    seg = sig[a:b].astype(np.float64)
    seg = seg - seg.mean()
    n = 1 << 14
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size), n))
    f = np.fft.rfftfreq(n, 1 / rate)
    band = (f > 900) & (f < 4000)
    if not band.any():
        return None
    k = int(np.argmax(np.where(band, spec, 0)))
    peak = spec[k]
    # raie nette : la quasi-totalité de l'énergie de la bande est autour du pic
    near = (f > f[k] - 120) & (f < f[k] + 120)
    tonal = (spec[near] ** 2).sum() / max((spec[band] ** 2).sum(), 1e-12)
    if peak <= 0 or tonal < 0.8:
        return None
    # interpolation parabolique du pic
    if 0 < k < spec.size - 1:
        y0, y1, y2 = np.log(spec[k - 1] + 1e-12), np.log(peak), np.log(spec[k + 1] + 1e-12)
        d = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) if (y0 - 2 * y1 + y2) != 0 else 0.0
        return float(f[k] + d * (f[1] - f[0]))
    return float(f[k])


def detect_claps(sig: np.ndarray, rate: int, t0: float, sensitivity: float = 1.0,
                 min_gap: float = 0.25) -> list[AudioClap]:
    """sensitivity > 1 : détecte des claps plus faibles."""
    if sig.size < rate // 10:
        return []
    env = _envelope(sig, rate)

    # enveloppe décimée à 1 kHz pour la recherche des pics et du bruit de fond
    blk = max(1, rate // 1000)
    nb = env.size // blk
    env_k = env[: nb * blk].reshape(nb, blk).max(axis=1)
    noise = percentile_filter(env_k, 20, size=1001, mode="nearest")
    noise = np.maximum(noise, 1e-6)

    gmax = float(np.percentile(env_k, 99.99)) if env_k.size else 0.0
    if gmax <= 0:
        return []
    rel = 0.12 / sensitivity
    ratio = 8.0 / sensitivity
    height = np.maximum(noise * ratio, gmax * rel)
    peaks, _ = find_peaks(env_k, height=height, distance=max(1, int(min_gap * 1000)))

    claps: list[AudioClap] = []
    for p in peaks:
        # pic précis à pleine résolution
        a = max(0, (p - 2) * blk)
        b = min(env.size, (p + 3) * blk)
        ip = a + int(np.argmax(env[a:b]))
        pv = float(env[ip])
        nz = float(noise[p])
        # début d'attaque : dernier passage sous 20 % avant le pic (jusqu'à 400 ms avant :
        # sur un bip long, le pic peut tomber n'importe où pendant la note)
        lvl = nz + 0.2 * (pv - nz)
        lo = max(0, ip - int(0.400 * rate))
        seg = env[lo: ip + 1]
        below = np.nonzero(seg < lvl)[0]
        start = lo + (int(below[-1]) + 1 if below.size else 0)
        # interpolation linéaire entre start-1 et start
        if start > 0 and env[start] != env[start - 1]:
            frac = (lvl - env[start - 1]) / (env[start] - env[start - 1])
            onset = start - 1 + float(np.clip(frac, 0, 1))
        else:
            onset = float(start)
        if claps and abs((t0 + onset / rate) - claps[-1].t) < 0.05:
            continue  # même note longue détectée deux fois
        # montée : temps pour passer de 20 % à 90 % (attaque franche d'un clap ou d'un bip)
        hi_lvl = nz + 0.9 * (pv - nz)
        up = np.nonzero(env[start: ip + 1] >= hi_lvl)[0]
        rise = ((start + int(up[0])) - onset) / rate if up.size else (ip - onset) / rate
        freq = _tone_freq(sig, rate, int(onset))
        # décroissance : énergie 40-80 ms après le pic
        d0, d1 = ip + int(0.04 * rate), ip + int(0.08 * rate)
        tail = float(env[d0:d1].mean()) if d1 <= env.size and d1 > d0 else 0.0
        if rise > 0.012:
            continue  # attaque molle : voix, musique
        if freq is None and tail > 0.45 * pv:
            continue  # bruit tenu qui n'est ni un clap ni un bip (une note pure peut durer)
        claps.append(AudioClap(
            t=t0 + onset / rate,
            peak_t=t0 + ip / rate,
            snr_db=float(20 * np.log10(pv / nz)),
            level=pv,
            freq=freq,
        ))
    if claps:
        m = max(c.level for c in claps)
        for c in claps:
            c.level /= m
    return claps
