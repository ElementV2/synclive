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


def test_clap_right_after_a_slower_gesture_is_not_swallowed():
    """Courbe de mouvement réelle (vignette CAM 1) : un geste plus lent juste avant le clap ne doit pas
    « avaler » le clap (régression 1.1.0 : seul un faux candidat à -525 ms était trouvé)."""
    from types import SimpleNamespace
    import numpy as np
    from synclive import visual
    e = np.array([0.9, 0.9, 1.1, 1.3, 1.1, 1.0, 1.1, 1.1, 1.3, 1.5, 2.3, 2.5, 3.0, 2.8, 2.8, 2.4, 3.2, 3.4, 3.6,
                  3.5, 2.7, 2.1, 2.7, 4.3, 6.1, 7.3, 7.4, 6.0, 2.8, 1.7, 4.4, 5.5, 4.9, 4.5, 3.2, 2.3, 1.1, 0.3, 0.2])
    t = 1.4 - 0.63 + np.arange(e.size) * (1001 / 30000)
    sig = SimpleNamespace(t=t, energy=lambda *a, **k: (e, 0, e.size - 1))
    offs = [round((1.4 - v.t) * 1000) for v in visual.clap_candidates(sig, 1.4, 0.6)]
    assert any(-300 <= o <= -240 for o in offs), offs


def test_slow_camera_repeated_frames_are_not_claps():
    """Caméra à 20 i/s dans un fichier à 60 : le mouvement retombe à 0 à chaque image répétée.
    Chaque image réelle ne doit pas devenir un « arrêt net » de clap (vidéo OBS du 12/10 : un
    candidat toutes les 50 ms, n'importe quel bruit trouvait son appariement)."""
    from types import SimpleNamespace
    import numpy as np
    from synclive import visual
    per = 1001 / 60000
    real_e = [1.0, 1.2, 1.0, 1.1, 3.0, 8.0, 14.0, 16.0, 2.0, 1.2, 1.0, 1.1, 1.0, 1.2, 1.0, 1.1]
    n = 3 * len(real_e)
    e = np.zeros(n)
    real = np.zeros(n, bool)
    real[::3] = True
    e[::3] = real_e
    t = 1.0 + np.arange(n) * per
    sig = SimpleNamespace(t=t, real=real, energy=lambda *a, **k: (e, 0, n - 1))
    got = visual.clap_candidates(sig, 1.4, 0.6)
    assert len(got) <= 2, [round(v.t, 3) for v in got]
    contact = t[3 * 7]          # dernière image réelle en mouvement, avant l'arrêt
    assert any(abs(v.t - contact) <= 3 * per for v in got), [round(v.t, 3) for v in got]


def test_isolated_tonal_noise_is_not_a_beep_train():
    """Bruit de plateau tonal (voix, choc) près d'une mire sans son : pas de bips. Un vrai train :
    deux bips SyncLive, ou deux sons de même hauteur (mire d'une autre marque)."""
    from types import SimpleNamespace as N
    from synclive.analysis import _beep_train
    ev = lambda t, f: (N(t=t, freq=f), [])
    assert _beep_train([ev(9.3, 882.0), ev(18.6, 1228.0)]) == []          # 1228 Hz ≈ note SyncLive n°0
    assert len(_beep_train([ev(1.0, 1000.0), ev(2.0, 1004.0), ev(2.5, 640.0)])) == 2
    assert len(_beep_train([ev(1.0, 1200.0), ev(2.0, 1500.0)])) == 2       # notes SyncLive n°0, n°1


def test_spurious_audio_event_does_not_make_offset_ambiguous():
    """Faux clap audio en début de fichier (0,18 s / 0,45 s) à côté d'un vrai clap net : le décalage
    du vrai clap reste mesuré ; des claps réguliers trop rapprochés pour ce décalage, eux, l'invalident."""
    from types import SimpleNamespace as N
    from synclive.analysis import _ambiguity
    cand = lambda t, s: N(t=t, strength=s)
    clap = lambda ta, cands, sel=None: N(t_audio=ta, candidates=cands, excluded=False,
                                         selected=sel if sel is not None else (cands[0] if cands else None))
    spur = [clap(0.182, [cand(0.323, 0.12)], sel=False), clap(0.451, [cand(0.796, 0.16)], sel=False)]
    for c in spur:
        c.selected = None
    real = clap(5.438, [cand(5.554, 1.0), cand(5.158, 0.31)])
    assert _ambiguity(spur + [real], -0.116, 1 / 29.97) is None
    regular = [clap(t, [cand(t - 0.557, 1.0)]) for t in (6.63, 7.34, 8.06)]
    assert _ambiguity(regular, 0.557, 1 / 29.97) is not None


@pytest.mark.parametrize("fps", ["30000/1001", "60"])
def test_image_very_late_still_measured(tmp_path, fps):
    """Image très en retard (300-350 ms) : l'arrêt des mains tombe en bout de fenêtre de recherche ;
    la retombée du geste doit être regardée au-delà, sinon le clap est rejeté (bug corrigé)."""
    offs = {"A": -0.350, "B": -0.300}
    vid = tmp_path / "late.mkv"
    make(str(vid), Fraction(fps), offs, ["A", "B"])
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    cams = an.cameras()
    assert len(cams) == 2
    for cam, exp in zip(cams, [offs["A"], offs["B"]]):
        assert cam.offset is not None, cam.name
        assert abs(cam.offset - exp) < max(0.5 / float(Fraction(fps)), 0.015), (cam.name, cam.offset)


def test_manual_clap_frame_overrides_auto(tmp_path):
    """Bande d'images : l'image choisie à la main remplace la mesure automatique de ce clap, sans
    être écartée par les garde-fous, et « revenir à l'automatique » rend la mesure d'origine."""
    from synclive import media
    vid = tmp_path / "v.mkv"
    make(str(vid), Fraction(30000, 1001), OFFS, ORDER)
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    cam = an.cameras()[0]
    auto = cam.offset
    c = next(c for c in cam.claps if c.selected is not None)
    s = an.shots[c.shot]
    times, frames = media.decode_range(str(vid), c.selected.t - 0.3, c.selected.t + 0.3, s.region)
    assert len(times) == len(frames) >= 15 and abs(times[1] - times[0] - 1001 / 30000) < 1e-3
    # image 3 images plus tôt que l'automatique sur TOUS les claps de la source
    per = 1001 / 30000
    for cl in cam.claps:
        if cl.selected is not None:
            cl.manual_t = cl.selected.t - 3 * per
    cam2 = next(x for x in an.cameras() if x.id == cam.id)
    assert cam2.manual and abs(cam2.offset - (auto + 3 * per)) < 0.002
    assert cam2.uncertainty <= 0.5 * per + 1e-9          # ± ½ image : vérifié à l'œil
    assert "à la main" in export.status(cam2, 30000 / 1001)[1]
    # l'aperçu montre exactement l'image choisie à la main, plus celle de l'automatique
    import numpy as np
    first = next(cl for cl in cam.claps if cl.manual_t is not None)
    analysis.refresh_images(an, [first.shot])
    want = media.grab_frames(str(vid), [first.manual_t])[first.manual_t]
    got = next(x for x in an.cameras() if x.id == cam.id).image
    assert got is not None and got.shape == want.shape and np.array_equal(got, want)
    for cl in cam.claps:
        cl.manual_t = None
    cam3 = next(x for x in an.cameras() if x.id == cam.id)
    assert not cam3.manual and abs(cam3.offset - auto) < 1e-9


def test_one_manual_clap_is_the_measure_of_its_source(tmp_path):
    """Image du clap réglée à la main sur UN clap : c'est la mesure de la source. Les claps
    automatiques ne la brouillent plus (vidéo OBS du 12/10 : −34 ms réglé à la main, noyé dans
    une marge de ± 35 ms par des claps automatiques mal appariés, donc aucune correction)."""
    vid = tmp_path / "v.mkv"
    make(str(vid), Fraction(30000, 1001), OFFS, ORDER)
    an = analysis.run(str(vid), analysis.Settings(use_hands=False))
    cam = next(c for c in an.cameras() if c.n_used >= 2)
    c = next(cl for cl in cam.claps if cl.selected is not None)
    per = 1001 / 30000
    c.manual_t = c.selected.t - 2 * per
    want = c.t_audio - c.manual_t
    cam2 = next(x for x in an.cameras() if x.id == cam.id)
    assert cam2.n_used == 1 and abs(cam2.offset - want) < 1e-9
    real = 1.0 / cam2.source_fps if cam2.source_fps else per      # ± ½ image RÉELLE de la caméra
    assert cam2.uncertainty <= 0.5 * real + 1e-9
