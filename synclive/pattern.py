"""Générateur de mires de synchro audio/vidéo SyncLive.

Principe : toutes les « périodes » (≈ 1 s, exactement round(fps) images) une image unique
de FLASH coïncide avec un BIP. Chaque source (PC, console…) porte un identifiant
(numéro + nom, ex. « 07 · FALCON ») affiché en clair ET dans un QR code : SyncLive lit le QR
pour savoir quelle machine est à l'image, même si toutes affichent la même mire.

La boucle contient un nombre entier de périodes : elle peut tourner en boucle sans couture
(VLC, OBS « source média » en boucle, navigateur, lecteur de régie…).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

AUDIO_RATE = 48000

FRAME_RATES: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001), "24": Fraction(24), "25": Fraction(25),
    "29.97": Fraction(30000, 1001), "30": Fraction(30), "48": Fraction(48),
    "50": Fraction(50), "59.94": Fraction(60000, 1001), "60": Fraction(60),
    "100": Fraction(100), "119.88": Fraction(120000, 1001), "120": Fraction(120),
}

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "SD NTSC 720×480": (720, 480),
    "SD PAL 720×576": (720, 576),
    "HD 1280×720": (1280, 720),
    "Full HD 1920×1080": (1920, 1080),
    "QHD 2560×1440": (2560, 1440),
    "UHD 4K 3840×2160": (3840, 2160),
    "DCI 4K 4096×2160": (4096, 2160),
    "Vertical 1080×1920": (1080, 1920),
    "Carré 1080×1080": (1080, 1080),
}

from .brand import CREAM, INK, ORANGE, lockup  # noqa: E402

ACCENT = ORANGE
BG = INK
TRACK = (46, 46, 52)
MUTED = (138, 135, 128)
DIM = (74, 74, 80)

SOURCE_WORDS = [
    "FALCON", "ORION", "TIGER", "NOVA", "COBRA", "LYNX", "ATLAS", "VEGA", "PUMA", "RAVEN",
    "COMET", "BISON", "DELTA", "EAGLE", "FOX", "GECKO", "HAWK", "IBIS", "JAGUAR", "KOALA",
    "LIMA", "MANGO", "NEON", "OSPREY", "PANDA", "QUARTZ", "ROCKET", "SIRIUS", "TITAN", "URSA",
    "VIPER", "WOLF", "XENON", "YETI", "ZEBRA", "AURORA", "BOREAL", "CEDAR", "DRACO", "EMBER",
    "FJORD", "GRIZZLY", "HALO", "IRIS", "JUNO", "KRAKEN", "LOTUS", "MAPLE", "NIMBUS", "ONYX",
    "PHOENIX", "QUASAR", "RIVER", "SAKURA", "TUNDRA", "UMBRA", "VORTEX", "WALRUS", "ZENITH", "ALBATROS",
]
QR_PREFIX = "SYNCLIVE"
MAX_SOURCES = 99

# Numérotation des flashs/bips sur un cycle : chaque bip a sa note, chaque flash son QR.
# Un bip n°k ne peut correspondre qu'au flash n°k : le décalage est mesuré sans ambiguïté
# jusqu'à ± CYCLE/2 périodes (± 4 s), même s'il dépasse une période entière.
CYCLE = 8
TONES = [1200 + 300 * k for k in range(CYCLE)]      # 1200, 1500 … 3300 Hz


def source_name(source: int) -> str:
    return SOURCE_WORDS[(source - 1) % len(SOURCE_WORDS)]


def source_label(source: int) -> str:
    return f"{source:02d} · {source_name(source)}"


def qr_payload(source: int, cycle_index: int | None = None) -> str:
    base = f"{QR_PREFIX}|{source:02d}|{source_name(source)}"
    return base if cycle_index is None else f"{base}|{cycle_index}"


def parse_qr_full(text: str) -> tuple[int, str, int | None] | None:
    """« SYNCLIVE|07|ATLAS|3 » -> (7, "ATLAS", 3)."""
    parts = (text or "").strip().split("|")
    if len(parts) >= 3 and parts[0] == QR_PREFIX and parts[1].isdigit():
        p = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else None
        return int(parts[1]), parts[2], p
    return None


def parse_qr(text: str) -> tuple[int, str] | None:
    r = parse_qr_full(text)
    return (r[0], r[1]) if r else None


def decode_qr(rgb: np.ndarray) -> tuple[int, str] | None:
    r = decode_qr_full(rgb)
    return (r[0], r[1]) if r else None


def tone_index(freq: float | None, tol: float = 90.0) -> int | None:
    """Numéro de bip à partir de sa fréquence mesurée."""
    if freq is None:
        return None
    k = int(np.argmin([abs(freq - f) for f in TONES]))
    return k if abs(freq - TONES[k]) <= tol else None


def decode_qr_full(rgb: np.ndarray) -> tuple[int, str, int | None] | None:
    """Cherche un QR SyncLive dans une image (même petite : agrandissement automatique)."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
    det = cv2.QRCodeDetector()
    w = gray.shape[1]
    tries = [gray]
    if w < 2600:
        f = min(3.0, 2600 / w)
        tries.append(cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC))
    for g in tries:
        try:
            r = parse_qr_full(det.detectAndDecode(g)[0])
        except cv2.error:
            r = None
        if r:
            return r
        try:
            ok, texts, _, _ = det.detectAndDecodeMulti(g)
        except cv2.error:
            ok, texts = False, []
        for t in (texts if ok else []):
            r = parse_qr_full(t)
            if r:
                return r
    return None


def qr_matrix(text: str) -> np.ndarray:
    """Matrice QR, 1 pixel par module (255 = blanc)."""
    return cv2.QRCodeEncoder.create().encode(text)


@dataclass
class PatternSpec:
    width: int
    height: int
    fps: Fraction
    source: int = 1
    periods: int = 16            # durée de la boucle en périodes (~ s), arrondie à un multiple de CYCLE
    container: str = "mp4"       # "mp4" (H.264 + AAC) | "mov" (H.264 + PCM 24 bits)

    @property
    def fps_label(self) -> str:
        for k, v in FRAME_RATES.items():
            if v == self.fps:
                return k
        return f"{float(self.fps):.3f}".rstrip("0").rstrip(".")

    @property
    def period_frames(self) -> int:
        return int(round(float(self.fps)))

    def filename(self) -> str:
        return (f"SyncLive_{self.source:02d}-{source_name(self.source)}_{self.width}x{self.height}_"
                f"{self.fps_label.replace('.', '-')}fps.{self.container}")


# ---------------------------------------------------------------------------
# rendu
# ---------------------------------------------------------------------------
def _font(size: int, weight: str = "semibold") -> ImageFont.ImageFont:
    names = {"bold": ["segoeuib.ttf", "arialbd.ttf"], "semibold": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"],
             "regular": ["segoeui.ttf", "arial.ttf"], "light": ["segoeuil.ttf", "segoeui.ttf", "arial.ttf"]}
    for n in names.get(weight, names["regular"]):
        try:
            return ImageFont.truetype(str(Path("C:/Windows/Fonts") / n), max(6, size))
        except OSError:
            continue
    try:
        return ImageFont.truetype("DejaVuSans.ttf", max(6, size))
    except OSError:
        return ImageFont.load_default()


def _text(d: ImageDraw.ImageDraw, xy, txt, font, fill, anchor="mm"):
    d.text(xy, txt, font=font, fill=fill, anchor=anchor)


class _Renderer:
    """Mire épurée : anneau de progression, QR + identifiant, règle en images."""

    def __init__(self, spec: PatternSpec):
        self.s = spec
        W, H = spec.width, spec.height
        self.W, self.H = W, H
        self.u = min(W, H) / 1080.0
        ratio = W / H
        if ratio >= 1.15:                    # paysage : anneau à gauche, identité à droite
            self.cx, self.cy = int(W * 0.36), int(H * 0.43)
            self.R = int(H * 0.26)
            self.q = int(min(H * 0.34, W * 0.28))
            self.qx, self.qy = int(W * 0.76), int(H * 0.37)
        elif ratio > 0.85:                   # carré
            self.cx, self.cy = int(W * 0.31), int(H * 0.40)
            self.R = int(min(W, H) * 0.23)
            self.q = int(W * 0.30)
            self.qx, self.qy = int(W * 0.76), int(H * 0.34)
        else:                                # vertical
            self.cx, self.cy = W // 2, int(H * 0.28)
            self.R = int(W * 0.27)
            self.q = int(W * 0.40)
            self.qx, self.qy = W // 2, int(H * 0.56)
        self.bg = self._background()

    # --- éléments fixes -------------------------------------------------------------
    def _identity(self, pil: Image.Image, d: ImageDraw.ImageDraw) -> None:
        u = self.u
        mats = [qr_matrix(qr_payload(self.s.source, k)) for k in range(CYCLE)]
        n = max(m.shape[0] for m in mats) + 8          # + zone de silence de 4 modules
        px = max(2, self.q // n)
        side = px * n
        x0, y0 = self.qx - side // 2, self.qy - side // 2
        d.rectangle([x0, y0, x0 + side - 1, y0 + side - 1], fill=(255, 255, 255))
        self.qr_imgs = []
        for m in mats:
            pad = n - 8 - m.shape[0]
            m = cv2.copyMakeBorder(m, 0, pad, 0, pad, cv2.BORDER_CONSTANT, value=255)
            big = cv2.resize(m, None, fx=px, fy=px, interpolation=cv2.INTER_NEAREST)
            self.qr_imgs.append(np.repeat(big[..., None], 3, axis=2))
        self.qr_pos = (x0 + 4 * px, y0 + 4 * px)
        # identifiant : grand numéro + nom
        ty = y0 + side + int(70 * u)
        _text(d, (self.qx, ty), f"{self.s.source:02d}", _font(int(96 * u), "semibold"), CREAM)
        _text(d, (self.qx, ty + int(78 * u)), " ".join(source_name(self.s.source)),
              _font(int(30 * u), "regular"), MUTED)
        # n° du bip : 8 traits fins, le courant en blanc
        self.dots_y = ty + int(128 * u)
        w = max(6, int(22 * u))
        gap = max(3, int(8 * u))
        total = CYCLE * w + (CYCLE - 1) * gap
        self.dots = [(int(self.qx - total / 2 + k * (w + gap)), w) for k in range(CYCLE)]
        self.dot_h = max(2, int(4 * u))

    def _background(self) -> np.ndarray:
        W, H, u = self.W, self.H, self.u
        img = np.empty((H, W, 3), np.uint8)
        img[:] = BG
        cv2.circle(img, (self.cx, self.cy), self.R, TRACK, max(2, int(6 * u)), cv2.LINE_AA)
        pil = Image.fromarray(img)
        d = ImageDraw.Draw(pil)
        m = int(48 * u)
        logo = lockup(max(12, int(40 * u)))
        pil.paste(logo, (m, m), logo)
        self._identity(pil, d)
        _text(d, (m, H - m), f"{self.W} × {self.H}   {self.s.fps_label} i/s", _font(int(22 * u), "regular"),
              MUTED, "lb")
        _text(d, (W - m, H - m), f"période {self.s.period_frames} images", _font(int(22 * u), "regular"),
              MUTED, "rb")
        return np.asarray(pil).copy()

    # --- images ------------------------------------------------------------------------
    def frame(self, i: int, cycle_index: int = 0) -> np.ndarray:
        """i = index dans la période (0 = image du flash) ; cycle_index = n° du flash (0…CYCLE-1)."""
        return self.decorate(self.base(i), cycle_index)

    def decorate(self, base: np.ndarray, cycle_index: int) -> np.ndarray:
        img = base.copy()
        q = self.qr_imgs[cycle_index % CYCLE]
        x, y = self.qr_pos
        img[y:y + q.shape[0], x:x + q.shape[1]] = q
        for k, (dx, w) in enumerate(self.dots):
            col = ORANGE if k == cycle_index % CYCLE else DIM
            cv2.rectangle(img, (dx, self.dots_y), (dx + w, self.dots_y + self.dot_h), col, -1)
        return img

    def base(self, i: int) -> np.ndarray:
        N = self.s.period_frames
        u, cx, cy, R = self.u, self.cx, self.cy, self.R
        img = self.bg.copy()
        flash = i == 0
        if flash:
            # FLASH : disque blanc plein, une seule image
            cv2.circle(img, (cx, cy), R + int(3 * u), CREAM, -1, cv2.LINE_AA)
        else:
            frac = i / N
            cv2.ellipse(img, (cx, cy), (R, R), -90, 0, 360 * frac, ORANGE, max(2, int(6 * u)),
                        cv2.LINE_AA)
        pil = Image.fromarray(img)
        d = ImageDraw.Draw(pil)
        rel = i if i <= N // 2 else i - N
        if flash:
            _text(d, (cx, cy), "0", _font(int(220 * u), "semibold"), BG)
        else:
            _text(d, (cx, cy - int(6 * u)), f"{rel:+d}", _font(int(170 * u), "light"), CREAM)
            _text(d, (cx, cy + int(112 * u)), "images", _font(int(24 * u), "regular"), MUTED)
        self._ruler(d, rel)
        return np.asarray(pil)

    def _ruler(self, d: ImageDraw.ImageDraw, rel: int) -> None:
        """Règle −15…+15 images. Au moment où l'on ENTEND le bip, la position du curseur
        donne le décalage en images (+ : son en retard)."""
        W, H, u = self.W, self.H, self.u
        n = 15
        y = int(H * (0.80 if H >= W else 0.82))
        span = W * (0.84 if W > H else 0.88)
        x0 = (W - span) / 2
        step = span / (2 * n)
        lw = max(1, int(2 * u))
        d.line([(x0, y), (x0 + span, y)], fill=TRACK, width=lw)
        f = _font(int(20 * u), "regular")
        for k in range(-n, n + 1):
            x = x0 + (k + n) * step
            major = k % 5 == 0
            h = int((16 if major else 8) * u)
            d.line([(x, y - h), (x, y)], fill=MUTED if major else DIM, width=lw)
            if major:
                _text(d, (x, y + int(26 * u)), "0" if k == 0 else f"{k:+d}", f, MUTED)
        if -n <= rel <= n:
            x = x0 + (rel + n) * step
            cw = max(3, int(6 * u))
            d.rectangle([x - cw / 2, y - int(34 * u), x + cw / 2, y + int(2 * u)], fill=ORANGE)
        _text(d, (W / 2, y + int(64 * u)), "position du curseur au bip = décalage en images   ( + son en retard )",
              _font(int(20 * u), "regular"), DIM)


def _beep(n: int, cycle_index: int = 0) -> np.ndarray:
    """Bip court très net (attaque 1 ms, décroissance rapide), note propre à son numéro."""
    t = np.arange(n) / AUDIO_RATE
    env = np.minimum(1.0, t / 0.001) * np.exp(-t / 0.022)
    tone = np.sin(2 * np.pi * TONES[cycle_index % CYCLE] * t)
    return (0.5 * env * tone).astype(np.float32)   # crête ≈ -6 dBFS


def render(spec: PatternSpec, out_path: str | Path, progress: Callable[[float], None] | None = None,
           cancel: Callable[[], bool] | None = None) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    N = spec.period_frames
    periods = -(-spec.periods // CYCLE) * CYCLE          # multiple du cycle : boucle sans couture
    total = N * periods
    rnd = _Renderer(spec)
    # les images de base d'une période sont mises en cache si la mémoire le permet
    cache_ok = N * spec.width * spec.height * 3 <= 700_000_000
    bases = [rnd.base(i) for i in range(N)] if cache_ok else None

    # audio : longueur exacte de la vidéo, bip (note n° p) à l'image 0 de chaque période
    n_audio = int(round(total * AUDIO_RATE / spec.fps))
    audio = np.zeros(n_audio, np.float32)
    for p in range(periods):
        beep = _beep(int(0.12 * AUDIO_RATE), p)
        i0 = int(round(p * N * AUDIO_RATE / spec.fps))
        seg = audio[i0:i0 + beep.size]
        seg += beep[: seg.size]

    out = av.open(str(out_path), "w")
    vs = out.add_stream("libx264", rate=spec.fps)
    vs.width, vs.height, vs.pix_fmt = spec.width, spec.height, "yuv420p"
    vs.options = {"crf": "16", "preset": "fast", "g": str(N), "keyint_min": str(N),
                  "x264-params": "scenecut=0:colorprim=bt709:transfer=bt709:colormatrix=bt709"}
    if spec.container == "mov":
        as_ = out.add_stream("pcm_s24le", rate=AUDIO_RATE)
    else:
        as_ = out.add_stream("aac", rate=AUDIO_RATE)
        as_.bit_rate = 192000
    as_.layout = "stereo"

    for k in range(total):
        if cancel and cancel():
            out.close()
            out_path.unlink(missing_ok=True)
            raise InterruptedError
        # le QR (n° du flash) bascule à mi-période : au flash n°p, il affiche p sans perturber le flash
        i, p = k % N, (k + N // 2) // N
        rgb = rnd.decorate(bases[i] if bases is not None else rnd.base(i), p)
        fr = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(
            format="yuv420p", dst_colorspace="ITU709", dst_color_range="MPEG")
        fr.pts = k
        fr.time_base = 1 / spec.fps
        for pkt in vs.encode(fr):
            out.mux(pkt)
        if progress and k % 10 == 0:
            progress(0.95 * k / total)
    for pkt in vs.encode():
        out.mux(pkt)

    fmt = "s32" if spec.container == "mov" else "fltp"
    chunk = 1024
    stereo = np.stack([audio, audio])
    for i in range(0, n_audio, chunk):
        blk = stereo[:, i:i + chunk]
        if fmt == "s32":
            data = (np.clip(blk, -1, 1) * 2147483647).astype(np.int32)
            af = av.AudioFrame.from_ndarray(data.T.reshape(1, -1).copy(), format="s32", layout="stereo")
        else:
            af = av.AudioFrame.from_ndarray(np.ascontiguousarray(blk), format="fltp", layout="stereo")
        af.sample_rate = AUDIO_RATE
        af.pts = i
        af.time_base = Fraction(1, AUDIO_RATE)
        for pkt in as_.encode(af):
            out.mux(pkt)
    for pkt in as_.encode():
        out.mux(pkt)
    out.close()
    if progress:
        progress(1.0)
    return out_path


def preview(spec: PatternSpec, i: int = 0, cycle_index: int = 3) -> np.ndarray:
    return _Renderer(spec).frame(i % spec.period_frames, cycle_index)
