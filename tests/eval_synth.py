"""Évalue la précision sur vidéos synthétiques : python tests/eval_synth.py <dossier>"""
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from synth import make  # noqa: E402
from synclive import analysis  # noqa: E402

OFFS = {"A": 0.080, "B": -0.040, "C": 0.133}
ORDER = ["A", "B", "C", "A", "B"]


def evaluate(path, fps, seed=1, codec="pcm_s16le"):
    truth = make(path, fps, OFFS, ORDER, seed=seed, audio_codec=codec)
    an = analysis.run(path, analysis.Settings())
    cams = an.cameras()
    ea, ev = [], []
    for c in an.claps:
        tr = min(truth, key=lambda r: abs(r["t_audio"] - c.t_audio))
        ea.append(c.t_audio - tr["t_audio"])
        if c.selected:
            ev.append(c.selected.t - tr["t_contact"])
    res = []
    for cam in cams:
        # caméra réelle = majorité des claps
        letters = [min(truth, key=lambda r: abs(r["t_audio"] - c.t_audio))["camera"] for c in cam.claps]
        L = max(set(letters), key=letters.count) if letters else "?"
        err = None if cam.offset is None else (cam.offset - OFFS[L]) * 1000
        res.append((cam.name, L, cam.offset, err, cam.n_used, cam.n_total, cam.confidence))
    return an, np.array(ea) * 1000, np.array(ev) * 1000, res


if __name__ == "__main__":
    d = Path(sys.argv[1])
    d.mkdir(parents=True, exist_ok=True)
    for fps in ["25", "30000/1001", "60", "24000/1001", "50"]:
        f = Fraction(fps)
        an, ea, ev, res = evaluate(str(d / f"t_{float(f):.2f}.mkv"), f)
        print(f"== {float(f):.3f} i/s : {len(an.shots)} plans, {len(an.claps)} claps"
              f" | audio err {ea.mean():+.2f}±{ea.std():.2f} ms"
              f" | image err {ev.mean():+.2f}±{ev.std():.2f} ms (1 img = {1000/float(f):.1f} ms)")
        for r in res:
            print(f"   {r[0]} (réel {r[1]}) mesuré {r[2]*1000:+.1f} ms  erreur {r[3]:+.1f} ms  {r[4]}/{r[5]} {r[6]}")
