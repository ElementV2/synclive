# SPDX-License-Identifier: GPL-3.0-or-later
"""Découpe en plans et regroupement des plans par caméra."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist

from .media import MotionScan


@dataclass
class Shot:
    index: int
    i0: int          # première image (incluse)
    i1: int          # dernière image (exclue)
    start: float
    end: float
    camera: int = 0
    look: int = 0               # groupe d'apparence
    qr: str | None = None       # identifiant de mire SyncLive lu dans le QR (« 07 · ATLAS »)
    region: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)   # vignette (x0, y0, x1, y1)
    tile: str | None = None     # libellé de la vignette (« vignette haut gauche »), None = plein écran
    mire: bool = False          # affiche une mire (flashs réguliers), même sans bips enregistrés

    @property
    def duration(self) -> float:
        return self.end - self.start


def _is_flash(scan: MotionScan, k: int, thr: float) -> bool:
    """Changement brutal qui revient à l'image d'avant en ≤ 4 images : flash de mire, pas une coupe."""
    if k < 1:
        return False
    before = scan.thumbs[k - 1].astype(np.int16)
    jump = np.abs(scan.thumbs[k].astype(np.int16) - before).mean()
    for j in range(k + 1, min(scan.times.size, k + 5)):
        # l'image redevient bien plus proche de l'avant-flash que du flash lui-même
        back = np.abs(scan.thumbs[j].astype(np.int16) - before).mean()
        if back < 0.35 * jump or back / 255.0 < 0.4 * thr:
            return True
    # fin d'impulsion : l'image k revient à ce qu'on voyait juste avant le flash
    cur = scan.thumbs[k].astype(np.int16)
    for g in range(2, 5):
        if k - g < 0:
            break
        if np.abs(cur - scan.thumbs[k - g].astype(np.int16)).mean() < 0.35 * jump:
            return True
    return False


DUP_DIFF = 8e-4   # écart de vignette d'une image répétée (≤ 3e-4 mesuré) ; une vraie image fixe : ≥ 1e-3


def _motion_base(d: np.ndarray, k: int, count: int = 5, reach: int = 40) -> float:
    """Écart « normal » autour de l'image k : médiane des `count` images voisines de chaque côté
    qui changent vraiment. Les images répétées (écart ~0) sont ignorées : sinon, dans une vidéo
    dont la source tourne à 15 i/s, la base vaut 0 et le moindre geste rapide passe pour une coupe."""
    n = d.size
    before = [d[j] for j in range(k - 1, max(0, k - reach), -1) if d[j] > DUP_DIFF][:count]
    after = [d[j] for j in range(k + 1, min(n, k + reach)) if d[j] > DUP_DIFF][:count]
    neigh = before + after
    return float(np.median(neigh)) if neigh else 0.0


SCENE_CHANGE = 0.04   # écart de décor minimal d'une vraie coupe (claps devant la webcam : ~0,02)


def _scene_change(scan: MotionScan, k: int, window: float = 0.25) -> float:
    """Écart de décor entre avant et après l'image k : médiane temporelle de chaque côté (efface ce
    qui bouge, les mains), puis médiane spatiale de l'écart (ignore ce qui reste d'un geste)."""
    n = scan.times.size
    period = float(np.median(np.diff(scan.times))) if n > 1 else 1 / 30
    w = max(4, int(round(window / max(period, 1e-3))))
    a = np.median(scan.thumbs[max(0, k - w):k], axis=0)
    b = np.median(scan.thumbs[k:min(n, k + w)], axis=0)
    return float(np.median(np.abs(a - b))) / 255.0


def detect_shots(scan: MotionScan, cut_threshold: float = 0.05,
                 min_shot: float = 0.4) -> list[Shot]:
    n = scan.times.size
    d = scan.thumb_diff.astype(np.float64)
    cuts = [0]
    for k in range(1, n):
        if d[k] < cut_threshold or scan.frac_moving[k] < 0.5:
            continue
        base = _motion_base(d, k)
        if d[k] < 2.5 * base + 1e-3:
            continue
        if _is_flash(scan, k, cut_threshold):
            continue
        if _scene_change(scan, k) < SCENE_CHANGE:
            continue   # le décor ne change pas : geste rapide (clap tout près de l'objectif), pas une coupe
        if scan.times[k] - scan.times[cuts[-1]] < min_shot:
            # deux coupes trop proches : garder la plus forte
            if cuts[-1] != 0 and d[k] > d[cuts[-1]]:
                cuts[-1] = k
            continue
        cuts.append(k)
    cuts.append(n)
    period = float(np.median(np.diff(scan.times))) if n > 1 else 1 / 30
    shots = []
    for i, (a, b) in enumerate(zip(cuts[:-1], cuts[1:])):
        end = scan.times[b] if b < n else scan.times[-1] + period
        shots.append(Shot(index=i, i0=a, i1=b, start=float(scan.times[a]), end=float(end)))
    return shots


def group_cameras(scan: MotionScan, shots: list[Shot], threshold: float = 0.07) -> None:
    """Assigne shot.camera (0, 1, 2… dans l'ordre d'apparition)."""
    if not shots:
        return
    if len(shots) == 1:
        shots[0].camera = 0
        return
    feats = []
    for s in shots:
        # on ignore la première et la dernière image (transitions) si possible
        a, b = (s.i0 + 1, s.i1 - 1) if s.i1 - s.i0 > 4 else (s.i0, s.i1)
        mean = scan.thumbs[a:b].astype(np.float32).mean(axis=0) / 255.0
        feats.append(mean.reshape(-1))
    X = np.stack(feats)
    dist = pdist(X, metric="cityblock") / X.shape[1]
    Z = linkage(dist, method="average")
    labels = fcluster(Z, t=threshold, criterion="distance")
    remap: dict[int, int] = {}
    for s, lab in zip(shots, labels):
        if lab not in remap:
            remap[lab] = len(remap)
        s.camera = remap[lab]
