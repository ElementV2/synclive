# SPDX-License-Identifier: GPL-3.0-or-later
"""Médias de régie générés au hasard pour tester la normalisation et le montage : vidéos de
fréquences et de GOP variés, son de nature et de volume très différents (musique, parole,
pub compressée, ambiance), dans les formats qu'on reçoit (MP4/AAC, MOV/PCM, MKV/Opus, MXF,
WAV, MP3, 5.1)."""
from __future__ import annotations

from fractions import Fraction

import av
import numpy as np

SR = 48000
KINDS = ("musique", "parole", "pub", "ambiance")
FORMATS = [  # (extension, codec vidéo ou None, codec audio, disposition)
    (".mp4", "libx264", "aac", "stereo"),
    (".mov", "libx264", "pcm_s24le", "stereo"),
    (".mkv", "libx264", "libopus", "stereo"),
    (".mxf", "mpeg2video", "pcm_s16le", "stereo"),
    (".mp4", "libx264", "aac", "5.1"),
    (".wav", None, "pcm_s16le", "stereo"),
    (".mp3", None, "libmp3lame", "stereo"),
]


def audio(kind: str, dur: float, level_db: float, rng: np.random.Generator, channels: int = 2) -> np.ndarray:
    """Son (canaux, n) d'une nature donnée, normalisé à une crête de `level_db` dBFS environ."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    if kind == "musique":
        x = np.zeros(n)
        bpm = rng.uniform(80, 140)
        beat = 60 / bpm
        for k in range(int(dur / beat)):
            i0 = int(k * beat * SR)
            root = rng.choice([110, 130.8, 146.8, 164.8, 196])
            seg = slice(i0, min(n, i0 + int(beat * SR)))
            tt = t[seg] - t[i0]
            env = np.exp(-tt * rng.uniform(1.5, 4))
            for mult in (1, 1.25, 1.5, 2):
                x[seg] += env * np.sin(2 * np.pi * root * mult * tt) * rng.uniform(0.2, 0.5)
            hat = slice(i0, min(n, i0 + 900))
            x[hat] += rng.standard_normal(hat.stop - hat.start) * np.exp(-np.arange(hat.stop - hat.start) / 150) * 0.4
    elif kind == "parole":
        noise = rng.standard_normal(n)
        from scipy.signal import butter, sosfilt
        x = sosfilt(butter(4, [250, 3500], "bandpass", fs=SR, output="sos"), noise)
        syl = 0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(3, 5) * t)
        pauses = np.ones(n)
        for _ in range(int(dur / 3)):
            a = rng.integers(0, max(1, n - SR))
            pauses[a:a + int(rng.uniform(0.3, 0.9) * SR)] = 0.02
        x = x * syl ** 2 * pauses
    elif kind == "pub":
        x = np.tanh(3 * (np.sin(2 * np.pi * 220 * t) * 0.5 + rng.standard_normal(n) * 0.3
                         + np.sin(2 * np.pi * 2 * t) * 0.3))
    else:  # ambiance
        x = np.cumsum(rng.standard_normal(n))
        x = x - np.convolve(x, np.ones(2000) / 2000, mode="same")
        x = x / (np.abs(x).max() + 1e-9) * (0.4 + 0.3 * np.sin(2 * np.pi * 0.2 * t))
    x = x / (np.abs(x).max() + 1e-9) * 10 ** (level_db / 20)
    out = np.vstack([x * rng.uniform(0.7, 1.0) for _ in range(channels)])
    if channels == 6:
        out[3] *= 0.3                     # LFE
    return out.astype(np.float32)


def make(path: str, kind: str, level_db: float, dur: float, fmt: tuple, seed: int = 0) -> dict:
    ext, vcodec, acodec, layout = fmt
    rng = np.random.default_rng(seed)
    ch = 6 if layout == "5.1" else 2
    sig = audio(kind, dur, level_db, rng, ch)
    fps = rng.choice([Fraction(25), Fraction(30000, 1001), Fraction(50), Fraction(60000, 1001)])
    gop = int(rng.integers(12, 75))
    out = av.open(path, "w")
    vs = None
    if vcodec:
        vs = out.add_stream(vcodec, rate=fps)
        vs.width, vs.height, vs.pix_fmt = 320, 180, "yuv420p"
        if vcodec == "libx264":
            vs.options = {"crf": "26", "preset": "ultrafast", "g": str(gop), "keyint_min": str(gop)}
        else:
            vs.options = {"g": str(gop), "qscale": "6"}
            vs.codec_context.gop_size = gop
    as_ = out.add_stream(acodec, rate=SR)
    as_.layout = layout
    if acodec in ("aac", "libmp3lame", "libopus"):
        as_.bit_rate = 192000 if ch == 2 else 384000
    nv = int(dur * float(fps)) if vs else 0
    base = rng.uniform(0, 255, 3)
    yy, xx = np.mgrid[0:180, 0:320]
    for i in range(nv):
        ph = i / max(1, nv)
        img = np.empty((180, 320, 3), np.uint8)
        for c in range(3):
            img[..., c] = (base[c] + 120 * np.sin(xx / 30 + ph * 20 + c) + 60 * np.cos(yy / 20 - ph * 7)) % 255
        img[(i * 3) % 160:(i * 3) % 160 + 20, 140:180] = 255 * (i % 2)     # repère qui bouge
        fr = av.VideoFrame.from_ndarray(img, format="rgb24")
        fr.pts = i
        fr.time_base = Fraction(1, 1) / fps
        for p in vs.encode(fr):
            out.mux(p)
    if vs:
        for p in vs.encode(None):
            out.mux(p)
    step = 1024
    lay = "5.1" if ch == 6 else "stereo"
    for i in range(0, sig.shape[1], step):
        chunk = np.ascontiguousarray(sig[:, i:i + step])
        fr = av.AudioFrame.from_ndarray(chunk, format="fltp", layout=lay)
        fr.sample_rate = SR
        fr.pts = i
        fr.time_base = Fraction(1, SR)
        for p in as_.encode(fr):
            out.mux(p)
    for p in as_.encode(None):
        out.mux(p)
    out.close()
    return dict(path=path, kind=kind, level=level_db, fps=float(fps), gop=gop, ext=ext, layout=layout)


def batch(folder, n: int = 12, seed: int = 7, dur=(8.0, 20.0)) -> list[dict]:
    """n médias variés (nature, volume, format, durée) dans `folder`."""
    from pathlib import Path
    rng = np.random.default_rng(seed)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for k in range(n):
        fmt = FORMATS[k % len(FORMATS)]
        kind = KINDS[int(rng.integers(0, len(KINDS)))]
        level = float(rng.uniform(-40, -8))
        d = float(rng.uniform(*dur))
        p = folder / f"media{k:02d}_{kind}{'_51' if fmt[3] == '5.1' else ''}{fmt[0]}"
        out.append(make(str(p), kind, level, d, fmt, seed=seed * 100 + k))
    return out
