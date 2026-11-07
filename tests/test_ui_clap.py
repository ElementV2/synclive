# SPDX-License-Identifier: GPL-3.0-or-later
"""Gestes de « Régler l'image du clap » : on se déplace librement (rien n'est choisi), le bouton
« Cette image est le clap » retient l'image affichée, avec la valeur annoncée sur la vignette."""
from fractions import Fraction
from pathlib import Path
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
sys.path.insert(0, str(Path(__file__).parent))
from synth import make  # noqa: E402
from synclive import analysis  # noqa: E402

OFFS = {"A": 0.080, "B": -0.040, "C": 0.133}
ORDER = ["A", "B", "C", "A", "B"]


@pytest.fixture(scope="module")
def dialog(tmp_path_factory):
    from PySide6.QtWidgets import QApplication
    from synclive.ui_clap import ClapFrameDialog
    app = QApplication.instance() or QApplication([])
    vid = tmp_path_factory.mktemp("ui") / "v.mkv"
    make(str(vid), Fraction(30000, 1001), OFFS, ORDER)
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    cam = next(c for c in an.cameras() if c.claps)
    d = ClapFrameDialog(an, cam)
    d.resize(1300, 900)
    d.show()
    app.processEvents()
    yield app, d
    d.close()


def _ms(label: str) -> int:
    return int(label.split("\n")[0].replace("−", "-").replace("ms", "").strip() or 0)


def test_thumbnail_click_moves_then_button_chooses_with_its_value(dialog):
    """Clic sur une vignette : on y va, rien n'est choisi. « Cette image est le clap » : c'est
    elle qui est retenue, avec la valeur annoncée sur la vignette."""
    app, d = dialog
    for ci in range(len(d.claps)):
        d._select_clap(d.claps[ci])
        app.processEvents()
        n = len(d._data()[0])
        for frac in (0.15, 0.5, 0.85):
            d.chosen = {c.id: c.manual_t for c in d.claps}
            d._select_clap(d.claps[ci])
            d._choose_time(d._data()[0][int(frac * (n - 1))])
            app.processEvents()
            for j in (0, len(d._slots) // 2, len(d._slots) - 1):
                before = dict(d.chosen)
                t_thumb = d._t(d._slots[j])
                label = d.thumbs[j].text()
                d.thumbs[j].click()
                app.processEvents()
                assert d.chosen == before                              # se déplacer ne choisit rien
                assert abs(d._t(d.cur) - t_thumb) < 1e-6               # mais on y est
                d.pick_btn.click()
                app.processEvents()
                c = d._clap()
                got = d.chosen[c.id]
                assert abs(got - t_thumb) < 1e-6                       # l'image affichée
                assert abs(d._t(d.cur) - got) < 1e-6 and d.cur in d._slots   # et elle reste affichée
                assert _ms(label) == round((c.t_audio - got) * 1000)    # la valeur annoncée
                d.chosen = {cl.id: cl.manual_t for cl in d.claps}
                d._select_clap(d.claps[ci])
                d._choose_time(d._data()[0][int(frac * (n - 1))])
                app.processEvents()


def _click(app, tl, t, drag_to=None):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    y = int(tl.height() / 2)
    p0 = QPointF(tl.x_of(t), y).toPoint()
    QTest.mousePress(tl, Qt.LeftButton, pos=p0)
    app.processEvents()
    p1 = p0 if drag_to is None else QPointF(tl.x_of(drag_to), y).toPoint()
    if drag_to is not None:
        QTest.mouseMove(tl, p1)
        app.processEvents()
    return p1


def _loaded(app, d, timeout: float = 5.0) -> None:
    """Attend que les images demandées par un clic soient chargées (en fond)."""
    import time
    t0 = time.time()
    while d._wanted is not None and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.005)
    assert d._wanted is None


def _shown(d) -> float:
    return float(d.time_lbl.text().split()[0])


def test_timeline_click_arrows_then_enter(dialog):
    """Clic sur la timeline puis ← → : on se déplace librement, rien n'est choisi ; Entrée choisit
    l'image affichée."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d._select_clap(d.claps[0])
    app.processEvents()
    before = dict(d.chosen)
    times = d._data()[0]
    p = _click(app, d.timeline, times[len(times) // 3])
    QTest.mouseRelease(d.timeline, Qt.LeftButton, pos=p)
    app.processEvents()
    assert d.chosen == before
    t0 = _shown(d)
    QTest.keyClick(d, Qt.Key_Right)
    app.processEvents()
    assert _shown(d) > t0 and d.chosen == before
    QTest.keyClick(d, Qt.Key_Left)
    app.processEvents()
    assert abs(_shown(d) - t0) < 1e-6 and d.chosen == before
    QTest.keyClick(d, Qt.Key_Return)
    app.processEvents()
    c = d._clap()
    assert abs(d.chosen[c.id] - _shown(d)) < 0.0006
    assert abs(d.timeline.chosen - d.chosen[c.id]) < 1e-9 and abs(d.timeline.playhead - d.chosen[c.id]) < 1e-9


def test_timeline_drag_follows_and_never_chooses(dialog):
    """Glisser la poignée de la tête de lecture : l'image suit, rien n'est choisi, même au
    relâchement ; le bouton retient exactement l'image affichée."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d._select_clap(d.claps[0])
    app.processEvents()
    times = d._data()[0]
    before = dict(d.chosen)
    tl = d.timeline
    y = tl.height() // 2
    p0 = QPointF(tl.x_of(times[len(times) // 4]), y).toPoint()
    QTest.mouseClick(tl, Qt.LeftButton, pos=p0)                 # la tête va là…
    app.processEvents()
    QTest.mousePress(tl, Qt.LeftButton, pos=QPointF(tl.x_of(tl.playhead), y).toPoint())   # … on saisit sa poignée
    seen = []
    for frac in (0.3, 0.4, 0.5):
        QTest.mouseMove(tl, QPointF(tl.x_of(times[int(frac * len(times))]), y).toPoint())
        app.processEvents()
        seen.append(_shown(d))
        assert d.chosen == before
    assert len(set(seen)) == 3                                    # l'image a suivi
    QTest.mouseRelease(tl, Qt.LeftButton, pos=QPointF(tl.x_of(times[int(0.5 * len(times))]), y).toPoint())
    app.processEvents()
    assert d.chosen == before
    shown = _shown(d)
    d.pick_btn.click()
    c = d._clap()
    assert abs(d.chosen[c.id] - shown) < 0.0006


def _play_for(app, d, sec):
    import time
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.005)


def test_play_pause_never_chooses(dialog):
    """▶ puis ❚❚ : la vidéo avance en vrai, pause sur l'image en cours (ses images exactes se
    chargent : c'est la même), rien n'est choisi ; jusqu'au bout du plan : arrêt à la fin."""
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d._select_clap(d.claps[0])
    app.processEvents()
    d._ensure_player()
    d.audio.setVolume(0.0)
    kept = dict(d.chosen)
    sh = d.an.shots[d.shot]
    d._goto(sh.start + 0.3)
    t_start = _shown(d)
    d.play_btn.click()
    assert d._playing
    import time
    t0 = time.time()
    while (d._play_t or 0) < t_start + 0.5 and time.time() - t0 < 5:   # le lecteur démarre
        app.processEvents()
        time.sleep(0.005)
    d.play_btn.click()
    played = d._play_t
    assert not d._playing and d.chosen == kept
    assert played > t_start + 0.4                                   # la vidéo a vraiment avancé
    _loaded(app, d)
    assert abs(d._t(d.cur) - played) < 1.5 / d.fps                  # pause = l'image montrée
    assert abs(_shown(d) - d._t(d.cur)) < 0.0006
    d._goto(sh.end - 0.4)                                           # jusqu'au bout du plan
    d.play_btn.click()
    _play_for(app, d, 1.5)
    _loaded(app, d)
    assert not d._playing and d.chosen == kept
    assert _shown(d) > sh.end - 3.0 / d.fps


def _wheel(app, w, dy):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    pos = QPointF(w.width() / 2, w.height() / 2)
    QApplication.sendEvent(w, QWheelEvent(pos, w.mapToGlobal(pos), QPoint(0, 0), QPoint(0, dy), Qt.NoButton,
                                          Qt.NoModifier, Qt.NoScrollPhase, False))
    app.processEvents()


def test_video_size_is_stable_and_wheel_steps_images(dialog):
    """La vidéo tient dans sa zone dès l'ouverture et ne change pas de taille au clic ; la molette
    ne modifie aucune liste ; sur l'image elle passe d'une image à l'autre comme ← → (sans rien choisir)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d._select_clap(d.claps[0])
    app.processEvents()
    r0 = d.preview.drawn()
    assert r0.width() <= d.preview.width() + 0.5 and r0.height() <= d.preview.height() + 0.5
    assert abs(r0.width() - d.preview.width()) < 1 or abs(r0.height() - d.preview.height()) < 1   # ajustée
    tl = d.timeline
    QTest.mouseClick(tl, Qt.LeftButton, pos=QPointF(tl.x_of(d._data()[0][10]), tl.height() // 2).toPoint())
    app.processEvents()
    assert d.preview.drawn() == r0
    state = (d.clap_box.currentIndex(), d.strip_box.currentIndex())
    for w in (d.clap_box, d.strip_box):
        _wheel(app, w, -120)
    assert (d.clap_box.currentIndex(), d.strip_box.currentIndex()) == state
    assert d.preview.drawn() == r0
    kept = dict(d.chosen)
    c = d._clap()
    t0 = _shown(d)
    _wheel(app, d.preview, -120)                   # molette sur l'image : image suivante
    assert d._clap() is c and _shown(d) > t0 and d.chosen == kept
    _wheel(app, d.preview, 120)
    assert d._clap() is c and abs(_shown(d) - t0) < 1e-6 and d.chosen == kept


@pytest.fixture()
def fresh(tmp_path):
    """Analyse neuve (les tests ci-dessous modifient les claps)."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    vid = tmp_path / "v.mkv"
    make(str(vid), Fraction(25), OFFS, ORDER)
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    return app, an


def test_timeline_is_the_whole_shot(fresh):
    """La timeline montre tout le plan, avec ses sons de clap, et ne change jamais d'échelle ; on
    va n'importe où (clic, flèches au-delà des images chargées) ; le bouton choisit l'image affichée."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from synclive.ui_clap import ClapFrameDialog
    app, an = fresh
    cam = next(c for c in an.cameras() if c.claps)
    d = ClapFrameDialog(an, cam)
    d.resize(1300, 900)
    d.show()
    app.processEvents()
    sh = an.shots[d.shot]
    tl = d.timeline

    def whole():
        return abs(tl.ta - sh.start) < 1e-6 and abs(tl.tb - sh.end) < 1e-6
    ts = [c.t_audio for c in d._shot_claps()]
    assert tl.ta <= min(ts) - 0.5 or tl.ta <= sh.start + 1e-6        # vue de départ : tous les claps
    assert tl.tb >= max(ts) + 0.5 or tl.tb >= sh.end - 1e-6
    d.unzoom_btn.click() if d.unzoom_btn.isVisible() else None        # tout le plan
    assert whole()
    assert all(any(abs(t - c.t_audio) < 1e-9 for t, _cur in tl.claps) for c in d._shot_claps())
    for frac in (0.05, 0.95, 0.5):                           # clic n'importe où dans le plan
        t = sh.start + frac * (sh.end - sh.start)
        QTest.mouseClick(tl, Qt.LeftButton, pos=QPointF(tl.x_of(t), tl.height() // 2).toPoint())
        app.processEvents()
        assert abs(float(d.time_lbl.text().split()[0]) - t) < 0.05      # l'instant cliqué, aussitôt
        _loaded(app, d)                                                  # les images arrivent en fond
        assert abs(d._t(d.cur) - t) < 1.5 / d.fps and whole()
    d.pick_btn.click()
    c = d._clap()
    assert d.chosen[c.id] is not None and abs(d.chosen[c.id] - d._t(d.cur)) < 1e-9
    t0 = d._t(d.cur)
    for _ in range(int(2.5 * d.fps)):                       # → au-delà des images chargées
        d._step(1)
    assert d._t(d.cur) > t0 + 2.3 and whole()
    for c in d.claps:                                        # passer d'un clap à l'autre
        d._select_clap(c)
        app.processEvents()
        s2 = an.shots[c.shot]
        assert s2.start - 1e-6 <= tl.ta <= c.t_audio <= tl.tb <= s2.end + 1e-6   # son plan, le clap visible
        assert abs(tl.fa - s2.start) < 1e-6 and abs(tl.fb - s2.end) < 1e-6
    d.close()


def test_false_clap_excluded(fresh):
    """« Ce son n'est pas un clap » : le son ne compte plus (et se remet) ; validé, la fenêtre
    principale l'écarte, Annuler la dernière modification le remet."""
    from synclive import ui
    from synclive.ui_clap import ClapFrameDialog
    app, an = fresh
    cam = next(c for c in an.cameras() if len(c.claps) >= 2)
    d = ClapFrameDialog(an, cam)
    d.show()
    false = d._shot_claps()[0]
    d._select_clap(false)
    app.processEvents()
    d._toggle_excluded()
    assert d.exclusions() == {false.id: True} and not d.pick_btn.isEnabled()
    d._toggle_excluded()
    assert d.exclusions() == {}
    d.close()
    w = ui.MainWindow()
    w.an = an

    def scripted(self):
        self._select_clap(false)
        self._toggle_excluded()
        return ClapFrameDialog.Accepted
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(ClapFrameDialog, "exec", scripted)
    mp.setattr(ClapFrameDialog, "fit_to", lambda self, scr: None)
    try:
        w._adjust(cam.id)
    finally:
        mp.undo()
    assert an.claps[false.id].excluded
    w.undo()
    assert not an.claps[false.id].excluded
    w.close()


def test_stop_goes_back_to_start_of_shot(fresh):
    """■ Stop : la lecture s'arrête et on revient au début du plan, sans rien choisir."""
    from synclive.ui_clap import ClapFrameDialog
    app, an = fresh
    cam = next(c for c in an.cameras() if c.claps)
    d = ClapFrameDialog(an, cam)
    d.show()
    app.processEvents()
    d._ensure_player()
    d.audio.setVolume(0.0)
    kept = dict(d.chosen)
    d.play_btn.click()
    _play_for(app, d, 0.4)
    d.stop_btn.click()
    app.processEvents()
    sh = an.shots[d.shot]
    assert not d._playing and d.chosen == kept
    assert abs(d._t(d.cur) - sh.start) < 1.5 / d.fps and abs(d.timeline.playhead - sh.start) < 1.5 / d.fps
    d.close()


def test_wheel_zooms_like_montage_and_default_view_is_the_claps(fresh):
    """Molette sur la timeline : zoom autour de la souris (comme au Montage), la tête et le choix ne
    bougent pas ; double-clic ou « Tout le plan » : tout le plan. Un plan sans clap détecté
    s'ouvre sur tout le plan."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from synclive.ui_clap import ClapFrameDialog
    app, an = fresh
    cam = next(c for c in an.cameras() if c.claps)
    d = ClapFrameDialog(an, cam)
    d.resize(1300, 900)
    d.show()
    app.processEvents()
    sh = an.shots[d.shot]
    tl = d.timeline
    d.timeline.reset_zoom()
    kept, head = dict(d.chosen), tl.playhead
    span0 = tl.tb - tl.ta
    for _ in range(4):
        _wheel(app, tl, 120)                                  # zoom avant
    assert tl.tb - tl.ta < 0.5 * span0 and d.unzoom_btn.isVisible()
    assert tl.playhead == head and d.chosen == kept
    for _ in range(3):
        _wheel(app, tl, -120)                                 # zoom arrière
    assert tl.tb - tl.ta > 0.5 * span0 * 0.8 ** 4 * 1.25 ** 2
    QTest.mouseDClick(tl, Qt.LeftButton, pos=QPointF(tl.width() // 2, tl.height() // 2).toPoint())
    app.processEvents()
    assert abs(tl.ta - sh.start) < 1e-6 and abs(tl.tb - sh.end) < 1e-6 and not d.unzoom_btn.isVisible()
    d.close()
    # plan sans clap détecté : tout le plan
    for c in an.claps:
        c.excluded = True
    cam2 = next(c for c in an.cameras() if c.id == cam.id)
    d2 = ClapFrameDialog(an, cam2)
    d2.show()
    app.processEvents()
    sh2 = an.shots[d2.shot]
    for c in d2.claps:
        d2.excluded[c.id] = True
    d2._view_shot = None
    d2._set_timeline()
    assert abs(d2.timeline.ta - sh2.start) < 1e-6 and abs(d2.timeline.tb - sh2.end) < 1e-6
    d2.close()


def test_image_stays_with_the_selected_clap(fresh):
    """L'image choisie va avec le clap sélectionné, même tout près du son d'un autre clap (caméra
    très en retard) : jamais de bascule sur un autre son."""
    from synclive.ui_clap import ClapFrameDialog
    app, an = fresh
    cam = next(c for c in an.cameras() if len(c.claps) >= 2)
    d = ClapFrameDialog(an, cam)
    d.show()
    app.processEvents()
    first = d._shot_claps()[0]
    other = next(c for c in d._shot_claps() if c is not first)
    d._select_clap(first)
    before_other = d.chosen.get(other.id)
    d._goto(other.t_audio)                                   # image pile au son de l'autre clap
    d.pick_btn.click()
    app.processEvents()
    assert d._clap() is first
    assert abs(d.chosen[first.id] - d._t(d.cur)) < 1e-9 and d.chosen.get(other.id) == before_other
    d.close()
