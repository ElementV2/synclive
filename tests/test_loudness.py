# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalisation du son : mesure conforme EBU (signaux de test), niveau atteint, crête sous le
plafond, vidéo recopiée au bit près, son synchro à l'échantillon, et l'onglet « Normalisation »."""
from fractions import Fraction
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
os.environ.setdefault("SYNCLIVE_SETTINGS_INI",                  # réglages de test, à part
                      os.path.join(os.environ.get("TEMP", "/tmp"), "synclive_test_settings.ini"))
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
sys.path.insert(0, str(Path(__file__).parent))
from synth import make  # noqa: E402
from synclive import loudness as L  # noqa: E402

RATE = 48000


def _wav(path, sig, rate=RATE, layout=None, codec="pcm_s24le"):
    with av.open(str(path), "w") as o:
        s = o.add_stream(codec, rate=rate)
        s.layout = layout or ("stereo" if sig.shape[0] == 2 else "mono")
        fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(sig, np.float32), format="fltp", layout=s.layout.name)
        fr.sample_rate = rate
        fr.pts = 0
        for p in s.encode(fr):
            o.mux(p)
        for p in s.encode(None):
            o.mux(p)
    return str(path)


def test_ebu_3341_sine_and_gating(tmp_path):
    t = np.arange(RATE * 20) / RATE
    a = 10 ** (-23 / 20)
    m = L.measure(_wav(tmp_path / "s.wav", np.vstack([a * np.sin(2 * np.pi * 1000 * t)] * 2))).main
    assert abs(m.integrated + 23.0) < 0.1 and abs(m.true_peak + 23.0) < 0.1 and m.lra < 0.1
    # 10 s à −36 puis 10 s à −23 : le fenêtrage relatif ignore le passage calme (EBU 3341 test 3)
    x = np.where(t < 10, 10 ** (-36 / 20), a) * np.sin(2 * np.pi * 1000 * t)
    m = L.measure(_wav(tmp_path / "g.wav", np.vstack([x, x]))).main
    assert abs(m.integrated + 23.0) < 0.1
    # crête entre échantillons : sinus à fs/4 déphasé, crête vraie 0,9 (−0,9 dBTP) mais échantillons à 0,64
    x = 0.9 * np.sin(2 * np.pi * (RATE / 4) * t + np.pi / 4)
    m = L.measure(_wav(tmp_path / "i.wav", np.vstack([x, x]))).main
    assert -1.1 < m.true_peak < -0.6


def test_lfe_is_not_counted(tmp_path):
    t = np.arange(RATE * 6) / RATE
    tone = 0.1 * np.sin(2 * np.pi * 1000 * t)
    sig = np.zeros((6, t.size))
    sig[0] = sig[1] = tone
    ref = L.measure(_wav(tmp_path / "a.wav", sig, layout="5.1")).main.integrated
    sig[3] = 0.9 * np.sin(2 * np.pi * 60 * t)            # canal LFE très fort
    assert abs(L.measure(_wav(tmp_path / "b.wav", sig, layout="5.1")).main.integrated - ref) < 0.05


def _video_hash(path):
    h = hashlib.sha1()
    with av.open(path) as c:
        for p in c.demux(c.streams.video[0]):
            if p.size:
                h.update(bytes(p))
                h.update(str(p.pts).encode())
    return h.hexdigest()


def _mono(path):
    with av.open(path) as c:
        st = c.streams.audio[0]
        res = av.AudioResampler(format="flt", layout="mono", rate=st.codec_context.sample_rate)
        out, t0 = [], None
        for f in c.decode(st):
            if t0 is None:
                t0 = float(f.time or 0.0)
            for o in res.resample(f):
                out.append(o.to_ndarray().reshape(-1))
        return np.concatenate(out), t0


@pytest.mark.parametrize("ext,codec", [(".mkv", "pcm_s16le"), (".mp4", "aac")])
def test_normalize_video_keeps_video_and_sync(tmp_path, ext, codec):
    src = tmp_path / f"v{ext}"
    make(str(src), Fraction(30000, 1001), {"A": 0.05}, ["A"], shot_len=4.0, audio_codec=codec)
    for target in (L.Target(-23.0, -1.0), L.Target(-14.0, -1.0)):
        info = L.measure(str(src))
        dst = L.output_path(str(src), str(tmp_path / "out"), target)
        plans, after = L.normalize_checked(str(src), dst, info, target)
        after = after.main
        assert after.true_peak <= target.peak + 0.05
        assert abs(after.integrated - plans[0].expected_lufs) < 0.5          # ce qui était annoncé
        if plans[0].expected_lufs >= target.lufs - 0.25:
            assert abs(after.integrated - target.lufs) < 0.5
        assert _video_hash(str(src)) == _video_hash(str(dst))                # vidéo au bit près
        a, ta = _mono(str(src))
        b, tb = _mono(str(dst))
        n = min(a.size, b.size)
        cc = fftconvolve(b[:n], a[:n][::-1], mode="full")
        lag = int(np.argmax(np.abs(cc))) - (n - 1) + round((tb - ta) * RATE)
        assert lag == 0                                                      # synchro à l'échantillon
        assert abs(b.size - a.size) < 2048 + RATE * 0.03


def test_limiter_respects_ceiling_and_reaches_target(tmp_path):
    rng = np.random.default_rng(3)
    t = np.arange(RATE * 12) / RATE
    x = 0.03 * rng.standard_normal(t.size)
    for k in range(1, 12):                                # claps : crêtes fortes et brèves
        i = int(k * RATE)
        x[i:i + 300] += 0.95 * np.exp(-np.arange(300) / 60) * np.sign(rng.standard_normal(300))
    src = _wav(tmp_path / "claps.wav", np.vstack([x, x]))
    info = L.measure(src)
    target = L.Target(-16.0, -1.0)
    pl = L.plan(info.main, target)
    assert 0 < pl.limited_db <= L.MAX_LIMIT_DB + 1e-6
    dst = L.output_path(src, str(tmp_path), target)
    L.normalize(src, dst, info, target)
    after = L.measure(str(dst)).main
    assert after.true_peak <= -1.0 + 0.05
    assert abs(after.integrated - pl.expected_lufs) < 0.3
    # sans limiteur : gain plafonné, crête respectée, fichier sous la cible
    t2 = L.Target(-16.0, -1.0, limiter=False)
    p2 = L.plan(info.main, t2)
    assert p2.limited_db == 0 and p2.expected_lufs < -16.0 and "au lieu de" in p2.note


def test_page_drop_measure_common_target_normalize(tmp_path):
    from PySide6.QtWidgets import QApplication
    from synclive.ui_loudness import LoudnessPage
    app = QApplication.instance() or QApplication([])
    t = np.arange(RATE * 6) / RATE
    files = [_wav(tmp_path / f"f{k}.wav", np.vstack([a * np.sin(2 * np.pi * 440 * t)] * 2))
             for k, a in enumerate((0.02, 0.2, 0.6))]
    (tmp_path / "sub").mkdir()
    files.append(_wav(tmp_path / "sub" / "g.wav", np.vstack([0.05 * np.sin(2 * np.pi * 300 * t)] * 2)))
    page = LoudnessPage()
    page.show()
    page.add_paths(files[:3] + [str(tmp_path / "sub"), files[0]])      # dossier + doublon
    assert len(page.items) == 4
    t0 = time.time()
    while any(it.state == "mesure" for it in page.items) and time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.01)
    assert all(it.state == "prêt" for it in page.items), [(it.state, it.error) for it in page.items]
    page.out_choose.setChecked(True)
    page.out_dir = str(tmp_path / "out")
    page.common_btn.click()
    t0 = time.time()                                    # les plans se calculent en tâche de fond
    while (any(it.plan is None for it in page.items) or not page.go_btn.isEnabled()) and time.time() - t0 < 30:
        app.processEvents()
        time.sleep(0.01)
    page.go_btn.click()
    t0 = time.time()
    while page.norm is not None and time.time() - t0 < 120:
        app.processEvents()
        time.sleep(0.01)
    assert all(it.state == "fait" for it in page.items), [(it.state, it.error) for it in page.items]
    lv = [it.after.main.integrated for it in page.items]
    assert max(lv) - min(lv) < 0.3                                     # tout le lot au même niveau
    assert all(it.after.main.true_peak <= page.target().peak + 0.05 for it in page.items)
    page.shutdown()


def _packets(path, ranges=None):
    with av.open(path) as c:
        out = []
        for p in c.demux(c.streams.video[0]):
            if p.size:
                out.append((float(p.pts * p.time_base), p.is_keyframe, hashlib.sha1(bytes(p)).hexdigest()))
        return out


@pytest.mark.parametrize("k", [0, 2, 4, 5])
def test_edit_random_media_lossless_and_in_sync(tmp_path, k):
    """Médias variés (format, nature, volume au hasard), montage dans le désordre : paquets vidéo
    recopiés à l'identique entre images clés, son de chaque morceau à sa place (± 1 échantillon,
    ½ ms en MKV), niveau et crête atteints sur le fichier écrit."""
    import media_gen
    fmt = media_gen.FORMATS[k]
    src = str(tmp_path / f"m{fmt[0]}")
    media_gen.make(src, media_gen.KINDS[k % 4], -45.0 + 8 * k, 10.0, fmt, seed=k)
    info = L.measure(src)
    D = L.file_end(info) - info.start
    segs = [(L.snap(info, info.start + 0.55 * D), L.snap(info, info.start + 0.9 * D)),
            (L.snap(info, info.start + 0.1 * D), L.snap(info, info.start + 0.4 * D))]
    segs = [(a, b) for a, b in segs if b - a > 0.5]
    assert len(segs) == 2
    target = L.Target(-23.0, -1.0)
    plans, after = L.normalize_checked(src, tmp_path / f"o{fmt[0]}", info, target, segments=segs)
    out = str(tmp_path / f"o{fmt[0]}")
    assert abs(after.duration - L.kept(segs)) < 0.08
    assert abs(after.main.integrated - target.lufs) < 0.3 and after.main.true_peak <= -0.95
    if info.has_video:
        srcp = _packets(src)
        exp = []
        for a, b in segs:                               # de l'image clé a (incluse) à l'image clé b (exclue)
            on = False
            for t, key, h in srcp:
                if not on:
                    on = key and t >= a - 1e-4
                    if not on:
                        continue
                if key and t >= b - 1e-4:
                    break
                exp.append(h)
        assert [h for _, _, h in _packets(out)] == exp
    a_src, ta = _mono(src)
    a_out, tb = _mono(out)
    off = info.start
    for a, b in segs:
        i0, j0, n = int((a + 0.3 - ta) * RATE), int((off + 0.3 - tb) * RATE), int((b - a - 0.6) * RATE)
        x, y = a_src[i0:i0 + n], a_out[j0:j0 + n]
        cc = fftconvolve(y, x[::-1], mode="full")
        assert abs(int(np.argmax(np.abs(cc))) - (x.size - 1)) <= (24 if fmt[0] == ".mkv" else 1)
        off += b - a
