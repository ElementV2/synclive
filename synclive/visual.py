# SPDX-License-Identifier: GPL-3.0-or-later
"""Instant de l'événement visuel associé à un son.

Deux types d'événements :

* « clap » : les mains accélèrent puis s'arrêtent net au contact. Entre deux images
  k-1 et k, l'énergie de mouvement est proportionnelle à la fraction de l'intervalle
  pendant laquelle les mains bougent ; si le contact a lieu dans l'intervalle m,
  E_m ≈ f · E_(m-1) et t_contact = t_(m-1) + f·T (précision sous l'image).
* « flash » : mire de synchro (PhotoJoseph, mire SyncLive…) — une image change d'un
  coup puis revient. L'événement est l'instant d'affichage de la première image du flash.
"""
from __future__ import annotations

import colorsys
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import binary_dilation

from .media import GRID_COLS, GRID_ROWS, MotionScan
from .shots import Shot

MIN_PEAK = 2.0        # énergie minimale (niveaux de gris moyens / 255) d'un vrai événement
MAX_CELLS = 32          # grille 32x18 : 4 fois plus de cellules qu en 16x9
DROP = 0.55           # seuil de retombée du mouvement (fraction du pic) = fin du clap


@dataclass
class VisualCandidate:
    t: float              # instant estimé de l'événement visuel
    strength: float       # 0-1, force relative dans la fenêtre
    kind: str             # "clap" | "flash"
    hue: float | None = None   # teinte du flash (0-1), pour distinguer les sources de mire


def roi_from_boxes(boxes: list[tuple[float, float, float, float]]) -> np.ndarray | None:
    if not boxes:
        return None
    m = np.zeros((GRID_ROWS, GRID_COLS), bool)
    for x0, y0, x1, y1 in boxes:
        cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) * 1.4, (y1 - y0) * 1.4
        c0 = int(np.clip(np.floor((cx - w / 2) * GRID_COLS), 0, GRID_COLS - 1))
        c1 = int(np.clip(np.ceil((cx + w / 2) * GRID_COLS), 1, GRID_COLS))
        r0 = int(np.clip(np.floor((cy - h / 2) * GRID_ROWS), 0, GRID_ROWS - 1))
        r1 = int(np.clip(np.ceil((cy + h / 2) * GRID_ROWS), 1, GRID_ROWS))
        m[r0:r1, c0:c1] = True
    return binary_dilation(m, iterations=1)


class ShotSignal:
    """Accès au mouvement par cellule d'un plan, avec ligne de base par cellule."""

    def __init__(self, scan: MotionScan, shot: Shot):
        self.scan = scan
        self.shot = shot
        self.t = scan.times[shot.i0:shot.i1]
        self.cells = scan.cells[shot.i0:shot.i1].reshape(len(self.t), -1).astype(np.float32)
        if len(self.t) > 1:
            self.cells[0] = 0.0  # différence à travers la coupe
            self.base = np.median(self.cells[1:], axis=0)
        else:
            self.base = np.zeros(self.cells.shape[1], np.float32)
        # cellules dont le centre est dans la vignette (multiview) ; tout l'écran sinon
        x0, y0, x1, y1 = getattr(shot, "region", (0.0, 0.0, 1.0, 1.0))
        cy = (np.arange(GRID_ROWS) + 0.5) / GRID_ROWS
        cx = (np.arange(GRID_COLS) + 0.5) / GRID_COLS
        self.region_mask = ((cy[:, None] >= y0) & (cy[:, None] < y1) &
                            (cx[None, :] >= x0) & (cx[None, :] < x1)).reshape(-1)
        th, tw = scan.thumbs.shape[1:3]
        self.ty = slice(int(round(y0 * th)), max(int(round(y0 * th)) + 1, int(round(y1 * th))))
        self.tx = slice(int(round(x0 * tw)), max(int(round(x0 * tw)) + 1, int(round(x1 * tw))))

    def window(self, t_audio: float, half: float) -> tuple[int, int]:
        lo = max(1, int(np.searchsorted(self.t, t_audio - half)) - 1)
        hi = min(len(self.t) - 1, int(np.searchsorted(self.t, t_audio + half)) + 1)
        return lo, hi

    def energy(self, t_audio: float, half: float, mask: np.ndarray | None = None) -> tuple[np.ndarray, int, int]:
        """Énergie sur les cellules les plus actives autour de l'événement."""
        lo, hi = self.window(t_audio, half)
        if hi - lo < 3:
            return np.zeros(0), lo, hi
        seg = self.cells[lo:hi + 1] - self.base[None, :]
        peak = seg.max(axis=0)
        score = np.where(self.region_mask, peak / (self.base + 2.0), 0.0)
        if mask is not None:
            m = mask.reshape(-1)
            if (score[m] > 0.5 * score.max()).sum() >= 1:
                score = np.where(m, score, 0.0)
        best = float(score.max())
        if best <= 0:
            return np.zeros(hi - lo + 1), lo, hi
        order = np.argsort(score)[::-1]
        sel = [i for i in order[:MAX_CELLS] if score[i] >= 0.5 * best]
        e = np.maximum(seg[:, sel], 0.0).mean(axis=1).astype(np.float64)
        return e, lo, hi


def _flash_hue(thumbs: np.ndarray, k: int, region: np.ndarray) -> float | None:
    if region.sum() < 2:
        return None
    rgb = thumbs[k][region].astype(np.float64).mean(axis=0) / 255.0
    h, s, v = colorsys.rgb_to_hsv(*rgb)
    return float(h) if s > 0.35 and v > 0.25 else None


def flash_candidates(sig: ShotSignal, t_audio: float, window: float) -> list[VisualCandidate]:
    """Impulsions : une zone change à l'image k puis revient à l'identique en ≤ 3 images.
    Un objet qui se déplace (curseur de mire, mains) ne « revient » pas ; un flash, si."""
    scan, shot = sig.scan, sig.shot
    lo, hi = sig.window(t_audio, window)
    th = scan.thumbs[:, sig.ty, sig.tx]        # vignette seulement (multiview)
    imp = np.zeros(hi - lo + 1)
    regions = {}
    for j, k in enumerate(range(shot.i0 + lo, shot.i0 + hi + 1)):
        if k < 1 or k + 1 >= scan.times.size or k + 1 >= shot.i1:
            continue
        prev = th[k - 1].astype(np.int16)
        d = np.abs(th[k].astype(np.int16) - prev).sum(axis=2)
        R = d > 45
        nR = int(R.sum())
        if nR < 3:
            continue
        best, ratio = 0.0, 1.0
        jump = float(d[R].mean())
        for g in range(1, 4):
            if k + g >= shot.i1:
                break
            back = np.abs(th[k + g].astype(np.int16) - prev).sum(axis=2)
            best = max(best, float((back[R] < 30).mean()))
            # écart moyen de retour rapporté au saut : ~0 pour un flash, ~0,5 pour un objet qui bouge
            ratio = min(ratio, float(back[R].mean()) / max(jump, 1e-6))
        if best >= 0.75 or ratio <= 0.35:
            imp[j] = nR * max(best, 1.0 - ratio)
            regions[j] = R
    if imp.max() < 3:
        return []
    out = []
    peak = imp.max()
    for j in range(imp.size):
        # début d'impulsion seulement (l'image suivante qui « revient » ne compte pas)
        if imp[j] >= 0.3 * peak and (j == 0 or imp[j - 1] < 0.3 * peak):
            k = shot.i0 + lo + j
            out.append(VisualCandidate(t=float(scan.times[k]), strength=float(imp[j] / peak),
                                       kind="flash", hue=_flash_hue(th, k, regions[j])))
    return [c for c in out if abs(c.t - t_audio) <= window]


def find_candidates(sig: ShotSignal, t_audio: float, window: float,
                    mask: np.ndarray | None = None) -> list[VisualCandidate]:
    flashes = flash_candidates(sig, t_audio, window)
    claps = clap_candidates(sig, t_audio, window, mask)
    # un clap qui coïncide avec un flash (à 2 images près) est le même événement
    per = float(np.median(np.diff(sig.t))) if sig.t.size > 1 else 1 / 30
    claps = [c for c in claps if all(abs(c.t - f.t) > 2.5 * per for f in flashes)]
    return flashes + claps


def clap_candidates(sig: ShotSignal, t_audio: float, window: float,
                    mask: np.ndarray | None = None) -> list[VisualCandidate]:
    e, lo, hi = sig.energy(t_audio, window, mask)
    if e.size < 3:
        return []
    t = sig.t
    wmax = float(e.max())
    if wmax < MIN_PEAK:
        return []        # rien de visible : clap hors champ, mire sans flash…
    out: list[VisualCandidate] = []
    n = e.size
    k = 1
    while k < n - 1:
        ek = e[k]
        if not (ek >= 0.3 * wmax and ek >= MIN_PEAK and ek >= e[k - 1] and ek >= e[k + 1]):
            k += 1
            continue
        thr = DROP * ek     # sur un vrai tournage il reste du mouvement résiduel après contact
        r = k + 1
        bigger = False
        while r < n and e[r] > thr:
            if e[r] > 1.15 * ek:     # le mouvement repart plus fort : un autre geste (le clap ?) commence
                bigger = True
                break
            r += 1
        if bigger or r >= n:  # pas un arrêt net de CE geste : on examine le pic suivant
            k += 1
            continue
        m = r - 1
        am, ar = lo + m, lo + r
        if m > k:
            ref = e[m - 1]
            f = float(np.clip(e[m] / ref, 0, 1)) if ref > 0 else 1.0
        else:
            f = 1.0
        if f < 0.85:
            tc = t[am - 1] + f * (t[am] - t[am - 1])
        else:
            fr = float(np.clip(e[r] / max(e[m], 1e-9), 0, 1))
            tc = t[am] + fr * (t[ar] - t[am])
        # un clap s'arrête net (< 80 ms) ; un geste normal ralentit progressivement
        sharp = 1.0 if t[ar] - t[lo + k] <= 0.08 else 0.35
        out.append(VisualCandidate(t=float(tc), strength=float(ek / wmax * sharp), kind="clap"))
        k = r + 1
    return [c for c in out if abs(c.t - t_audio) <= window]
