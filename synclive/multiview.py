# SPDX-License-Identifier: GPL-3.0-or-later
"""Détection des vignettes d'un multiview « maison » (caméras bord à bord, sans séparateur).

Indice principal : entre deux caméras, l'image saute brutalement le long d'une ligne droite qui
traverse presque toute la zone et ne bouge jamais au cours de la vidéo. Le cadre est découpé
récursivement (coupes « guillotine », comme une mise en page OBS) : on cherche dans la zone la
ligne verticale ou horizontale la plus nette, on coupe, on recommence dans chaque morceau.

Garde-fous contre les fausses coupes (montant d'étagère, bord d'un panneau, ligne d'une mire…) :
  * la ligne doit couvrir au moins 60 % de la zone, pour toutes les images échantillonnées ;
  * elle doit tomber sur une position de mise en page usuelle (1/2, 1/3, 2/3, 1/4, 3/4) ;
  * les deux côtés doivent être SANS RAPPORT : dans une même image, les lignes de la scène se
    prolongent à travers un bord (planches d'une étagère de part et d'autre d'un montant) et ce
    qui bouge le traverse (quelqu'un qui passe devant un panneau) ; entre deux caméras, non ;
  * chaque vignette doit faire au moins 1/8 de l'image et avoir une forme d'image de caméra
    (pas une bande 5 fois plus large que haute).

Incrustation (une caméra posée par-dessus la mosaïque ou par-dessus une image plein écran) :
rectangle à l'intérieur de l'image dont les quatre bords sont des jointures franches ; elle est
retirée avant de chercher la mosaïque, et les vignettes qu'elle recouvre en partie sont recadrées
sur leur partie visible.
"""
from __future__ import annotations

import numpy as np

FRACTIONS = (1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4)
TOL = 0.025          # tolérance sur la position (fraction de la zone)
MIN_COVER = 0.6      # part de la ligne où le saut est franc
RELAXED_COVER = 0.3  # seuil pour une coupe alignée sur une coupe déjà trouvée (grille)
MIN_TILE = 1 / 8     # taille minimale d'une vignette (fraction de l'image)
MAX_LINES = 0.42     # lignes de la scène qui traversent : au-delà, même image (vraies jointures ≤ 0,2)
MAX_MOTION = 0.30    # mouvement qui traverse : au-delà, même image (vraies jointures ≤ 0,2)
PIP_MIN = 0.16       # taille minimale d'une incrustation (fraction de la largeur et de la hauteur)
MIN_DETAIL = 10.0    # écart type minimal (niveaux de gris) dans une incrustation : une image, pas un aplat
ASPECT = (0.45, 2.6)  # largeur / hauteur d'une vignette (9:16 … 21:9, avec de la marge)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum()) / d if d > 1e-9 else 0.0


def _aligned(A: np.ndarray, B: np.ndarray) -> float:
    """Ressemblance des deux côtés AU MÊME ENDROIT, au-delà de ce qu'elle est quand on décale l'un
    des deux (A, B : (n, L), une rangée par image). Une ligne de la scène ou un mouvement qui
    traverse ne se retrouve qu'au même endroit ; deux dégradés ou deux caméras sans rapport se
    ressemblent autant décalés que non décalés : ~0."""
    A = A - A.mean(axis=1, keepdims=True)
    B = B - B.mean(axis=1, keepdims=True)
    na = np.sqrt((A * A).sum(axis=1))
    nb = np.sqrt((B * B).sum(axis=1))
    ok = (na > 1e-6) & (nb > 1e-6)
    if not ok.any():
        return 0.0
    A, B = A[ok] / na[ok, None], B[ok] / nb[ok, None]
    n = A.shape[1]
    m = min(40, n // 3)
    lags = [d for d in range(-m, m + 1, 2) if abs(d) >= 6]

    def at(d: int) -> float:
        if d >= 0:
            return float((A[:, :n - d] * B[:, d:]).sum(axis=1).mean())
        return float((A[:, -d:] * B[:, :n + d]).sum(axis=1).mean())
    base = float(np.percentile([abs(at(d)) for d in lags], 90)) if lags else 0.0
    return at(0) - base


def _related_scores(F: np.ndarray, axis: int, pos: int, a: int, b: int,
                    keep: np.ndarray | None = None) -> tuple[float, float] | None:
    lim = F.shape[2] if axis == 1 else F.shape[1]
    if pos - 2 < 0 or pos + 1 >= lim:
        return None
    if axis == 1:
        L, R = F[:, a:b, pos - 2], F[:, a:b, pos + 1]
    else:
        L, R = F[:, pos - 2, a:b], F[:, pos + 1, a:b]
    if keep is not None:
        L, R = L[:, keep], R[:, keep]
    if L.shape[1] < 8:
        return None
    rs = _aligned(np.diff(L, axis=1), np.diff(R, axis=1))     # lignes de la scène qui traversent
    rt = _aligned(np.abs(np.diff(L, axis=0)), np.abs(np.diff(R, axis=0))) if len(F) > 2 else 0.0
    return rs, rt                                             # rt : mouvement qui traverse


def _related(F: np.ndarray, axis: int, pos: int, a: int, b: int, keep: np.ndarray | None = None) -> bool:
    """Les deux côtés de la ligne sont-ils la même image ? (lignes de la scène qui se prolongent à
    travers, ou mouvement qui la traverse). `keep` : positions le long de la ligne à prendre."""
    sc = _related_scores(F, axis, pos, a, b, keep)
    return sc is not None and (sc[0] > MAX_LINES or sc[1] > MAX_MOTION)


def _keep(mask: np.ndarray | None, axis: int, pos: int, a: int, b: int) -> np.ndarray | None:
    """Positions de la ligne hors de la zone masquée (incrustation)."""
    if mask is None:
        return None
    if axis == 1:
        band = mask[a:b, max(0, pos - 3):pos + 3]
    else:
        band = mask[max(0, pos - 3):pos + 3, a:b].T
    return ~band.any(axis=1)


def _line_cover(F: np.ndarray, axis: int, pos: int, a: int, b: int, mask: np.ndarray | None = None) -> float:
    """Part de la ligne (colonne `pos` si axis=1, rangée si axis=0) où le saut entre pos-1 et pos
    est net comparé aux variations juste à côté, en moyenne sur le temps (hors zone masquée)."""
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
    keep = _keep(mask, axis, pos, a, b)
    if keep is not None:
        if keep.sum() < 0.25 * keep.size:
            return 0.0
        hit = hit[keep]
    return float(hit.mean())


def _best_cut(F: np.ndarray, rect: tuple[int, int, int, int], axis: int,
              min_px: int, mask: np.ndarray | None = None) -> tuple[float, int] | None:
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
            c = _line_cover(F, axis, pos, a, b, mask)
            if best is None or c > best[0]:
                best = (c, pos)
    if best is not None and best[0] >= RELAXED_COVER and _related(F, axis, best[1], a, b,
                                                                   _keep(mask, axis, best[1], a, b)):
        return None                  # bord dans une seule image (étagère, panneau) : pas une jointure
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


def detect_tiles(samples: np.ndarray, max_depth: int = 4,
                 frame_aspect: float = 16 / 9) -> list[tuple[float, float, float, float]]:
    """Vignettes (x0, y0, x1, y1) en fractions de l'image, de gauche à droite puis de haut en bas.
    Une seule vignette [0, 0, 1, 1] si l'image n'est pas un multiview."""
    return [r for r, _lab, _view in detect_layout(samples, max_depth, frame_aspect)]


def detect_layout(samples: np.ndarray, max_depth: int = 4, frame_aspect: float = 16 / 9
                  ) -> list[tuple[tuple[float, float, float, float], str, tuple[float, float, float, float]]]:
    """Vignettes (zone de mesure, nom pour l'utilisateur, cadre montré), en fractions de l'image.
    Une incrustation est une vignette à part ; une vignette qu'elle recouvre en partie est mesurée
    sur sa partie visible, mais montrée entière (au format de la caméra)."""
    full = [((0.0, 0.0, 1.0, 1.0), "plein écran", (0.0, 0.0, 1.0, 1.0))]
    if samples is None or len(samples) < 2:
        return full
    F = samples.astype(np.float32)
    H, W = F.shape[1], F.shape[2]
    # vignettes posées sur un fond uni : ce sont des vignettes libres, pas des incrustations
    pips = [] if _background(F) is not None else _overlays(F, frame_aspect)
    if pips:
        # un cadre à l'intérieur d'une seule vignette fait partie de son image (cadre d'une mire)
        grid = _plain_tiles(samples, max_depth, None)
        if len(grid) > 1 and _camera_shaped(grid, frame_aspect):
            def inside(p, t):
                return (p[0] >= t[0] * W - 2 and p[2] <= t[2] * W + 2 and p[1] >= t[1] * H - 2
                        and p[3] <= t[3] * H + 2)
            pips = [p for p in pips if not any(inside(p, t) for t in grid)]
    mask = None
    if pips:
        mask = np.zeros((H, W), bool)
        for x0, y0, x1, y1 in pips:
            mask[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2] = True
    tiles = _plain_tiles(samples, max_depth, mask)
    if not _camera_shaped(tiles, frame_aspect):
        tiles = [(0.0, 0.0, 1.0, 1.0)]
    if not pips:
        return full if len(tiles) == 1 else [(t, tile_label(t, len(tiles)), t) for t in tiles]
    out = []
    for t in tiles:
        px = (int(round(t[0] * W)), int(round(t[1] * H)), int(round(t[2] * W)), int(round(t[3] * H)))
        vis = _visible_part(F, px, pips)
        if vis is not None:
            out.append(((vis[0] / W, vis[1] / H, vis[2] / W, vis[3] / H), tile_label(t, max(2, len(tiles)))
                        if len(tiles) > 1 else "image principale", t))
    for i, (x0, y0, x1, y1) in enumerate(pips):
        r = (x0 / W, y0 / H, x1 / W, y1 / H)
        out.append((r, "incrustation" + (f" {i + 1}" if len(pips) > 1 else ""), r))
    return out if len(out) > 1 else full


def _camera_shaped(tiles, frame_aspect: float) -> bool:
    """Chaque vignette a-t-elle une forme d'image de caméra ? Des bandes très allongées viennent
    des lignes d'une scène (planches d'étagère), pas d'une mosaïque."""
    if len(tiles) <= 1:
        return True
    for x0, y0, x1, y1 in tiles:
        ar = (x1 - x0) / max(1e-6, y1 - y0) * frame_aspect
        if not ASPECT[0] <= ar <= ASPECT[1]:
            return False
    return True


def _plain_tiles(samples: np.ndarray, max_depth: int, mask: np.ndarray | None):
    tiles = _detect_tiles(samples, max_depth, mask=mask)
    if len(tiles) == 1 and mask is None:
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
                  start: tuple[int, int, int, int] | None = None,
                  mask: np.ndarray | None = None) -> list[tuple[float, float, float, float]]:
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
            v = _best_cut(F, rect, 1, min_w, mask)
            h = _best_cut(F, rect, 0, min_h, mask)
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
                    c, p = max((_line_cover(F, 1, p, y0, y1, mask), p) for p in range(pos - 1, pos + 2))
                    if c >= RELAXED_COVER and not _related(F, 1, p, y0, y1, _keep(mask, 1, p, y0, y1)):
                        out += [(x0, y0, pos, y1), (pos, y0, x1, y1)]
                        done = changed = True
                        break
            if not done:
                for pos in ys:
                    if y0 + min_h <= pos <= y1 - min_h:
                        c, p = max((_line_cover(F, 0, p, x0, x1, mask), p) for p in range(pos - 1, pos + 2))
                        if c >= RELAXED_COVER and not _related(F, 0, p, x0, x1, _keep(mask, 0, p, x0, x1)):
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


def _hits(F: np.ndarray, axis: int, persist: float = 0.7) -> np.ndarray:
    """Carte des sauts francs présents dans au moins `persist` des images (axis=1 : entre colonnes
    x-1 et x ; axis=0 : entre rangées). Un objet qui bouge (des mains) ne laisse pas de bord fixe."""
    ax = 2 if axis == 1 else 1
    j = np.abs(np.diff(F, axis=ax))                          # (N, H, W-1) ou (N, H-1, W)
    base = np.full_like(j, np.inf)
    if axis == 1:
        base[:, :, 2:-2] = 0.5 * (j[:, :, :-4] + j[:, :, 4:])
    else:
        base[:, 2:-2] = 0.5 * (j[:, :-4] + j[:, 4:])
    frac = (j > np.maximum(2.5 * base, base + 6.0)).mean(axis=0)
    hit = np.zeros(F.shape[1:], bool)
    if axis == 1:
        hit[:, 1:] = frac >= persist
    else:
        hit[1:] = frac >= persist
    return hit


def _runs(col: np.ndarray, min_len: int, gap: int = 3) -> list[tuple[int, int]]:
    out, start, last = [], None, None
    for i in np.flatnonzero(col):
        if start is None:
            start = last = i
        elif i - last <= gap:
            last = i
        else:
            if last - start + 1 >= min_len:
                out.append((start, last + 1))
            start = last = i
    if start is not None and last - start + 1 >= min_len:
        out.append((start, last + 1))
    return out


def _overlays(F: np.ndarray, frame_aspect: float = 16 / 9) -> list[tuple[int, int, int, int]]:
    """Incrustations : rectangles à l'intérieur de l'image (sans toucher les bords) dont les quatre
    côtés sont des jointures franches entre images sans rapport. En pixels de l'échantillon.
    Un côté peut s'effacer par endroits (zone sombre des deux côtés) : le haut et le bas sont
    cherchés autour des bouts des côtés verticaux, là où la ligne horizontale est la plus franche."""
    H, W = F.shape[1], F.shape[2]
    min_w, min_h = max(8, int(W * MIN_TILE)), max(6, int(H * MIN_TILE))
    min_w, min_h = max(min_w, int(W * PIP_MIN)), max(min_h, int(H * PIP_MIN))
    hv, hh = _hits(F, 1), _hits(F, 0)
    v2 = hv.copy()
    v2[:, 1:] |= hv[:, :-1]                        # saut entre x-1 et x, à un pixel près
    h2 = hh.copy()
    h2[1:] |= hh[:-1]
    segs = []                                       # bords verticaux candidats (x, y0, y1)
    for x in range(4, W - 4):
        for a, b in _runs(v2[:, x], min_h // 2):
            if not any(abs(x - q[0]) <= 1 and abs(a - q[1]) <= 3 and abs(b - q[2]) <= 3 for q in segs[-8:]):
                segs.append((x, a, b))
    hcum = np.concatenate([np.zeros((H, 1)), np.cumsum(h2, axis=1)], axis=1)   # couverture rapide
    vcum = np.concatenate([np.zeros((1, W)), np.cumsum(v2, axis=0)], axis=0)

    def hcover(y, xa, xb):
        return (hcum[y, xb] - hcum[y, xa]) / max(1, xb - xa)

    def vcover(x, ya, yb):
        return (vcum[yb, x] - vcum[ya, x]) / max(1, yb - ya)

    found = []
    for i, (xa, a0, b0) in enumerate(segs):
        for xb, a1, b1 in segs[i + 1:]:
            if xb - xa < min_w or xb - xa > 0.75 * W:
                continue
            if min(b0, b1) - max(a0, a1) < min_h // 2:
                continue                            # les deux côtés ne se font pas face
            win = max(6, int(0.12 * H))
            tops = [(hcover(y, xa, xb), y) for y in range(max(4, min(a0, a1) - win), min(H - 4, max(a0, a1) + win))]
            bots = [(hcover(y, xa, xb), y) for y in range(max(4, min(b0, b1) - win), min(H - 4, max(b0, b1) + win))]
            if not tops or not bots:
                continue
            ct, y0 = max(tops)
            cb, y1 = max(bots)
            if ct < 0.6 or cb < 0.6 or y1 - y0 < min_h or y1 - y0 > 0.75 * H:
                continue
            if vcover(xa, y0, y1) < 0.5 or vcover(xb, y0, y1) < 0.5:
                continue
            ar = ((xb - xa) / W) / ((y1 - y0) / H) * frame_aspect
            if not ASPECT[0] <= ar <= ASPECT[1]:
                continue
            found.append((ct + cb + vcover(xa, y0, y1) + vcover(xb, y0, y1), (xa, y0, xb, y1)))
    found.sort(key=lambda c: (-c[0], -(c[1][2] - c[1][0]) * (c[1][3] - c[1][1])))
    out: list[tuple[int, int, int, int]] = []
    for _sc, r in found[:200]:
        if not all(r[2] <= q[0] or r[0] >= q[2] or r[3] <= q[1] or r[1] >= q[3] for q in out):
            continue                                # recouvre une incrustation déjà retenue
        xa, y0, xb, y1 = r
        inner = F[:, y0 + 3:y1 - 3, xa + 3:xb - 3]
        if inner.size == 0 or float(inner.std(axis=(1, 2)).mean()) < MIN_DETAIL:
            continue                                # aplat de couleur (objet du décor) : pas une caméra
        if any(_related(F, 1, x, y0, y1) for x in (xa, xb)) or any(_related(F, 0, y, xa, xb) for y in (y0, y1)):
            continue                                # cadre dans une seule image (tableau, fenêtre)
        out.append(r)
        if len(out) == 3:
            break
    return out


def _visible_part(F: np.ndarray, rect: tuple[int, int, int, int],
                  pips: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    """Partie d'une vignette que les incrustations ne recouvrent pas : le plus grand rectangle
    libre parmi ceux à gauche, à droite, au-dessus, au-dessous de chaque incrustation, celui où
    il se passe le plus de choses (l'action) à surface égale."""
    x0, y0, x1, y1 = rect
    cands = [rect]
    for px0, py0, px1, py1 in pips:
        nxt = []
        for r in cands:
            a0, b0, a1, b1 = r
            if px1 <= a0 or px0 >= a1 or py1 <= b0 or py0 >= b1:
                nxt.append(r)
                continue
            for c in ((a0, b0, min(a1, px0), b1), (max(a0, px1), b0, a1, b1),
                      (a0, b0, a1, min(b1, py0)), (a0, max(b0, py1), a1, b1)):
                if c[2] - c[0] >= 8 and c[3] - c[1] >= 6:
                    nxt.append(c)
        cands = nxt
    if not cands:
        return None
    motion = np.abs(np.diff(F, axis=0)).mean(axis=0) if len(F) > 1 else np.ones(F.shape[1:])

    def score(r):
        a0, b0, a1, b1 = r
        return float(motion[b0:b1, a0:a1].sum()) + 1e-3 * (a1 - a0) * (b1 - b0)
    return max(cands, key=score)


def tile_label(tile: tuple[float, float, float, float], n: int) -> str:
    x0, y0, x1, y1 = tile
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    v = "haut" if cy < 0.4 else "bas" if cy > 0.6 else "milieu"
    h = "gauche" if cx < 0.4 else "droite" if cx > 0.6 else "centre"
    return "plein écran" if n == 1 else f"vignette {v} {h}".replace("milieu centre", "centrale")
