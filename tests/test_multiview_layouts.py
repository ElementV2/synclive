# SPDX-License-Identifier: GPL-3.0-or-later
"""Découpe des mosaïques « maison » sur des échantillons synthétiques (rapide, sans vidéo).

Les échantillons imitent ce que voit `detect_tiles` : niveaux de gris 384x216, une image par
instant échantillonné. Chaque vignette est une scène texturée qui bouge (une caméra)."""
import numpy as np
import pytest

from synclive import multiview as mv

H, W, N = 216, 384, 24


def _scene(h: int, w: int, seed: int) -> np.ndarray:
    """N images d'une « caméra » : fond texturé propre à la caméra + un objet qui se déplace."""
    import cv2
    rng = np.random.default_rng(seed)
    # bruit lissé : une « vraie » scène n'a pas de lignes droites franches alignées partout
    base = cv2.GaussianBlur(rng.normal(0, 1, size=(h, w)).astype(np.float32), (0, 0), 4)
    base = 130 + 60 * base / (base.std() + 1e-6) + rng.uniform(-40, 40)
    out = np.repeat(base[None], N, axis=0)
    for k in range(N):
        y, x = (k * 3) % max(1, h - 10), (k * 5) % max(1, w - 10)
        out[k, y:y + 10, x:x + 10] = 255 - out[k, y:y + 10, x:x + 10]
    return out


def _mosaic(rects, bg: float) -> np.ndarray:
    F = np.full((N, H, W), bg, np.float32)
    for i, (x, y, w, h) in enumerate(rects):
        x0, y0, x1, y1 = int(round(x * W)), int(round(y * H)), int(round((x + w) * W)), int(round((y + h) * H))
        F[:, y0:y1, x0:x1] = _scene(y1 - y0, x1 - x0, seed=10 + i)
    return np.clip(F, 0, 255).astype(np.uint8)


def _match(found, rects, tol=0.03):
    want = [(x, y, x + w, y + h) for x, y, w, h in rects]
    assert len(found) == len(want), found
    for r in want:
        assert any(all(abs(a - b) <= tol for a, b in zip(r, f)) for f in found), (r, found)


@pytest.mark.parametrize("bg", [0.0, 255.0, 62.0])
def test_free_tiles_on_uniform_background(bg):
    """Vignettes de tailles variées posées librement sur un fond noir, blanc ou gris (croquis)."""
    rects = [(0.125, 0.135, 0.256, 0.258), (0.438, 0.092, 0.346, 0.347),
             (0.063, 0.467, 0.434, 0.423), (0.570, 0.478, 0.360, 0.340)]
    _match(mv.detect_tiles(_mosaic(rects, bg)), rects)


def test_tiles_touching_by_a_corner_are_split():
    """Mosaïque OBS mal alignée : deux vignettes qui se touchent par un coin (forme en « L »)."""
    rects = [(0.534, 0.0, 0.466, 0.467), (0.006, 0.467, 0.531, 0.533)]
    _match(mv.detect_tiles(_mosaic(rects, 0.0)), rects)


def test_band_of_three_edge_to_edge_tiles_on_black():
    """Trois vignettes collées en bande au milieu d'un fond noir : jointures à 1/3 et 2/3."""
    rects = [(i / 3, 1 / 3, 1 / 3, 1 / 3) for i in range(3)]
    _match(mv.detect_tiles(_mosaic(rects, 0.0)), rects)


def test_single_camera_is_not_split():
    F = _scene(H, W, seed=3).astype(np.uint8)
    assert mv.detect_tiles(F) == [(0.0, 0.0, 1.0, 1.0)]


def test_test_card_on_plain_background_is_not_a_mosaic():
    """Une mire (QR, textes, graduations sur fond uni) n'est pas une mosaïque : beaucoup de matière
    reste hors des « blocs » trouvés."""
    F = np.full((N, H, W), 20, np.float32)
    F[:, 40:110, 250:320] = _scene(70, 70, seed=5)              # « QR »
    F[:, 80:105, 110:170] = _scene(25, 60, seed=6)              # bloc de texte
    for x in range(10, W - 10, 12):                             # règle graduée
        F[:, 180:200, x:x + 4] = 230
    for y in range(20, 160, 18):                                # lignes de texte
        F[:, y:y + 5, 15:90] = 200
    assert mv.detect_tiles(F.astype(np.uint8)) == [(0.0, 0.0, 1.0, 1.0)]
