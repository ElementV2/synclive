# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipeline complet et agrégation par source (caméra ou boucle de mire).

Convention : décalage = t_son − t_image.
  > 0 : le son arrive APRÈS l'image (son en retard)
  < 0 : le son arrive AVANT l'image (image en retard)

Une même image ne veut pas dire une même source : deux PC qui jouent la même mire
ont la même apparence mais pas forcément la même latence. Les plans sont donc
regroupés par apparence PUIS séparés par décalage mesuré (et couleur de flash).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import audio_claps, media, shots as shots_mod, visual
from .hands import HandLocator


@dataclass
class Settings:
    max_offset: float = 0.6          # s, décalage max cherché de part et d'autre
    audio_track: int = 0
    audio_sensitivity: float = 1.0
    cut_threshold: float = 0.05
    group_threshold: float = 0.07
    use_hands: bool = True
    split_by_offset: bool = True     # séparer les plans d'apparence identique mais de décalage différent


@dataclass
class Clap:
    """Un événement de synchro : clap à la main ou bip/flash de mire."""
    id: int
    shot: int
    t_audio: float
    snr_db: float
    candidates: list[visual.VisualCandidate]
    method: str                      # "clap (mains)" | "clap (mouvement)" | "mire (flash)"
    excluded: bool = False
    selected: visual.VisualCandidate | None = None
    forced: int | None = None        # index de candidat choisi à la main
    tone: int | None = None          # n° de bip d'une mire SyncLive (note), None sinon
    manual_t: float | None = None    # instant de l'image choisie à la main (bande d'images)

    @property
    def offset(self) -> float | None:
        return None if self.selected is None else self.t_audio - self.selected.t


@dataclass
class CameraResult:
    id: int
    name: str
    shots: list[int]
    claps: list[Clap]
    offset: float | None             # s
    spread: float | None             # s (écart-type robuste)
    n_used: int
    n_total: int
    confidence: str                  # "élevée" | "moyenne" | "faible" | "aucune"
    kind: str = "camera"             # "camera" | "mire"
    source_label: str | None = None  # identifiant de mire SyncLive lu dans le QR (« 07 · ATLAS »)
    tile: str | None = None          # vignette du multiview (« vignette haut gauche »)
    same_look_as: list[str] = field(default_factory=list)
    image: np.ndarray | None = None
    image_shot: int | None = None     # plan dont l'image est montrée
    uncertainty: float | None = None  # s, marge probable (≈ 95 %) sur le décalage
    source_fps: float | None = None   # cadence réelle si la caméra est plus lente que le fichier
    ambiguous: str | None = None      # décalage ambigu (claps trop rapprochés) : explication
    manual: bool = False              # au moins un clap réglé à la main (image choisie)


@dataclass
class Analysis:
    info: media.VideoInfo
    settings: Settings
    shots: list[shots_mod.Shot]
    claps: list[Clap]
    camera_names: dict[int, str]
    look: dict[int, int] = field(default_factory=dict)      # plan -> groupe d'apparence
    shot_images: dict[int, np.ndarray] = field(default_factory=dict)
    hands_status: str = ""
    source_period: dict[int, float] = field(default_factory=dict)   # plan -> durée d'une image réelle
    removed: set[int] = field(default_factory=set)   # sources supprimées à la main (rien à voir dessus)
    image_clap: dict[int, int] = field(default_factory=dict)  # plan -> clap réglé en dernier (aperçu)

    @property
    def period(self) -> float:
        return 1.0 / self.info.fps_float

    def camera_ids(self) -> list[int]:
        seen: list[int] = []
        for s in self.shots:
            if s.camera not in seen and s.camera not in self.removed:
                seen.append(s.camera)
        return seen

    def cameras(self) -> list[CameraResult]:
        res = [aggregate_camera(self, cid) for cid in self.camera_ids()]
        # signaler les sources qui ont la même image qu'une autre
        for r in res:
            looks = {self.look.get(i) for i in r.shots}
            r.same_look_as = [o.name for o in res if o.id != r.id
                              and looks & {self.look.get(i) for i in o.shots}]
        return res

    # --- édition manuelle -------------------------------------------------
    def merge_cameras(self, src: int, dst: int) -> None:
        for s in self.shots:
            if s.camera == src:
                s.camera = dst
        self.camera_names.pop(src, None)

    def remove_camera(self, cid: int) -> None:
        """Source sans intérêt (aucun clap visible) : retirée des résultats et de l'export."""
        self.removed.add(cid)

    def move_shot(self, shot_index: int, camera: int | None) -> None:
        """camera=None : crée une nouvelle caméra pour ce plan."""
        if camera is None:
            camera = max(self.camera_names) + 1 if self.camera_names else 0
            self.camera_names[camera] = f"CAM {len(self.camera_names) + 1}"
        old = self.shots[shot_index].camera
        self.shots[shot_index].camera = camera
        if all(s.camera != old for s in self.shots):
            self.camera_names.pop(old, None)


# ---------------------------------------------------------------------------
# sélection des candidats cohérents
# ---------------------------------------------------------------------------
def _tolerance(claps: list[Clap], period: float) -> float:
    flash = sum(1 for c in claps if c.method.startswith("mire"))
    return max(0.6 * period, 0.010) if flash > len(claps) / 2 else max(1.0 * period, 0.015)


def _mode(claps: list[Clap], period: float) -> tuple[float | None, float]:
    """Décalage le plus soutenu par l'ensemble des claps (score = support pondéré)."""
    sigma = max(0.4 * period, 0.006)
    offs = [np.array([c.t_audio - v.t for v in c.candidates]) for c in claps]
    wts = [np.array([max(v.strength, 0.05) for v in c.candidates]) for c in claps]
    best_o, best_s = None, -1.0
    for arr in offs:
        for o in arr:
            score = sum(float(np.max(np.exp(-((a - o) / sigma) ** 2) * w)) for a, w in zip(offs, wts))
            if score > best_s:
                best_o, best_s = float(o), score
    if best_o is None:
        return None, 0.0
    for _ in range(2):  # affinage par médiane des candidats proches
        near = []
        for arr in offs:
            j = int(np.argmin(np.abs(arr - best_o)))
            if abs(arr[j] - best_o) <= 1.5 * period:
                near.append(arr[j])
        if near:
            best_o = float(np.median(near))
    return best_o, best_s


def _assign(claps: list[Clap], center: float, tol: float) -> list[Clap]:
    got = []
    for c in claps:
        offs = np.array([c.t_audio - v.t for v in c.candidates])
        j = int(np.argmin(np.abs(offs - center)))
        if abs(offs[j] - center) <= tol:
            c.selected = c.candidates[j]
            got.append(c)
    return got


def _select(claps: list[Clap], period: float) -> None:
    for c in claps:
        c.selected = None
    forced = []
    for c in claps:
        if c.excluded:
            continue
        if c.manual_t is not None:      # image choisie à la main : prime sur tout le reste
            c.selected = visual.VisualCandidate(
                t=float(c.manual_t), strength=1.0, kind="flash" if c.method.startswith("mire") else "clap")
            forced.append(c)
        elif c.forced is not None and c.forced < len(c.candidates):
            c.selected = c.candidates[c.forced]
            forced.append(c)
    active = [c for c in claps if not c.excluded and c.candidates and c not in forced]
    if not active:
        return
    if forced:  # les choix manuels servent de référence
        center = float(np.median([c.offset for c in forced]))
    else:
        center, _ = _mode(active, period)
    if center is not None:
        _assign(active, center, _tolerance(active, period))


def _clusters(claps: list[Clap], period: float) -> list[tuple[float, list[Clap]]]:
    """Extraction itérative des groupes de claps cohérents entre eux."""
    remaining = [c for c in claps if not c.excluded and c.candidates]
    tol = _tolerance(remaining, period) if remaining else period
    out = []
    while remaining:
        center, _ = _mode(remaining, period)
        if center is None:
            break
        got = _assign(remaining, center, tol)
        if not got:
            break
        if len(got) < 2 and out:
            for c in got:
                c.selected = None
            break
        out.append((float(np.median([c.offset for c in got])), got))
        remaining = [c for c in remaining if c not in got]
        if len(remaining) < 2:
            break
    return out


def aggregate_camera(an: Analysis, cid: int) -> CameraResult:
    shot_ids = [s.index for s in an.shots if s.camera == cid]
    claps = [c for c in an.claps if c.shot in shot_ids]
    p = an.period
    # cadence réelle : une webcam qui manque de lumière répète ses images (15 i/s dans un fichier
    # à 30) ; l'instant d'un clap n'est alors connu qu'à une image réelle près. Un flash de mire,
    # lui, tombe pile sur une image : pas de plancher.
    periods = [an.source_period[i] for i in shot_ids if i in an.source_period]
    t_src = max(p, float(np.median(periods))) if periods else p
    # appariement à la précision de la source (une image réelle), pas du fichier
    _select(claps, t_src)
    # un candidat très faible à côté de vrais claps nets (faux clap audio : bruit en début de
    # fichier, choc) ne doit pas tirer la médiane ; on ne l'écarte pas s'il est seul
    sel = [c for c in claps if c.selected is not None]
    if len(sel) >= 2:
        top = max(c.selected.strength for c in sel)
        for c in sel:
            if c.forced is None and c.manual_t is None and c.selected.strength < 0.25 * top:
                c.selected = None
    used = [c for c in claps if c.selected is not None]
    # image du clap vérifiée à l'œil sur au moins un clap : c'est la mesure de cette source ; les
    # claps automatiques (souvent mal appariés quand on a dû régler à la main) ne la brouillent plus
    if any(c.manual_t is not None for c in used):
        for c in used:
            if c.manual_t is None:
                c.selected = None
        used = [c for c in used if c.manual_t is not None]
    offs = np.array([c.offset for c in used], dtype=np.float64)
    if offs.size:
        off = float(np.median(offs))
        spread = float(1.4826 * np.median(np.abs(offs - off))) if offs.size > 1 else 0.0
        spread = max(spread, float(offs.std()) if offs.size > 2 else spread)
    else:
        off, spread = None, None
    n = len(used)
    name = an.camera_names.get(cid, f"CAM {cid + 1}")
    kind = "mire" if name.startswith("MIRE") or (
        claps and sum(c.method.startswith("mire") for c in claps) > len(claps) / 2) else "camera"
    unc = sigma = None
    # image choisie à la main sur chaque clap retenu : le contact est connu à ± ½ image réelle
    all_manual = bool(used) and all(c.manual_t is not None for c in used)
    if n:
        floor = (0.25 if all_manual else 0.5) * t_src / np.sqrt(n) if kind == "camera" else 0.001
        sigma = max(spread or 0.0, floor)
        unc = 2.0 * sigma
    if n == 0:
        conf = "aucune"
    elif n >= 3 and sigma <= 0.35 * t_src:
        conf = "élevée"
    elif n >= 2 and sigma <= 0.7 * t_src:
        conf = "moyenne"
    else:
        conf = "faible"
    if all_manual and conf in ("faible", "moyenne") and sigma <= 0.7 * t_src:
        conf = "moyenne" if n < 2 else "élevée"   # vérifié à l'œil, image par image
    img = None
    image_shot = None
    if shot_ids:
        # plan où l'image du clap a été choisie à la main d'abord : c'est elle qu'on montre
        manual_ids = {c.id for c in used if c.manual_t is not None}
        best_shot = max(shot_ids, key=lambda i: (an.image_clap.get(i) in manual_ids,
                                                 any(c.shot == i and c.manual_t is not None for c in used),
                                                 sum(1 for c in used if c.shot == i),
                                                 an.shots[i].duration))
        img = an.shot_images.get(best_shot)
        image_shot = best_shot
    qrs = [an.shots[i].qr for i in shot_ids if an.shots[i].qr]
    label = max(set(qrs), key=qrs.count) if qrs else None
    tiles = {an.shots[i].tile for i in shot_ids}
    tile = tiles.pop() if len(tiles) == 1 else None
    # un choix manuel (candidat forcé dans l'interface) n'est jamais annulé par les garde-fous
    manual = any((c.forced is not None or c.manual_t is not None) and c.selected is not None for c in claps)
    ambiguous = _ambiguity(claps, off, t_src) if kind == "camera" and not manual else None
    if kind == "mire" and n < 2 and not manual:
        # un seul bip sur un flash ne prouve rien : un flash revient chaque seconde
        off, spread, unc, conf, n = None, None, None, "aucune", 0
    if not ambiguous and kind == "camera" and not manual and n == 1 and used[0].method == "clap (mouvement)":
        # un seul clap retenu, aucune main vue, et d'autres mouvements aussi nets dans sa fenêtre
        # (décor animé, projection, geste quelconque) : rien ne dit lequel est le clap
        c = used[0]
        rivals = [v for v in c.candidates if v is not c.selected and abs(v.t - c.selected.t) > 2 * t_src
                  and v.strength >= 0.7 * c.selected.strength]
        if rivals:
            ambiguous = ("un seul clap et plusieurs mouvements possibles à l'image : "
                         "régler l'image du clap à la main, ou supprimer la source si aucun clap n'y est visible")
    if ambiguous:
        off, conf = None, "aucune"
    return CameraResult(
        id=cid, name=name, shots=shot_ids, claps=claps, source_label=label, tile=tile,
        offset=off, spread=spread, n_used=n, n_total=len([c for c in claps if not c.excluded]),
        confidence=conf, kind=kind, image=img, image_shot=image_shot, uncertainty=unc,
        source_fps=1.0 / t_src if t_src > 1.25 * p else None, ambiguous=ambiguous,
        manual=any(c.manual_t is not None and c.selected is not None for c in claps),
    )


def _support(claps: list[Clap], o: float, sigma: float) -> float:
    """Soutien d'un décalage par les candidats visuels : somme, clap par clap, du meilleur
    candidat proche de ce décalage (pondéré par sa force)."""
    tot = 0.0
    for c in claps:
        best = 0.0
        for v in c.candidates:
            d = (c.t_audio - v.t - o) / sigma
            best = max(best, max(v.strength, 0.05) * float(np.exp(-d * d)))
        tot += best
    return tot


def _ambiguity(claps: list[Clap], off: float | None, period: float) -> str | None:
    """Claps réguliers : l'image du clap n°2 peut s'apparier au son du n°3, ce qui donne un
    décalage faux d'un espacement de claps. Le résultat n'est ambigu que si ce décalage voisin
    (décalage ± espacement) est lui aussi soutenu par ce qu'on voit à l'image ; un faux clap audio
    isolé (bruit en début de fichier) ne suffit pas."""
    if off is None:
        return None
    # seuls comptent les claps visibles dans cette source (une mosaïque partage la bande son :
    # les claps des autres caméras y sont aussi, mais celle-ci ne les voit pas)
    vis = sorted((c for c in claps if not c.excluded and c.candidates), key=lambda c: c.t_audio)
    if len(vis) < 2:
        return None
    sigma = max(0.5 * period, 0.015)
    here = _support(vis, off, sigma)
    worst = None
    for g in sorted({round(float(b.t_audio - a.t_audio), 3) for a, b in zip(vis, vis[1:])}):
        for alt in (off - g, off + g):
            if abs(alt) > 0.6:
                continue
            sup = _support(vis, alt, sigma)
            if sup >= 0.7 * here and (worst is None or sup > worst[0]):
                worst = (sup, alt, g)
    if worst is None:
        # claps retenus réguliers et décalage proche de la moitié de leur espacement : la moindre
        # erreur d'appariement donnerait un résultat faux d'un espacement entier
        ts = sorted(c.t_audio for c in vis if c.selected is not None)
        gaps = np.diff(ts)
        if gaps.size == 0 or abs(off) < 0.45 * float(gaps.min()):
            return None
        gap = float(gaps.min())
        alt = off - gap if off > 0 else off + gap
    else:
        _, alt, gap = worst
    # pas de chiffres : l'un des deux appariements est faux, les afficher laisse croire à une mesure
    return (f"ambigu : claps espacés de {gap:.2f} s".replace(".", ",")
            + ", trop rapprochés pour savoir quel son va avec quel geste : régler l'image du clap "
              "à la main (et espacer les claps d'au moins 1 s la prochaine fois)")


def _source_period(scan: media.MotionScan, shot: shots_mod.Shot, period: float) -> float:
    """Durée d'une image réellement capturée dans ce plan (vignette seulement en multiview) :
    les images répétées par une caméra trop lente ont un écart ~0 avec la précédente."""
    th = scan.thumbs[shot.i0:shot.i1]
    if th.shape[0] < 8:
        return period
    hh, ww = th.shape[1:3]
    x0, y0, x1, y1 = shot.region
    th = th[:, int(y0 * hh):max(int(y0 * hh) + 1, int(round(y1 * hh))),
            int(x0 * ww):max(int(x0 * ww) + 1, int(round(x1 * ww)))].astype(np.int16)
    d = np.abs(np.diff(th, axis=0)).mean(axis=(1, 2, 3)) / 255.0
    real = np.flatnonzero(d >= shots_mod.DUP_DIFF)
    if real.size < 4:
        return period
    gaps = np.diff(real)
    gaps = gaps[gaps <= 8]           # une longue pause (image figée) n'est pas une cadence
    if gaps.size < 3:
        return period
    return float(np.mean(gaps)) * period


# ---------------------------------------------------------------------------
# découpe des plans par décalage et attribution des sources
# ---------------------------------------------------------------------------
def _split_shots_by_offset(shots: list[shots_mod.Shot], claps: list[Clap], scan: media.MotionScan,
                           period: float) -> tuple[list[shots_mod.Shot], dict[int, float | None],
                                                   dict[int, float | None]]:
    """Coupe un plan quand le décalage change en cours de route (changement de PC sur une mire
    identique, sans coupe visible). Retourne les nouveaux plans, leur décalage et leur teinte."""
    new_shots: list[shots_mod.Shot] = []
    seg_off: dict[int, float | None] = {}
    seg_hue: dict[int, float | None] = {}
    times = scan.times
    for s in shots:
        sc = sorted([c for c in claps if c.shot == s.index], key=lambda c: c.t_audio)
        cl = _clusters(sc, max(period, _source_period(scan, s, period)))
        label = {}
        for li, (_, g) in enumerate(cl):
            for c in g:
                label[c.id] = li
        # séquences temporelles d'événements de même groupe (A A A B B B A A…)
        runs: list[list[Clap]] = []
        run_lab: list[int] = []
        for c in sc:
            li = label.get(c.id)
            if li is None:
                continue
            if run_lab and run_lab[-1] == li:
                runs[-1].append(c)
            else:
                runs.append([c])
                run_lab.append(li)
        # une séquence d'un seul événement isolé n'est pas un changement de source
        keep = [(r, l) for r, l in zip(runs, run_lab) if len(r) >= 2 or len(runs) == 1]
        for r, l in zip(runs, run_lab):
            if (r, l) not in keep:
                for c in r:
                    c.selected = None
        merged: list[tuple[list[Clap], int]] = []
        for r, l in keep:
            if merged and merged[-1][1] == l:
                merged[-1][0].extend(r)
            else:
                merged.append((list(r), l))
        spans = [(r[0].t_audio, r[-1].t_audio, float(np.median([c.offset for c in r])), r) for r, _ in merged]
        bounds = [s.start]
        for a, b in zip(spans, spans[1:]):
            # frontière : milieu entre le dernier événement de a et le premier de b
            last_a = max(c.selected.t for c in a[3])
            first_b = min(c.selected.t for c in b[3])
            bounds.append((last_a + first_b) / 2)
        bounds.append(s.end)
        for j in range(len(bounds) - 1):
            a, b = bounds[j], bounds[j + 1]
            i0 = s.i0 if j == 0 else int(np.searchsorted(times, a))
            i1 = s.i1 if j == len(bounds) - 2 else int(np.searchsorted(times, b))
            ns = shots_mod.Shot(index=len(new_shots), i0=i0, i1=i1,
                                start=float(times[i0]) if j else s.start,
                                end=float(times[i1]) if i1 < times.size and j < len(bounds) - 2 else s.end,
                                camera=s.camera)
            ns.look, ns.qr, ns.region, ns.tile, ns.mire = s.look, s.qr, s.region, s.tile, s.mire
            new_shots.append(ns)
            group = spans[j][3] if spans else []
            seg_off[ns.index] = spans[j][2] if spans else None
            hues = [c.selected.hue for c in group if c.selected and c.selected.hue is not None]
            seg_hue[ns.index] = float(np.median(hues)) if hues else None
            for c in sc:
                if a <= c.t_audio < b or (j == len(bounds) - 2 and c.t_audio >= b):
                    c.shot = -1 - ns.index   # marquage : évite les collisions avec les plans suivants
    for c in claps:
        if c.shot < 0:
            c.shot = -1 - c.shot
    return new_shots, seg_off, seg_hue


def _match_cycle(path: str, shots: list[shots_mod.Shot], scan: media.MotionScan,
                 claps: list[Clap], window: float) -> None:
    """Lève l'ambiguïté d'une période : chaque bip SyncLive a une note (n° 0-7) et chaque flash
    un QR qui porte le même n°. On cherche les flashs sur ± 3,5 périodes et on ne garde que
    celui qui porte le bon n° : un décalage de 1,3 s n'est plus confondu avec 0,3 s."""
    from .pattern import CYCLE, decode_qr_full
    coded = [c for c in claps if c.tone is not None]
    if not coded:
        return
    period = float(np.median(np.diff(scan.times))) if scan.times.size > 1 else 1 / 30
    per_shot: dict[int, visual.ShotSignal] = {}
    wide: dict[int, list[visual.VisualCandidate]] = {}
    for c in coded:
        s = shots[c.shot]
        sig = per_shot.setdefault(c.shot, visual.ShotSignal(scan, s))
        # période de la mire ≈ écart médian entre bips de ce plan
        bips = sorted(x.t_audio for x in coded if x.shot == c.shot)
        P = float(np.median(np.diff(bips))) if len(bips) > 1 else 1.0
        wide[c.id] = visual.flash_candidates(sig, c.t_audio, max(window, (CYCLE / 2 - 0.5) * P))
    times = sorted({v.t for vs in wide.values() for v in vs})
    frames = media.grab_frames(path, times, max_width=1920, apply_rotation=False)
    region_of = {}
    for c in coded:
        for v in wide[c.id]:
            region_of[v.t] = shots[c.shot].region
    idx: dict[float, int | None] = {}
    for t in times:
        r = decode_qr_full(_crop(frames[t], region_of[t])) if t in frames else None
        idx[t] = r[2] if r else None
    for c in coded:
        cands = wide[c.id]
        if not any(idx.get(v.t) is not None for v in cands):
            continue                      # QR illisible : on garde la recherche classique
        good = [v for v in cands if idx.get(v.t) == c.tone]
        c.candidates = good
        c.method = "mire (flash n°)"


def _split_shots_by_qr(path: str, shots: list[shots_mod.Shot], claps: list[Clap],
                       scan: media.MotionScan, step: float = 1.0) -> list[shots_mod.Shot]:
    """Lit le QR des mires SyncLive ~1 fois/s et coupe un plan quand l'identifiant change
    (changement de PC à la régie sans coupe visible : même mire, autre machine)."""
    from .pattern import decode_qr, source_label
    mire_shots = {c.shot for c in claps if c.method.startswith("mire")}
    targets: dict[int, list[float]] = {}
    for s in shots:
        if s.index not in mire_shots and s.duration < 1.0:
            continue
        ts = list(np.arange(s.start + 0.3, s.end - 0.2, step))
        if s.index not in mire_shots:
            ts = ts[:3]          # plan sans bip : on regarde juste s'il y a un QR
        targets[s.index] = [float(t) for t in ts]
    frames = media.grab_frames(path, [t for ts in targets.values() for t in ts], max_width=1920,
                               apply_rotation=False)
    times = scan.times
    out: list[shots_mod.Shot] = []
    for s in shots:
        reads = []
        for t in targets.get(s.index, []):
            img = frames.get(t)
            r = decode_qr(_crop(img, s.region)) if img is not None else None
            if r:
                reads.append((t, source_label(r[0])))
        if s.index in mire_shots:
            pass
        elif reads and len({lab for _, lab in reads}) == 1:
            reads = reads[:1]
        else:
            reads = []
        # séquences de même QR (une lecture isolée différente est ignorée)
        runs: list[list] = []
        for t, lab in reads:
            if runs and runs[-1][0] == lab:
                runs[-1][2] = t
            else:
                runs.append([lab, t, t, 0])
            runs[-1][3] += 1
        runs = [r for r in runs if r[3] >= 2 or len(runs) == 1]
        merged = []
        for r in runs:
            if merged and merged[-1][0] == r[0]:
                merged[-1][2] = r[2]
            else:
                merged.append(list(r))
        if not merged:
            old = s.index
            s.index = len(out)
            out.append(s)
            for c in claps:
                if c.shot == old:
                    c.shot = -1 - s.index
            continue
        # frontière affinée : relecture du QR toutes les 0,1 s entre la dernière lecture A et la 1re B
        bounds = [s.start]
        for a_run, b_run in zip(merged, merged[1:]):
            lo, hi = a_run[2], b_run[1]
            fine = [float(t) for t in np.arange(lo + 0.1, hi - 0.05, 0.1)]
            got = media.grab_frames(path, fine, max_width=1920, apply_rotation=False) if fine else {}
            for t in fine:
                r = decode_qr(_crop(got[t], s.region)) if t in got else None
                lab = source_label(r[0]) if r else None
                if lab == a_run[0]:
                    lo = t
                elif lab == b_run[0]:
                    hi = t
                    break
            bounds.append((lo + hi) / 2)
        bounds.append(s.end)
        old = s.index
        for j, r in enumerate(merged):
            a, b = bounds[j], bounds[j + 1]
            i0 = s.i0 if j == 0 else int(np.searchsorted(times, a))
            i1 = s.i1 if j == len(merged) - 1 else int(np.searchsorted(times, b))
            ns = shots_mod.Shot(index=len(out), i0=i0, i1=i1,
                                start=s.start if j == 0 else float(times[i0]),
                                end=s.end if j == len(merged) - 1 else float(times[i1]),
                                camera=s.camera, look=s.look, qr=r[0], region=s.region, tile=s.tile,
                                mire=s.mire)
            out.append(ns)
        segs = list(zip(range(len(merged)), bounds[:-1], bounds[1:]))
        first_new = len(out) - len(merged)

        def seg_of(t: float) -> int:
            for j, a, b in segs:
                if t < b or j == len(segs) - 1:
                    return j
            return len(segs) - 1

        for c in claps:
            if c.shot != old:
                continue
            # le bip reste du côté de son SON s'il y a un flash de ce côté ; sinon il suit son flash
            # le plus fort. Les flashs de l'autre côté (autre machine) sont écartés.
            j = seg_of(c.t_audio)
            inside = [v for v in c.candidates if seg_of(v.t) == j]
            if not inside and c.candidates:
                j = seg_of(max(c.candidates, key=lambda v: v.strength).t)
                inside = [v for v in c.candidates if seg_of(v.t) == j]
            c.candidates = inside
            c.shot = -1 - (first_new + j)       # marquage temporaire
    for c in claps:
        c.shot = -1 - c.shot     # tous les claps ont été marqués avec leur nouvel index
    return out


def _hue_dist(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1 - d)


def _assign_sources(shots: list[shots_mod.Shot], look: dict[int, object], seg_off: dict[int, float | None],
                    seg_hue: dict[int, float | None], tol: float, split: bool,
                    mire_looks: dict[int, bool]) -> None:
    """camera = (apparence, décalage, teinte de flash)."""
    sources: list[dict] = []   # {look, off, hue, id}
    unmeasured: dict[int, int] = {}
    measured_looks = {look[s.index] for s in shots if seg_off.get(s.index) is not None}
    # décalages distincts mesurés par apparence (pour savoir si un plan non mesuré est ambigu)
    looks_offsets: dict[int, list[float]] = {}
    for s in shots:
        if seg_off.get(s.index) is not None:
            looks_offsets.setdefault(look[s.index], []).append(seg_off[s.index])
    for lk, offs in looks_offsets.items():
        offs.sort()
        reps = [offs[0]]
        for o in offs[1:]:
            if o - reps[-1] > tol:
                reps.append(o)
        looks_offsets[lk] = reps
    # les plans mesurés d'abord, pour que les plans sans clap puissent s'y rattacher
    for s in sorted(shots, key=lambda x: seg_off.get(x.index) is None):
        lk, off, hue = look[s.index], seg_off.get(s.index), seg_hue.get(s.index)
        if not split:
            match = next((src for src in sources if src["look"] == lk), None)
        elif off is None:
            measured = [src for src in sources if src["look"] == lk and src["off"] is not None]
            if lk not in measured_looks:
                match = next((src for src in sources if src["look"] == lk), None)
            elif (lk[0] == "qr" or not mire_looks.get(lk)) and len(looks_offsets[lk]) == 1 and measured:
                match = measured[0]  # même machine (QR) ou caméra qui revient sans clap
            else:
                # même image qu'une mire ou que plusieurs sources : on ne suppose rien
                sid = unmeasured.get(lk)
                match = next((src for src in sources if src["id"] == sid), None)
        else:
            match = None
            for src in sources:
                if src["look"] != lk or src["off"] is None:
                    continue
                if abs(src["off"] - off) > tol:
                    continue
                if hue is not None and src["hue"] is not None and _hue_dist(hue, src["hue"]) > 0.06:
                    continue
                match = src
                break
        if match is None:
            match = {"look": lk, "off": off, "hue": hue, "id": len(sources)}
            sources.append(match)
            if off is None and lk in measured_looks:
                unmeasured[lk] = match["id"]
        s.camera = match["id"]


def _crop(img: np.ndarray, region: tuple[float, float, float, float]) -> np.ndarray:
    if region == (0.0, 0.0, 1.0, 1.0):
        return img
    h, w = img.shape[:2]
    x0, y0, x1, y1 = region
    return np.ascontiguousarray(img[int(y0 * h):max(int(y0 * h) + 1, int(y1 * h)),
                                    int(x0 * w):max(int(x0 * w) + 1, int(x1 * w))])


def _boxes_in_region(locator, img: np.ndarray, region) -> list:
    """Mains détectées dans la vignette, coordonnées ramenées à l'image entière."""
    x0, y0, x1, y1 = region
    out = []
    for bx0, by0, bx1, by1 in locator.boxes(_crop(img, region)):
        out.append((x0 + bx0 * (x1 - x0), y0 + by0 * (y1 - y0), x0 + bx1 * (x1 - x0), y0 + by1 * (y1 - y0)))
    return out


LOCAL_BASE_PCT = 20.0  # centile « calme » de l'analyse fine (fenêtre courte autour du clap)
TILE_AREA = 0.6     # sous cette part de l'image, une vignette est réanalysée finement autour des claps


def _tile_clap_candidates(path: str, shots: list[shots_mod.Shot], claps_by_shot: dict,
                          hand_boxes: dict, window: float, cancel=None) -> dict:
    """Candidats de clap des vignettes de mosaïque, cherchés sur une analyse du mouvement propre à
    chaque vignette (grille complète sur la vignette seule), autour de chaque clap audio. Un seul
    décodage par clap sert à toutes les vignettes (elles partagent la bande son)."""
    tiles = [s for s in shots
             if (s.region[2] - s.region[0]) * (s.region[3] - s.region[1]) < TILE_AREA]
    by_time: dict[float, list[shots_mod.Shot]] = {}
    for s in tiles:
        for ac in claps_by_shot.get(s.index, []):
            if ac.freq is None:
                by_time.setdefault(ac.t, []).append(s)
    half = window + visual.LOOKAHEAD + 0.25
    out: dict = {}
    for t, ss in sorted(by_time.items()):
        if cancel and cancel():
            raise InterruptedError
        scans = media.scan_regions(path, t - half, t + half, [s.region for s in ss])
        for s, ls in zip(ss, scans):
            if ls is None or ls.times.size < 4:
                continue
            per = float(np.median(np.diff(ls.times)))
            lshot = shots_mod.Shot(index=0, i0=0, i1=ls.times.size, start=float(ls.times[0]),
                                   end=float(ls.times[-1]) + per)
            x0, y0, x1, y1 = s.region
            rel = [((bx0 - x0) / (x1 - x0), (by0 - y0) / (y1 - y0), (bx1 - x0) / (x1 - x0), (by1 - y0) / (y1 - y0))
                   for bx0, by0, bx1, by1 in hand_boxes.get(s.index, [])]
            mask = visual.roi_from_boxes(rel)
            out[(s.index, t)] = visual.clap_candidates(visual.ShotSignal(ls, lshot, base_pct=LOCAL_BASE_PCT), t,
                                                       window, mask)
    return out


def _claps_agree(hand_claps: list, period: float) -> bool:
    """Les claps du plan donnent-ils un décalage cohérent (au moins 2, et au moins la moitié des
    claps qui ont un candidat) ? Une caméra qui filme une LED clignotante reste une caméra."""
    tmp = [Clap(id=i, shot=0, t_audio=ac.t, snr_db=0.0, method="clap",
                candidates=[v for v in c if v.kind == "clap"]) for i, (ac, c) in enumerate(hand_claps)]
    tmp = [c for c in tmp if c.candidates]
    if len(tmp) < 2:
        return False
    cl = _clusters(tmp, period)
    best = max((len(g) for _, g in cl), default=0)
    return best >= 2 and best >= 0.5 * len(tmp)


def _periodic_flashes(sig: visual.ShotSignal, shot: shots_mod.Shot) -> bool:
    """Le plan montre-t-il une mire : au moins 4 flashs à intervalle régulier (0,4 à 2,1 s) ?"""
    if shot.end - shot.start < 2.5:
        return False
    half = (shot.end - shot.start) / 2
    fl = sorted(v.t for v in visual.flash_candidates(sig, shot.start + half, half + 0.1))
    if len(fl) < 4:
        return False
    d = np.diff(fl)
    per = float(np.median(d))
    if not 0.4 <= per <= 2.1:
        return False
    return float(np.mean(np.abs(d - per) <= 0.1 * per)) >= 0.7


def _beep_train(tonal: list) -> list:
    """Bips d'une mire parmi les sons tonals d'un plan. Un vrai train de bips se répète : au moins
    deux bips SyncLive (notes numérotées), ou au moins deux sons de même hauteur (± 3 %) pour une
    mire d'une autre marque. Un son tonal isolé (voix, choc, bruit de plateau) n'en est pas un :
    près d'une mire qui flashe chaque seconde, il tomberait toujours près d'un flash."""
    from .pattern import tone_index
    syn = [x for x in tonal if tone_index(x[0].freq) is not None]
    other = [x for x in tonal if tone_index(x[0].freq) is None]
    keep = syn if len(syn) >= 2 else []
    best: list = []
    for a, _ in other:
        grp = [x for x in other if abs(x[0].freq / a.freq - 1.0) <= 0.03]
        if len(grp) > len(best):
            best = grp
    if len(best) >= 2:
        keep = keep + best
    return sorted(keep, key=lambda x: x[0].t)


def _split_multiview(shots: list[shots_mod.Shot], scan: media.MotionScan) -> list[shots_mod.Shot]:
    """Découpe chaque plan en vignettes s'il s'agit d'une mosaïque de caméras bord à bord."""
    from .multiview import detect_tiles, tile_label
    if scan.samples is None:
        return shots
    out: list[shots_mod.Shot] = []
    next_look = max((s.look for s in shots), default=0) + 1
    tile_looks: dict[tuple, int] = {}
    for s in shots:
        m = (scan.sample_idx >= s.i0) & (scan.sample_idx < s.i1)
        tiles = detect_tiles(scan.samples[m]) if m.sum() >= 4 else [(0.0, 0.0, 1.0, 1.0)]
        if len(tiles) <= 1:
            s.index = len(out)
            out.append(s)
            continue
        for k, reg in enumerate(tiles):
            key = (s.look, tuple(round(v, 2) for v in reg))
            if key not in tile_looks:
                tile_looks[key] = next_look
                next_look += 1
            out.append(shots_mod.Shot(index=len(out), i0=s.i0, i1=s.i1, start=s.start, end=s.end,
                                      camera=s.camera, look=tile_looks[key], region=reg,
                                      tile=tile_label(reg, len(tiles))))
    return out


def _auto_audio(path: str, info: media.VideoInfo, st: Settings, stage) -> tuple[list, int, float]:
    """Aucun réglage pour l'utilisateur : on prend la piste demandée (la 1re par défaut) ; si elle ne
    contient presque aucun clap/bip, on essaie les autres pistes puis une sensibilité plus élevée,
    et on garde le meilleur résultat."""
    tracks = [st.audio_track] + [i for i in range(info.n_audio_streams) if i != st.audio_track]
    best = ([], st.audio_track, st.audio_sensitivity)
    for n, tr in enumerate(tracks):
        try:
            sig, rate, t0 = media.decode_audio(path, tr, stage("Décodage audio", 0.0, 0.1) if n == 0 else None,
                                               info.duration)
        except ValueError:
            if n == 0:
                raise
            continue
        for sens in (st.audio_sensitivity, st.audio_sensitivity * 2.0):
            cl = audio_claps.detect_claps(sig, rate, t0, sens)
            if len(cl) > len(best[0]):
                best = (cl, tr, sens)
            if len(cl) >= 2:
                break
        del sig
        if len(best[0]) >= 2:
            break
    return best


def run(path: str, settings: Settings | None = None,
        progress: Callable[[str, float], None] | None = None,
        cancel: Callable[[], bool] | None = None) -> Analysis:
    st = settings or Settings()
    prog = progress or (lambda s, f: None)

    def stage(name: str, a: float, b: float):
        return lambda f: prog(name, a + (b - a) * f)

    prog("Lecture des infos du fichier", 0.0)
    info = media.probe(path)

    aclaps, st.audio_track, st.audio_sensitivity = _auto_audio(path, info, st, stage)

    scan = media.scan_video(path, info, stage("Analyse des images", 0.12, 0.75), cancel)

    prog("Découpe en plans", 0.76)
    shots = shots_mod.detect_shots(scan, st.cut_threshold)
    shots_mod.group_cameras(scan, shots, st.group_threshold)
    for s in shots:
        s.look = s.camera

    # --- multiview maison : chaque vignette devient une source ---------------------------
    prog("Recherche de vignettes (multiview)", 0.77)
    shots = _split_multiview(shots, scan)

    def shot_at(t: float) -> int | None:
        for s in shots:
            if s.start <= t < s.end:
                return s.index
        return None

    claps_by_shot: dict[int, list[audio_claps.AudioClap]] = {}
    for ac in aclaps:
        for s in shots:                       # plusieurs vignettes peuvent partager le même instant
            if s.start <= ac.t < s.end:
                claps_by_shot.setdefault(s.index, []).append(ac)

    # --- zone des mains par plan (aide facultative) ------------------------------
    masks: dict[int, np.ndarray] = {}
    hand_boxes: dict[int, list] = {}
    hands_status = "désactivée"
    locator = HandLocator() if st.use_hands else None
    if locator is not None:
        hands_status = "active" if locator.available else f"indisponible ({locator.error})"
    if locator is not None and locator.available:
        prog("Recherche des mains", 0.78)
        want: dict[int, list[float]] = {}
        for si, lst in claps_by_shot.items():
            s = shots[si]
            want[si] = [t for ac in lst[:4] for t in (ac.t - 0.3, ac.t + 0.1) if s.start < t < s.end]
        frames = media.grab_frames(path, [t for ts in want.values() for t in ts],
                                   max_width=640, apply_rotation=False)
        for si, ts in want.items():
            boxes = [b for t in ts if t in frames
                     for b in _boxes_in_region(locator, frames[t], shots[si].region)]
            hand_boxes[si] = boxes
            m = visual.roi_from_boxes(boxes)
            if m is not None:
                masks[si] = m
        locator.close()

    prog("Mesure des décalages", 0.85)
    local = _tile_clap_candidates(path, shots, claps_by_shot, hand_boxes, st.max_offset, cancel)
    claps: list[Clap] = []
    for si, lst in sorted(claps_by_shot.items()):
        s = shots[si]
        sigs = visual.ShotSignal(scan, s)
        per = [(ac, visual.find_candidates(sigs, ac.t, st.max_offset, masks.get(si))) for ac in lst]
        # vignette de mosaïque : les claps sont cherchés sur l'analyse fine de la vignette
        per = [(ac, [v for v in c if v.kind == "flash"] + local[(si, ac.t)]) if (si, ac.t) in local
               else (ac, c) for ac, c in per]
        # Un bip de mire est une note pure (fréquence mesurée), un clap un bruit large bande (None).
        # Les deux peuvent se trouver dans la même bande son (mosaïque caméras + mires) : chaque
        # plan ne garde que les événements de son type.
        from .pattern import tone_index
        bips = _beep_train([(ac, c) for ac, c in per if ac.freq is not None])
        hand_claps = [(ac, c) for ac, c in per if ac.freq is None]
        # mire : au moins 2 bips qui tombent chacun sur un flash DE CETTE VIGNETTE (un seul « flash »
        # peut être un décor qui scintille). Bips SyncLive numérotés : recherche large (± 3,5 s),
        # le n° du bip lèvera ensuite l'ambiguïté de période.
        flash_bips = 0
        for ac, c in bips:
            wide = tone_index(ac.freq) is not None
            fl = visual.flash_candidates(sigs, ac.t, 3.5) if wide else [v for v in c if v.kind == "flash"]
            flash_bips += bool(fl)
        kind = "flash" if flash_bips >= 2 else "clap"
        if kind == "clap" and not _claps_agree(hand_claps, 1.0 / info.fps_float) and _periodic_flashes(sigs, s):
            # une mire à l'écran mais pas de train de bips : son de la machine non enregistré.
            # Les bruits du plateau ne doivent pas être pris pour des claps (l'animation de la
            # mire leur fournirait toujours un « mouvement » à associer)
            kind = "flash"
            s.mire = True
            keep = bips
        else:
            keep = (bips if kind == "flash" else hand_claps) or per
        method = "mire (flash)" if kind == "flash" else ("clap (mains)" if si in masks else "clap (mouvement)")
        for ac, cands in keep:
            claps.append(Clap(id=len(claps), shot=si, t_audio=ac.t, snr_db=ac.snr_db,
                              candidates=[v for v in cands if v.kind == kind], method=method,
                              tone=tone_index(ac.freq) if kind == "flash" else None))

    # --- mires SyncLive numérotées : un bip n°k ne va qu'avec le flash n°k ---------------
    _match_cycle(path, shots, scan, claps, st.max_offset)

    # --- identification des mires SyncLive par QR ------------------------------------
    prog("Lecture des QR des mires", 0.88)
    shots = _split_shots_by_qr(path, shots, claps, scan)

    # --- séparation des sources -----------------------------------------------
    prog("Identification des sources", 0.9)
    period = 1.0 / info.fps_float
    if st.split_by_offset:
        shots, seg_off, seg_hue = _split_shots_by_offset(shots, claps, scan, period)
    else:
        seg_off, seg_hue = {}, {}
        for s in shots:
            cl = _clusters([c for c in claps if c.shot == s.index], period)
            seg_off[s.index] = cl[0][0] if cl else None
            seg_hue[s.index] = None
    look = {s.index: s.look for s in shots}
    # un QR identifie la machine mieux que l'apparence (toutes les mires se ressemblent)
    key = {s.index: (("qr", s.qr) if s.qr else ("look", s.look)) for s in shots}
    mire_looks: dict = {key[s.index]: True for s in shots if s.mire}
    for c in claps:
        k = key[c.shot]
        mire_looks[k] = mire_looks.get(k, False) or c.method.startswith("mire")
    _assign_sources(shots, key, seg_off, seg_hue, max(1.5 * period, 0.02), st.split_by_offset, mire_looks)

    names: dict[int, str] = {}
    counters = {"CAM": 0, "MIRE": 0}
    for s in shots:
        if s.camera in names:
            continue
        cc = [c for c in claps if shots[c.shot].camera == s.camera]
        is_mire = bool(s.qr) or ((sum(c.method.startswith("mire") for c in cc) > len(cc) / 2) if cc
                                 else mire_looks.get(key[s.index], False))
        prefix = "MIRE" if is_mire else "CAM"
        if s.qr:
            n = sum(1 for v in names.values() if v == s.qr or v.startswith(s.qr + " ("))
            names[s.camera] = s.qr if n == 0 else f"{s.qr} ({n + 1})"
        else:
            counters[prefix] += 1
            names[s.camera] = f"{prefix} {counters[prefix]}"
    an = Analysis(info=info, settings=st, shots=shots, claps=claps, camera_names=names,
                  look=look, hands_status=hands_status)
    an.source_period = {s.index: _source_period(scan, s, an.period) for s in shots}

    # --- images représentatives -------------------------------------------------
    prog("Extraction des images", 0.93)
    for cid in an.camera_ids():
        aggregate_camera(an, cid)   # remplit clap.selected
    refresh_images(an)
    prog("Terminé", 1.0)
    return an


def shown_clap(an: Analysis, shot_index: int) -> Clap | None:
    """Clap dont l'image est montrée pour ce plan (carte, export, ouverture du réglage) : le clap
    réglé à la main en dernier, sinon le premier réglé, sinon le clap automatique le plus net."""
    claps = [c for c in an.claps if c.shot == shot_index and c.selected is not None]
    manual = [c for c in claps if c.manual_t is not None]
    pref = next((c for c in manual if c.id == an.image_clap.get(shot_index)), None)
    if pref is not None or manual:
        return pref or manual[0]
    return max(claps, key=lambda c: c.selected.strength) if claps else None


def clap_image_t(an: Analysis, c: Clap) -> float:
    """Instant de l'image montrée pour un clap : l'image choisie à la main (exactement), sinon
    pour un clap automatique l'image juste après le contact (mains jointes), l'image du flash."""
    if c.manual_t is not None:
        return c.manual_t
    dt = 0.0 if c.selected.kind == "flash" else 1.5 * an.period
    return c.selected.t + dt


def _image_time(an: Analysis, s: shots_mod.Shot) -> float:
    c = shown_clap(an, s.index)
    t = clap_image_t(an, c) if c is not None else (s.start + s.end) / 2
    return min(max(t, s.start), s.end - an.period)


def refresh_images(an: Analysis, shot_ids: list[int] | None = None) -> None:
    """(Re)lit l'image représentative des plans demandés (tous par défaut), d'après la sélection
    actuelle des claps : à appeler après un réglage manuel."""
    for cid in an.camera_ids():
        aggregate_camera(an, cid)   # remplit clap.selected
    shots = [s for s in an.shots if shot_ids is None or s.index in shot_ids]
    targets = {s.index: _image_time(an, s) for s in shots}
    if not targets:
        return
    frames = media.grab_frames(an.info.path, list(targets.values()))
    for si, t in targets.items():
        if t in frames:
            an.shot_images[si] = _crop(frames[t], an.shots[si].region)
