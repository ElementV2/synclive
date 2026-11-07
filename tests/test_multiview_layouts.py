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
    oy, ox = int(rng.integers(0, 1000)), int(rng.integers(0, 1000))   # chaque caméra a sa scène
    vy, vx = int(rng.integers(2, 6)), int(rng.integers(3, 8))
    for k in range(N):
        y, x = (oy + k * vy) % max(1, h - 10), (ox + k * vx) % max(1, w - 10)
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


def _shelves(seed: int = 7) -> np.ndarray:
    """Une seule caméra fixe devant une étagère : planches horizontales et montants verticaux
    pile aux tiers et à la moitié de l'image (lignes droites franches qui ne bougent jamais),
    objets sur les planches, quelqu'un qui passe."""
    rng = np.random.default_rng(seed)
    img = _scene(H, W, seed=seed)[0].copy()
    for y in (H // 3, 2 * H // 3):                              # planches
        img[y - 3:y + 3, :] = 215
        img[y + 3:y + 8, :] = 90
    for x in (W // 3, W // 2, 2 * W // 3):                      # montants
        img[:, x - 3:x + 3] = 40
    for _ in range(40):                                         # bouteilles sur les planches
        x = int(rng.integers(5, W - 15))
        y = int(rng.choice([H // 3, 2 * H // 3, H - 2]))
        img[y - 30:y - 3, x:x + 8] = rng.uniform(60, 250)
    F = np.repeat(img[None], N, axis=0).astype(np.float32)
    for k in range(N):                                          # passant qui traverse les montants
        x = 20 + k * 14
        F[k, 60:200, x:x + 40] = 170 + 30 * np.sin(np.arange(40) / 5)
    F += np.random.default_rng(1).normal(0, 2, F.shape)
    return np.clip(F, 0, 255).astype(np.uint8)


def test_shelves_single_camera_is_not_a_mosaic():
    """Étagère filmée par une seule caméra : les lignes de la scène se prolongent à travers les
    montants et le passant les traverse : ce n'est pas une mosaïque (on y trouvait 12 vignettes)."""
    assert mv.detect_tiles(_shelves()) == [(0.0, 0.0, 1.0, 1.0)]


def test_panel_edge_with_person_crossing_is_not_a_mosaic():
    """Bord franc d'un panneau aux 2/3 de l'image, quelqu'un passe devant : une seule caméra."""
    F = _scene(H, W, seed=21).astype(np.float32)
    F[:, :, 2 * W // 3:] = np.clip(F[:, :, 2 * W // 3:] * 0.4 + 150, 0, 255)       # panneau clair
    for k in range(N):
        x = 140 + k * 6
        F[k, 30:210, x:x + 50] = 60 + 40 * np.cos(np.arange(180)[:, None] / 9)
    assert mv.detect_tiles(np.clip(F, 0, 255).astype(np.uint8)) == [(0.0, 0.0, 1.0, 1.0)]


def test_grid_with_picture_in_picture():
    """Grille 2x2 et une 5e caméra incrustée au centre, par-dessus les quatre : 5 sources ; les
    vignettes de la grille sont recadrées hors de l'incrustation."""
    rects = [(0, 0, .5, .5), (.5, 0, .5, .5), (0, .5, .5, .5), (.5, .5, .5, .5)]
    F = _mosaic(rects, 0.0).astype(np.float32)
    x0, y0, x1, y1 = int(.31 * W), int(.31 * H), int(.69 * W), int(.69 * H)
    F[:, y0:y1, x0:x1] = _scene(y1 - y0, x1 - x0, seed=40)
    lay = mv.detect_layout(F.astype(np.uint8))
    labels = [lab for _r, lab, _v in lay]
    assert len(lay) == 5 and "incrustation" in labels, labels
    pip = next(r for r, lab, _v in lay if lab == "incrustation")
    assert all(abs(a - b) < 0.02 for a, b in zip(pip, (.31, .31, .69, .69))), pip
    for r, lab, view in lay:
        if lab != "incrustation":            # mesurée hors de l'incrustation…
            assert r[2] <= pip[0] + 0.01 or r[0] >= pip[2] - 0.01 or r[3] <= pip[1] + 0.01 \
                or r[1] >= pip[3] - 0.01, (lab, r)
        # … mais montrée entière, au format de la caméra (16/9 ici)
        ar = (view[2] - view[0]) / (view[3] - view[1]) * 16 / 9
        assert abs(ar - 16 / 9) < 0.05, (lab, view)
        assert view[0] <= r[0] + 1e-9 and view[1] <= r[1] + 1e-9 and r[2] <= view[2] + 1e-9 and r[3] <= view[3] + 1e-9
