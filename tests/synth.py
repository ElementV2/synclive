# SPDX-License-Identifier: GPL-3.0-or-later
"""Génère une vidéo multicam synthétique avec claps et décalages connus.

Chaque « caméra » a son propre décor ; deux blocs couleur peau (les mains) se
rapprochent en accélérant, se touchent à t_contact, restent jointes puis se séparent.
Le clap audio est placé à t_contact + décalage_de_la_caméra.
"""
from __future__ import annotations

import argparse
from fractions import Fraction

import av
import numpy as np

W, H = 640, 360
SR = 48000


def background(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    base = rng.uniform(40, 200, 3)
    grad = rng.uniform(-60, 60, 3)
    img = base[None, None, :] + grad[None, None, :] * (xx / W)[..., None] \
        + 30 * np.sin(xx / rng.uniform(15, 60) + yy / rng.uniform(15, 60))[..., None]
    for _ in range(12):  # quelques objets dans le décor
        x0, y0 = rng.integers(0, W - 80), rng.integers(0, H - 80)
        img[y0:y0 + rng.integers(20, 80), x0:x0 + rng.integers(20, 80)] = rng.uniform(0, 255, 3)
    return np.clip(img, 0, 255).astype(np.uint8)


def hand_gap(t: float, contacts: list[float]) -> float:
    """Écart entre les mains (px) à l'instant t."""
    d0, approach, hold, sep = 140.0, 0.13, 0.25, 0.45
    g = d0
    for tc in contacts:
        ts = tc - approach
        if ts <= t < tc:  # accélération jusqu'au contact
            u = (t - ts) / approach
            return d0 * (1 - u * u)
        if tc <= t < tc + hold:
            return 0.0
        if tc + hold <= t < tc + hold + sep:  # séparation douce (ease-out)
            u = (t - tc - hold) / sep
            return d0 * (1 - (1 - u) ** 2)
    return g


def render(bg: np.ndarray, gap: float, cx: int) -> np.ndarray:
    img = bg.astype(np.float32)
    hw, hh = 46, 70
    cy = H // 2 + 40
    skin = np.array([224, 172, 140], np.float32)
    lx1 = int(round(cx - gap / 2))
    rx0 = int(round(cx + gap / 2))
    img[cy - hh // 2: cy + hh // 2, lx1 - hw: lx1] = skin
    img[cy - hh // 2: cy + hh // 2, rx0: rx0 + hw] = skin * 0.95
    return img


def make(path: str, fps: Fraction, offsets: dict[str, float], order: list[str],
         shot_len: float = 5.5, claps_per_shot: int = 4, seed: int = 1,
         audio_codec: str = "pcm_s16le", gop: int | None = None) -> list[dict]:
    rng = np.random.default_rng(seed)
    bgs = {cam: background(seed * 100 + i) for i, cam in enumerate(sorted(offsets))}
    centers = {cam: 220 + 100 * i for i, cam in enumerate(sorted(offsets))}
    T = 1.0 / float(fps)
    total = shot_len * len(order)
    n_frames = int(round(total * float(fps)))

    # plan de chaque image et temps des contacts
    truth = []
    shots = []
    for i, cam in enumerate(order):
        s0 = i * shot_len
        cts = []
        t = s0 + 0.7 + rng.uniform(0, 0.2)
        for _ in range(claps_per_shot):
            cts.append(t)
            t += rng.uniform(0.95, 1.2)
        shots.append((s0, s0 + shot_len, cam, cts))
        for tc in cts:
            truth.append({"camera": cam, "t_contact": tc, "t_audio": tc + offsets[cam]})

    # audio
    n_audio = int(total * SR)
    audio = (rng.normal(0, 0.003, n_audio) + 0.01 * np.sin(2 * np.pi * 100 * np.arange(n_audio) / SR))
    for tr in truth:
        i0 = int(round(tr["t_audio"] * SR))
        L = int(0.12 * SR)
        tt = np.arange(L) / SR
        burst = rng.normal(0, 1, L) * np.exp(-tt / 0.012) * np.minimum(1, tt / 0.0005)
        seg = audio[i0:i0 + L]
        seg += 0.6 * burst[: seg.size]

    out = av.open(path, "w")
    vs = out.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
    vs.options = {"crf": "20", "preset": "veryfast", **({"g": str(gop), "keyint_min": str(gop)} if gop else {})}
    silent = None
    as_ = out.add_stream(audio_codec, rate=SR)
    as_.layout = "mono"

    for k in range(n_frames):
        t = k * T
        sh = next(s for s in shots if s[0] <= t < s[1]) if t < shots[-1][1] else shots[-1]
        cam = sh[2]
        acc = np.zeros((H, W, 3), np.float32)
        for sub in np.linspace(-T / 4, T / 4, 4):  # flou de bouger (obturation 180°)
            acc += render(bgs[cam], hand_gap(t + sub, sh[3]), centers[cam])
        img = acc / 4 + rng.normal(0, 2.5, (H, W, 1))
        frame = av.VideoFrame.from_ndarray(np.clip(img, 0, 255).astype(np.uint8), format="rgb24")
        frame.pts = k
        frame.time_base = Fraction(1, 1) / fps
        for p in vs.encode(frame):
            out.mux(p)
    for p in vs.encode():
        out.mux(p)

    a16 = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    chunk = 1024
    for i in range(0, a16.size, chunk):
        af = av.AudioFrame.from_ndarray(a16[None, i:i + chunk], format="s16", layout="mono")
        af.sample_rate = SR
        af.pts = i
        af.time_base = Fraction(1, SR)
        for p in as_.encode(af):
            out.mux(p)
        if silent is not None:
            zf = av.AudioFrame.from_ndarray(np.zeros_like(a16[None, i:i + 1024]), format="s16", layout="mono")
            zf.sample_rate = SR
            zf.pts = i
            zf.time_base = Fraction(1, SR)
            for p in silent.encode(zf):
                out.mux(p)
    for p in as_.encode():
        out.mux(p)
    if silent is not None:
        for p in silent.encode():
            out.mux(p)
    out.close()
    return truth


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--fps", default="30000/1001")
    a = ap.parse_args()
    make(a.out, Fraction(a.fps), {"A": 0.080, "B": -0.040, "C": 0.133}, ["A", "B", "C", "A", "B"])


def make_pattern_capture(path: str, fps: Fraction, segments: list[tuple[int, float, float]],
                         size: tuple[int, int] = (640, 360), silent_first_track: bool = False) -> None:
    """Capture simulée de mires SyncLive : segments = [(source, délai_son_s, durée_s)].
    Deux segments de même source = même image, latence éventuellement différente."""
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from synclive import pattern as P

    rends = {}
    N = int(round(float(fps)))
    T = 1 / float(fps)
    out = av.open(path, "w")
    vs = out.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = size[0], size[1], "yuv420p"
    vs.options = {"crf": "20", "preset": "veryfast"}
    silent = None
    if silent_first_track:          # piste 1 muette (micro coupé), le son utile est sur la piste 2
        silent = out.add_stream("pcm_s16le", rate=SR)
        silent.layout = "mono"
    as_ = out.add_stream("pcm_s16le", rate=SR)
    as_.layout = "mono"
    total = sum(d for _, _, d in segments)
    audio = np.random.default_rng(0).normal(0, 0.002, int(total * SR) + SR)
    k = 0
    t_seg = 0.0
    phase = 7  # les PC ne sont pas calés entre eux
    for src, delay, dur in segments:
        if src not in rends:
            r = P._Renderer(P.PatternSpec(size[0], size[1], fps, source=src))
            rends[src] = (r, [r.base(i) for i in range(N)])
        rnd, bases = rends[src]
        n = int(round(dur * float(fps)))
        for j in range(n):
            i = (j + phase) % N
            cyc = ((j + phase + N // 2) // N) % P.CYCLE
            fr = av.VideoFrame.from_ndarray(rnd.decorate(bases[i], cyc), format="rgb24")
            fr.pts = k
            fr.time_base = Fraction(1, 1) / fps
            for p in vs.encode(fr):
                out.mux(p)
            if i == 0:
                i0 = int(round((k * T + delay) * SR))
                if i0 >= 0:
                    b = P._beep(int(0.12 * SR), cyc)
                    seg = audio[i0:i0 + b.size]
                    seg += b[: seg.size]
            k += 1
        t_seg += dur
        phase += 11
    for p in vs.encode():
        out.mux(p)
    a16 = (np.clip(audio[: int(k * T * SR)], -1, 1) * 32767).astype(np.int16)
    for i in range(0, a16.size, 1024):
        af = av.AudioFrame.from_ndarray(a16[None, i:i + 1024], format="s16", layout="mono")
        af.sample_rate = SR
        af.pts = i
        af.time_base = Fraction(1, SR)
        for p in as_.encode(af):
            out.mux(p)
        if silent is not None:
            zf = av.AudioFrame.from_ndarray(np.zeros_like(a16[None, i:i + 1024]), format="s16", layout="mono")
            zf.sample_rate = SR
            zf.pts = i
            zf.time_base = Fraction(1, SR)
            for p in silent.encode(zf):
                out.mux(p)
    for p in as_.encode():
        out.mux(p)
    if silent is not None:
        for p in silent.encode():
            out.mux(p)
    out.close()


def make_multiview(path: str, fps: Fraction, cam_latency: list[float], mire_delay: float,
                   mire_source: int = 3, tile: tuple[int, int] = (320, 180)) -> None:
    """Multiview maison 2x2 bord à bord : 3 caméras (claps) + 1 mire SyncLive.

    Comme en vrai : l'image de tout le monde est visible en même temps ; le son contient d'abord
    les claps (vus par toutes les caméras au même instant physique), puis le son de la mire.
    Caméra i : son clap visible à tc + latence_i -> décalage attendu = -latence_i.
    Mire : bip à flash + mire_delay -> décalage attendu = +mire_delay."""
    import cv2 as _cv2
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from synclive import pattern as P

    TW, TH = tile
    T = 1 / float(fps)
    total = 7.0
    contacts = [0.8, 1.9, 3.0]                       # claps (temps physique)
    bgs = [background(10 + i) for i in range(3)]
    centers = [260, 320, 380]
    rnd = P._Renderer(P.PatternSpec(TW, TH, fps, source=mire_source))
    N = int(round(float(fps)))
    bases = [rnd.base(i) for i in range(N)]

    out = av.open(path, "w")
    vs = out.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = TW * 2, TH * 2, "yuv420p"
    vs.options = {"crf": "18", "preset": "veryfast"}
    as_ = out.add_stream("pcm_s16le", rate=SR)
    as_.layout = "mono"
    n = int(round(total * float(fps)))
    rng = np.random.default_rng(5)
    audio = rng.normal(0, 0.002, int(total * SR) + SR)
    for tc in contacts:                              # claps : bruit large bande
        i0 = int(round(tc * SR))
        L = int(0.12 * SR)
        tt = np.arange(L) / SR
        audio[i0:i0 + L] += 0.6 * rng.normal(0, 1, L) * np.exp(-tt / 0.012) * np.minimum(1, tt / 0.0005)
    for k in range(n):
        t = k * T
        canvas = np.zeros((TH * 2, TW * 2, 3), np.uint8)
        for i in range(3):
            tl = t - cam_latency[i]                  # la caméra i montre l'instant t - latence
            acc = np.zeros((360, 640, 3), np.float32)
            for sub in np.linspace(-T / 4, T / 4, 4):
                acc += render(bgs[i], hand_gap(tl + sub, contacts), centers[i])
            img = np.clip(acc / 4 + rng.normal(0, 2.0, (360, 640, 1)), 0, 255).astype(np.uint8)
            r, c = divmod(i, 2)
            canvas[r * TH:(r + 1) * TH, c * TW:(c + 1) * TW] = _cv2.resize(img, (TW, TH), interpolation=_cv2.INTER_AREA)
        j = k + 5                                    # mire (phase quelconque)
        i_m, cyc = j % N, ((j + N // 2) // N) % P.CYCLE
        canvas[TH:, TW:] = rnd.decorate(bases[i_m], cyc)
        if i_m == 0 and t > 3.6:                     # son de la mire, après les claps
            i0 = int(round((t + mire_delay) * SR))
            b = P._beep(int(0.12 * SR), cyc)
            seg = audio[i0:i0 + b.size]
            seg += b[: seg.size]
        fr = av.VideoFrame.from_ndarray(canvas, format="rgb24")
        fr.pts, fr.time_base = k, Fraction(1, 1) / fps
        for p in vs.encode(fr):
            out.mux(p)
    for p in vs.encode():
        out.mux(p)
    a16 = (np.clip(audio[: int(n * T * SR)], -1, 1) * 32767).astype(np.int16)
    for i in range(0, a16.size, 1024):
        af = av.AudioFrame.from_ndarray(a16[None, i:i + 1024], format="s16", layout="mono")
        af.sample_rate, af.pts, af.time_base = SR, i, Fraction(1, SR)
        for p in as_.encode(af):
            out.mux(p)
    for p in as_.encode():
        out.mux(p)
    out.close()
