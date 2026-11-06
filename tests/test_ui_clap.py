# SPDX-License-Identifier: GPL-3.0-or-later
"""Gestes de « Régler l'image du clap » : ce qu'on clique est ce qui est retenu, et la valeur
affichée sur la vignette est celle obtenue."""
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


def test_thumbnail_click_keeps_that_image_and_its_value(dialog):
    app, d = dialog
    for ci in range(len(d.claps)):
        d.clap_box.setCurrentIndex(ci)
        app.processEvents()
        n = len(d._data()[0])
        for frac in (0.15, 0.5, 0.85):
            d.chosen = {c.id: c.manual_t for c in d.claps}
            d.clap_box.setCurrentIndex(ci)
            d._choose_time(d._data()[0][int(frac * (n - 1))])
            app.processEvents()
            for j in (0, len(d._slots) // 2, len(d._slots) - 1):
                t_thumb = d._t(d._slots[j])
                label = d.thumbs[j].text()
                d.thumbs[j].click()
                app.processEvents()
                c = d._clap()
                got = d.chosen[c.id]
                assert abs(got - t_thumb) < 1e-6                       # l'image cliquée
                assert abs(d._t(d.cur) - got) < 1e-6 and d.cur in d._slots   # et elle reste affichée
                assert _ms(label) == round((c.t_audio - got) * 1000)    # la valeur annoncée
                d.chosen = {cl.id: cl.manual_t for cl in d.claps}
                d.clap_box.setCurrentIndex(ci)
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


def _shown(d) -> float:
    return float(d.time_lbl.text().split()[0])


def test_timeline_click_then_arrows(dialog):
    """Clic sur la timeline : l'image est choisie tout de suite ; les flèches partent de là."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d.clap_box.setCurrentIndex(0)
    app.processEvents()
    times = d._data()[0]
    p = _click(app, d.timeline, times[len(times) // 3])
    QTest.mouseRelease(d.timeline, Qt.LeftButton, pos=p)
    app.processEvents()
    c = d._clap()
    assert abs(d.chosen[c.id] - _shown(d)) < 0.0006
    assert abs(d.timeline.chosen - d.chosen[c.id]) < 1e-9 and abs(d.timeline.playhead - d.chosen[c.id]) < 1e-9
    t0 = d.chosen[c.id]
    QTest.keyClick(d, Qt.Key_Right)
    app.processEvents()
    assert d._clap() is c and d.chosen[c.id] > t0
    QTest.keyClick(d, Qt.Key_Left)
    app.processEvents()
    assert d._clap() is c and abs(d.chosen[c.id] - t0) < 1e-6


def test_timeline_drag_chooses_on_release_only(dialog):
    """Glisser sur la timeline : l'image suit, elle n'est choisie qu'au relâchement, et c'est
    exactement celle affichée à ce moment-là."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d.clap_box.setCurrentIndex(0)
    app.processEvents()
    c = d._clap()
    times = d._data()[0]
    before = d.chosen[c.id]
    tl = d.timeline
    y = tl.height() // 2
    QTest.mousePress(tl, Qt.LeftButton, pos=QPointF(tl.x_of(times[len(times) // 4]), y).toPoint())
    for frac in (0.3, 0.4, 0.5):
        QTest.mouseMove(tl, QPointF(tl.x_of(times[int(frac * len(times))]), y).toPoint())
        app.processEvents()
        assert d.chosen[c.id] == before                  # rien n'est choisi pendant le glisser
    shown = _shown(d)
    QTest.mouseRelease(tl, Qt.LeftButton, pos=QPointF(tl.x_of(times[int(0.5 * len(times))]), y).toPoint())
    app.processEvents()
    c = d._clap()
    assert abs(d.chosen[c.id] - shown) < 0.0006


def test_pause_chooses_the_image_shown(dialog):
    """▶ puis ⏸ : l'image où l'on met pause est choisie ; la fin de l'extrait ne change rien."""
    import time
    import synclive.ui_audio as ua
    app, d = dialog
    ua.AudioPlayer.play = lambda *a, **k: None
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d.clap_box.setCurrentIndex(0)
    app.processEvents()
    d.cur = 0
    d.play_btn.click()
    assert d._playing and d._timer.isActive()
    t0 = time.time()
    while time.time() - t0 < 0.3:
        app.processEvents()
        time.sleep(0.005)
    d.play_btn.click()
    app.processEvents()
    assert not d._playing and not d._timer.isActive()
    c = d._clap()
    assert d.chosen[c.id] is not None and abs(d.chosen[c.id] - _shown(d)) < 0.0006
    # lecture jusqu'au bout : arrêt, retour sur l'image choisie, choix inchangé
    kept = dict(d.chosen)
    d._set_speed(1.0)
    d.cur = len(d._data()[0]) - 3
    d.play_btn.click()
    t0 = time.time()
    while d._playing and time.time() - t0 < 2:
        app.processEvents()
        time.sleep(0.005)
    assert not d._playing and d.chosen == kept
    assert abs(_shown(d) - d.chosen[d._clap().id]) < 0.0006


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
    ne modifie aucune liste, elle passe d'une image à l'autre comme ← →."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d.clap_box.setCurrentIndex(0)
    app.processEvents()
    r0 = d.preview.drawn()
    assert r0.width() <= d.preview.width() + 0.5 and r0.height() <= d.preview.height() + 0.5
    assert abs(r0.width() - d.preview.width()) < 1 or abs(r0.height() - d.preview.height()) < 1   # ajustée
    tl = d.timeline
    QTest.mouseClick(tl, Qt.LeftButton, pos=QPointF(tl.x_of(d._data()[0][10]), tl.height() // 2).toPoint())
    app.processEvents()
    assert d.preview.drawn() == r0
    state = (d.clap_box.currentIndex(), d.strip_box.currentIndex(), d.span_box.currentIndex())
    for w in (d.clap_box, d.strip_box, d.span_box):
        _wheel(app, w, -120)
    assert (d.clap_box.currentIndex(), d.strip_box.currentIndex(), d.span_box.currentIndex()) == state
    assert d.preview.drawn() == r0
    for w in (d.preview, d.timeline):
        c = d._clap()
        t0 = d.chosen[c.id]
        _wheel(app, w, -120)                       # molette vers soi : image suivante
        assert d._clap() is c and d.chosen[c.id] > t0
        _wheel(app, w, 120)
        assert d._clap() is c and abs(d.chosen[c.id] - t0) < 1e-6
