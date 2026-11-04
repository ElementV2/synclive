# SPDX-License-Identifier: GPL-3.0-or-later
"""Décodage vidéo / audio via PyAV (FFmpeg embarqué).

Tous les temps sont en secondes sur la timeline du conteneur (pts * time_base),
ce qui gère nativement 23.976 / 25 / 29.97 / 30 / 50 / 59.94 / 60 i/s et le VFR.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

import av
import cv2
import numpy as np

ProgressCb = Callable[[float], None]

# Résolution de travail pour l'analyse de mouvement (grille de 16x9 cellules de 20x20 px)
MOTION_W, MOTION_H = 320, 180
GRID_COLS, GRID_ROWS = 32, 18   # cellules de 10x10 px, assez fines pour les vignettes de multiview
THUMB_W, THUMB_H = 48, 27


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: Fraction           # fréquence nominale exacte (ex. 30000/1001)
    duration: float
    rotation: int
    audio_rate: int | None
    audio_channels: int | None
    n_audio_streams: int

    @property
    def fps_float(self) -> float:
        return float(self.fps)

    @property
    def fps_label(self) -> str:
        f = float(self.fps)
        return f"{f:.3f}".rstrip("0").rstrip(".")


@dataclass
class MotionScan:
    times: np.ndarray       # (n,) temps de présentation de chaque image
    cells: np.ndarray       # (n, 18, 32) mouvement moyen par cellule entre image k-1 et k
    thumbs: np.ndarray      # (n, 27, 48, 3) vignettes couleur pour coupes/regroupement
    frac_moving: np.ndarray # (n,) fraction de cellules en mouvement
    thumb_diff: np.ndarray  # (n,) différence moyenne de vignette avec l'image précédente
    sample_idx: np.ndarray | None = None   # (m,) indices des images échantillonnées
    samples: np.ndarray | None = None      # (m, 180, 320) niveaux de gris (détection des vignettes)


def probe(path: str) -> VideoInfo:
    with av.open(path) as c:
        if not c.streams.video:
            raise ValueError("Aucune piste vidéo dans ce fichier.")
        vs = c.streams.video[0]
        fps = vs.average_rate or vs.guessed_rate or vs.base_rate or Fraction(30)
        dur = None
        if vs.duration is not None and vs.time_base is not None:
            dur = float(vs.duration * vs.time_base)
        if not dur and c.duration:
            dur = c.duration / av.time_base
        aud = c.streams.audio[0] if c.streams.audio else None
        rotation = 0
        try:
            # PyAV >= 13 expose la matrice d'affichage via les side data de frame ;
            # on lit la première frame pour être fiable.
            for fr in c.decode(vs):
                rotation = int(getattr(fr, "rotation", 0) or 0)
                break
        except Exception:
            pass
        return VideoInfo(
            path=path,
            width=vs.codec_context.width,
            height=vs.codec_context.height,
            fps=Fraction(fps),
            duration=float(dur or 0.0),
            rotation=rotation,
            audio_rate=aud.codec_context.sample_rate if aud else None,
            audio_channels=aud.codec_context.channels if aud else None,
            n_audio_streams=len(c.streams.audio),
        )


def decode_audio(path: str, stream_index: int = 0, progress: ProgressCb | None = None,
                 duration: float = 0.0) -> tuple[np.ndarray, int, float]:
    """Retourne (signal mono float32, fréquence, temps du premier échantillon)."""
    with av.open(path) as c:
        if not c.streams.audio:
            raise ValueError("Aucune piste audio dans ce fichier : impossible de mesurer les claps.")
        if stream_index >= len(c.streams.audio):
            raise ValueError(f"Piste audio {stream_index + 1} inexistante "
                             f"({len(c.streams.audio)} piste(s) disponible(s)).")
        st = c.streams.audio[stream_index]
        rate = st.codec_context.sample_rate
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        chunks: list[np.ndarray] = []
        t0: float | None = None
        n = 0
        for frame in c.decode(st):
            if t0 is None and frame.time is not None:
                # temps du premier échantillon corrigé de ce qui a déjà été accumulé
                t0 = float(frame.time) - n / rate
            for out in resampler.resample(frame):
                a = out.to_ndarray().reshape(-1)
                chunks.append(a.astype(np.float32, copy=False))
                n += a.size
            if progress and duration > 0 and frame.time is not None:
                progress(min(1.0, frame.time / duration))
        for out in resampler.resample(None):
            a = out.to_ndarray().reshape(-1)
            chunks.append(a.astype(np.float32, copy=False))
        sig = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        return sig, rate, float(t0 or 0.0)


def scan_video(path: str, info: VideoInfo, progress: ProgressCb | None = None,
               cancel: Callable[[], bool] | None = None) -> MotionScan:
    """Un seul passage sur toute la vidéo : temps exacts, mouvement par cellule, vignettes."""
    times: list[float] = []
    cells: list[np.ndarray] = []
    thumbs: list[np.ndarray] = []
    fracs: list[float] = []
    tdiffs: list[float] = []
    prev_gray = None
    prev_thumb = None
    period = 1.0 / info.fps_float
    every = max(1, int(info.duration * info.fps_float / 200))   # ~200 images pour la mosaïque
    samples: list[np.ndarray] = []
    sample_idx: list[int] = []
    with av.open(path) as c:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        idx = 0
        for frame in c.decode(vs):
            if cancel and cancel():
                raise InterruptedError
            t = frame.time
            if t is None:
                t = (times[-1] + period) if times else 0.0
            rgb = frame.to_ndarray(format="rgb24", width=MOTION_W, height=MOTION_H)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.int16)
            thumb = cv2.resize(rgb, (THUMB_W, THUMB_H), interpolation=cv2.INTER_AREA)
            if prev_gray is None:
                cell = np.zeros((GRID_ROWS, GRID_COLS), np.float32)
                tdiff = 0.0
            else:
                d = np.abs(gray - prev_gray).astype(np.float32)
                cell = d.reshape(GRID_ROWS, MOTION_H // GRID_ROWS,
                                 GRID_COLS, MOTION_W // GRID_COLS).mean(axis=(1, 3))
                tdiff = float(np.abs(thumb.astype(np.int16) - prev_thumb).mean() / 255.0)
            times.append(float(t))
            cells.append(cell)
            thumbs.append(thumb)
            fracs.append(float((cell > 6.0).mean()))
            tdiffs.append(tdiff)
            prev_gray = gray
            prev_thumb = thumb.astype(np.int16)
            if idx % every == 0:
                samples.append(gray.astype(np.uint8))
                sample_idx.append(idx)
            idx += 1
            if progress and info.duration > 0 and idx % 15 == 0:
                progress(min(1.0, (t - times[0]) / info.duration))
    if not times:
        raise ValueError("Impossible de décoder la moindre image de la vidéo.")
    order = np.argsort(np.asarray(times), kind="stable")  # sécurité si pts non monotones
    return MotionScan(
        times=np.asarray(times, np.float64)[order],
        cells=np.stack(cells)[order],
        thumbs=np.stack(thumbs)[order],
        frac_moving=np.asarray(fracs, np.float32)[order],
        thumb_diff=np.asarray(tdiffs, np.float32)[order],
        sample_idx=np.argsort(order)[np.asarray(sample_idx, int)] if sample_idx else None,
        samples=np.stack(samples) if samples else None,
    )


def _rotate(img: np.ndarray, rotation: int) -> np.ndarray:
    r = (-rotation) % 360  # PyAV donne la rotation anti-horaire à appliquer en négatif
    if r == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if r == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if r == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def grab_frames(path: str, targets: list[float], max_width: int = 1920,
                apply_rotation: bool = True) -> dict[float, np.ndarray]:
    """Récupère l'image la plus proche de chaque temps demandé (RGB uint8)."""
    out: dict[float, np.ndarray] = {}
    if not targets:
        return out
    with av.open(path) as c:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        tb = vs.time_base
        fps = float(vs.average_rate or vs.guessed_rate or 30)
        half = 0.5 / fps
        it = None
        last_t = None
        for t in sorted(set(targets)):
            if it is None or last_t is None or t < last_t or t - last_t > 3.0:
                c.seek(max(0, int(max(0.0, t - 1.0) / tb)), stream=vs, backward=True, any_frame=False)
                it = c.decode(vs)
            best = None
            for frame in it:
                ft = frame.time if frame.time is not None else 0.0
                last_t = ft
                if ft + half >= t:
                    best = frame
                    break
                best = frame
            if best is None:
                continue
            w, h = best.width, best.height
            if w > max_width:
                h = int(round(h * max_width / w / 2) * 2)
                w = max_width
            img = best.to_ndarray(format="rgb24", width=w, height=h)
            if apply_rotation:
                img = _rotate(img, int(getattr(best, "rotation", 0) or 0))
            out[t] = img
    return out
