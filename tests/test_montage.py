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
os.environ.setdefault("SYNCLIVE_PROJECTS_DIR",                  # montages de test, à part
                      os.path.join(os.environ.get("TEMP", "/tmp"), "synclive_test_montages"))
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
    s = page.srcs[0]

    def click(p):                                   # p : instant du MONTAGE (la timeline est le montage)
        QTest.mouseClick(tl, Qt.LeftButton, pos=QPointF(tl.x_of(p), y).toPoint())
        app.processEvents()

    D = s.end - s.start
    click(2.0); QTest.keyClick(ed, Qt.Key_I)                # début : tout ce qui est avant part
    assert abs(tl.total() - (D - ed.segments[0][1] + s.start)) < 1e-6 and ed._out() < 0.05
    click(tl.total() - 2.0); QTest.keyClick(ed, Qt.Key_O)   # fin
    n0 = tl.total()
    click(4.0); QTest.keyClick(ed, Qt.Key_C)
    click(8.0); QTest.keyClick(ed, Qt.Key_C)
    assert len(ed.segments) == 3
    click(6.0); QTest.keyClick(ed, Qt.Key_Delete)           # suppression : les suivants se recollent
    app.processEvents()
    assert len(ed.segments) == 2
    removed = n0 - tl.total()
    assert removed > 2.0 and abs(ed._out() - (ed.segments[0][2] - ed.segments[0][1])) < 0.05
    for _f, a, b in ed.segments:
        assert a in s.keys.known
    assert ed._clip == 1                                    # la tête : début du morceau qui suit
    QTest.keyClick(ed, Qt.Key_Left)                         # image par image, à travers le raccord
    assert ed._clip == 0
    QTest.keyClick(ed, Qt.Key_Right)
    assert ed._clip == 1
    ed.move_segment(1, -1)
    assert ed.segments[0][1] > ed.segments[1][1] and ed.list.count() == 2
    ed.undo()
    assert ed.segments[0][1] < ed.segments[1][1]
    page.out = Path(tmp_path / "montage.mp4")
    page.go_btn.click()
    t0 = time.time()
    while page.worker is not None and time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.01)
    assert page.result.text().startswith("✓"), page.result.text()
    o = M.open_media(str(tmp_path / "montage.mp4"))
    assert abs(o.duration - sum(b - a for _f, a, b in ed.segments)) < 0.06
    page.shutdown()


def _pause_live(tmp_path, tracks: bool = False) -> str:
    """Live de test : écran d'attente fixe, live (bruit qui bouge), écran animé en boucle, live.
    Avec `tracks` : piste 1 = son du live (muette pendant les pauses), piste 2 = musique des pauses."""
    from fractions import Fraction
    fps = Fraction(25)
    path = str(tmp_path / ("pauses_pistes.mp4" if tracks else "pauses.mp4"))
    rng = np.random.default_rng(1)
    plan = [("fixe", 30.0), ("live", 40.0), ("anim", 30.0), ("live", 40.0)]    # 43 % de pauses
    out = av.open(path, "w")
    vs = out.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = 160, 90, "yuv420p"
    vs.options = {"g": "25", "keyint_min": "25", "preset": "ultrafast"}
    ats = [out.add_stream("aac", rate=48000) for _ in range(2 if tracks else 1)]
    for a in ats:
        a.layout = "stereo"
    k = 0
    pauses = []
    tsec = 0.0
    for kind, dur in plan:
        n = int(dur * 25)
        if kind != "live":
            pauses.append((tsec, tsec + dur))
        for i in range(n):
            if kind == "fixe":
                img = np.full((90, 160, 3), 60, np.uint8)
            elif kind == "anim":                      # boucle de 2 s : un carré qui tourne
                ph = (i % 50) / 50
                img = np.full((90, 160, 3), (30, 30, 90), np.uint8)
                x = int(60 + 40 * np.cos(2 * np.pi * ph))
                img[30:60, x:x + 30] = 250
            else:
                img = rng.integers(0, 255, (90, 160, 3), dtype=np.uint8)
            fr = av.VideoFrame.from_ndarray(img, format="rgb24")
            fr.pts = k
            fr.time_base = Fraction(1, 25)
            k += 1
            for pk in vs.encode(fr):
                out.mux(pk)
        tsec += dur
    for pk in vs.encode(None):
        out.mux(pk)
    total = tsec
    t = np.arange(int(total * 48000)) / 48000
    inp = np.zeros(t.size, bool)
    for a, b in pauses:
        inp |= (t >= a) & (t < b)
    live = (rng.standard_normal(t.size) * 0.1).astype(np.float32)
    music = (0.2 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    sig1 = np.where(inp, 0.0, live).astype(np.float32) if tracks else live
    sigs = [sig1, np.where(inp, music, 0.0).astype(np.float32)] if tracks else [sig1]
    for a, sig in zip(ats, sigs):
        for i in range(0, sig.size, 1024):
            chunk = np.ascontiguousarray(np.vstack([sig[i:i + 1024]] * 2))
            fr = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
            fr.sample_rate = 48000
            fr.pts = i
            fr.time_base = Fraction(1, 48000)
            for pk in a.encode(fr):
                out.mux(pk)
        for pk in a.encode(None):
            out.mux(pk)
    out.close()
    return path


def test_pause_detection_still_like_and_track(tmp_path):
    """Écran fixe repéré tout seul ; écran animé retrouvé « comme celui-ci » ; piste de la musique
    des pauses proposée et ses passages repérés ; bornes sur des images clés, dans la pause."""
    p = _pause_live(tmp_path)
    m = M.open_media(p)
    ov = M.Overview(m)
    ov.start()
    ov.join(30)
    kt = M.key_thumbs(m)
    st = M.detect_still(kt, m, 10.0, ov)
    assert len(st) == 1 and st[0].start < 1.0 and 27.0 <= st[0].end <= 30.0, st     # l'écran fixe
    lk = M.detect_like(kt, m, M.reference_thumbs(m, 85.0), 10.0, ov)
    assert any(70.0 <= x.start <= 72.0 and 97.0 <= x.end <= 100.0 for x in lk), lk  # l'écran animé
    keys = set(kt.times.tolist())
    assert all(x.start in keys and x.end in keys for x in st + lk)
    p2 = _pause_live(tmp_path, tracks=True)
    m2 = M.open_media(p2)
    ov2 = M.Overview(m2)
    ov2.start()
    ov2.join(30)
    kt2 = M.key_thumbs(m2)
    assert M.suggest_pause_track(ov2, 10.0) == 1                                 # piste 2
    tr = M.detect_track(ov2, m2, 1, kt2.times, 10.0)
    assert len(tr) == 2, tr
    assert tr[0].start < 1.0 and 28.0 <= tr[0].end <= 30.0
    assert 70.0 <= tr[1].start <= 71.0 and 98.0 <= tr[1].end <= 100.0


def test_multi_file_export_and_refusal(tmp_path):
    """Deux fichiers de même format bout à bout (dans le désordre) : vidéo identique au bit près ;
    un fichier d'un autre format est refusé avec la raison."""
    import shutil
    fmt = media_gen.FORMATS[0]
    a, b = str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")
    media_gen.make(a, "ambiance", -20.0, 12.0, fmt, seed=1)
    shutil.copy(a, b)
    ma, mb = M.open_media(a), M.open_media(b)
    ka, kb = M.KeyIndex(ma), M.KeyIndex(mb)
    segs = [(1, kb.snap(6.0), kb.snap(11.0)), (0, ka.snap(1.0), ka.snap(5.0))]
    assert M.incompatibility([ma, mb]) is None
    dst = str(tmp_path / "out.mp4")
    M.export([ma, mb], segs, dst)
    o = M.open_media(dst)
    assert abs(o.duration - sum(y - x for _s, x, y in segs)) < 0.08
    exp = _vpk(b, [segs[0][1:]]) + _vpk(a, [segs[1][1:]])
    assert _vpk(dst, [(o.start, o.end + 5)]) == exp
    c = str(tmp_path / "c.mkv")
    media_gen.make(c, "ambiance", -20.0, 6.0, media_gen.FORMATS[2], seed=2)
    mc = M.open_media(c)
    assert M.incompatibility([ma, mc])
    with pytest.raises(ValueError):
        M.export([ma, mc], [(0, 0.0, 2.0), (1, 0.0, 2.0)], str(tmp_path / "x.mp4"))
    ka.close()
    kb.close()


def test_project_saved_and_reopened(tmp_path, monkeypatch):
    """Le montage est enregistré tout seul et repris à la réouverture ; une vidéo modifiée n'est
    pas reprise (montage périmé)."""
    from PySide6.QtWidgets import QApplication
    from synclive.ui_montage import MontagePage
    monkeypatch.setenv("SYNCLIVE_PROJECTS_DIR", str(tmp_path / "projets"))
    app = QApplication.instance() or QApplication([])
    src = str(tmp_path / "live.mp4")
    media_gen.make(src, "parole", -20.0, 12.0, media_gen.FORMATS[0], seed=4)
    page = MontagePage()
    page.show()
    page.open_file(src)
    ed = page.editor
    ed.audio.setVolume(0.0)
    ed._goto_out(3.0)
    ed.start_here()
    ed._goto_out(4.0)
    ed.split_here()
    app.processEvents()
    segs = list(ed.segments)
    assert len(segs) == 2
    page.save()
    page.close_file()
    rec = M.recent_projects()
    assert rec and rec[0]["files"][0]["path"].endswith("live.mp4")
    page.open_file(src)
    assert page.editor.segments == segs and not page.banner.isHidden()
    page.close_file()
    os.utime(src, (1, 1))                                   # vidéo changée : montage périmé
    assert M.load_project(src) is None
    page.shutdown()


def test_track_meter_all_tracks(tmp_path, monkeypatch):
    """Vumètre : toutes les pistes à la fois, mêmes niveaux par la lecture à la volée que par
    l'aperçu ; la piste de la musique ne s'allume que pendant les pauses ; clic = écouter."""
    import time as _t
    from PySide6.QtWidgets import QApplication
    from synclive.ui_montage import MontagePage
    monkeypatch.setenv("SYNCLIVE_PROJECTS_DIR", str(tmp_path / "projets"))
    app = QApplication.instance() or QApplication([])
    p = _pause_live(tmp_path, tracks=True)
    m = M.open_media(p)
    r = M.LevelReader(m)
    r.start()
    ov = M.Overview(m)
    ov.start()
    ov.join(30)
    for t in (10.0, 40.0, 60.0, 90.0):
        r.request(t)
        t0 = _t.perf_counter()
        while r.level(0, t) is None and _t.perf_counter() - t0 < 5:
            _t.sleep(0.01)
        i = int((t - m.start) / M.Overview.STEP)
        for k in range(2):
            assert r.level(k, t) is not None and abs(r.level(k, t) - float(ov.tracks[k][i])) < 3.0, (t, k)
    r.stop()
    page = MontagePage()
    page.show()
    page.open_file(p)
    ed = page.editor
    ed.audio.setVolume(0.0)
    for _ in range(20):                                     # le vumètre se met à jour tout seul
        app.processEvents()
        _t.sleep(0.02)
    assert ed.meter.n == 2 and ed.meter.isVisible()
    lv_pause = ed.track_levels(0, 15.0)                     # écran fixe : musique seule
    lv_live = ed.track_levels(0, 40.0)
    deadline = _t.perf_counter() + 5
    while (None in lv_pause or None in lv_live) and _t.perf_counter() < deadline:
        app.processEvents()
        _t.sleep(0.02)
        lv_pause, lv_live = ed.track_levels(0, 15.0), ed.track_levels(0, 40.0)
    assert lv_pause[1] > -30 and lv_pause[0] < -60
    assert lv_live[0] > -40 and lv_live[1] < -60
    ed.meter.picked.emit(1)
    app.processEvents()
    assert page.listen_box.currentData() == 1 and ed.meter.listened == 1
    page.shutdown()
