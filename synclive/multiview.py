# SPDX-License-Identifier: GPL-3.0-or-later
"""Détection des vignettes d'un multiview « maison » (caméras bord à bord, sans séparateur).

Indice principal : entre deux caméras, l'image saute brutalement le long d'une ligne droite qui
traverse presque toute la zone et ne bouge jamais au cours de la vidéo. Le cadre est découpé
récursivement (coupes « guillotine », comme une mise en page OBS) : on cherche dans la zone la
ligne verticale ou horizontale la plus nette, on coupe, on recommence dans chaque morceau.

Garde-fous contre les fausses coupes (montant de porte, échelle, ligne d'une mire…) :
  * la ligne doit couvrir au moins 60 % de la zone, pour toutes les images échantillonnées ;
  * elle doit tomber sur une position de mise en page usuelle (1/2, 1/3, 2/3, 1/4, 3/4) ;
  * chaque vignette doit faire au moins 1/8 de l'image.
"""
from __future__ import annotations

import numpy as np

FRACTIONS = (1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4)
TOL = 0.025          # tolérance sur la position (fraction de la zone)
MIN_COVER = 0.6      # part de la ligne où le saut est franc
RELAXED_COVER = 0.3  # seuil pour une coupe alignée sur une coupe déjà trouvée (grille)
MIN_TILE = 1 / 8     # taille minimale d'une vignette (fraction de l'image)


def _line_cover(F: np.ndarray, axis: int, pos: int, a: int, b: int) -> float:
    """Part de la ligne (colonne `pos` si axis=1, rangée si axis=0) où le saut entre pos-1 et pos
    est net comparé aux variations juste à côté, en moyenne sur le temps."""
    if axis == 1:
        seg = F[:, a:b, :]
        jump = np.abs(seg[:, :, pos] - seg[:, :, pos - 1]).mean(axis=0)
        near = []
        for q in (pos - 2, pos + 2):
            if 1 <= q < F.shape[2]:
                near.append(np.abs(seg[:, :, q] - seg[:, :, q - 1]).mean(axis=0))
    else:
        seg = F[:, :, a:b]
        jump = np.abs(seg[:, pos, :] - seg[:, pos - 1, :]).mean(axis=0)
        near = []
        for q in (pos - 2, pos + 2):
            if 1 <= q < F.shape[1]:
                near.append(np.abs(seg[:, q, :] - seg[:, q - 1, :]).mean(axis=0))
    if not near:
        return 0.0
    base = np.mean(near, axis=0)
    hit = jump > np.maximum(2.5 * base, base + 6.0)
    return float(hit.mean())


def _best_cut(F: np.ndarray, rect: tuple[int, int, int, int], axis: int,
              min_px: int) -> tuple[float, int] | None:
    x0, y0, x1, y1 = rect
    lo, hi = (x0, x1) if axis == 1 else (y0, y1)
    a, b = (y0, y1) if axis == 1 else (x0, x1)
    size = hi - lo
    best = None
    for fr in FRACTIONS:
        centre = lo + fr * size
        for pos in range(int(round(centre - TOL * size)), int(round(centre + TOL * size)) + 1):
            if pos - lo < min_px or hi - pos < min_px:
                continue
            c = _line_cover(F, axis, pos, a, b)
            if best is None or c > best[0]:
                best = (c, pos)
    return best


def _is_empty(F: np.ndarray, rect: tuple[int, int, int, int]) -> bool:
    x0, y0, x1, y1 = rect
    r = F[:, y0:y1, x0:x1]
    return float(r.std()) < 3.0       # vignette noire / unie : pas une caméra


def detect_tiles(samples: np.ndarray, max_depth: int = 4) -> list[tuple[float, float, float, float]]:
    """Vignettes (x0, y0, x1, y1) en fractions de l'image, de gauche à droite puis de haut en bas.
    Une seule vignette [0, 0, 1, 1] si l'image n'est pas un multiview."""
    if samples is None or len(samples) < 2:
        return [(0.0, 0.0, 1.0, 1.0)]
    F = samples.astype(np.float32)
    H, W = F.shape[1], F.shape[2]
    min_w, min_h = max(8, int(W * MIN_TILE)), max(6, int(H * MIN_TILE))
    leaves: list[tuple[int, int, int, int]] = []

    def split(rect, depth):
        x0, y0, x1, y1 = rect
        cands = []
        if depth < max_depth:
            v = _best_cut(F, rect, 1, min_w)
            h = _best_cut(F, rect, 0, min_h)
            if v and v[0] >= MIN_COVER:
                cands.append((v[0], 1, v[1]))
            if h and h[0] >= MIN_COVER:
                cands.append((h[0], 0, h[1]))
        if not cands:
            leaves.append(rect)
            return
        _, axis, pos = max(cands)
        if axis == 1:
            split((x0, y0, pos, y1), depth + 1)
            split((pos, y0, x1, y1), depth + 1)
        else:
            split((x0, y0, x1, pos), depth + 1)
            split((x0, pos, x1, y1), depth + 1)

    split((0, 0, W, H), 0)

    # cohérence de grille : une coupe trouvée dans une rangée (ou colonne) est retestée, avec un
    # seuil plus souple, dans les vignettes voisines (deux caméras aux fonds proches se
    # distinguent mal, mais une mosaïque maison est presque toujours une grille régulière)
    for _ in range(3):
        xs = sorted({r[0] for r in leaves if r[0] > 0} | {r[2] for r in leaves if r[2] < W})
        ys = sorted({r[1] for r in leaves if r[1] > 0} | {r[3] for r in leaves if r[3] < H})
        changed = False
        out = []
        for r in leaves:
            x0, y0, x1, y1 = r
            done = False
            for pos in xs:
                if x0 + min_w <= pos <= x1 - min_w:
                    c = max(_line_cover(F, 1, p, y0, y1) for p in range(pos - 1, pos + 2))
                    if c >= RELAXED_COVER:
                        out += [(x0, y0, pos, y1), (pos, y0, x1, y1)]
                        done = changed = True
                        break
            if not done:
                for pos in ys:
                    if y0 + min_h <= pos <= y1 - min_h:
                        c = max(_line_cover(F, 0, p, x0, x1) for p in range(pos - 1, pos + 2))
                        if c >= RELAXED_COVER:
                            out += [(x0, y0, x1, pos), (x0, pos, x1, y1)]
                            done = changed = True
                            break
            if not done:
                out.append(r)
        leaves[:] = out
        if not changed:
            break

    tiles = [r for r in leaves if not _is_empty(F, r)] or leaves
    tiles.sort(key=lambda r: (round(r[1] / H, 2), r[0]))
    return [(x0 / W, y0 / H, x1 / W, y1 / H) for x0, y0, x1, y1 in tiles]


def tile_label(tile: tuple[float, float, float, float], n: int) -> str:
    x0, y0, x1, y1 = tile
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    v = "haut" if cy < 0.4 else "bas" if cy > 0.6 else "milieu"
    h = "gauche" if cx < 0.4 else "droite" if cx > 0.6 else "centre"
    return "plein écran" if n == 1 else f"vignette {v} {h}".replace("milieu centre", "centrale")
