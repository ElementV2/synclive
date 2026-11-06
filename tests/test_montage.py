# SPDX-License-Identifier: GPL-3.0-or-later
"""Onglet Montage : ouverture instantanée, coupes calées sur les images clés, export en copie
(vidéo identique au bit près, son recopié synchro à ½ paquet près, sans dérive) ou son recalculé
(synchro à l'échantillon), et les gestes du banc."""
from pathlib import Path
import hashlib
import os
import sys
import time

import av
import numpy as np
import pytest
from scipy.signal import fftconvolve

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
sys.path.insert(0, str(Path(__file__).parent))
import media_gen  # noqa: E402
from synclive import montage as M  # noqa: E402


def _vpk(path, ranges):
    out = []
    with av.open(path) as c:
        v = c.streams.video[0]
        for a, b in ranges:
            c.seek(int(max(0, a - 1) / v.time_base), stream=v, backward=True)
            on = False
            for p in c.demux(v):
                if not p.size or p.pts is None:
                    continue
                t = float(p.pts * p.time_base)
                if not on:
                    on = p.is_keyframe and t >= a - 1e-4
                    if not on:
                        continue
                if p.is_keyframe and t >= b - 1e-4:
                    break
                out.append(hashlib.sha1(bytes(p)).hexdigest())
    return out


def _audio_at(path, t, n):
    with av.open(path) as c:
        a = c.streams.audio[0]
        r = a.codec_context.sample_rate
        c.seek(int(max(0, t - 2) / a.time_base), stream=a, backward=True)
        res = av.AudioResampler(format="flt", layout="mono", rate=r)
        buf, t0 = [], None
        for f in c.decode(a):
            if f.time is None:
                continue
            for x in res.resample(f):
                if t0 is None:
                    t0 = float(f.time)
                buf.append(x.to_ndarray().reshape(-1))
            if float(f.time) > t + n / r + 1:
                break
        y = np.concatenate(buf)
        i = int(round((t - t0) * r))
        return y[i:i + n], r


def _lag_ms(src, ts, dst, to):
    x, r = _audio_at(src, ts, 24000)
    y, _ = _audio_at(dst, to - 0.1, 24000 + int(0.2 * r))
    cc = fftconvolve(y, x[::-1], mode="valid")
    return int(np.argmax(np.abs(cc))) / (r / 1000) - 100


@pytest.mark.parametrize("k", [0, 1, 2, 3])            # MP4/AAC, MOV/PCM, MKV/Opus, MXF/PCM
@pytest.mark.parametrize("exact", [False, True])
def test_export_lossless_and_in_sync(tmp_path, k, exact):
    fmt = media_gen.FORMATS[k]
    src = str(tmp_path / f"live{fmt[0]}")
    media_gen.make(src, "ambiance", -20.0, 14.0, fmt, seed=k)        # bruit : pas de motif répété
    t0 = time.perf_counter()
    m = M.open_media(src)
    ki = M.KeyIndex(m)
    assert time.perf_counter() - t0 < 1.0                      # ouverture immédiate
    D = m.duration
    segs = [(ki.snap(m.start + 0.55 * D), ki.snap(m.start + 0.9 * D)),
            (ki.snap(m.start + 0.1 * D), ki.snap(m.start + 0.45 * D))]
    for a, b in segs:                                          # coupes sur des images clés
        assert a in ki.known and (b in ki.known or b == m.end)
    dst = str(tmp_path / f"out{fmt[0]}")
    M.export(m, segs, dst, exact_audio=exact)
    o = M.open_media(dst)
    assert abs(o.duration - sum(b - a for a, b in segs)) < 0.06
    assert _vpk(dst, [(o.start, o.end + 5)]) == _vpk(src, segs)      # vidéo au bit près
    ki.close()
    off = o.start
    # ms. Recopié : ½ paquet au plus (AAC 21 ms, Opus 20 ms). Recalculé : à l'échantillon, sauf en MKV
    # dont les horodatages sont à la milliseconde
    tol = (2.0 if fmt[0] == ".mkv" else 0.1) if exact else 11.0
    for a, b in segs:
        for frac in (0.15, 0.5, 0.8):
            ts = a + frac * (b - a)
            assert abs(_lag_ms(src, ts, dst, off + (ts - a))) <= tol
        off += b - a


def test_keyindex_and_overview(tmp_path):
    src = str(tmp_path / "v.mp4")
    media_gen.make(src, "parole", -25.0, 12.0, media_gen.FORMATS[0], seed=3)
    m = M.open_media(src)
    ki = M.KeyIndex(m)
    b, a = ki.around(5.0)
    assert b is not None and a is not None and b <= 5.0 < a
    assert ki.step(b, 1) == a and ki.step(a, -1) == b
    assert ki.snap(5.0) in (b, a)
    ov = M.Overview(m)
    ov.start()
    ov.join(20)
    assert ov.done_until >= m.end - 0.5 and ov.levels.max() > -40
    ki.close()


def test_page_open_edit_export(tmp_path):
    """L'onglet : glisser le fichier, I / O / C / Suppr, exporter, fichier écrit juste."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from synclive.ui_montage import MontagePage
    app = QApplication.instance() or QApplication([])
    src = str(tmp_path / "live.mp4")
    media_gen.make(src, "musique", -18.0, 16.0, media_gen.FORMATS[0], seed=5)
    page = MontagePage()
    page.resize(1300, 900)
    page.show()
    page.open_file(src)
    ed = page.editor
    ed.audio.setVolume(0.0)
    app.processEvents()
    tl = ed.timeline
    y = tl.height() // 2
    s = page.src

    def click(p):                                   # p : instant du MONTAGE (la timeline est le montage)
        QTest.mouseClick(tl, Qt.LeftButton, pos=QPointF(tl.x_of(p), y).toPoint())
        app.processEvents()

    D = s.end - s.start
    click(2.0); QTest.keyClick(ed, Qt.Key_I)                # début : tout ce qui est avant part
    assert abs(tl.total() - (D - ed.segments[0][0] + s.start)) < 1e-6 and ed._out() < 0.05
    click(tl.total() - 2.0); QTest.keyClick(ed, Qt.Key_O)   # fin
    n0 = tl.total()
    click(4.0); QTest.keyClick(ed, Qt.Key_C)
    click(8.0); QTest.keyClick(ed, Qt.Key_C)
    assert len(ed.segments) == 3
    click(6.0); QTest.keyClick(ed, Qt.Key_Delete)           # suppression : les suivants se recollent
    app.processEvents()
    assert len(ed.segments) == 2
    removed = n0 - tl.total()
    assert removed > 2.0 and abs(ed._out() - (ed.segments[0][1] - ed.segments[0][0])) < 0.05
    for a, b in ed.segments:
        assert a in s.keys.known
    assert ed._clip == 1                                    # la tête : début du morceau qui suit
    QTest.keyClick(ed, Qt.Key_Left)                         # image par image, à travers le raccord
    assert ed._clip == 0
    QTest.keyClick(ed, Qt.Key_Right)
    assert ed._clip == 1
    ed.move_segment(1, -1)
    assert ed.segments[0][0] > ed.segments[1][0] and ed.list.count() == 2
    ed.undo()
    assert ed.segments[0][0] < ed.segments[1][0]
    page.out = Path(tmp_path / "montage.mp4")
    page.go_btn.click()
    t0 = time.time()
    while page.worker is not None and time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.01)
    assert page.result.text().startswith("✓"), page.result.text()
    o = M.open_media(str(tmp_path / "montage.mp4"))
    assert abs(o.duration - sum(b - a for a, b in ed.segments)) < 0.06
    page.shutdown()
