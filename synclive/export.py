# SPDX-License-Identifier: GPL-3.0-or-later
"""Export : une image annotée par caméra, planche récapitulative, CSV et JSON."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .analysis import Analysis, CameraResult


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    for n in names:
        for p in (Path("C:/Windows/Fonts") / n, Path(n)):
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default()


def fmt_ms(sec: float | None) -> str:
    if sec is None:
        return "—"
    v = round(sec * 1000)
    return f"{v:+d} ms" if v else "0 ms"


def fmt_frames(sec: float | None, fps: float) -> str:
    if sec is None:
        return "—"
    return f"{sec * fps:+.1f} img"


def display_name(cam: CameraResult) -> str:
    name = cam.name
    if cam.source_label and not name.startswith(cam.source_label):
        name = f"{name} · {cam.source_label}"
    if getattr(cam, "tile", None):
        name = f"{name} · {cam.tile}"
    return name


@dataclass
class SourceFix:
    cam: CameraResult
    video_delay: float | None    # s, retard idéal à appliquer à l'image de cette source
    frames: int | None           # même retard arrondi à l'image (fréquence de l'enregistrement)
    residual: float | None       # s, écart restant après arrondi (+ : son encore en retard)


@dataclass
class CorrectionPlan:
    """Le son est commun à toutes les sources (un seul décalage global) ; l'image se retarde
    source par source. On retarde donc le son de la plus grande avance du son, puis chaque
    image du complément, en images entières."""
    audio_delay: float           # s, à appliquer au son (global, toutes pistes)
    fps: float
    fixes: list[SourceFix]

    def fix_of(self, cam_id: int) -> SourceFix | None:
        return next((f for f in self.fixes if f.cam.id == cam_id), None)


def within_uncertainty(cam: CameraResult) -> bool:
    """Écart mesuré plus petit que la marge de mesure : rien de significatif à corriger."""
    unc = getattr(cam, "uncertainty", None)
    return cam.offset is not None and unc is not None and abs(cam.offset) <= unc


def effective_offset(cam: CameraResult) -> float | None:
    """Décalage retenu pour la correction : on ne corrige pas plus finement que la mesure."""
    if cam.offset is None:
        return None
    return 0.0 if within_uncertainty(cam) else cam.offset


def correction_plan(cams: list[CameraResult], fps: float) -> CorrectionPlan:
    offs = {c.id: effective_offset(c) for c in cams}
    measured = [o for o in offs.values() if o is not None]
    # son en avance (décalage < 0) sur au moins une source -> on retarde le son de la pire avance
    d = max([0.0] + [-o for o in measured])
    fixes = []
    for c in cams:
        if offs[c.id] is None:
            fixes.append(SourceFix(c, None, None, None))
            continue
        v = max(0.0, offs[c.id] + d)            # après retard du son, le son n'est plus jamais en avance
        n = int(round(v * fps))
        fixes.append(SourceFix(c, v, n, v - n / fps))
    return CorrectionPlan(audio_delay=d, fps=fps, fixes=fixes)


def _num(x: float, dec: int = 1) -> str:
    return f"{x:.{dec}f}".replace(".", ",")


def both(sec: float | None, fps: float, signed: bool = False) -> str:
    """« 118 ms · 3,5 img » — toujours les deux unités."""
    if sec is None:
        return "—"
    ms = round(sec * 1000)
    fr = sec * fps
    dec = 2 if abs(fr) < 0.1 else 1
    if signed:
        return f"{ms:+d} ms · {'+' if fr > 0 else ''}{_num(fr, dec)} img" if ms else "0 ms · 0 img"
    return f"{abs(ms)} ms · {_num(abs(fr), dec)} img"


def status(cam: CameraResult, fps: float) -> tuple[str, str]:
    """(constat en majuscules, explication) : qui est en retard, l'image ou le son."""
    if getattr(cam, "ambiguous", None):
        return "AMBIGU", cam.ambiguous
    if cam.offset is None:
        return "NON MESURÉE", ("aucun bip associé à un flash : le son de cette machine n'a pas été enregistré ?"
                               if cam.kind == "mire" else "aucun clap à la fois visible et audible")
    margin = precision(cam)
    if round(cam.offset * 1000) == 0:
        return "SYNCHRO", "le son et l'image arrivent en même temps" + margin
    if within_uncertainty(cam):
        return ("SYNCHRO", f"écart mesuré {cam.offset * 1000:+.0f} ms, plus petit que la marge de mesure"
                + margin)
    if cam.offset < 0:
        return (f"IMAGE EN RETARD de {both(cam.offset, fps)}",
                "le son arrive avant l'image" + margin)
    return (f"SON EN RETARD de {both(cam.offset, fps)}",
            "le son arrive après l'image" + margin)


def precision(cam: CameraResult) -> str:
    """« · ± 39 ms (caméra à 15 i/s réels) » : marge de mesure et cause quand la caméra est lente."""
    unc = getattr(cam, "uncertainty", None)
    if unc is None:
        return ""
    txt = f" · ± {unc * 1000:.0f} ms"
    why = []
    sfps = getattr(cam, "source_fps", None)
    if sfps:
        why.append(f"caméra à {sfps:.0f} i/s réels")
    if getattr(cam, "manual", False):
        why.append("image du clap réglée à la main")
    return txt + (f" ({', '.join(why)})" if why else "")


def fmt_audio_delay(plan: CorrectionPlan) -> str:
    if round(plan.audio_delay * 1000) == 0:
        return "SON : ne pas toucher (le son n'est en avance sur aucune source)"
    return (f"SON : retarder de {both(plan.audio_delay, plan.fps)} "
            f"(réglage global, une seule fois pour toutes les sources)")


def fmt_residual(fix: SourceFix, fps: float) -> str:
    if fix.residual is None or round(fix.residual * 1000) == 0:
        return "reste après correction 0 ms · 0 img"
    return f"reste après correction {both(fix.residual, fps, signed=True)}"


def fmt_video_action(fix: SourceFix, fps: float) -> str:
    if fix.video_delay is None:
        return "non mesurée : aucune correction calculée"
    if fix.frames == 0:
        return "IMAGE : aucun retard à mettre"
    return (f"IMAGE : retarder de {fix.frames} img · {round(fix.frames / fps * 1000)} ms "
            f"(cette source uniquement)")


def fmt_fix(fix: SourceFix, fps: float | None = None) -> str:
    fps = fps or 30.0
    if fix.video_delay is None:
        return fmt_video_action(fix, fps)
    return f"{fmt_video_action(fix, fps)}   ({fmt_residual(fix, fps)})"


def describe(cam: CameraResult, fps: float = 30.0) -> str:
    head, why = status(cam, fps)
    return f"{head} — {why}"


CONF_COLORS = {"élevée": (46, 204, 113), "moyenne": (241, 196, 15),
               "faible": (231, 76, 60), "aucune": (149, 165, 166)}


def annotate(cam: CameraResult, an: Analysis, plan: CorrectionPlan | None = None) -> Image.Image:
    fps = an.info.fps_float
    if plan is None:
        plan = correction_plan(an.cameras(), fps)
    fix = plan.fix_of(cam.id)
    if cam.image is not None:
        img = Image.fromarray(cam.image).convert("RGB")
        if img.width < 1280:  # texte lisible même sur des sources basse définition
            img = img.resize((1280, round(img.height * 1280 / img.width)), Image.LANCZOS)
    else:
        img = Image.new("RGB", (1280, 720), (30, 30, 30))
    W, H = img.size
    s = W / 1280
    big, mid, small = _font(int(54 * s), True), _font(int(28 * s)), _font(int(22 * s))
    pad = int(24 * s)
    band_h = int(228 * s)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rectangle([0, H - band_h, W, H], fill=(0, 0, 0, 185))
    img = Image.alpha_composite(img.convert("RGBA"), overlay)
    d = ImageDraw.Draw(img)
    y = H - band_h + pad // 2
    head, why = status(cam, fps)
    title = f"{display_name(cam)}   {head}"
    d.text((pad, y), title, font=big, fill=(255, 255, 255))
    y += int(66 * s)
    d.text((pad, y), f"{why}   ·   mesuré à {an.info.fps_label} i/s", font=mid, fill=(230, 230, 230))
    y += int(38 * s)
    if fix is not None and fix.video_delay is not None:
        a_txt = (f"retarder le SON de {both(plan.audio_delay, fps)} (global)" if round(plan.audio_delay * 1000)
                 else "ne pas toucher au SON")
        v_txt = (f"retarder cette IMAGE de {fix.frames} img · {round(fix.frames / fps * 1000)} ms" if fix.frames
                 else "aucun retard sur cette IMAGE")
        d.text((pad, y), f"À FAIRE   {a_txt}   ·   {v_txt}", font=mid, fill=(255, 90, 31))
    y += int(40 * s)
    spread = f"±{cam.spread * 1000:.0f} ms" if cam.spread is not None else "—"
    what = "bips" if cam.kind == "mire" else "claps"
    same = f" · même image que {', '.join(cam.same_look_as)}" if cam.same_look_as else ""
    info = (f"{cam.n_used}/{cam.n_total} {what} retenus · dispersion {spread} · "
            f"{len(cam.shots)} plan(s){same} · confiance ")
    d.text((pad, y), info, font=small, fill=(200, 200, 200))
    x = pad + d.textlength(info, font=small)
    d.text((x, y), cam.confidence, font=small, fill=CONF_COLORS.get(cam.confidence, (255, 255, 255)))
    return img.convert("RGB")


def contact_sheet(images: list[Image.Image], title: str, plan: CorrectionPlan | None = None) -> Image.Image:
    cols = 1 if len(images) == 1 else 2 if len(images) <= 4 else 3
    tw = 960
    thumbs = [im.resize((tw, int(im.height * tw / im.width)), Image.LANCZOS) for im in images]
    th = max(t.height for t in thumbs)
    rows = (len(thumbs) + cols - 1) // cols
    gap = 16
    lines = []
    if plan is not None:
        lines.append(fmt_audio_delay(plan))
        for f in plan.fixes:
            lines.append(f"{display_name(f.cam)}  —  {status(f.cam, plan.fps)[0]}  →  {fmt_fix(f, plan.fps)}")
    head = 70 + (40 + 30 * len(lines) if lines else 0)
    W = cols * tw + (cols + 1) * gap
    from .brand import CREAM, INK, lockup
    sheet = Image.new("RGB", (W, head + rows * (th + gap) + gap), INK)
    d = ImageDraw.Draw(sheet)
    logo = lockup(34)
    sheet.paste(logo, (gap, 18), logo)
    d.text((gap + logo.width + 24, 20), title, font=_font(28), fill=CREAM)
    if lines:
        y = 76
        d.text((gap, y), "PLAN DE CORRECTION", font=_font(18, True), fill=(255, 90, 31))
        y += 30
        for i, ln in enumerate(lines):
            d.text((gap, y), ln, font=_font(24, i == 0), fill=(240, 240, 242) if i == 0 else (200, 202, 206))
            y += 30
    for i, t in enumerate(thumbs):
        r, c = divmod(i, cols)
        sheet.paste(t, (gap + c * (tw + gap), head + r * (th + gap)))
    return sheet


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or "CAM"


def export_all(an: Analysis, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cams = an.cameras()
    fps = an.info.fps_float
    written: list[Path] = []
    imgs = []
    plan = correction_plan(cams, fps)
    for cam in cams:
        im = annotate(cam, an, plan)
        p = out / f"{_safe(cam.name)}.png"
        im.save(p)
        written.append(p)
        imgs.append(im)
    if imgs:
        sheet = contact_sheet(imgs, f"{Path(an.info.path).name}   {an.info.fps_label} i/s", plan)
        p = out / "recapitulatif.png"
        sheet.save(p)
        written.append(p)

    p = out / "rapport_claps.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["camera", "plan", "clap", "t_son_s", "t_image_s", "decalage_ms",
                    "decalage_images", "retenu", "exclu", "methode", "snr_db"])
        for cam in cams:
            for c in cam.claps:
                off = c.offset
                w.writerow([cam.name, c.shot + 1, c.id + 1, f"{c.t_audio:.4f}",
                            f"{c.selected.t:.4f}" if c.selected else "",
                            f"{off * 1000:.1f}" if off is not None else "",
                            f"{off * fps:.2f}" if off is not None else "",
                            "oui" if c.selected else "non", "oui" if c.excluded else "non",
                            c.method, f"{c.snr_db:.1f}"])
    written.append(p)

    p = out / "plan_correction.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["source", "constat", "decalage_mesure_ms", "decalage_mesure_images",
                    "retard_son_global_ms", "retard_son_global_images",
                    "retard_image_images", "retard_image_ms", "reste_apres_correction_ms", "reste_apres_correction_images"])
        fr = lambda sec: "" if sec is None else f"{sec * fps:.2f}"
        ms_ = lambda sec: "" if sec is None else f"{sec * 1000:.1f}"
        for fx in plan.fixes:
            w.writerow([fx.cam.name, status(fx.cam, fps)[0],
                        ms_(fx.cam.offset), fr(fx.cam.offset),
                        ms_(plan.audio_delay), fr(plan.audio_delay),
                        "" if fx.frames is None else fx.frames,
                        "" if fx.frames is None else f"{fx.frames / fps * 1000:.1f}",
                        ms_(fx.residual), fr(fx.residual)])
    written.append(p)

    p = out / "rapport.json"
    data = {
        "video": an.info.path,
        "fps": an.info.fps_float,
        "fps_exact": f"{an.info.fps.numerator}/{an.info.fps.denominator}",
        "resolution": [an.info.width, an.info.height],
        "duree_s": round(an.info.duration, 3),
        "convention": "decalage = t_son - t_image (positif = son en retard)",
        "plan_correction": {
            "principe": "le son est global : on le retarde de la plus grande avance du son, "
                        "puis chaque image est retardee du complement (en images entieres)",
            "retard_son_ms": round(plan.audio_delay * 1000, 1),
            "retard_son_images": round(plan.audio_delay * fps, 2),
            "sources": [
                {
                    "nom": f.cam.name,
                    "constat": status(f.cam, fps)[0],
                    "retard_image_images": f.frames,
                    "retard_image_ms": None if f.frames is None else round(f.frames / fps * 1000, 1),
                    "reste_apres_correction_ms": None if f.residual is None else round(f.residual * 1000, 1),
                    "reste_apres_correction_images": None if f.residual is None else round(f.residual * fps, 2),
                }
                for f in plan.fixes
            ],
        },
        "cameras": [
            {
                "nom": c.name,
                "type": c.kind,
                "id_mire": c.source_label,
                "meme_image_que": c.same_look_as,
                "decalage_ms": None if c.offset is None else round(c.offset * 1000, 1),
                "decalage_images": None if c.offset is None else round(c.offset * fps, 2),
                "dispersion_ms": None if c.spread is None else round(c.spread * 1000, 1),
                "incertitude_ms": None if c.uncertainty is None else round(c.uncertainty * 1000, 1),
                "cadence_reelle_ips": None if c.source_fps is None else round(c.source_fps, 2),
                "ambigu": c.ambiguous,
                "reglage_manuel": c.manual,
                "claps_retenus": c.n_used,
                "claps_total": c.n_total,
                "confiance": c.confidence,
                "plans": [
                    {"plan": i + 1, "debut_s": round(an.shots[i].start, 3), "fin_s": round(an.shots[i].end, 3)}
                    for i in c.shots
                ],
            }
            for c in cams
        ],
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(p)
    return written
