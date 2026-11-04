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
         audio_codec: str = "pcm_s16le") -> list[dict]:
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
    vs.options = {"crf": "20", "preset": "veryfast"}
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
    for p in as_.encode():
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
                         size: tuple[int, int] = (640, 360)) -> None:
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
    for p in as_.encode():
        out.mux(p)
    out.close()
