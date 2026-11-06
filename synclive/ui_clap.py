# SPDX-License-Identifier: GPL-3.0-or-later
"""Réglage manuel de l'image du clap : lecteur (timeline, ×1, ×1/2, ×1/4) et bande d'images autour
du clap ; on choisit l'image où les mains se touchent (ou où le flash apparaît). Le décalage devient son du clap − instant de
cette image, à ± ½ image près."""
from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QElapsedTimer, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

from . import analysis, export, media
from .ui_audio import AudioPlayer, Timeline
from .shots import DUP_DIFF

STRIP = 9            # images visibles dans la bande (par défaut)
STRIPS = (9, 15, 21)
HALF_SPAN = 1.0      # s chargées de part et d'autre du centre (par défaut)
SPANS = (1.0, 2.0, 4.0)
PREVIEW_W = 760
THUMB_W = 132


def _qpix(img: np.ndarray, width: int) -> QPixmap:
    h, w = img.shape[:2]
    hh = max(1, int(round(h * width / w)))
    small = cv2.resize(img, (width, hh), interpolation=cv2.INTER_AREA)
    small = np.ascontiguousarray(small)
    q = QImage(small.data, width, hh, 3 * width, QImage.Format_RGB888)
    return QPixmap.fromImage(q.copy())


class FrameView(QWidget):
    """La vidéo : l'image est toujours ajustée à la zone disponible (proportions gardées), quelle
    que soit sa définition et à tout moment (ouverture, clic, fenêtre redimensionnée). La molette
    passe à l'image précédente / suivante (signal `step`)."""

    step = Signal(int)

    def __init__(self, parent=None, background: str | None = None):
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._img: QImage | None = None
        self._text = ""
        self._bg = QColor(background) if background else None

    def set_frame(self, img: np.ndarray) -> None:
        img = np.ascontiguousarray(img)
        h, w = img.shape[:2]
        self._img = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self._text = ""
        self.update()

    def set_qimage(self, img: QImage) -> None:
        """Image déjà prête (images du lecteur Qt) : même affichage que les nôtres."""
        self._img = img
        self._text = ""
        self.update()

    def set_message(self, text: str) -> None:
        self._img, self._text = None, text
        self.update()

    def has_frame(self) -> bool:
        return self._img is not None

    def drawn(self) -> QRectF:
        """Rectangle où l'image est dessinée."""
        if self._img is None:
            return QRectF()
        iw, ih = self._img.width(), self._img.height()
        sc = min(self.width() / iw, self.height() / ih)
        w, h = iw * sc, ih * sc
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, _e):
        p = QPainter(self)
        if self._bg is not None:
            p.fillRect(self.rect(), self._bg)
        if self._img is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(self.drawn(), self._img)
        elif self._text:
            p.setPen(QColor("#8A8780"))
            p.drawText(self.rect(), Qt.AlignCenter, self._text)
        p.end()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if dy:
            self.step.emit(-1 if dy > 0 else 1)
        e.accept()


class NoWheelComboBox(QComboBox):
    """Liste qui ignore la molette : passer dessus ne change ni le clap, ni la bande, ni la durée."""

    def wheelEvent(self, e):
        e.ignore()


def brighten(img: np.ndarray) -> np.ndarray:
    """Image sombre rendue lisible : gain (blancs au 97e centile) + gamma qui remonte les ombres."""
    top = max(float(np.percentile(img[::4, ::4], 97)), 1.0)
    # table de 256 valeurs : rapide assez pour la lecture à 60 i/s
    lut = (np.clip(np.arange(256, dtype=np.float32) / top, 0.0, 1.0) ** 0.55 * 255.0).astype(np.uint8)
    return cv2.LUT(np.ascontiguousarray(img), lut)


def duplicates(frames: list[np.ndarray]) -> list[bool]:
    """Image répétée (webcam lente) : quasi identique à la précédente, même critère que l'analyse."""
    out = [False]
    prev = None
    for f in frames:
        t = cv2.resize(f, (media.THUMB_W, media.THUMB_H), interpolation=cv2.INTER_AREA).astype(np.int16)
        if prev is not None:
            out.append(float(np.abs(t - prev).mean() / 255.0) < DUP_DIFF)
        prev = t
    return out[:len(frames)]


def motion_box(frames: list[np.ndarray], lo: int, hi: int) -> tuple[int, int, int, int] | None:
    """Zone du geste (les mains) entre les images lo..hi, élargie, au format de l'image.

    Un décor qui bouge tout le temps (flamme animée, écran) est retiré : on garde ce qui bouge
    autour du clap nettement plus que d'habitude sur toute la séquence chargée."""
    if hi - lo < 2 or len(frames) < 4:
        return None
    H, W = frames[0].shape[:2]
    sw, sh = 128, max(1, int(round(128 * H / W)))
    g = [cv2.cvtColor(cv2.resize(f, (sw, sh), interpolation=cv2.INTER_AREA),
                      cv2.COLOR_RGB2GRAY).astype(np.float32) for f in frames]
    d = np.stack([np.abs(b - a) for a, b in zip(g, g[1:])])          # d[i] : image i -> i+1
    usual = np.median(d, axis=0)
    local = d[max(0, lo):max(lo + 1, hi)].max(axis=0)
    acc = cv2.GaussianBlur(np.maximum(local - 2.0 * usual - 4.0, 0.0), (0, 0), 2.0)
    if acc.max() < 6.0:
        return None
    # composante qui contient le maximum : le geste principal, pas un reflet ailleurs
    n, lab = cv2.connectedComponents((acc >= 0.5 * acc.max()).astype(np.uint8))
    yx = np.unravel_index(int(np.argmax(acc)), acc.shape)
    ys, xs = np.nonzero(lab == lab[yx])
    cx, cy = (xs.min() + xs.max() + 1) / 2 / sw, (ys.min() + ys.max() + 1) / 2 / sh
    return _square(xs.min() / sw, ys.min() / sh, (xs.max() + 1) / sw, (ys.max() + 1) / sh, W, H, grow=1.4)


def _square(x0: float, y0: float, x1: float, y1: float, W: int, H: int,
            grow: float, min_s: float = 0.3) -> tuple[int, int, int, int] | None:
    """Cadre au format de l'image autour de la boîte normalisée, agrandi de `grow`."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    s = max(max(x1 - x0, y1 - y0) * grow, min_s)
    if s >= 0.85:
        return None
    bx = int(np.clip(cx - s / 2, 0, 1 - s) * W)
    by = int(np.clip(cy - s / 2, 0, 1 - s) * H)
    return bx, by, bx + int(s * W), by + int(s * H)


def hand_box(locator, frames: list[np.ndarray], k: int, reach: int = 4) -> tuple[int, int, int, int] | None:
    """Cadre sur les mains (MediaPipe) de l'image k, ou de la plus proche où elles sont trouvées
    (au contact elles sont souvent floues ; pendant l'élan elles traversent l'image)."""
    if locator is None or not locator.available or not frames:
        return None
    H, W = frames[0].shape[:2]
    for d in sorted(range(-reach, reach + 1), key=abs):
        j = k + d
        if 0 <= j < len(frames):
            got = locator.boxes(frames[j])
            if got:
                a = np.array(got)
                return _square(float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 2].max()),
                               float(a[:, 3].max()), W, H, grow=1.8, min_s=0.25)
    return None


class ClapFrameDialog(QDialog):
    """Une source : chaque clap a sa timeline et sa bande d'images. Le résultat est `self.chosen`
    {id du clap: instant choisi ou None pour revenir à l'automatique}.

    `self.cur` est l'image affichée. Hors lecture et hors glisser, c'est l'image choisie : clic
    ou relâchement sur la timeline, clic dans la bande, flèches ← →, pause."""

    def __init__(self, an: analysis.Analysis, cam: analysis.CameraResult, parent=None):
        super().__init__(parent)
        self.an, self.cam = an, cam
        self.fps = an.info.fps_float
        self.setWindowTitle(f"Régler l'image du clap — {export.display_name(cam)}")
        self.claps = [c for c in cam.claps]
        self.chosen: dict[int, float | None] = {c.id: c.manual_t for c in self.claps}
        self.last_changed: int | None = None     # clap réglé en dernier (image montrée sur la carte)
        self._cache: dict[int, tuple[list[float], list[np.ndarray], list[bool]]] = {}
        self._boxes: dict[int, tuple[int, int, int, int] | None] = {}
        self._series: dict[int, list[int]] = {}
        self._auto_k: int | None = None
        self._locator = None
        self._slots: list[int] = []
        self._notice = ""
        self._switching = False
        self._audio = AudioPlayer()
        self._snd: dict[int, tuple[np.ndarray, int, float]] = {}
        self._playing = False
        self.cur = 0          # index de l'image affichée dans la séquence chargée

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Clap :"))
        self.clap_box = NoWheelComboBox()
        for c in self.claps:
            self.clap_box.addItem(self._clap_label(c), c.id)
        self.clap_box.currentIndexChanged.connect(lambda _i: self._clap_changed())
        top.addWidget(self.clap_box, 1)
        self.zoom = QCheckBox("Zoom sur les mains")
        self.zoom.setChecked(False)      # décoché d'office : l'image entière, le zoom au besoin
        self.zoom.toggled.connect(lambda _b: self._show())
        top.addWidget(self.zoom)
        self.bright = QCheckBox("Éclaircir")
        self.bright.setToolTip("Rend lisibles les images sombres (plateau éteint, contre-jour)")
        self.bright.toggled.connect(lambda _b: self._show())
        top.addWidget(self.bright)
        # caméra plus lente que le fichier (20 i/s dans un fichier à 60) : le fichier recopie chaque
        # image 2 ou 3 fois ; la bande et les flèches ne montrent que les vraies images
        self._hide = bool(cam.source_fps)
        v.addLayout(top)

        help_ = QLabel("Choisis l'image où les mains se touchent (ou où le flash apparaît) : clique ou glisse "
                       "sur la timeline (l'image où tu relâches est choisie), clique une vignette ou utilise "
                       "← →. ▶ / Espace : lecture avec le son, l'image où tu mets pause est choisie.")
        help_.setWordWrap(True)
        help_.setStyleSheet("color:#8A8780;")
        v.addWidget(help_)

        self.preview = FrameView()
        self.preview.step.connect(self._step)
        v.addWidget(self.preview, 1)

        # --- lecteur : lecture / pause (avec le son), timeline, vitesse ------------------
        player = QHBoxLayout()
        player.setSpacing(12)
        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("play")
        self.play_btn.setFixedSize(46, 46)
        self.play_btn.setToolTip("Lecture avec le son / pause (Espace). L'image où tu mets pause est choisie.")
        self.play_btn.setStyleSheet(
            "QPushButton#play { background:#FF5A1F; color:#16161A; border:none; border-radius:23px;"
            " font-size:18px; font-weight:700; padding:0; }"
            "QPushButton#play:hover { background:#FF7240; }")
        self.play_btn.clicked.connect(self._toggle_play)
        player.addWidget(self.play_btn, 0, Qt.AlignVCenter)
        # une seule timeline : le son, les sons de clap détectés, l'image choisie, la tête de lecture
        self.timeline = Timeline()
        self.timeline.scrub.connect(self._scrub)
        self.timeline.released.connect(self._choose_time)
        self.timeline.step.connect(self._step)
        player.addWidget(self.timeline, 1)
        side = QVBoxLayout()
        side.setSpacing(6)
        self.time_lbl = QLabel()
        self.time_lbl.setAlignment(Qt.AlignCenter)
        self.time_lbl.setFixedWidth(118)
        self.time_lbl.setStyleSheet("font-size:16px; font-weight:600; font-family:Consolas, 'Segoe UI';")
        side.addWidget(self.time_lbl)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self._speed = 1.0
        self.speed_btns: list[QToolButton] = []
        for j, (lab, val) in enumerate((("×1", 1.0), ("×½", 0.5), ("×¼", 0.25))):
            b = QToolButton()
            b.setText(lab)
            b.setCheckable(True)
            b.setChecked(j == 0)
            b.setToolTip({1.0: "Vitesse normale", 0.5: "Ralenti ×1/2", 0.25: "Ralenti ×1/4"}[val])
            left, right = ("6px", "0") if j == 0 else (("0", "6px") if j == 2 else ("0", "0"))
            b.setStyleSheet(
                "QToolButton { background:#1E1E23; color:#B8B5AE; border:1px solid #303037; padding:4px 0;"
                f" min-width:38px; border-top-left-radius:{left}; border-bottom-left-radius:{left};"
                f" border-top-right-radius:{right}; border-bottom-right-radius:{right}; }}"
                "QToolButton:hover { background:#24242A; }"
                "QToolButton:checked { background:#2E2E35; color:#FF5A1F; font-weight:600; border-color:#45454D; }")
            b.clicked.connect(lambda _=False, val=val: self._set_speed(val))
            seg.addWidget(b)
            self.speed_btns.append(b)
        side.addLayout(seg)
        player.addLayout(side)
        v.addLayout(player)
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._play_tick)
        self._clock = QElapsedTimer()
        self._play_t0 = 0.0

        self.info = QLabel()
        self.info.setAlignment(Qt.AlignCenter)
        self.info.setWordWrap(True)
        self.info.setStyleSheet("font-size:15px; font-weight:600;")
        # hauteur fixe (4 lignes) : le texte qui passe de 1 à 4 lignes ne fait plus sauter la vidéo
        self.info.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        f = self.info.font()
        f.setPixelSize(15)
        f.setWeight(QFont.DemiBold)
        self.info.setFixedHeight(QFontMetrics(f).lineSpacing() * 4 + 6)
        self.info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        v.addWidget(self.info)

        strip = QHBoxLayout()
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setToolTip("Image précédente (←)")
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        strip.addWidget(self.prev_btn)
        self.thumb_row = QHBoxLayout()
        strip.addLayout(self.thumb_row, 1)
        self.thumbs: list[QToolButton] = []
        self.next_btn = QPushButton("▶")
        self.next_btn.setToolTip("Image suivante (→)")
        self.next_btn.clicked.connect(lambda: self._step(1))
        strip.addWidget(self.next_btn)
        v.addLayout(strip)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Images dans la bande :"))
        self.strip_box = NoWheelComboBox()
        for n in STRIPS:
            self.strip_box.addItem(str(n), n)
        self.strip_box.currentIndexChanged.connect(lambda _i: self._build_strip())
        opts.addWidget(self.strip_box)
        opts.addSpacing(16)
        opts.addWidget(QLabel("Durée chargée :"))
        self.span_box = NoWheelComboBox()
        for sp in SPANS:
            self.span_box.addItem(f"± {sp:g} s autour du son", sp)
        self.span_box.currentIndexChanged.connect(lambda _i: self._reload())
        opts.addWidget(self.span_box)
        opts.addStretch(1)
        self.auto_btn = QPushButton("Revenir à l'automatique pour ce clap")
        self.auto_btn.clicked.connect(self._reset_auto)
        opts.addWidget(self.auto_btn)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Valider")
        ok.setObjectName("primary")
        ok.setAutoDefault(False)
        ok.clicked.connect(self.accept)
        opts.addWidget(cancel)
        opts.addWidget(ok)
        v.addLayout(opts)
        self._build_strip(show=False)

        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self._step(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self._step(1))
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self._toggle_play)
        # même clap que l'image de la carte, sinon le premier clap retenu
        shown = analysis.shown_clap(an, cam.image_shot) if cam.image_shot is not None else None
        first = next((i for i, c in enumerate(self.claps) if c is shown),
                     next((i for i, c in enumerate(self.claps) if c.selected is not None), 0))
        self.clap_box.setCurrentIndex(first)
        if first == 0:
            self._load()

    def fit_to(self, scr) -> None:
        """Taille d'ouverture : aussi haute que l'écran le permet (88 %), et juste assez large
        pour la vidéo à cette hauteur (pas toute la largeur d'un écran large), centrée."""
        frames = self._data()[1]
        aspect = frames[0].shape[1] / frames[0].shape[0] if frames else 16 / 9
        h = min(scr.height(), max(700, int(scr.height() * 0.88)))
        w = 1000
        for _ in range(3):                      # la bande change un peu de hauteur avec la largeur
            self.resize(w, h)
            self.layout().activate()
            self._show()
            self.layout().activate()
            want = int(self.preview.height() * aspect) + 2 * self.layout().contentsMargins().left() + 4
            w = int(min(max(want, 1000), scr.width() * 0.92))   # petit écran : sa largeur d'abord
        self.resize(w, h)
        self.move(scr.center() - self.rect().center())

    def _hands(self):
        if self._locator is None and self.cam.kind == "camera":
            from .hands import HandLocator
            self._locator = HandLocator()
        return self._locator

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._cache:
            self._show()

    def done(self, r: int) -> None:
        self._timer.stop()
        self._audio.close()
        if self._locator is not None:
            self._locator.close()
        super().done(r)

    # --- données ---------------------------------------------------------------
    def _clap(self) -> analysis.Clap:
        return self.claps[max(0, self.clap_box.currentIndex())]

    def _data(self):
        return self._cache.get(self._clap().id, ([], [], []))

    def _clap_label(self, c: analysis.Clap) -> str:
        t = self.chosen.get(c.id)
        if t is not None:
            how = f"à la main : {export.fmt_ms(c.t_audio - t)}"
        elif c.selected is not None and c.manual_t is None:
            how = f"auto : {export.fmt_ms(c.offset)}"
        else:
            how = "pas d'image retenue"
        ex = " · écarté" if c.excluded else ""
        return f"son à {c.t_audio:.3f} s — {how}{ex}"

    def _auto_t(self, c: analysis.Clap) -> float | None:
        """Instant automatique (hors choix manuel) : candidat retenu, sinon décalage de la source."""
        if c.selected is not None and c.manual_t is None:
            return c.selected.t
        if c.candidates:
            off = self.cam.offset
            if off is not None:
                return min(c.candidates, key=lambda v: abs(c.t_audio - v.t - off)).t
            return max(c.candidates, key=lambda v: v.strength).t
        return None

    def _index_at(self, times: list[float], t: float) -> int:
        """Image affichée à l'instant t : la dernière dont l'horodatage est ≤ t (¼ d'image de marge)."""
        k = int(np.searchsorted(times, t + 0.25 / self.fps, side="right")) - 1
        return int(np.clip(k, 0, len(times) - 1))

    def _index_near(self, times: list[float], t: float) -> int:
        """Image la plus proche de t, choisie comme pour l'image de la carte (même image)."""
        k = int(np.searchsorted(np.asarray(times) + 0.5 / self.fps, t, side="left"))
        return int(np.clip(k, 0, len(times) - 1))

    def _first_of_series(self, k: int) -> int:
        """Image répétée : sa première apparition (c'est elle que la caméra a prise). On ne remonte
        pas plus loin que la cadence réelle de la caméra : dans un plan immobile, deux vraies images
        successives se ressemblent aussi."""
        starts = self._starts()
        return starts[k] if 0 <= k < len(starts) else k

    def _starts(self) -> list[int]:
        """Pour chaque image, l'indice de la première image de sa série de répétitions. Calculé une
        fois, en avançant : une série ne dure pas plus d'une image réelle de la caméra."""
        c = self._clap()
        got = self._series.get(c.id)
        if got is not None:
            return got
        times, _f, dups = self._data()
        src = self.cam.source_fps
        reach = max(0, int(round(self.fps / src)) - 1) if src else 0
        out, start = [], 0
        for i, dup in enumerate(dups):
            # coupure sur le n° absolu de l'image dans un plan immobile : les séries sont les mêmes
            # quel que soit le passage chargé (sinon une image change de série d'un clap à l'autre)
            absn = int(round(times[i] * self.fps))
            if not dup or i - start > reach or absn % (reach + 1) == 0:
                start = i
            out.append(start)
        self._series[c.id] = out
        return out

    def _t(self, i: int) -> float:
        """Instant d'une image tel qu'il sera retenu si on la choisit (première apparition)."""
        return self._data()[0][self._first_of_series(i)]

    def _clap_changed(self) -> None:
        if not self._switching:
            self._notice = ""
        self._load()

    def _load(self) -> None:
        self._pause()
        c = self._clap()
        if c.id not in self._cache:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                center = c.t_audio - (self.cam.offset or 0.0)
                shot = self.an.shots[c.shot]
                span = float(self.span_box.currentData() or HALF_SPAN)
                t0 = max(shot.start, center - span)
                t1 = min(shot.end, center + span)
                # plus la durée chargée est longue, plus les images sont réduites (mémoire)
                width = {1.0: 1280, 2.0: 960}.get(span, 720)
                times, frames = media.decode_range(self.an.info.path, t0, t1, shot.region, max_width=width)
            finally:
                QApplication.restoreOverrideCursor()
            # répétitions signalées seulement si la source est une caméra lente : dans un décor
            # fixe, deux images sans mouvement se ressemblent aussi
            dups = duplicates(frames) if self.cam.source_fps else [False] * len(frames)
            self._cache[c.id] = (times, frames, dups)
            if times:
                try:
                    self._snd[c.id] = media.decode_audio_range(
                        self.an.info.path, self.an.settings.audio_track, times[0], times[-1] + 1.0 / self.fps)
                except Exception:
                    self._snd[c.id] = (np.zeros(0, np.float32), 48000, times[0])
        times = self._cache[c.id][0]
        if times:
            sig, rate, t_sig = self._snd.get(c.id, (np.zeros(0, np.float32), 48000, times[0]))
            in_range = [(o.t_audio, o is c) for o in self.an.claps
                        if times[0] <= o.t_audio <= times[-1] and not o.method.startswith("mire") or o is c]
            self.timeline.set_data(sig, rate, t_sig, times[0], times[-1], in_range,
                                   frames=[times[i] for i in self._visible()], ref=c.t_audio)
        if not times:
            self.cur = 0
            self._show()
            return
        # image automatique : exactement celle de la carte (juste après le contact)
        a = self._auto_t(c)
        if a is not None and c.selected is not None and c.manual_t is None:
            a_img = analysis.clap_image_t(self.an, c)
        elif a is not None:
            a_img = a + (0.0 if self.cam.kind == "mire" else 1.5 / self.fps)
        else:
            a_img = None
        self._auto_k = None if a_img is None else self._first_of_series(self._index_near(times, a_img))
        self.cur = self._rest_index()
        self._show()

    # --- affichage ---------------------------------------------------------------
    def _box(self):
        if not self.zoom.isChecked():
            return None
        c = self._clap()
        times, frames, _d = self._data()
        if c.id not in self._boxes and frames:
            ref = self._auto_k if self._auto_k is not None and not self.cam.ambiguous else self.cur
            # mains au moment du contact seulement : pendant l'élan elles traversent l'image
            hb = hand_box(self._hands(), frames, ref)
            self._boxes[c.id] = hb or motion_box(frames, max(0, ref - 6), min(len(frames) - 1, ref + 2))
        return self._boxes.get(c.id)

    def _show_frame(self) -> None:
        """Grande image, timeline et ligne d'info pour l'image affichée (rapide : lecture)."""
        c = self._clap()
        times, frames, _d = self._data()
        if not frames:
            return
        k = self.cur
        big = self._img(frames[k], self._box())
        self.preview.set_frame(big)
        tk = self._t(k)
        self.time_lbl.setText(f"{tk:.3f} s")
        self.timeline.set_marks(times[k], self.chosen.get(c.id))
        chosen = self.chosen.get(c.id)
        is_chosen = chosen is not None and abs(chosen - tk) < 0.25 / self.fps
        tag = "image choisie" if is_chosen else "image affichée"
        txt = f"{tag} : {tk:.3f} s · son du clap : {c.t_audio:.3f} s  →  {self._verdict(c.t_audio - tk)}"
        if chosen is not None and not is_chosen:
            txt += f"\n(image choisie pour ce clap : {chosen:.3f} s, {export.fmt_ms(c.t_audio - chosen)})"
        other = self._pair(c, tk)
        if not is_chosen and other is not c:
            txt += (f"\n(cette image ira avec le son de clap de {other.t_audio:.3f} s, plus proche : "
                    f"{self._verdict(other.t_audio - tk)})")
        if self._notice:
            txt += f"\n{self._notice}"
        a = self._auto_t(c)
        if chosen is None and k == self._auto_k and a is not None:
            # l'automatique situe le contact entre deux images : sa valeur diffère un peu
            txt += f"\n(mesure automatique : {export.fmt_ms(c.t_audio - a)}, contact situé entre deux images)"
        self.info.setText("\n".join(txt.split("\n")[:4]))     # place réservée : 4 lignes

    def _show(self) -> None:
        c = self._clap()
        times, frames, dups = self._data()
        if not frames:
            self.preview.set_message("Images introuvables autour de ce clap.")
            self.info.setText("")
            for b in self.thumbs:
                b.setVisible(False)
            return
        self._show_frame()
        box = self._box()
        k = self.cur
        ref = frames[k] if box is None else frames[k][box[1]:box[3], box[0]:box[2]]
        n_strip = len(self.thumbs)
        tw = int(np.clip((self.width() - 160) // n_strip - 14, 56, 320))
        ih = int(tw * ref.shape[0] / ref.shape[1])
        line = QFontMetrics(self.thumbs[0].font()).lineSpacing() if self.thumbs else 16
        for b in self.thumbs:
            # taille fixe (image + 2 lignes) : un libellé plus long ne déplace ni la bande ni la vidéo
            b.setIconSize(QSize(tw, ih))
            b.setFixedSize(tw + 12, ih + 2 * line + 16)
        chosen = self.chosen.get(c.id)
        vis = self._visible()
        ks = self._first_of_series(k)     # image affichée = sa vraie image (pas une copie)
        self._slots = self._window(vis, ks)
        for j, b in enumerate(self.thumbs):
            if j >= len(self._slots):
                b.setVisible(False)
                continue
            i = self._slots[j]
            b.setVisible(True)
            img = self._img(frames[i], box)
            b.setIcon(_qpix(img, tw))
            marks = []
            ti = self._t(i)
            is_chosen = chosen is not None and abs(chosen - ti) < 0.25 / self.fps
            # l'image choisie appartient à ce clap : sa valeur est celle de ce son
            pair = c if is_chosen else self._pair(c, ti)
            if is_chosen:
                marks.append("choisie")
            if i == self._auto_k:
                marks.append("auto")
            if pair is not c:
                marks.insert(0, f"son {pair.t_audio:.2f}".replace(".", ","))
            # valeur obtenue si on clique cette vignette (avec le son auquel elle sera rattachée)
            b.setText(f"{export.fmt_ms(pair.t_audio - ti)}" + (f"\n{' · '.join(marks)}" if marks else "\n"))
            color = "#FF5A1F" if i == ks else ("#3FB950" if "choisie" in marks else "transparent")
            b.setStyleSheet("QToolButton { border: 3px solid %s; border-radius: 4px; padding: 2px; }" % color)
        self.prev_btn.setEnabled(bool(vis) and ks > vis[0])
        self.next_btn.setEnabled(bool(vis) and ks < vis[-1])

    def _img(self, img: np.ndarray, box) -> np.ndarray:
        if box is not None:
            img = img[box[1]:box[3], box[0]:box[2]]
        return brighten(img) if self.bright.isChecked() else img

    def _visible(self) -> list[int]:
        """Images de la bande : les vraies images de la caméra (pas les copies du fichier)."""
        times = self._data()[0]
        if self._hide:
            return sorted(set(self._starts()))
        return list(range(len(times)))

    def _window(self, vis: list[int], k: int) -> list[int]:
        n = len(self.thumbs)
        if not vis:
            return []
        pos = int(np.searchsorted(vis, k))
        lo = int(np.clip(pos - n // 2, 0, max(0, len(vis) - n)))
        return vis[lo:lo + n]

    def _build_strip(self, show: bool = True) -> None:
        n = int(self.strip_box.currentData() or STRIP)
        for b in self.thumbs:
            self.thumb_row.removeWidget(b)
            b.hide()
            b.setParent(None)
            b.deleteLater()
        self.thumbs = []
        for j in range(n):
            b = QToolButton()
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(THUMB_W, int(THUMB_W * 9 / 16)))
            b.clicked.connect(lambda _=False, j=j: self._pick_slot(j))
            self.thumb_row.addWidget(b)
            self.thumbs.append(b)
        self._slots = []
        if show and self._cache:
            self._show()

    def _reload(self) -> None:
        """Nouvelle durée chargée autour du son : on relit les images (le choix est gardé)."""
        self._cache.clear()
        self._snd.clear()
        self._boxes.clear()
        self._series.clear()
        self._load()

    def _verdict(self, off: float) -> str:
        ms = round(off * 1000)
        if ms == 0:
            return "synchro"
        who = "IMAGE EN RETARD" if off < 0 else "SON EN RETARD"
        return f"{who} de {export.both(off, self.fps)}"

    # --- lecteur -------------------------------------------------------------------
    def _scrub(self, t: float) -> None:
        """Appui ou glisser sur la timeline : l'image suit (elle est choisie au relâchement)."""
        self._pause()
        times = self._data()[0]
        if times:
            self.cur = self._index_at(times, t)
            self._show_frame()

    def _choose_time(self, t: float) -> None:
        times = self._data()[0]
        if times:
            self._choose(self._index_at(times, t))

    def _set_speed(self, val: float) -> None:
        self._speed = val
        for b, v in zip(self.speed_btns, (1.0, 0.5, 0.25)):
            b.setChecked(v == val)
        self._restart_clock()

    def _toggle_play(self) -> None:
        """▶ : lecture avec le son depuis l'image affichée. ⏸ : pause, l'image affichée est choisie."""
        if self._playing:
            self._choose(self.cur)
            return
        times = self._data()[0]
        if len(times) < 2:
            return
        if self.cur >= len(times) - 1:
            self.cur = 0
        self._show_frame()          # la 1re image de la lecture est montrée elle aussi
        self._playing = True
        self.play_btn.setText("❚❚")
        self._restart_clock()
        self._timer.start()

    def _restart_clock(self) -> None:
        times = self._data()[0]
        if times:
            self._play_t0 = times[self.cur]
        if self._playing:
            self._play_sound(self._play_t0)
        self._clock.start()

    def _play_sound(self, t_from: float) -> None:
        snd = self._snd.get(self._clap().id)
        if snd is not None:
            sig, rate, t_sig = snd
            self._audio.play(sig, rate, t_sig, t_from, self._speed)

    def _pause(self) -> None:
        """Arrête la lecture et le son, sans rien choisir."""
        self._playing = False
        if self._timer.isActive():
            self._timer.stop()
        self._audio.stop()
        self.play_btn.setText("▶")

    def _rest_index(self) -> int:
        """Image montrée à l'arrêt : l'image choisie, sinon l'automatique, sinon face au son."""
        c = self._clap()
        times = self._data()[0]
        target = self.chosen.get(c.id)
        if target is not None:
            return self._first_of_series(self._index_at(times, target))
        if self._auto_k is not None:
            return self._auto_k
        return self._first_of_series(self._index_at(times, c.t_audio - (self.cam.offset or 0.0)))

    def _play_tick(self) -> None:
        times = self._data()[0]
        if len(times) < 2:
            self._pause()
            return
        t = self._play_t0 + self._clock.elapsed() / 1000.0 * self._speed
        if t >= times[-1] + 1.0 / self.fps:   # fin de l'extrait (la dernière image a eu sa durée) :
            # arrêt, retour sur l'image choisie (rien ne change)
            self._pause()
            self.cur = self._rest_index()
            self._show()
            return
        k = int(np.searchsorted(times, t, side="right")) - 1
        k = int(np.clip(k, 0, len(times) - 1))
        if k != self.cur or not self.preview.has_frame():
            self.cur = k
            self._show_frame()

    # --- choix -------------------------------------------------------------------
    def _choose(self, k: int) -> None:
        self._pause()
        c = self._clap()
        times = self._data()[0]
        if not times:
            return
        self.cur = self._first_of_series(int(np.clip(k, 0, len(times) - 1)))
        t_img = times[self.cur]
        best = self._pair(c, t_img)
        if best is not c:
            # claps rapprochés : l'image de contact choisie appartient à un autre son que celui
            # de la liste ; on la rattache à ce son-là (sinon décalage faux d'un espacement)
            j = self.claps.index(best)
            self.chosen[best.id] = t_img
            self.last_changed = best.id
            self.clap_box.setItemText(j, self._clap_label(best))
            self._switching = True
            self.clap_box.setCurrentIndex(j)      # recharge autour de ce clap, sur l'image choisie
            self._switching = False
            # dans le passage chargé autour de ce son, la même image peut appartenir à une série
            # commencée plus tôt (au bord d'un passage, la 1re image passe pour une vraie image) :
            # on retient sa première apparition ici, celle qui est affichée
            nt = self._data()[0]
            if nt:
                self.cur = self._first_of_series(self._index_at(nt, t_img))
                self.chosen[best.id] = nt[self.cur]
                self.clap_box.setItemText(j, self._clap_label(best))
            self._notice = (f"Image rattachée au son de clap le plus proche : {best.t_audio:.3f} s "
                            f"(et non {c.t_audio:.3f} s)")
            self._show()
            return
        self.chosen[c.id] = t_img
        self.last_changed = c.id
        self.clap_box.setItemText(self.clap_box.currentIndex(), self._clap_label(c))
        self._show()

    def _ref_offset(self, exclude: analysis.Clap) -> float:
        """Décalage de référence pour apparier une image à un son : les AUTRES claps réglés à la
        main, sinon la mesure automatique si elle est sûre, sinon 0 (son le plus proche). Une source
        ambiguë ne sert pas de référence : ses claps automatiques sont justement mal appariés."""
        offs = [o.t_audio - t for o in self.claps
                if o is not exclude and not o.excluded and (t := self.chosen.get(o.id)) is not None]
        if offs:
            return float(np.median(offs))
        if self.cam.offset is not None and not self.cam.ambiguous:
            return float(self.cam.offset)
        return 0.0

    def _pair(self, c: analysis.Clap, t_img: float) -> analysis.Clap:
        """Son de clap qui va avec cette image : celui qui donne le décalage le plus proche de celui
        des autres claps (ou de 0). On ne change de clap que si l'écart est net (> 100 ms)."""
        ref = self._ref_offset(c)
        same = [o for o in self.claps if o.shot == c.shot]
        best = min(same, key=lambda o: abs(o.t_audio - t_img - ref))
        if best is not c and abs(c.t_audio - t_img - ref) - abs(best.t_audio - t_img - ref) > 0.1:
            return best
        return c

    def _step(self, d: int) -> None:
        vis = self._visible()
        if not vis:
            return
        pos = int(np.searchsorted(vis, self._first_of_series(self.cur)))
        if pos < len(vis) and vis[pos] == self.cur:
            pos += d
        elif d < 0:
            pos -= 1
        self._choose(vis[int(np.clip(pos, 0, len(vis) - 1))])

    def _pick_slot(self, j: int) -> None:
        if j < len(self._slots):
            self._choose(self._slots[j])

    def _reset_auto(self) -> None:
        c = self._clap()
        self.chosen[c.id] = None
        self.last_changed = c.id
        self.clap_box.setItemText(self.clap_box.currentIndex(), self._clap_label(c))
        self._load()

    def changes(self) -> dict[int, float | None]:
        return {c.id: t for c in self.claps if (t := self.chosen.get(c.id)) != c.manual_t}
