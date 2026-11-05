# SPDX-License-Identifier: GPL-3.0-or-later
"""Réglage manuel de l'image du clap : bande d'images autour du clap, on clique l'image où les
mains se touchent (ou où le flash apparaît). Le décalage devient son du clap − instant de
cette image, à ± ½ image près."""
from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QToolButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import analysis, export, media
from .shots import DUP_DIFF

STRIP = 9            # images visibles dans la bande
HALF_SPAN = 1.0      # s chargées de part et d'autre du centre
PREVIEW_W = 760
THUMB_W = 132


def _qpix(img: np.ndarray, width: int) -> QPixmap:
    h, w = img.shape[:2]
    hh = max(1, int(round(h * width / w)))
    small = cv2.resize(img, (width, hh), interpolation=cv2.INTER_AREA)
    small = np.ascontiguousarray(small)
    q = QImage(small.data, width, hh, 3 * width, QImage.Format_RGB888)
    return QPixmap.fromImage(q.copy())


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
    """Une source : chaque clap a sa bande d'images. Le résultat est `self.chosen`
    {id du clap: instant choisi ou None pour revenir à l'automatique}."""

    def __init__(self, an: analysis.Analysis, cam: analysis.CameraResult, parent=None):
        super().__init__(parent)
        self.an, self.cam = an, cam
        self.fps = an.info.fps_float
        self.setWindowTitle(f"Régler l'image du clap — {export.display_name(cam)}")
        self.claps = [c for c in cam.claps]
        self.chosen: dict[int, float | None] = {c.id: c.manual_t for c in self.claps}
        self._cache: dict[int, tuple[list[float], list[np.ndarray], list[bool]]] = {}
        self._boxes: dict[int, tuple[int, int, int, int] | None] = {}
        self._auto_k: int | None = None
        self._locator = None
        self.cur = 0          # index de l'image courante dans la bande chargée

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Clap :"))
        self.clap_box = QComboBox()
        for c in self.claps:
            self.clap_box.addItem(self._clap_label(c), c.id)
        self.clap_box.currentIndexChanged.connect(lambda _i: self._load())
        top.addWidget(self.clap_box, 1)
        self.zoom = QCheckBox("Zoom sur le mouvement (mains)")
        self.zoom.setChecked(True)
        self.zoom.toggled.connect(lambda _b: self._show())
        top.addWidget(self.zoom)
        v.addLayout(top)

        help_ = QLabel("Clique l'image où les mains se touchent (ou où le flash apparaît). "
                       "Flèches ← → : une image avant / après. Les images grisées sont des répétitions "
                       "(caméra plus lente que le fichier) : choisis la première de la série.")
        help_.setWordWrap(True)
        help_.setStyleSheet("color:#8A8780;")
        v.addWidget(help_)

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 200)
        self.preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        v.addWidget(self.preview, 1)
        self.info = QLabel()
        self.info.setAlignment(Qt.AlignCenter)
        self.info.setStyleSheet("font-size:15px; font-weight:600;")
        v.addWidget(self.info)

        strip = QHBoxLayout()
        self.prev_btn = QPushButton("◀")
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        strip.addWidget(self.prev_btn)
        self.thumbs: list[QToolButton] = []
        for j in range(STRIP):
            b = QToolButton()
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setIconSize(QSize(THUMB_W, int(THUMB_W * 9 / 16)))
            b.clicked.connect(lambda _=False, j=j: self._pick_slot(j))
            strip.addWidget(b)
            self.thumbs.append(b)
        self.next_btn = QPushButton("▶")
        self.next_btn.clicked.connect(lambda: self._step(1))
        strip.addWidget(self.next_btn)
        v.addLayout(strip)

        bot = QHBoxLayout()
        self.auto_btn = QPushButton("Revenir à l'automatique pour ce clap")
        self.auto_btn.clicked.connect(self._reset_auto)
        bot.addWidget(self.auto_btn)
        bot.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Valider")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        bot.addWidget(cancel)
        bot.addWidget(ok)
        v.addLayout(bot)

        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self._step(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self._step(1))
        first = next((i for i, c in enumerate(self.claps) if c.selected is not None), 0)
        self.clap_box.setCurrentIndex(first)
        if first == 0:
            self._load()

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
        if self._locator is not None:
            self._locator.close()
        super().done(r)

    # --- données ---------------------------------------------------------------
    def _clap(self) -> analysis.Clap:
        return self.claps[max(0, self.clap_box.currentIndex())]

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

    def _load(self) -> None:
        c = self._clap()
        if c.id not in self._cache:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                center = c.t_audio - (self.cam.offset or 0.0)
                region = self.an.shots[c.shot].region
                shot = self.an.shots[c.shot]
                t0 = max(shot.start, center - HALF_SPAN)
                t1 = min(shot.end, center + HALF_SPAN)
                times, frames = media.decode_range(self.an.info.path, t0, t1, region)
            finally:
                QApplication.restoreOverrideCursor()
            # répétitions signalées seulement si la source est une caméra lente : dans un décor
            # fixe, deux images sans mouvement se ressemblent aussi
            dups = duplicates(frames) if self.cam.source_fps else [False] * len(frames)
            self._cache[c.id] = (times, frames, dups)
        times = self._cache[c.id][0]
        if not times:
            self.cur = 0
            self._show()
            return
        target = self.chosen.get(c.id)
        if target is None:
            target = self._auto_t(c)
        if target is None:
            target = c.t_audio - (self.cam.offset or 0.0)
        # l'image affichée à l'instant t est la dernière dont l'horodatage est ≤ t
        k = int(np.searchsorted(times, target + 0.25 / self.fps, side="right")) - 1
        self.cur = int(np.clip(k, 0, len(times) - 1))
        self._auto_k = None
        a = self._auto_t(c)
        if a is not None:
            self._auto_k = int(np.clip(np.searchsorted(times, a + 0.25 / self.fps, side="right") - 1,
                                       0, len(times) - 1))
        self._show()

    # --- affichage ---------------------------------------------------------------
    def _show(self) -> None:
        c = self._clap()
        times, frames, dups = self._cache.get(c.id, ([], [], []))
        if not frames:
            self.preview.setText("Images introuvables autour de ce clap.")
            self.info.setText("")
            for b in self.thumbs:
                b.setVisible(False)
            return
        k = self.cur
        # zone du geste calculée une fois par clap, autour de l'image automatique : la bande ne
        # « saute » pas quand on avance image par image
        if c.id not in self._boxes:
            ref = self._auto_k if self._auto_k is not None else k
            # mains au moment du contact seulement : pendant l'élan elles traversent l'image
            self._boxes[c.id] = (hand_box(self._hands(), frames, ref)
                                 or motion_box(frames, max(0, ref - 6), min(len(frames) - 1, ref + 2)))
        box = self._boxes[c.id] if self.zoom.isChecked() else None
        big = frames[k] if box is None else frames[k][box[1]:box[3], box[0]:box[2]]
        # grande image : toute la place disponible, proportions gardées
        pw = max(320, min(self.preview.width(), int(self.preview.height() * big.shape[1] / big.shape[0])))
        self.preview.setPixmap(_qpix(big, pw))
        tw = int(np.clip((self.width() - 160) // STRIP - 14, 90, 320))
        for b in self.thumbs:
            b.setIconSize(QSize(tw, int(tw * big.shape[0] / big.shape[1])))
        off = c.t_audio - times[k]
        chosen = self.chosen.get(c.id)
        tag = ("image choisie" if chosen is not None and abs(chosen - times[k]) < 0.25 / self.fps
               else "image affichée")
        head = self._verdict(off)
        txt = f"{tag} : {times[k]:.3f} s · son du clap : {c.t_audio:.3f} s  →  {head}"
        a = self._auto_t(c)
        if chosen is None and k == self._auto_k and a is not None:
            # l'automatique situe le contact entre deux images : sa valeur diffère un peu
            txt += f"\n(mesure automatique : {export.fmt_ms(c.t_audio - a)}, contact situé entre deux images)"
        self.info.setText(txt)
        lo = int(np.clip(k - STRIP // 2, 0, max(0, len(frames) - STRIP)))
        for j, b in enumerate(self.thumbs):
            i = lo + j
            if i >= len(frames):
                b.setVisible(False)
                continue
            b.setVisible(True)
            img = frames[i]
            if box is not None:
                x0, y0, x1, y1 = box
                img = img[y0:y1, x0:x1]
            if dups[i]:
                img = (img.astype(np.float32) * 0.35 + 40).astype(np.uint8)
            b.setIcon(_qpix(img, tw))
            marks = []
            if i == self._auto_k:
                marks.append("auto")
            if dups[i]:
                marks.append("répétée")
            b.setText(f"{export.fmt_ms(c.t_audio - times[i])}" + (f"\n{' · '.join(marks)}" if marks else "\n"))
            sel = i == k
            b.setStyleSheet("QToolButton { border: 3px solid %s; border-radius: 4px; padding: 2px; }"
                            % ("#FF5A1F" if sel else "transparent"))
        self.prev_btn.setEnabled(k > 0)
        self.next_btn.setEnabled(k < len(frames) - 1)

    def _verdict(self, off: float) -> str:
        ms = round(off * 1000)
        if ms == 0:
            return "synchro"
        who = "IMAGE EN RETARD" if off < 0 else "SON EN RETARD"
        return f"{who} de {export.both(off, self.fps)}"

    # --- actions ---------------------------------------------------------------
    def _choose(self, k: int) -> None:
        c = self._clap()
        times = self._cache[c.id][0]
        if not times:
            return
        self.cur = int(np.clip(k, 0, len(times) - 1))
        self.chosen[c.id] = times[self.cur]
        self.clap_box.setItemText(self.clap_box.currentIndex(), self._clap_label(c))
        self._show()

    def _step(self, d: int) -> None:
        self._choose(self.cur + d)

    def _pick_slot(self, j: int) -> None:
        c = self._clap()
        n = len(self._cache[c.id][0])
        lo = int(np.clip(self.cur - STRIP // 2, 0, max(0, n - STRIP)))
        self._choose(lo + j)

    def _reset_auto(self) -> None:
        c = self._clap()
        self.chosen[c.id] = None
        self.clap_box.setItemText(self.clap_box.currentIndex(), self._clap_label(c))
        self._load()

    def changes(self) -> dict[int, float | None]:
        return {c.id: t for c in self.claps if (t := self.chosen.get(c.id)) != c.manual_t}
