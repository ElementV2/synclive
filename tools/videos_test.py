# SPDX-License-Identifier: GPL-3.0-or-later
"""Banque de vidéos de test réelles et mosaïques de synthèse.

    python tools/videos_test.py scan       ajoute les nouveaux clips de videos_test/sources
                                           à videos_test/sources.json (mesure de référence)
    python tools/videos_test.py compose    fabrique les mosaïques dans videos_test/compos
    python tools/videos_test.py check      analyse les mosaïques et compare à l'attendu
    python tools/videos_test.py all        les trois à la suite

Une mosaïque = du multicam simulé : un clip par vignette, toutes les vignettes clappent en même
temps et la bande son ne contient qu'un seul clap par événement (celui d'un clip de référence).
Chaque vignette reçoit un retard d'image connu (−300 à +300 ms). Décalage attendu d'une vignette
= mesure de référence de ses claps dans le clip seul − retard ajouté.

Ajouter un clip : le déposer dans videos_test/sources puis « scan ». Une seule caméra plein
écran avec des claps mesurés -> utilisable en vignette. Mettre "utiliser": false dans
sources.json pour l'écarter.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BANK = ROOT / "videos_test"
SOURCES = BANK / "sources"
COMPOS = BANK / "compos"
MANIFEST = BANK / "sources.json"
MANIFEST_VERSION = 2   # à incrémenter quand « scan » enregistre un nouveau champ : tout est remesuré
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".mxf", ".ts", ".m4v"}

W, H = 1920, 1080
FPS = "30000/1001"


# ---------------------------------------------------------------------------
# dispositions (fractions de l'image : x, y, largeur, hauteur)
# ---------------------------------------------------------------------------
def _grid(cols: int, rows: int) -> list[tuple[float, float, float, float]]:
    return [(c / cols, r / rows, 1 / cols, 1 / rows) for r in range(rows) for c in range(cols)]


SKETCH = [(0.125, 0.135, 0.256, 0.258), (0.438, 0.092, 0.346, 0.347),   # croquis : 4 vignettes
          (0.063, 0.467, 0.434, 0.423), (0.570, 0.478, 0.360, 0.340)]   # libres, tailles variées


def _random_free(n: int, seed: int) -> list[tuple[float, float, float, float]]:
    """n rectangles 16:9 de tailles variées, sans chevauchement, marge entre eux."""
    rng = random.Random(seed)
    out: list[tuple[float, float, float, float]] = []
    tries = 0
    while len(out) < n and tries < 5000:
        tries += 1
        w = rng.uniform(0.2, 0.42)
        h = w * 16 / 9 * H / W * 9 / 16 * W / H   # garde le 16:9 à l'écran
        x, y = rng.uniform(0.01, 0.99 - w), rng.uniform(0.01, 0.99 - h)
        if all(x + w + 0.02 < a or a + c + 0.02 < x or y + h + 0.02 < b or b + d + 0.02 < y
               for a, b, c, d in out):
            out.append((x, y, w, h))
    return out


LAYOUTS = {
    "grille_2x2": dict(bg="black", rects=_grid(2, 2)),
    "grille_3x2": dict(bg="black", rects=_grid(3, 2)),
    "grand_plus_2": dict(bg="black", rects=[(0, 0, 2 / 3, 1), (2 / 3, 0, 1 / 3, 0.5), (2 / 3, 0.5, 1 / 3, 0.5)]),
    "bande_3_fond_noir": dict(bg="black", rects=[(i / 3, 1 / 3, 1 / 3, 1 / 3) for i in range(3)]),
    "croquis_fond_noir": dict(bg="black", rects=SKETCH),
    "croquis_fond_blanc": dict(bg="white", rects=SKETCH),
    "libre_fond_gris_5": dict(bg="0x404040", rects=_random_free(5, seed=7)),
    "libre_fond_bleu_4": dict(bg="0x1a3a8a", rects=_random_free(4, seed=3)),
    # incrustation (une vignette par-dessus une image plein écran) : limite connue
    "incrustation_pip": dict(bg="black", rects=[(0, 0, 1, 1), (0.70, 0.68, 0.27, 0.27)]),
}


# ---------------------------------------------------------------------------
def _load() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {"clips": {}}


def _save(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _analyse(path: Path):
    from synclive import analysis
    an = analysis.run(str(path))
    return an, an.cameras()


def scan() -> None:
    """Mesure de référence de chaque nouveau clip (une fois pour toutes, modifiable à la main)."""
    m = _load()
    for f in sorted(SOURCES.iterdir()):
        old = m["clips"].get(f.name)
        if f.suffix.lower() not in VIDEO_EXT or (old and old.get("version") == MANIFEST_VERSION):
            continue
        print(f"analyse {f.name}…", flush=True)
        an, cams = _analyse(f)
        entry = {
            "description": (old or {}).get("description", ""),
            "fps": an.info.fps_label,
            "fps_exact": an.info.fps_float,
            "version": MANIFEST_VERSION,
            "duree_s": round(an.info.duration, 3),
            "cameras": [
                {
                    "nom": c.name, "type": c.kind,
                    "decalage_ms": None if c.offset is None else round(c.offset * 1000, 1),
                    "incertitude_ms": None if c.uncertainty is None else round(c.uncertainty * 1000, 1),
                    "cadence_reelle_ips": None if c.source_fps is None else round(c.source_fps, 1),
                    "ambigu": c.ambiguous,
                    "claps_s": [round(k.t_audio, 4) for k in c.claps if k.selected is not None],
                    "claps": [{"t": round(k.t_audio, 4), "decalage_ms": round(k.offset * 1000, 1)}
                              for k in c.claps if k.selected is not None],
                    # tous les événements audio de la source, retenus ou non (isolement des claps)
                    "claps_audio_s": [round(k.t_audio, 4) for k in c.claps],
                }
                for c in cams
            ],
        }
        # utilisable comme vignette : une seule source mesurée, plein écran
        measured = [c for c in entry["cameras"] if c["decalage_ms"] is not None]
        entry["vignette_ok"] = len(cams) == 1 and len(measured) == 1 and len(an.shots) == 1
        if old and "utiliser" in old:
            entry["utiliser"] = old["utiliser"]
        m["clips"][f.name] = entry
        _save(m)
        cs = ", ".join(f"{c['nom']} {c['decalage_ms']} ms" for c in entry["cameras"])
        print(f"  {cs}   vignette : {'oui' if entry['vignette_ok'] else 'non'}")
    _save(m)


# ---------------------------------------------------------------------------
EVENTS = 5                 # claps simultanés par mosaïque
SEG_FRAMES = 57            # durée d'un extrait en images à 29,97 (≈ 1,9 s : claps espacés de 1,9 s)
PRE = 0.95                 # s avant le clap : les raccords entre extraits tombent à ± 0,95 s du son,
                           # hors de la zone examinée (± 0,6 s + 0,3 s de retombée du geste)
ISOLATED = 1.2             # un clap n'est utilisé que sans autre clap à moins de 1,2 s : dans un
                           # vrai multicam, tout clap visible s'entend ; ici le son n'a qu'un clap
FPS_F = 30000 / 1001
SEG = SEG_FRAMES / FPS_F
RATE = 48000
# retards d'image ajoutés (+ : image en retard ; − : image en avance, donc son en retard)
DELAY_CHOICES = [-0.300, -0.200, -0.133, -0.067, 0.0, 0.033, 0.067, 0.100, 0.133, 0.200, 0.300]
MAX_ABS = 0.45             # |décalage attendu| max : reste mesurable (recherche ± 0,6 s)


def _isolated(cam: dict, duration: float) -> list[dict]:
    """Claps utilisables : sans autre clap audio à moins de ISOLATED, et avec assez d'image avant
    et après pour un extrait complet même décalé du retard maximal (sinon l'extrait est trop court
    et tous les événements suivants glissent)."""
    ts = cam.get("claps_audio_s") or [c["t"] for c in cam.get("claps", [])]
    return [c for c in cam.get("claps", [])
            if all(abs(c["t"] - t) >= ISOLATED for t in ts if t != c["t"]) and _delay_range(c, duration)]


def _delay_range(clap: dict, duration: float) -> tuple[float, float] | None:
    """Retards d'image possibles pour un clap : l'extrait [t − PRE − d, t − PRE − d + SEG] doit
    tenir dans le clip (sinon il est trop court et les événements suivants glissent)."""
    lo = clap["t"] - PRE + SEG - duration + 0.02
    hi = clap["t"] - PRE - 0.02
    return (lo, hi) if lo <= hi else None


def _tiles_pool(m: dict) -> list[tuple[str, dict]]:
    """Clips d'une seule caméra (pas les mires : leur son est un bip, ici on ne garde qu'un clap)."""
    return [(name, e) for name, e in m["clips"].items()
            if e.get("vignette_ok") and e.get("utiliser", True) and e["cameras"][0]["type"] == "camera"
            and _isolated(e["cameras"][0], e["duree_s"])]


def compose(only: list[str] | None = None) -> None:
    """Multicam simultané : à chaque événement, toutes les vignettes clappent au même instant et la
    bande son ne contient qu'UN clap (celui d'un clip de référence). Chaque vignette reçoit un retard
    d'image connu. Pour chaque extrait, le décalage attendu est exact : décalage de référence de ce
    clap − retard ajouté − recalage sur la grille d'images de la source."""
    import math
    m = _load()
    pool = _tiles_pool(m)
    if not pool:
        raise SystemExit("aucun clip utilisable comme vignette : lancer d'abord « scan »")
    COMPOS.mkdir(parents=True, exist_ok=True)
    for li, (lname, lay) in enumerate(LAYOUTS.items()):
        if only and lname not in only:
            continue
        rng = random.Random(1000 + li)
        rects = lay["rects"]
        picks = [pool[(li * 3 + i) % len(pool)] for i in range(len(rects))]   # un clip peut revenir
        ref_name, ref = picks[0]                       # le son : les claps de ce clip
        total = EVENTS * SEG
        tiles = []
        cmd = ["ffmpeg", "-v", "error", "-y"]
        n_in = 0
        fc = [f"color=c={lay['bg']}:s={W}x{H}:r={FPS}:d={total:.4f}[bg0]"]
        for i, ((name, e), r) in enumerate(zip(picks, rects)):
            cam = e["cameras"][0]
            claps = _isolated(cam, e["duree_s"])
            base = float(cam["decalage_ms"]) / 1000
            ranges = [_delay_range(c, e["duree_s"]) for c in claps]
            ok = [d for d in DELAY_CHOICES if abs(base - d) <= MAX_ABS
                  and all(lo <= d <= hi for lo, hi in ranges)]
            if not ok:
                raise SystemExit(f"{name} : aucun retard possible (clap trop près du bord du clip)")
            # la 1re vignette garde son décalage d'origine quand c'est possible
            delay = 0.0 if i == 0 and 0.0 in ok else rng.choice(ok)
            sfps = float(e.get("fps_exact") or FPS_F)
            offs = []
            for k in range(EVENTS):
                c = claps[k % len(claps)]
                start = c["t"] - PRE - delay
                # l'extrait commence pile sur une image de la source ; l'écart est compté dans l'attendu
                snapped = max(0.0, math.floor(start * sfps + 1e-6) / sfps)
                offs.append(c["decalage_ms"] / 1000 - delay - (start - snapped))
                # ffmpeg garde la 1re image dont le temps est >= au point demandé : on vise un
                # quart d'image AVANT, sinon l'image `snapped` saute et tout glisse d'une image
                cmd += ["-ss", f"{max(0.0, snapped - 0.25 / sfps):.5f}", "-t", f"{SEG + 0.2:.4f}",
                        "-i", str(SOURCES / name)]
            x, y, w, h = r
            pw, ph = int(round(w * W)) // 2 * 2, int(round(h * H)) // 2 * 2
            parts = []
            for k in range(EVENTS):
                fc.append(f"[{n_in + k}:v:0]setpts=PTS-STARTPTS,fps={FPS},tpad=stop_mode=clone:stop_duration=1,"
                          f"trim=end_frame={SEG_FRAMES},"
                          f"setpts=PTS-STARTPTS,scale={pw}:{ph},setsar=1[s{i}_{k}]")
                parts.append(f"[s{i}_{k}]")
            fc.append("".join(parts) + f"concat=n={EVENTS}:v=1:a=0[v{i}]")
            fc.append(f"[bg{i}][v{i}]overlay=x={int(round(x * W))}:y={int(round(y * H))}"
                      f":eof_action=pass[bg{i + 1}]")
            n_in += EVENTS
            exp = sorted(offs)[len(offs) // 2]
            tiles.append(dict(source=name, rect=r, delay=delay, base_ms=cam["decalage_ms"],
                              base_unc_ms=cam["incertitude_ms"], type=cam["type"],
                              attendu_par_clap_ms=[round(o * 1000, 1) for o in offs],
                              attendu_ms=round(exp * 1000, 1)))
        mix = COMPOS / f"{lname}.f32"
        _single_clap_audio(ref_name, _isolated(ref["cameras"][0], ref["duree_s"]), total, mix)
        cmd += ["-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", str(mix)]
        cmd += ["-filter_complex", ";".join(fc), "-map", f"[bg{len(tiles)}]", "-map", f"{n_in}:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.4f}", str(COMPOS / f"{lname}.mp4")]
        print(f"compose {lname} ({len(tiles)} vignettes, {EVENTS} claps simultanés, son de {ref_name})…",
              flush=True)
        subprocess.run(cmd, check=True)
        mix.unlink(missing_ok=True)
        truth = dict(disposition=lname, fond=lay["bg"], duree_s=round(total, 3), son=ref_name,
                     instants_claps_s=[round(k * SEG + PRE, 4) for k in range(EVENTS)], vignettes=tiles)
        (COMPOS / f"{lname}.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2),
                                              encoding="utf-8")


def _single_clap_audio(name: str, claps: list[dict], total: float, out: Path) -> None:
    """Un seul son pour toutes les caméras : l'extrait audio d'un clap du clip de référence, posé
    à chaque événement (clap à PRE secondes du début de l'événement)."""
    import numpy as np
    buf = np.zeros((int(round(total * RATE)) + RATE, 2), np.float32)
    for k in range(EVENTS):
        c = claps[k % len(claps)]
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(SOURCES / name), "-map", "0:a:0",
                              "-af", f"atrim=start={c['t'] - PRE:.5f}:duration={SEG:.5f}",
                              "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
        a = np.frombuffer(raw, np.float32).reshape(-1, 2).copy()
        # un seul clap : silence autour (les claps voisins du clip de référence sont coupés)
        keep0, keep1 = int((PRE - 0.15) * RATE), int((PRE + 0.35) * RATE)
        a[:keep0] = 0.0
        a[keep1:] = 0.0
        p0 = int(round(k * SEG * RATE))
        n = min(a.shape[0], buf.shape[0] - p0)
        buf[p0:p0 + n] = a[:n]
    out.write_bytes(buf[:int(round(total * RATE))].tobytes())


# ---------------------------------------------------------------------------
def check(only: list[str] | None = None) -> int:
    bad = 0
    for tj in sorted(COMPOS.glob("*.json")):
        truth = json.loads(tj.read_text(encoding="utf-8"))
        if only and truth["disposition"] not in only:
            continue
        an, cams = _analyse(tj.with_suffix(".mp4"))
        print(f"\n{truth['disposition']} : {len(truth['vignettes'])} vignettes attendues, "
              f"{len(cams)} source(s) trouvée(s)")
        used = set()
        for tl in truth["vignettes"]:
            x, y, w, h = tl["rect"]
            found = None
            for c in cams:
                for si in c.shots:
                    rx0, ry0, rx1, ry1 = an.shots[si].region
                    cx, cy = (rx0 + rx1) / 2, (ry0 + ry1) / 2
                    # la vignette trouvée doit avoir son centre dans la vignette posée et en couvrir
                    # l'essentiel (pas une image plein écran qui la contient)
                    if x <= cx <= x + w and y <= cy <= y + h and (rx1 - rx0) <= 1.3 * w + 0.02:
                        found = c
                if found:
                    break
            tol = max(40.0, tl["base_unc_ms"] or 0.0) + 15.0
            if found is None:
                verdict, got = "VIGNETTE NON TROUVÉE", "—"
            elif found.offset is None:
                verdict, got = "NON MESURÉE" + (" (ambigu)" if found.ambiguous else ""), "—"
            else:
                got = f"{found.offset * 1000:+.0f} ms"
                verdict = "ok" if abs(found.offset * 1000 - tl["attendu_ms"]) <= tol else "ÉCART"
            if found is not None:
                used.add(found.id)
            ok = verdict == "ok"
            bad += not ok
            print(f"  {'✔' if ok else '✘'} {tl['source'][:44]:44s} retard +{tl['delay'] * 1000:3.0f} ms  "
                  f"attendu {tl['attendu_ms']:+6.0f} ms  mesuré {got:>8s}  {verdict}")
        extra = [c.name for c in cams if c.id not in used]
        if extra:
            print(f"  ? sources en trop : {', '.join(extra)}")
    print(f"\n{bad} vignette(s) en échec")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "all"
    only = argv[1:] or None
    if cmd in ("scan", "all"):
        scan()
    if cmd in ("compose", "all"):
        compose(only)
    if cmd in ("check", "all"):
        return check(only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
