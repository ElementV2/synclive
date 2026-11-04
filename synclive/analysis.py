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
    same_look_as: list[str] = field(default_factory=list)
    image: np.ndarray | None = None


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

    @property
    def period(self) -> float:
        return 1.0 / self.info.fps_float

    def camera_ids(self) -> list[int]:
        seen: list[int] = []
        for s in self.shots:
            if s.camera not in seen:
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
    forced = [c for c in claps if not c.excluded and c.forced is not None and c.forced < len(c.candidates)]
    for c in forced:
        c.selected = c.candidates[c.forced]
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
    _select(claps, an.period)
    used = [c for c in claps if c.selected is not None]
    offs = np.array([c.offset for c in used], dtype=np.float64)
    if offs.size:
        off = float(np.median(offs))
        spread = float(1.4826 * np.median(np.abs(offs - off))) if offs.size > 1 else 0.0
        spread = max(spread, float(offs.std()) if offs.size > 2 else spread)
    else:
        off, spread = None, None
    p = an.period
    n = len(used)
    if n == 0:
        conf = "aucune"
    elif n >= 3 and spread is not None and spread <= 0.35 * p:
        conf = "élevée"
    elif n >= 2 and spread is not None and spread <= 0.7 * p:
        conf = "moyenne"
    else:
        conf = "faible"
    img = None
    if shot_ids:
        best_shot = max(shot_ids, key=lambda i: (sum(1 for c in used if c.shot == i),
                                                 an.shots[i].duration))
        img = an.shot_images.get(best_shot)
    name = an.camera_names.get(cid, f"CAM {cid + 1}")
    kind = "mire" if name.startswith("MIRE") or (
        claps and sum(c.method.startswith("mire") for c in claps) > len(claps) / 2) else "camera"
    qrs = [an.shots[i].qr for i in shot_ids if an.shots[i].qr]
    label = max(set(qrs), key=qrs.count) if qrs else None
    return CameraResult(
        id=cid, name=name, shots=shot_ids, claps=claps, source_label=label,
        offset=off, spread=spread, n_used=n, n_total=len([c for c in claps if not c.excluded]),
        confidence=conf, kind=kind, image=img,
    )


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
        cl = _clusters(sc, period)
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
            ns.look, ns.qr = s.look, s.qr
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
    idx: dict[float, int | None] = {}
    for t in times:
        r = decode_qr_full(frames[t]) if t in frames else None
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
            r = decode_qr(img) if img is not None else None
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
                r = decode_qr(got[t]) if t in got else None
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
                                camera=s.camera, look=s.look, qr=r[0])
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


def run(path: str, settings: Settings | None = None,
        progress: Callable[[str, float], None] | None = None,
        cancel: Callable[[], bool] | None = None) -> Analysis:
    st = settings or Settings()
    prog = progress or (lambda s, f: None)

    def stage(name: str, a: float, b: float):
        return lambda f: prog(name, a + (b - a) * f)

    prog("Lecture des infos du fichier", 0.0)
    info = media.probe(path)

    sig, rate, t0 = media.decode_audio(path, st.audio_track, stage("Décodage audio", 0.0, 0.1),
                                       info.duration)
    prog("Détection des claps et bips audio", 0.1)
    aclaps = audio_claps.detect_claps(sig, rate, t0, st.audio_sensitivity)
    del sig

    scan = media.scan_video(path, info, stage("Analyse des images", 0.12, 0.75), cancel)

    prog("Découpe en plans", 0.76)
    shots = shots_mod.detect_shots(scan, st.cut_threshold)
    shots_mod.group_cameras(scan, shots, st.group_threshold)
    for s in shots:
        s.look = s.camera

    def shot_at(t: float) -> int | None:
        for s in shots:
            if s.start <= t < s.end:
                return s.index
        return None

    claps_by_shot: dict[int, list[audio_claps.AudioClap]] = {}
    for ac in aclaps:
        si = shot_at(ac.t)
        if si is not None:
            claps_by_shot.setdefault(si, []).append(ac)

    # --- zone des mains par plan (aide facultative) ------------------------------
    masks: dict[int, np.ndarray] = {}
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
            boxes = [b for t in ts if t in frames for b in locator.boxes(frames[t])]
            m = visual.roi_from_boxes(boxes)
            if m is not None:
                masks[si] = m
        locator.close()

    prog("Mesure des décalages", 0.85)
    claps: list[Clap] = []
    for si, lst in sorted(claps_by_shot.items()):
        s = shots[si]
        sigs = visual.ShotSignal(scan, s)
        per = [(ac, visual.find_candidates(sigs, ac.t, st.max_offset, masks.get(si))) for ac in lst]
        # type d'événement du plan : majorité des candidats les plus forts
        kinds = [max(c, key=lambda v: v.strength).kind for _, c in per if c]
        kind = "flash" if kinds and kinds.count("flash") > len(kinds) / 2 else "clap"
        method = "mire (flash)" if kind == "flash" else ("clap (mains)" if si in masks else "clap (mouvement)")
        for ac, cands in per:
            from .pattern import tone_index
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
    mire_looks: dict = {}
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

    # --- images représentatives -------------------------------------------------
    prog("Extraction des images", 0.93)
    for cid in an.camera_ids():
        aggregate_camera(an, cid)   # remplit clap.selected
    targets: dict[int, float] = {}
    for s in shots:
        sel = [c for c in claps if c.shot == s.index and c.selected is not None]
        if sel:
            best = max(sel, key=lambda c: c.selected.strength)
            # clap : mains jointes juste après le contact ; flash : l'image du flash
            dt = 0.0 if best.selected.kind == "flash" else 1.5 * an.period
            t = best.selected.t + dt
        else:
            t = (s.start + s.end) / 2
        targets[s.index] = min(max(t, s.start), s.end - an.period)
    frames = media.grab_frames(path, list(targets.values()))
    for si, t in targets.items():
        if t in frames:
            an.shot_images[si] = frames[t]
    prog("Terminé", 1.0)
    return an
