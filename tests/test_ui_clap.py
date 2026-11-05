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
            d.timeline.setValue(int(frac * (n - 1)))
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
                d.timeline.setValue(int(frac * (n - 1)))
                app.processEvents()


def test_timeline_then_button_and_arrows(dialog):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d.clap_box.setCurrentIndex(0)
    app.processEvents()
    n = len(d._data()[0])
    d.timeline.setValue(n // 3)                     # clic sur la timeline : choisie, comme les flèches
    app.processEvents()
    shown = float(d.time_lbl.text().split()[0])
    c = d._clap()
    assert abs(d.chosen[c.id] - shown) < 0.0006
    d.pick_btn.click()
    app.processEvents()
    c = d._clap()
    assert abs(d.chosen[c.id] - shown) < 0.0006
    t0 = d.chosen[c.id]
    QTest.keyClick(d, Qt.Key_Right)
    app.processEvents()
    assert d._clap() is c and d.chosen[c.id] > t0
    QTest.keyClick(d, Qt.Key_Left)
    app.processEvents()
    assert d._clap() is c and abs(d.chosen[c.id] - t0) < 1e-6


def test_timeline_drag_chooses_on_release_only(dialog):
    """Glisser la timeline ou la forme d'onde : l'image suit, elle n'est choisie qu'au relâchement,
    et c'est exactement celle affichée à ce moment-là."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    app, d = dialog
    d.chosen = {c.id: c.manual_t for c in d.claps}
    d.clap_box.setCurrentIndex(0)
    app.processEvents()
    c = d._clap()
    n = len(d._data()[0])
    before = d.chosen[c.id]
    d.timeline.setSliderDown(True)                   # glisser en cours
    for k in (n // 4, n // 3, n // 2):
        d.timeline.setValue(k)
        app.processEvents()
        assert d.chosen[c.id] == before              # rien n'est choisi pendant le glisser
    shown = float(d.time_lbl.text().split()[0])
    d.timeline.setSliderDown(False)                  # relâché
    app.processEvents()
    c = d._clap()
    assert abs(d.chosen[c.id] - shown) < 0.0006
    # forme d'onde : glisser puis relâcher
    times = d._data()[0]
    x = d.wave.x_of(times[len(times) // 3])
    QTest.mousePress(d.wave, Qt.LeftButton, pos=QPointF(x, 20).toPoint())
    QTest.mouseMove(d.wave, QPointF(x + 30, 20).toPoint())
    app.processEvents()
    QTest.mouseRelease(d.wave, Qt.LeftButton, pos=QPointF(x + 30, 20).toPoint())
    app.processEvents()
    c = d._clap()
    assert abs(d.chosen[c.id] - float(d.time_lbl.text().split()[0])) < 0.0006
