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


BG_TOL = 8.0         # écart au fond sous lequel un pixel est resté « fond » pendant toute la vidéo
MIN_FILL = 0.9       # une vignette posée sur le fond est un rectangle plein


def _background(F: np.ndarray) -> tuple[float, float] | None:
    """(niveau de gris, bruit) d'un fond uni et immobile sur le bord de l'image, ou None. Le bruit
    est l'écart au fond que l'on y mesure (compression, grain) : ~0 pour un fond de synthèse,
    quelques niveaux pour un fond noir passé par OBS."""
    ring = np.concatenate([F[:, :2, :].reshape(len(F), -1), F[:, -2:, :].reshape(len(F), -1),
                           F[:, :, :2].reshape(len(F), -1), F[:, :, -2:].reshape(len(F), -1)], axis=1)
    b = float(np.median(ring))
    dev = np.abs(ring - b).max(axis=0)
    still = dev < BG_TOL
    if still.mean() < 0.5:                 # une vignette peut toucher le bord, pas toutes
        return None
    return b, float(np.percentile(dev[still], 99))


def _tiles_on_background(F: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Mosaïque dont les vignettes sont posées sur un fond uni (noir, blanc, couleur) : cases vides,
    marges, positions libres dans OBS. Rectangles d'image qui s'écartent du fond, où qu'ils soient.
    Vide si l'image n'en contient pas au moins deux."""
    from scipy import ndimage as ndi
    H, W = F.shape[1], F.shape[2]
    bg = _background(F)
    if bg is None:
        return []
    b, noise = bg
    # seuil calé sur le bruit du fond : une scène sombre posée sur un fond sombre de synthèse ne
    # doit pas se confondre avec lui, un fond OBS un peu bruité ne doit pas devenir « image »
    lit = np.abs(F - b).max(axis=0) > min(BG_TOL, max(2.0, 1.5 * noise + 1.0))
    if lit.mean() > 0.9:
        return []          # presque toute l'image a de la matière : pas de fond
    lab, _ = ndi.label(ndi.binary_opening(lit, np.ones((3, 3), bool)))
    # zones d'une vignette qui ont la couleur du fond (coin sombre sur fond sombre) : trous bouchés
    solid = ndi.binary_fill_holes(lab > 0)
    min_w, min_h = int(W * MIN_TILE), int(H * MIN_TILE)
    out = []
    for sl in ndi.find_objects(lab):
        if sl is None:
            continue
        ys, xs = sl
        if xs.stop - xs.start < min_w or ys.stop - ys.start < min_h:
            continue
        out += _rect_parts(solid, (xs.start, ys.start, xs.stop, ys.stop), min_w, min_h)
    # une seule zone d'image mais qui laisse voir le fond : peut-être des vignettes collées entre
    # elles (bande, bloc) ; detect_tiles cherchera leurs jointures dans ce rectangle
    if not out or (len(out) == 1 and (out[0][2] - out[0][0]) * (out[0][3] - out[0][1]) > 0.85 * W * H):
        return []
    # dans une mosaïque, toute l'image est dans les vignettes ; une mire (QR, textes, règle sur fond
    # uni) laisse beaucoup de matière éparpillée hors des « blocs » : ce n'est pas une mosaïque
    inside = np.zeros_like(lit)
    for x0, y0, x1, y1 in out:
        inside[y0:y1, x0:x1] = True
    core = lab > 0
    if core.sum() and (core & inside).sum() < 0.9 * core.sum():
        return []
    return out


def _rect_parts(solid: np.ndarray, rect: tuple[int, int, int, int], min_w: int, min_h: int,
                depth: int = 3) -> list[tuple[int, int, int, int]]:
    """Découpe une zone d'image en rectangles pleins. Deux vignettes qui se touchent par un coin
    ou un bord (mosaïque OBS mal alignée) forment une zone en « L » : on cherche la coupe
    horizontale ou verticale qui laisse de chaque côté un rectangle plein."""
    def trim(r):
        x0, y0, x1, y1 = r
        sub = solid[y0:y1, x0:x1]
        rows, cols = np.flatnonzero(sub.any(axis=1)), np.flatnonzero(sub.any(axis=0))
        if rows.size == 0:
            return None
        return (x0 + cols[0], y0 + rows[0], x0 + cols[-1] + 1, y0 + rows[-1] + 1)

    def fill(r):
        x0, y0, x1, y1 = r
        return float(solid[y0:y1, x0:x1].mean())

    r = trim(rect)
    if r is None or r[2] - r[0] < min_w or r[3] - r[1] < min_h:
        return []
    if fill(r) >= MIN_FILL:
        return [r]
    if depth == 0:
        return []
    x0, y0, x1, y1 = r
    best = None
    for axis in (0, 1):
        lo, hi = (y0, y1) if axis == 0 else (x0, x1)
        for pos in range(lo + 2, hi - 1):
            a = trim((x0, y0, x1, pos) if axis == 0 else (x0, y0, pos, y1))
            b = trim((x0, pos, x1, y1) if axis == 0 else (pos, y0, x1, y1))
            if a is None or b is None:
                continue
            score = min(fill(a), fill(b))
            if best is None or score > best[0]:
                best = (score, a, b)
    if best is None or best[0] < 0.75:
        return []
    return (_rect_parts(solid, best[1], min_w, min_h, depth - 1)
            + _rect_parts(solid, best[2], min_w, min_h, depth - 1))


def detect_tiles(samples: np.ndarray, max_depth: int = 4) -> list[tuple[float, float, float, float]]:
    """Vignettes (x0, y0, x1, y1) en fractions de l'image, de gauche à droite puis de haut en bas.
    Une seule vignette [0, 0, 1, 1] si l'image n'est pas un multiview."""
    if samples is None or len(samples) < 2:
        return [(0.0, 0.0, 1.0, 1.0)]
    tiles = _detect_tiles(samples, max_depth)
    if len(tiles) == 1:
        F = samples.astype(np.float32)
        free = _tiles_on_background(F)
        # chaque rectangle posé sur le fond peut lui-même contenir des vignettes bord à bord
        found = [t for r in free for t in _detect_tiles(samples, max_depth, r)]
        if len(found) >= 2:
            tiles = sorted(found, key=lambda r: (round(r[1], 2), r[0]))
    return tiles


def _regular(leaves: list, area: tuple[int, int, int, int], axis: int, min_px: int) -> set[int]:
    """Positions d'une grille régulière : une vignette de 1/3 trouvée laisse supposer une jointure
    à 2/3 (multiples de chaque taille de vignette trouvée, depuis le bord de la zone)."""
    a0, a1 = (area[0], area[2]) if axis == 0 else (area[1], area[3])
    size = a1 - a0
    out: set[int] = set()
    for r in leaves:
        step = (r[2] - r[0]) if axis == 0 else (r[3] - r[1])
        if step < min_px or step >= size:
            continue
        k = 1
        while a0 + k * step < a1 - min_px // 2:
            out.add(a0 + k * step)
            k += 1
    return out


def _detect_tiles(samples: np.ndarray, max_depth: int,
                  start: tuple[int, int, int, int] | None = None) -> list[tuple[float, float, float, float]]:
    """Mosaïque bord à bord : coupes « guillotine » le long des sauts d'image, dans `start`
    (rectangle en pixels de l'échantillon ; toute l'image par défaut)."""
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

    area = start or (0, 0, W, H)
    split(area, 0)

    # cohérence de grille : une coupe trouvée dans une rangée (ou colonne) est retestée, avec un
    # seuil plus souple, dans les vignettes voisines (deux caméras aux fonds proches se
    # distinguent mal, mais une mosaïque maison est presque toujours une grille régulière)
    for _ in range(3):
        xs = sorted({r[0] for r in leaves if r[0] > 0} | {r[2] for r in leaves if r[2] < W}
                    | _regular(leaves, area, 0, min_w))
        ys = sorted({r[1] for r in leaves if r[1] > 0} | {r[3] for r in leaves if r[3] < H}
                    | _regular(leaves, area, 1, min_h))
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
