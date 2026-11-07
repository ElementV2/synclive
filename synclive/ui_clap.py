# SPDX-License-Identifier: GPL-3.0-or-later
"""Réglage manuel de l'image du clap, sur tout le rush de la source : une timeline qui montre tout le
plan avec ses sons de clap, lecteur (×1, ×1/2, ×1/4) et bande d'images ; on choisit l'image où les
mains se touchent (ou où le flash apparaît). Le décalage devient son du clap − instant de cette
image, à ± ½ image près. Un son qui n'est pas un clap s'écarte, un clap pas entendu s'ajoute."""
from __future__ import annotations

import threading

import cv2
import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QToolButton, QVBoxLayout, QWidget,
)

from . import analysis, export, media
from .ui_audio import Timeline
from .shots import DUP_DIFF

STRIP = 9            # images visibles dans la bande (par défaut)
STRIPS = (9, 15, 21)
HALF_SPAN = 0.6      # s d'images chargées de part et d'autre de la tête (assez pour 21 vignettes à 60 i/s)
LOAD_W = 960         # largeur des images chargées (assez pour juger le contact, rapide à lire)
PREVIEW_W = 760
THUMB_W = 132


def _fmt(t: float) -> str:
    m, sec = divmod(max(0.0, t), 60.0)
    return f"{int(m)}:{sec:04.1f}".replace(".", ",")


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
    """Une source, tout son rush : la timeline montre le plan entier avec ses sons de clap ; les
    images sont chargées autour de la tête (bande, flèches, lecture). Pour chaque son de clap on choisit l'image du contact ;
    un son qui n'est pas un clap (bruit de micro) s'écarte.

    Résultats : `self.chosen` {id du clap: instant choisi ou None pour l'automatique},
    `exclusions()`. `self.cur` est l'image affichée dans la fenêtre chargée ; hors
    lecture et hors glisser, c'est l'image choisie."""

    def __init__(self, an: analysis.Analysis, cam: analysis.CameraResult, parent=None):
        super().__init__(parent)
        self.an, self.cam = an, cam
        self.fps = an.info.fps_float
        self.setWindowTitle(f"Régler l'image du clap — {export.display_name(cam)}")
        self.claps = sorted(cam.claps, key=lambda c: c.t_audio)
        self.chosen: dict[int, float | None] = {c.id: c.manual_t for c in self.claps}
        self.excluded: dict[int, bool] = {c.id: c.excluded for c in self.claps}
        self.last_changed: int | None = None     # clap réglé en dernier (image montrée sur la carte)
        self.shots = sorted(cam.shots, key=lambda i: an.shots[i].start)
        self.shot = self.shots[0] if self.shots else 0
        self._win: tuple[int, float, float] | None = None   # fenêtre de détail chargée
        self._cache: dict[tuple, tuple[list[float], list[np.ndarray], list[bool]]] = {}
        self._snd: dict[tuple, tuple[np.ndarray, int, float]] = {}
        self._boxes: dict[tuple, tuple[int, int, int, int] | None] = {}
        self._series: dict[tuple, list[int]] = {}
        self._peaks: dict[int, np.ndarray] = {}
        self._peak_threads: dict[int, threading.Thread] = {}
        self._closing = False
        self._scrubber = None
        self._loader: threading.Thread | None = None
        self._load_cv = threading.Condition()
        self._load_queue: list = []
        self._wanted: tuple | None = None         # passage attendu en fond, et l'instant à montrer
        self._view_shot: int | None = None        # plan dont la vue de départ est faite
        self._loading = None                      # passage en cours de lecture dans le fil
        self._auto_k: int | None = None
        self._locator = None
        self._slots: list[int] = []
        self._notice = ""
        self._switching = False
        self._playing = False
        self.player = None                        # lecteur vidéo de Qt (créé à la 1re lecture)
        self.audio = None
        self._QMP = None
        self._t_start = 0.0
        self._play_from: float | None = None
        self._play_t: float | None = None
        self.cur = 0          # index de l'image affichée dans la fenêtre chargée

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.shot_lbl = QLabel("Plan :")
        top.addWidget(self.shot_lbl)
        self.shot_box = NoWheelComboBox()
        for k, i in enumerate(self.shots):
            s = an.shots[i]
            self.shot_box.addItem(f"Plan {k + 1} : {_fmt(s.start)} → {_fmt(s.end)}", i)
        self.shot_box.currentIndexChanged.connect(lambda _i: self._shot_changed())
        top.addWidget(self.shot_box)
        self.shot_lbl.setVisible(len(self.shots) > 1)
        self.shot_box.setVisible(len(self.shots) > 1)
        top.addWidget(QLabel("Clap :"))
        self.clap_box = NoWheelComboBox()
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

        help_ = QLabel("La timeline montre les sons de clap détectés (orange). Clic ou glisser : y aller · molette : "
                       "zoom (« Tout le plan » pour tout voir) · Espace : lecture · ■ : début · ← → : image par image. "
                       "Rien n'est choisi tant que tu ne cliques pas « Cette image est le clap » (Entrée).")
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
        self.play_btn.setToolTip("Lecture avec le son / pause (Espace)")
        self.play_btn.setStyleSheet(
            "QPushButton#play { background:#FF5A1F; color:#16161A; border:none; border-radius:23px;"
            " font-size:18px; font-weight:700; padding:0; }"
            "QPushButton#play:hover { background:#FF7240; }")
        self.play_btn.clicked.connect(self._toggle_play)
        player.addWidget(self.play_btn, 0, Qt.AlignVCenter)
        self.stop_btn = QPushButton("■")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setFixedSize(40, 40)
        self.stop_btn.setToolTip("Stop : retour au début du plan (touche Début)")
        self.stop_btn.setStyleSheet(
            "QPushButton#stop { background:#2E2E35; color:#E6E6E6; border:none; border-radius:20px;"
            " font-size:15px; padding:0; } QPushButton#stop:hover { background:#3A3A42; }")
        self.stop_btn.clicked.connect(self._stop)
        player.addWidget(self.stop_btn, 0, Qt.AlignVCenter)
        # une seule timeline : le son, les sons de clap détectés, l'image choisie, la tête de lecture
        self.timeline = Timeline()
        self.timeline.scrub.connect(self._scrub)
        self.timeline.released.connect(self._choose_time)
        self.timeline.zoomed.connect(lambda _z: QTimer.singleShot(0, self._sync_unzoom))
        player.addWidget(self.timeline, 1)
        side = QVBoxLayout()
        side.setSpacing(6)
        self.time_lbl = QLabel()
        self.time_lbl.setAlignment(Qt.AlignCenter)
        self.time_lbl.setFixedWidth(118)
        self.time_lbl.setStyleSheet("font-size:16px; font-weight:600; font-family:Consolas, 'Segoe UI';")
        side.addWidget(self.time_lbl)
        self.unzoom_btn = QPushButton("Tout le plan")
        self.unzoom_btn.setToolTip("Voir tout le plan (double-clic sur la timeline)")
        self.unzoom_btn.clicked.connect(self.timeline.reset_zoom)
        self.unzoom_btn.setVisible(False)
        side.addWidget(self.unzoom_btn)
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
        self._load_timer = QTimer(self)              # passage chargé en fond : prise de relais
        self._load_timer.setInterval(20)
        self._load_timer.timeout.connect(self._load_tick)
        self._peak_timer = QTimer(self)              # aperçu du son du plan, rempli en tâche de fond
        self._peak_timer.setInterval(150)
        self._peak_timer.timeout.connect(self._peaks_tick)

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

        acts = QHBoxLayout()
        self.pick_btn = QPushButton("Cette image est le clap")
        self.pick_btn.setObjectName("primary")
        self.pick_btn.setAutoDefault(False)
        self.pick_btn.setToolTip("L'image affichée est celle où les mains se touchent (Entrée)")
        self.pick_btn.clicked.connect(self._pick_current)
        acts.addWidget(self.pick_btn)
        acts.addSpacing(12)
        self.excl_btn = QPushButton("Ce son n'est pas un clap")
        self.excl_btn.setToolTip("Bruit de micro, choc, voix… : ce son ne compte plus pour la mesure")
        self.excl_btn.clicked.connect(self._toggle_excluded)
        acts.addWidget(self.excl_btn)
        acts.addStretch(1)
        self.auto_btn = QPushButton("Revenir à l'automatique pour ce clap")
        self.auto_btn.clicked.connect(self._reset_auto)
        acts.addWidget(self.auto_btn)
        v.addLayout(acts)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Images dans la bande :"))
        self.strip_box = NoWheelComboBox()
        for n in STRIPS:
            self.strip_box.addItem(str(n), n)
        self.strip_box.currentIndexChanged.connect(lambda _i: self._build_strip())
        opts.addWidget(self.strip_box)
        opts.addStretch(1)
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
        QShortcut(QKeySequence(Qt.Key_Home), self, activated=self._stop)
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self._pick_current)
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=self._pick_current)
        # même clap que l'image de la carte, sinon le premier clap retenu
        shown = analysis.shown_clap(an, cam.image_shot) if cam.image_shot is not None else None
        first = next((c for c in self.claps if c is shown),
                     next((c for c in self.claps if c.selected is not None), self.claps[0] if self.claps else None))
        if first is not None:
            self.shot = first.shot
        self._fill_claps(first)
        self._sync_shot_box()
        self._load()

    # --- plans et claps ------------------------------------------------------------
    def _sync_unzoom(self) -> None:
        """« Tout le plan » visible quand la timeline est zoomée. Différé, et seulement s'il change :
        l'afficher redimensionne la fenêtre, qui redessine la timeline (pas de boucle)."""
        if self._closing:
            return
        z = self.timeline.zoomed_in()
        if self.unzoom_btn.isHidden() == z:
            self.unzoom_btn.setVisible(z)

    def _sync_shot_box(self) -> None:
        i = self.shot_box.findData(self.shot)
        if i >= 0 and i != self.shot_box.currentIndex():
            self.shot_box.blockSignals(True)
            self.shot_box.setCurrentIndex(i)
            self.shot_box.blockSignals(False)

    def _shot_claps(self) -> list[analysis.Clap]:
        return [c for c in self.claps if c.shot == self.shot]

    def _fill_claps(self, select: analysis.Clap | None = None) -> None:
        """Liste des sons de clap du plan affiché (les écartés y restent, marqués)."""
        self.clap_box.blockSignals(True)
        self.clap_box.clear()
        for c in self._shot_claps():
            self.clap_box.addItem(self._clap_label(c), c.id)
        if not self._shot_claps():
            self.clap_box.addItem("aucun son de clap retenu dans ce plan", None)
        i = self.clap_box.findData(select.id) if select is not None else -1
        if i < 0:
            i = next((k for k, c in enumerate(self._shot_claps()) if not self.excluded.get(c.id)), 0)
        self.clap_box.setCurrentIndex(i)
        self.clap_box.blockSignals(False)

    def _select_clap(self, c: analysis.Clap) -> None:
        """Affiche ce clap (et son plan)."""
        if c.shot != self.shot:
            self._pause()
            self.shot = c.shot
            self._sync_shot_box()
            self._fill_claps(c)
            self._load()
        else:
            i = self.clap_box.findData(c.id)
            if i == self.clap_box.currentIndex():
                self._load()
            else:
                self.clap_box.setCurrentIndex(i)

    def _clap(self) -> analysis.Clap | None:
        cid = self.clap_box.currentData()
        return next((c for c in self.claps if c.id == cid), None) if cid is not None else None

    def _relabel(self) -> None:
        for k in range(self.clap_box.count()):
            c = next((o for o in self.claps if o.id == self.clap_box.itemData(k)), None)
            if c is not None:
                self.clap_box.setItemText(k, self._clap_label(c))

    def _shot_changed(self) -> None:
        sh = self.shot_box.currentData()
        if sh is None or sh == self.shot:
            return
        self._pause()
        self.shot = sh
        self._notice = ""
        self._fill_claps()
        self._load()

    def _data(self):
        return self._cache.get(self._win, ([], [], []))

    def _clap_label(self, c: analysis.Clap) -> str:
        t = self.chosen.get(c.id)
        if self.excluded.get(c.id):
            return f"son à {c.t_audio:.3f} s — écarté (pas un clap)"
        if t is not None:
            how = f"à la main : {export.fmt_ms(c.t_audio - t)}"
        elif c.selected is not None and c.manual_t is None:
            how = f"auto : {export.fmt_ms(c.offset)}"
        else:
            how = "pas d'image retenue"
        return f"son à {c.t_audio:.3f} s — {how}"

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
        fois par fenêtre, en avançant : une série ne dure pas plus d'une image réelle de la caméra."""
        got = self._series.get(self._win)
        if got is not None:
            return got
        times, _f, dups = self._data()
        src = self.cam.source_fps
        reach = max(0, int(round(self.fps / src)) - 1) if src else 0
        out, start = [], 0
        for i, dup in enumerate(dups):
            # coupure sur le n° absolu de l'image dans un plan immobile : les séries sont les mêmes
            # quelle que soit la fenêtre chargée (sinon une image change de série d'une fenêtre à l'autre)
            absn = int(round(times[i] * self.fps))
            if not dup or i - start > reach or absn % (reach + 1) == 0:
                start = i
            out.append(start)
        self._series[self._win] = out
        return out

    def _t(self, i: int) -> float:
        """Instant d'une image tel qu'il sera retenu si on la choisit (première apparition)."""
        return self._data()[0][self._first_of_series(i)]

    def _clap_changed(self) -> None:
        if not self._switching:
            self._notice = ""
        self._load()

    # --- fenêtre de détail : durée fixe, elle glisse sur le plan ---------------------------
    def _half(self) -> float:
        return HALF_SPAN

    def _key_at(self, t: float) -> tuple[int, float, float]:
        sh = self.an.shots[self.shot]
        half = self._half()
        t0 = max(sh.start, min(t - half, sh.end - 2 * half))
        t1 = min(sh.end, t0 + 2 * half)
        return (self.shot, round(t0, 4), round(t1, 4))

    def _covers(self, t: float) -> bool:
        """Les images chargées montrent-elles t (hors des bords, sauf au bord du plan) ?"""
        if self._win is None:
            return False
        sh = self.an.shots[self.shot]
        s_, a, b = self._win
        m = 0.15 * (b - a)
        at_start, at_end = a <= sh.start + 1e-3, b >= sh.end - 1e-3
        central = a + m <= t <= b - m
        edge_ok = (t < a + m and at_start) or (t > b - m and at_end)    # bord du plan : rien au-delà
        return s_ == self.shot and a <= t <= b and (central or edge_ok)

    def _decode_window(self, key, stop=None) -> None:
        """Lit les images et le son d'un passage (fil de chargement ou, au besoin, tout de suite).
        `stop()` vrai : abandon (préchargement devenu inutile), rien n'est gardé."""
        if key in self._cache:
            return
        shot, t0, t1 = key
        sh = self.an.shots[shot]
        times, frames = media.decode_range(self.an.info.path, t0, t1, sh.shown, max_width=LOAD_W, stop=stop)
        # répétitions signalées seulement si la source est une caméra lente : dans un décor fixe,
        # deux images sans mouvement se ressemblent aussi
        dups = duplicates(frames) if self.cam.source_fps else [False] * len(frames)
        snd = (np.zeros(0, np.float32), 48000, times[0] if times else t0)
        if times:
            try:
                snd = media.decode_audio_range(self.an.info.path, self.an.settings.audio_track,
                                               times[0], times[-1] + 1.0 / self.fps)
            except Exception:
                pass
        self._snd[key] = snd
        self._cache[key] = (times, frames, dups)

    def _loader_run(self) -> None:
        """Fil de chargement : le passage demandé en dernier d'abord, puis les voisins (préchargés)."""
        while not self._closing:
            with self._load_cv:
                while not self._load_queue and not self._closing:
                    self._load_cv.wait(0.5)
                if self._closing:
                    return
                key = self._load_queue.pop(0)
                self._loading = key
            try:
                # un préchargement s'arrête dès qu'un passage demandé par un clic attend
                wanted = lambda k=key: self._closing or (self._wanted is not None and self._wanted[0] != k)  # noqa: E731
                self._decode_window(key, stop=wanted)
            except Exception:
                pass
            finally:
                self._loading = None

    def _request(self, keys: list) -> None:
        with self._load_cv:
            for k in reversed(keys):
                if k in self._load_queue:
                    self._load_queue.remove(k)
                if k not in self._cache:
                    self._load_queue.insert(0, k)
            del self._load_queue[6:]
            self._load_cv.notify()
        if self._loader is None:
            self._loader = threading.Thread(target=self._loader_run, daemon=True)
            self._loader.start()

    def _prefetch(self) -> None:
        """Passages juste avant et juste après : flèches et lecture enchaînent sans attendre."""
        if self._win is None:
            return
        _s, a, b = self._win
        sh = self.an.shots[self.shot]
        keys = [self._key_at(b + self._half() - 0.1)] if b < sh.end - 1e-3 else []
        if a > sh.start + 1e-3:
            keys.append(self._key_at(a - self._half() + 0.1))
        self._request([k for k in keys if k != self._win])

    def _use(self, key) -> None:
        self._win = key
        for old in [k for k in list(self._cache) if k != key][:-5]:     # 6 passages gardés au plus
            self._cache.pop(old, None)
            self._snd.pop(old, None)
            self._series.pop(old, None)
        self._set_timeline()
        self._prefetch()

    def _ensure(self, t: float, force: bool = False) -> bool:
        """Images chargées autour de l'instant t (bande, flèches, lecture ; la timeline montre
        toujours tout le plan). Tout de suite (on attend la lecture si besoin). Vrai si les images
        chargées ont changé."""
        self._wanted = None
        if not force and self._covers(t):
            return False
        key = self._key_at(t)
        if key == self._win:
            return False
        if key not in self._cache:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._decode_window(key)
            finally:
                QApplication.restoreOverrideCursor()
        self._use(key)
        return True

    def _go_soon(self, t: float) -> None:
        """Aller à t SANS attendre : l'image est montrée tout de suite (aperçu rapide), les images
        autour se chargent en fond et prennent le relais dès qu'elles sont là."""
        self._pause()
        self._to_shot_of(t)
        if self._covers(t):
            self._wanted = None
            times = self._data()[0]
            self.cur = self._first_of_series(self._index_at(times, t))
            self._show()
            return
        key = self._key_at(t)
        if key in self._cache:
            self._wanted = None
            self._use(key)
            self.cur = self._first_of_series(self._index_at(self._data()[0], t))
            self._show()
            return
        self._wanted = (key, t)
        self._request([key])
        self.timeline.set_marks(t, self.timeline.chosen)
        self.time_lbl.setText(f"{t:.3f} s")
        self._fast(t)
        self._load_timer.start()

    def _load_tick(self) -> None:
        if self._wanted is None:
            self._load_timer.stop()
            return
        key, t = self._wanted
        if key in self._cache and key in self._snd:
            self._wanted = None
            self._load_timer.stop()
            self._use(key)
            times = self._data()[0]
            if times:
                self.cur = self._first_of_series(self._index_at(times, t))
            self._show()

    def _settle(self) -> None:
        """Un passage est attendu en fond : on l'attend ici (geste qui a besoin des images)."""
        if self._wanted is not None:
            key, t = self._wanted
            self._ensure(t)
            times = self._data()[0]
            if times:
                self.cur = self._first_of_series(self._index_at(times, t))

    def _set_timeline(self) -> None:
        times = self._data()[0]
        if not times:
            return
        c = self._clap()
        sh = self.an.shots[self.shot]
        marks = [(o.t_audio, o is c) for o in self._shot_claps() if not self.excluded.get(o.id) or o is c]
        self.timeline.peaks = self._peaks.get(self.shot)
        self.timeline.set_data(np.zeros(0, np.float32), 48000, sh.start, sh.start, sh.end, marks)
        if self._view_shot != self.shot:
            self._view_shot = self.shot
            self._default_view()
        self._auto_k = None
        if c is not None:
            a_img = self._auto_img(c)
            if a_img is not None and times[0] <= a_img <= times[-1]:
                self._auto_k = self._first_of_series(self._index_near(times, a_img))

    def _default_view(self) -> None:
        """Vue de départ d'un plan : la zone de ses sons de clap détectés (1 s de marge), pas les
        longs passages sans clap ; tout le plan s'il n'y en a aucun (il faut chercher partout)."""
        sh = self.an.shots[self.shot]
        ts = [o.t_audio for o in self._shot_claps() if not self.excluded.get(o.id)]
        if not ts:
            self.timeline.reset_zoom()
            return
        a, b = min(ts) - 1.0, max(ts) + 1.0
        if b - a < 3.0:
            mid = (a + b) / 2
            a, b = mid - 1.5, mid + 1.5
        if a <= sh.start + 0.5 and b >= sh.end - 0.5:
            self.timeline.reset_zoom()
        else:
            self.timeline.zoom_to(max(sh.start, a), min(sh.end, b))

    def _auto_img(self, c: analysis.Clap) -> float | None:
        """Image automatique : exactement celle de la carte (juste après le contact)."""
        a = self._auto_t(c)
        if a is not None and c.selected is not None and c.manual_t is None:
            return analysis.clap_image_t(self.an, c)
        if a is not None:
            return a + (0.0 if self.cam.kind == "mire" else 1.5 / self.fps)
        return None

    def _rest_t(self) -> float:
        """Instant montré à l'arrêt : l'image choisie, sinon l'automatique, sinon face au son ;
        sans clap, le début du plan."""
        c = self._clap()
        sh = self.an.shots[self.shot]
        if c is None:
            times = self._data()[0]
            return times[self.cur] if times and self._win and self._win[0] == self.shot else sh.start
        target = self.chosen.get(c.id)
        if target is not None:
            return target
        a = self._auto_img(c)
        if a is not None:
            return a
        return min(max(c.t_audio - (self.cam.offset or 0.0), sh.start), sh.end)

    def _load(self) -> None:
        self._pause()
        c = self._clap()
        if c is not None and c.shot != self.shot:
            self.shot = c.shot
            self._sync_shot_box()
        self._start_peaks(self.shot)
        t = self._rest_t()
        if not self._ensure(t):
            self._set_timeline()
        times = self._data()[0]
        self.cur = self._first_of_series(self._index_at(times, t)) if times else 0
        self._update_buttons()
        self._show()

    # --- aperçu du son du plan entier ---------------------------------------------------
    def _start_peaks(self, shot: int) -> None:
        if shot in self._peaks:
            return
        sh = self.an.shots[shot]
        n = 12000                                   # assez fin pour zoomer sur une zone du plan
        arr = np.zeros(n, np.float32)
        self._peaks[shot] = arr
        th = threading.Thread(target=media.audio_peaks, daemon=True,
                              args=(self.an.info.path, self.an.settings.audio_track, sh.start, sh.end, n),
                              kwargs={"stop": lambda: self._closing, "out": arr})
        self._peak_threads[shot] = th
        th.start()
        self._peak_timer.start()

    def _peaks_tick(self) -> None:
        self.timeline._env = None                   # le son du plan se remplit : on le redessine
        self.timeline.update()
        if not any(t.is_alive() for t in self._peak_threads.values()):
            self._peak_timer.stop()

    def _to_shot_of(self, t: float) -> None:
        """Instant d'un autre plan de la source : on passe sur ce plan (liste des claps comprise)."""
        sh = self.an.shots[self.shot]
        if sh.start - 1e-6 <= t <= sh.end + 1e-6:
            return
        other = next((i for i in self.shots if self.an.shots[i].start - 1e-6 <= t <= self.an.shots[i].end + 1e-6), None)
        if other is None:
            return
        self.shot = other
        self._sync_shot_box()
        self._fill_claps()
        self._update_buttons()

    def _goto(self, t: float) -> None:
        """Va à l'instant t (du plan qui le contient), sans rien choisir, en attendant les images."""
        self._pause()
        self._to_shot_of(t)
        self._ensure(t)
        times = self._data()[0]
        if times:
            self.cur = self._first_of_series(self._index_at(times, t))
            self._show()

    # --- dimensions --------------------------------------------------------------------
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
        if self._cache and self._wanted is None:   # images attendues : on garde l'aperçu de l'endroit cliqué
            self._show()

    def done(self, r: int) -> None:
        self._closing = True
        with self._load_cv:
            self._load_cv.notify_all()
        self._load_timer.stop()
        if self.player is not None:
            self.player.stop()
        if self._scrubber is not None:
            self._scrubber.stop()
        self._peak_timer.stop()
        if self._locator is not None:
            self._locator.close()
        super().done(r)

    # --- affichage ---------------------------------------------------------------
    def _box(self):
        if not self.zoom.isChecked():
            return None
        c = self._clap()
        times, frames, _d = self._data()
        key = (c.id if c is not None else None, self._win)
        if key not in self._boxes and frames:
            ref = self._auto_k if self._auto_k is not None and not self.cam.ambiguous else self.cur
            # mains au moment du contact seulement : pendant l'élan elles traversent l'image
            hb = hand_box(self._hands(), frames, ref)
            self._boxes[key] = hb or motion_box(frames, max(0, ref - 6), min(len(frames) - 1, ref + 2))
        return self._boxes.get(key)

    def _show_frame(self) -> None:
        """Grande image, timeline, barre du plan et ligne d'info pour l'image affichée (rapide)."""
        c = self._clap()
        times, frames, _d = self._data()
        if not frames:
            return
        k = self.cur
        big = self._img(frames[k], self._box())
        self.preview.set_frame(big)
        tk = self._t(k)
        self.time_lbl.setText(f"{tk:.3f} s")
        self.timeline.set_marks(times[k], self.chosen.get(c.id) if c is not None else None)
        if c is None:
            txt = (f"image affichée : {tk:.3f} s · aucun son de clap retenu dans ce plan.\n"
                   "Choisis un autre plan, ou une autre source.")
            if self._notice:
                txt += f"\n{self._notice}"
            self.info.setText("\n".join(txt.split("\n")[:4]))
            return
        chosen = self.chosen.get(c.id)
        is_chosen = chosen is not None and abs(chosen - tk) < 0.25 / self.fps
        tag = "image choisie" if is_chosen else "image affichée"
        if self.excluded.get(c.id):
            txt = f"{tag} : {tk:.3f} s · le son de {c.t_audio:.3f} s est écarté (pas un clap)"
        else:
            txt = f"{tag} : {tk:.3f} s · son du clap : {c.t_audio:.3f} s  →  {self._verdict(c.t_audio - tk)}"
        if chosen is not None and not is_chosen:
            txt += f"\n(image choisie pour ce clap : {chosen:.3f} s, {export.fmt_ms(c.t_audio - chosen)})"
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
            self.preview.set_message("Images introuvables dans ce plan.")
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
        chosen = self.chosen.get(c.id) if c is not None else None
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
            ti = self._t(i)
            if c is None:
                b.setText(f"{ti:.2f} s".replace(".", ",") + "\n")
                b.setStyleSheet("QToolButton { border: 3px solid %s; border-radius: 4px; padding: 2px; }"
                                % ("#FF5A1F" if i == ks else "transparent"))
                continue
            marks = []
            is_chosen = chosen is not None and abs(chosen - ti) < 0.25 / self.fps
            if is_chosen:
                marks.append("choisie")
            if i == self._auto_k:
                marks.append("auto")
            # valeur obtenue si cette image devient l'image du clap sélectionné
            b.setText(f"{export.fmt_ms(c.t_audio - ti)}" + (f"\n{' · '.join(marks)}" if marks else "\n"))
            color = "#FF5A1F" if i == ks else ("#3FB950" if "choisie" in marks else "transparent")
            b.setStyleSheet("QToolButton { border: 3px solid %s; border-radius: 4px; padding: 2px; }" % color)
        sh = self.an.shots[self.shot]
        self.prev_btn.setEnabled(bool(vis) and (ks > vis[0] or times[0] > sh.start + 1e-3))
        self.next_btn.setEnabled(bool(vis) and (ks < vis[-1] or times[-1] < sh.end - 1.5 / self.fps))

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

    def _verdict(self, off: float) -> str:
        ms = round(off * 1000)
        if ms == 0:
            return "synchro"
        who = "IMAGE EN RETARD" if off < 0 else "SON EN RETARD"
        return f"{who} de {export.both(off, self.fps)}"

    def _update_buttons(self) -> None:
        c = self._clap()
        self.excl_btn.setEnabled(c is not None)
        self.pick_btn.setEnabled(c is not None and not self.excluded.get(c.id))
        self.excl_btn.setText("Remettre ce son" if c is not None and self.excluded.get(c.id)
                              else "Ce son n'est pas un clap")
        self.auto_btn.setEnabled(c is not None)

    # --- lecteur -------------------------------------------------------------------
    def _scrub(self, t: float) -> None:
        """Appui ou glisser sur la timeline : l'image suit (elle est choisie au relâchement). Hors
        des images chargées, un aperçu rapide (image clé puis image exacte) la montre aussitôt."""
        self._pause()
        times = self._data()[0]
        if times and times[0] <= t <= times[-1] + 1.0 / self.fps:
            self.cur = self._index_at(times, t)
            self._show_frame()
            return
        self.timeline.set_marks(t, self.timeline.chosen)
        self.time_lbl.setText(f"{t:.3f} s")
        self._fast(t)

    def _fast(self, t: float) -> None:
        if self._scrubber is None:
            from .ui_edit import ScrubDecoder         # (import ici : ui_edit importe ce module)
            self._scrubber = ScrubDecoder(self.an.info.path)
            self._scrubber.frame.connect(self._on_fast)
            self._scrubber.start()
        self._scrubber.request(t)

    def _on_fast(self, _ft: float, img, _exact: bool) -> None:
        if self._closing or not (self.timeline.dragging or self._wanted is not None):
            return
        x0, y0, x1, y1 = self.an.shots[self.shot].shown
        if (x0, y0, x1, y1) != (0.0, 0.0, 1.0, 1.0):
            h, w = img.shape[:2]
            img = img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
        self.preview.set_frame(self._img(np.ascontiguousarray(img), None))

    def _choose_time(self, t: float) -> None:
        """Relâchement sur la timeline : on reste sur cette image (rien n'est choisi), tout de suite."""
        self._go_soon(t)

    # --- lecture : le lecteur vidéo de Qt (image et son décodés en continu, fluide) ----------
    def _ensure_player(self) -> None:
        if self.player is not None:
            return
        import av
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        try:
            with av.open(self.an.info.path) as c:          # le lecteur compte depuis le début du flux
                self._t_start = c.start_time / 1_000_000 if c.start_time is not None else 0.0
        except Exception:
            self._t_start = 0.0
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_play_frame)
        self._QMP = QMediaPlayer
        self.player.mediaStatusChanged.connect(self._on_media)
        self.player.setSource(QUrl.fromLocalFile(self.an.info.path))

    def _on_media(self, status) -> None:
        QMP = self._QMP
        if status in (QMP.LoadedMedia, QMP.BufferedMedia):
            tr = int(self.an.settings.audio_track or 0)
            if tr and self.player.activeAudioTrack() != tr:
                try:
                    self.player.setActiveAudioTrack(tr)      # la piste son de l'analyse
                except Exception:
                    pass
            if self._play_from is not None:
                t, self._play_from = self._play_from, None
                self._start_player(t)

    def _ms(self, t: float) -> int:
        return int(round((t - self._t_start) * 1000))

    def _start_player(self, t: float) -> None:
        self.player.setPlaybackRate(self._speed)
        self.player.setPosition(self._ms(t))
        self.player.play()

    def _player_t(self) -> float:
        return self._t_start + self.player.position() / 1000.0

    def _on_play_frame(self, frame) -> None:
        """Image du lecteur pendant la lecture : montrée telle quelle (vignette d'un multiview
        recadrée) ; la tête de lecture suit ; arrêt au bout du plan."""
        if not self._playing or self._closing or not frame.isValid():
            return
        img = frame.toImage()
        if img.isNull():
            return
        x0, y0, x1, y1 = self.an.shots[self.shot].shown
        if (x0, y0, x1, y1) != (0.0, 0.0, 1.0, 1.0):
            from PySide6.QtCore import QRect
            w, h = img.width(), img.height()
            img = img.copy(QRect(int(x0 * w), int(y0 * h), int((x1 - x0) * w), int((y1 - y0) * h)))
        self.preview.set_qimage(img)
        t = self._t_start + frame.startTime() / 1_000_000 if frame.startTime() >= 0 else self._player_t()
        self._play_t = t
        self.timeline.set_marks(t, self.timeline.chosen)
        self.time_lbl.setText(f"{t:.3f} s")
        sh = self.an.shots[self.shot]
        if t >= sh.end - 1.0 / self.fps:                    # fin du plan
            self._pause_here()

    def _set_speed(self, val: float) -> None:
        self._speed = val
        for b, v in zip(self.speed_btns, (1.0, 0.5, 0.25)):
            b.setChecked(v == val)
        if self.player is not None:
            self.player.setPlaybackRate(val)

    def _toggle_play(self) -> None:
        """▶ : lecture avec le son depuis l'image affichée. ❚❚ : pause sur l'image en cours."""
        if self._playing:
            self._pause_here()
            return
        self._ensure_player()
        sh = self.an.shots[self.shot]
        if self._wanted is not None:
            t = self._wanted[1]
        else:
            times = self._data()[0]
            t = self._t(self.cur) if times else sh.start
        if t >= sh.end - 1.5 / self.fps:
            t = sh.start                                   # fin du plan : on repart du début
        self._playing = True
        self._play_t = t
        self.play_btn.setText("❚❚")
        QMP = self._QMP
        if self.player.mediaStatus() in (QMP.LoadedMedia, QMP.BufferedMedia, QMP.EndOfMedia):
            self._start_player(t)
        else:
            self._play_from = t                            # fichier en cours d'ouverture

    def _pause_here(self) -> None:
        """Pause : on reste sur l'image en cours, puis ses images exactes se chargent (bande, flèches)."""
        t = self._play_t if self._play_t is not None else (self._player_t() if self.player else None)
        self._pause()
        if t is not None:
            self._go_soon(min(t, self.an.shots[self.shot].end - 0.5 / self.fps))

    def _stop(self) -> None:
        """■ : arrêt, retour au début du plan (rien n'est choisi)."""
        self._pause()
        self._goto(self.an.shots[self.shot].start)

    def _pause(self) -> None:
        """Arrête la lecture et le son, sans rien choisir."""
        self._playing = False
        self._play_from = None
        if self.player is not None and self.player.playbackState() == self._QMP.PlayingState:
            self.player.pause()
        self.play_btn.setText("▶")

    def _rest_index(self) -> int:
        times = self._data()[0]
        return self._first_of_series(self._index_at(times, self._rest_t())) if times else 0

    # --- déplacement et choix -------------------------------------------------------
    def _move(self, k: int) -> None:
        """Va sur l'image k des images chargées (clic, glisser, flèches, vignette) : rien n'est
        choisi, on se déplace librement pour relire."""
        self._pause()
        times = self._data()[0]
        if not times:
            return
        self.cur = self._first_of_series(int(np.clip(k, 0, len(times) - 1)))
        t_img = times[self.cur]
        if self._ensure(t_img):                    # près du bord : les images chargées suivent
            nt = self._data()[0]
            self.cur = self._first_of_series(self._index_at(nt, t_img))
        self._show()

    def _pick_current(self) -> None:
        """« Cette image est le clap » (Entrée) : l'image affichée devient l'image du clap."""
        self._settle()
        self._choose(self.cur)

    def _choose(self, k: int) -> None:
        self._pause()
        c = self._clap()
        times = self._data()[0]
        if not times:
            return
        self.cur = self._first_of_series(int(np.clip(k, 0, len(times) - 1)))
        t_img = times[self.cur]
        if self._ensure(t_img):                    # près du bord : la fenêtre glisse (même échelle)
            nt = self._data()[0]
            self.cur = self._first_of_series(self._index_at(nt, t_img))
            t_img = nt[self.cur]
        if c is None:
            self._notice = "Aucun son de clap détecté dans ce plan."
            self._show()
            return
        if self.excluded.get(c.id):
            self._notice = "Ce son est écarté : « Remettre ce son » d'abord."
            self._show()
            return
        # l'image va TOUJOURS avec le clap sélectionné dans la liste : une caméra très en retard
        # peut montrer son contact plus près du son suivant, ce n'est pas une raison de changer
        self.chosen[c.id] = t_img
        self.last_changed = c.id
        self._relabel()
        self._show()

    def _ref_offset(self, exclude: analysis.Clap | None, trust_auto: bool = True) -> float | None:
        """Décalage de référence pour trouver le son d'un clap ajouté : les AUTRES claps réglés à la
        main, sinon la mesure automatique si elle est sûre. Une source ambiguë ne sert pas de
        référence : ses claps automatiques sont justement mal appariés."""
        offs = [o.t_audio - t for o in self.claps
                if o is not exclude and not self.excluded.get(o.id) and (t := self.chosen.get(o.id)) is not None]
        if offs:
            return float(np.median(offs))
        if trust_auto and self.cam.offset is not None and not self.cam.ambiguous:
            return float(self.cam.offset)
        return None

    def _step(self, d: int) -> None:
        self._settle()
        vis = self._visible()
        if not vis:
            return
        times = self._data()[0]
        pos = int(np.searchsorted(vis, self._first_of_series(self.cur)))
        if pos < len(vis) and vis[pos] == self.cur:
            pos += d
        elif d < 0:
            pos -= 1
        if 0 <= pos < len(vis):
            self._move(vis[pos])
            return
        # au bord de la fenêtre : la suite du plan (la fenêtre glisse, même échelle)
        sh = self.an.shots[self.shot]
        t = times[self.cur] + d * max(1.0 / self.fps, 1.0 / (self.cam.source_fps or self.fps))
        if not (sh.start <= t <= sh.end - 0.5 / self.fps):
            return
        self._ensure(t, force=True)
        nt = self._data()[0]
        if nt:
            self._move(self._index_near(nt, t))

    def _pick_slot(self, j: int) -> None:
        if j < len(self._slots):
            self._move(self._slots[j])

    def _reset_auto(self) -> None:
        c = self._clap()
        if c is None:
            return
        self.chosen[c.id] = None
        self.last_changed = c.id
        self._relabel()
        self._load()

    # --- sons qui ne sont pas des claps, claps pas entendus -----------------------------
    def _toggle_excluded(self) -> None:
        c = self._clap()
        if c is None:
            return
        self.excluded[c.id] = not self.excluded.get(c.id)
        self.last_changed = c.id
        self._notice = (f"Le son de {c.t_audio:.3f} s ne compte plus (pas un clap)."
                        if self.excluded[c.id] else f"Le son de {c.t_audio:.3f} s compte de nouveau.")
        self._relabel()
        self._update_buttons()
        self._set_timeline()
        self._show()

    # --- résultats -----------------------------------------------------------------
    def changes(self) -> dict[int, float | None]:
        """Images choisies à la main qui changent (claps de l'analyse)."""
        return {c.id: t for c in self.claps if (t := self.chosen.get(c.id)) != c.manual_t}

    def exclusions(self) -> dict[int, bool]:
        """Sons écartés ou remis (claps de l'analyse)."""
        return {c.id: ex for c in self.claps if (ex := bool(self.excluded.get(c.id))) != c.excluded}
