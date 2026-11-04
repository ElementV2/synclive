# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests de bout en bout sur vidéos synthétiques à décalages connus."""
from fractions import Fraction
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from synth import make, make_pattern_capture  # noqa: E402
from synclive import analysis, export  # noqa: E402

OFFS = {"A": 0.080, "B": -0.040, "C": 0.133}
ORDER = ["A", "B", "C", "A", "B"]


@pytest.mark.parametrize("fps", ["25", "30000/1001", "60"])
def test_offsets_per_camera(tmp_path, fps):
    f = Fraction(fps)
    vid = tmp_path / "v.mkv"
    make(str(vid), f, OFFS, ORDER)
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    cams = an.cameras()
    assert len(an.shots) == 5
    assert len(cams) == 3                       # A et B reviennent : regroupés
    expected = [OFFS["A"], OFFS["B"], OFFS["C"]]  # ordre d'apparition
    for cam, exp in zip(cams, expected):
        assert cam.offset is not None
        err = abs(cam.offset - exp)
        # précision méthode clap : ½ image ou 15 ms
        assert err < max(0.5 / float(f), 0.015), f"{cam.name}: {cam.offset*1000:.1f} ms vs {exp*1000:.0f} ms"
        assert cam.confidence in ("élevée", "moyenne")
    files = export.export_all(an, tmp_path / "out")
    assert {p.name for p in files} >= {"CAM_1.png", "CAM_2.png", "CAM_3.png", "recapitulatif.png",
                                       "rapport.json", "rapport_claps.csv"}


def test_aac_mp4(tmp_path):
    vid = tmp_path / "v.mp4"
    make(str(vid), Fraction(30000, 1001), OFFS, ORDER, seed=3, audio_codec="aac")
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    for cam, exp in zip(an.cameras(), [OFFS["A"], OFFS["B"], OFFS["C"]]):
        assert abs(cam.offset - exp) < 0.012


def test_exclusion_and_merge(tmp_path):
    vid = tmp_path / "v.mkv"
    make(str(vid), Fraction(25), OFFS, ORDER)
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    n0 = an.cameras()[0].n_used
    an.claps[0].excluded = True
    assert an.cameras()[0].n_used == n0 - 1
    an.merge_cameras(2, 0)
    assert len(an.cameras()) == 2
    an.move_shot(2, None)
    assert len(an.cameras()) == 3


@pytest.mark.parametrize("fps", ["25", "30000/1001", "60000/1001", "50"])
def test_pattern_sources_same_image_different_latency(tmp_path, fps):
    """Même mire (même image) jouée par deux PC de latences différentes + une autre source."""
    f = Fraction(fps)
    vid = tmp_path / "m.mkv"
    make_pattern_capture(str(vid), f, [(1, 0.100, 6), (1, 0.250, 6), (4, -0.050, 6), (1, 0.100, 5)])
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    cams = an.cameras()
    got = [(c.name, round(c.offset * 1000), c.source_label, c.kind) for c in cams]
    assert len(cams) == 3, got
    # même QR (même PC) mais latence différente -> deux sources, la 2e suffixée « (2) »
    for cam, (exp, label, name) in zip(cams, [(100, "01 · FALCON", "01 · FALCON"),
                                              (250, "01 · FALCON", "01 · FALCON (2)"),
                                              (-50, "04 · NOVA", "04 · NOVA")]):
        assert cam.kind == "mire"
        assert cam.source_label == label, got
        assert cam.name == name, got
        assert abs(cam.offset * 1000 - exp) < 3, got      # mire : précision à l'échantillon près
        assert cam.confidence == "élevée"
    assert len(cams[0].shots) == 2                          # la source 1 à +100 ms revient


def test_generated_pattern_file(tmp_path):
    """Le fichier de mire généré se relit avec un décalage nul (fichier parfaitement synchro)."""
    from synclive import pattern
    spec = pattern.PatternSpec(640, 360, pattern.FRAME_RATES["29.97"], source=2, periods=6)
    out = pattern.render(spec, tmp_path / spec.filename())
    an = analysis.run(str(out), analysis.Settings(use_hands=False))
    cams = an.cameras()
    assert len(cams) == 1 and cams[0].kind == "mire" and cams[0].source_label == "02 · ORION"
    assert abs(cams[0].offset) < 0.003


def test_two_pcs_same_loop_same_latency_told_apart_by_qr(tmp_path):
    """Deux PC, même mire, même latence, sans coupe : seul le QR permet de les distinguer."""
    vid = tmp_path / "q.mkv"
    make_pattern_capture(str(vid), Fraction(25), [(5, 0.080, 6), (9, 0.080, 6)])
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    cams = an.cameras()
    assert [c.name for c in cams] == ["05 · COBRA", "09 · PUMA"], [c.name for c in cams]
    for c in cams:
        assert abs(c.offset - 0.080) < 0.003


def test_lan_server_assigns_ids_and_serves_loops(tmp_path):
    """Partage réseau : chaque machine reçoit un identifiant, la boucle servie porte son QR."""
    import json
    import urllib.request
    from synclive import server
    srv = server.LoopServer(port=8791, periods=4)
    srv.cache = tmp_path
    srv.start()
    try:
        base = "http://127.0.0.1:8791"
        get = lambda u: json.load(urllib.request.urlopen(base + u))
        a, b = get("/api/hello?client=A"), get("/api/hello?client=B")
        assert (a["id"], b["id"]) == (1, 2) and get("/api/hello?client=A")["id"] == 1
        assert get("/api/hello?client=C&want=42")["label"] == "42 · GRIZZLY"
        # renumérotation depuis la régie : A (01) passe en 02 -> échange avec B
        srv.registry.renumber(1, 2)
        assert get("/api/ping?id=1")["reassign"] == 2 and get("/api/ping?id=2")["reassign"] == 1
        assert get("/api/hello?client=A&want=2")["id"] == 2 and get("/api/hello?client=B&want=1")["id"] == 1
        assert get("/api/ping?id=2")["reassign"] is None
        req = urllib.request.Request(base + "/loop/2/640x360/25.mp4", headers={"Range": "bytes=0-9"})
        r = urllib.request.urlopen(req)
        assert r.status == 206 and len(r.read()) == 10
        f = tmp_path / "dl.mp4"
        f.write_bytes(urllib.request.urlopen(base + "/loop/2/640x360/25.mp4?dl=1").read())
    finally:
        srv.stop()
    an = analysis.run(str(f), analysis.Settings(use_hands=False))
    cams = an.cameras()
    assert [c.name for c in cams] == ["02 · ORION"] and abs(cams[0].offset) < 0.003


@pytest.mark.parametrize("fps", ["25", "30000/1001", "60"])
def test_offsets_larger_than_one_period(tmp_path, fps):
    """Décalage > 1 période : le n° du bip (note) et du flash (QR) évite de se caler sur le mauvais flash."""
    vid = tmp_path / "big.mkv"
    make_pattern_capture(str(vid), Fraction(fps), [(3, 1.300, 9), (5, -0.700, 9), (8, 2.200, 10)])
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    got = [(c.name, None if c.offset is None else round(c.offset * 1000)) for c in an.cameras()]
    assert [n for n, _ in got] == ["03 · TIGER", "05 · COBRA", "08 · VEGA"], got
    for (_, off), exp in zip(got, [1300, -700, 2200]):
        assert off is not None and abs(off - exp) < 3, got


def test_correction_plan_global_audio_then_per_source_frame_delay():
    """Exemple : son 100 ms en avance sur A, 50 ms sur B -> son +100 ms, B : image +50 ms (≈ 1-2 img)."""
    from types import SimpleNamespace
    from synclive.export import correction_plan
    A = SimpleNamespace(id=0, name="A", offset=-0.100)
    B = SimpleNamespace(id=1, name="B", offset=-0.050)
    C = SimpleNamespace(id=2, name="C", offset=+0.020)     # son déjà en retard
    N = SimpleNamespace(id=3, name="N", offset=None)       # non mesurée
    plan = correction_plan([A, B, C, N], fps=25.0)
    assert round(plan.audio_delay * 1000) == 100
    fa, fb, fc, fn = plan.fixes
    assert fa.frames == 0 and abs(fa.video_delay) < 1e-9
    assert round(fb.video_delay * 1000) == 50 and fb.frames == 1 and round(fb.residual * 1000) == 10
    assert round(fc.video_delay * 1000) == 120 and fc.frames == 3 and round(fc.residual * 1000) == 0
    assert fn.video_delay is None
    # son en retard partout : on ne touche pas au son, chaque image est retardée de son décalage
    plan2 = correction_plan([SimpleNamespace(id=0, name="X", offset=0.080)], fps=25.0)
    assert plan2.audio_delay == 0 and plan2.fixes[0].frames == 2


def test_no_settings_needed_audio_on_second_track(tmp_path):
    """Aucun réglage : la 1re piste audio est muette, SyncLive trouve tout seul la bonne piste."""
    vid = tmp_path / "pistes.mkv"
    make_pattern_capture(str(vid), Fraction(25), [(6, 0.120, 8)], silent_first_track=True)
    an = analysis.run(str(vid))                       # réglages par défaut, comme dans l'interface
    assert an.settings.audio_track == 1
    cams = an.cameras()
    assert [c.name for c in cams] == ["06 · LYNX"] and abs(cams[0].offset - 0.120) < 0.003


def test_homemade_multiview_claps_then_mire(tmp_path):
    """Multiview maison 2x2 bord à bord : 3 caméras + 1 mire, d'abord les claps puis le son de la mire."""
    from synth import make_multiview
    vid = tmp_path / "mv.mkv"
    make_multiview(str(vid), Fraction(25), cam_latency=[0.100, 0.180, 0.040], mire_delay=0.120)
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    tiles = sorted({s.tile for s in an.shots})
    assert len(tiles) == 4, tiles
    by_tile = {c.tile: c for c in an.cameras()}
    expected = {"vignette haut gauche": -100, "vignette haut droite": -180,
                "vignette bas gauche": -40, "vignette bas droite": 120}
    got = {t: (None if c.offset is None else round(c.offset * 1000), c.kind) for t, c in by_tile.items()}
    for t, exp in expected.items():
        off, kind = got[t]
        assert off is not None and abs(off - exp) <= 0.5 * 40, got   # ½ image à 25 i/s
        assert kind == ("mire" if t.endswith("bas droite") else "camera"), got


@pytest.mark.parametrize("beep_ms", [30, 120, 300])
def test_other_brand_beeps_short_and_long(beep_ms):
    """Mires d'autres marques : bips courts ou longs (note tenue), début mesuré à la ms près."""
    import numpy as np
    from synclive import audio_claps
    sr = 48000
    sig = np.random.default_rng(0).normal(0, 0.002, sr * 6).astype(np.float32)
    onsets = [1.0, 2.001, 3.002, 4.003]
    n = int(beep_ms / 1000 * sr)
    t = np.arange(n) / sr
    tone = 0.5 * np.sin(2 * np.pi * 1000 * t) * np.minimum(1, t / 0.001) * np.minimum(1, (t[::-1]) / 0.002)
    for o in onsets:
        i = int(o * sr)
        sig[i:i + n] += tone.astype(np.float32)
    got = audio_claps.detect_claps(sig, sr, 0.0)
    assert len(got) == 4, [round(c.t, 3) for c in got]
    for c, o in zip(got, onsets):
        assert abs(c.t - o) < 0.002 and c.freq is not None and abs(c.freq - 1000) < 30
