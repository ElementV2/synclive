# SPDX-License-Identifier: GPL-3.0-or-later
"""Lecteur de « Régler l'image du clap » : la timeline (son, sons de clap détectés, image
choisie, tête de lecture) et la lecture du son avec la vidéo, ralentie au besoin."""
from __future__ import annotations

import os
import sys
import tempfile
import wave

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

ORANGE = QColor("#FF5A1F")
GREEN = QColor("#3FB950")
WAVE = QColor("#6E8CA0")
BG = QColor("#16161A")


class AudioPlayer:
    """Lecture asynchrone d'un extrait (Windows : winsound, sans dépendance). Ralenti : le même
    son déclaré à une fréquence plus basse (plus lent et plus grave, comme une bande)."""

    def __init__(self) -> None:
        self.available = sys.platform == "win32"
        self._path = os.path.join(tempfile.gettempdir(), f"synclive_clap_{os.getpid()}.wav")

    def play(self, sig: np.ndarray, rate: int, t_start: float, t_from: float, speed: float = 1.0,
             t_to: float | None = None) -> None:
        if not self.available or sig.size == 0:
            return
        import winsound
        i0 = int(np.clip(round((t_from - t_start) * rate), 0, sig.size))
        i1 = sig.size if t_to is None else int(np.clip(round((t_to - t_start) * rate), i0, sig.size))
        part = sig[i0:i1]
        if part.size == 0:
            return
        self.stop()
        peak = float(np.max(np.abs(part))) or 1.0
        pcm = (np.clip(part / max(peak, 0.05), -1.0, 1.0) * 0.9 * 32767).astype("<i2")
        try:
            with wave.open(self._path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(max(4000, int(round(rate * speed))))
                w.writeframes(pcm.tobytes())
            winsound.PlaySound(self._path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            self.available = False      # pas de sortie son : la vidéo continue sans

    def stop(self) -> None:
        if not self.available:
            return
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def close(self) -> None:
        self.stop()
        try:
            os.remove(self._path)
        except OSError:
            pass


class Timeline(QWidget):
    """Timeline du lecteur : le son de l'extrait chargé, une règle en secondes autour du son du
    clap, les images réelles, le son du clap (orange), l'image choisie (vert) et la tête de lecture
    (blanc, poignée ronde). Appuyer ou glisser : `scrub` (on affiche) ; relâcher : `released`
    (l'image à cet instant est choisie)."""

    scrub = Signal(float)
    released = Signal(float)
    step = Signal(int)      # molette : image précédente (-1) / suivante (+1)

    MARGIN = 12         # px de chaque côté : la poignée reste entière aux extrémités
    RULER = 20          # px : étiquette du son du clap et règle des secondes au-dessus de la piste
    FLAGS = 18          # px : étiquette « image choisie » sous la piste

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip("Clique ou glisse pour te placer : l'image où tu relâches devient l'image du clap.")
        self.sig = np.zeros(0, np.float32)
        self.rate = 48000
        self.t_sig = 0.0
        self.ta, self.tb = 0.0, 1.0
        self.ref: float | None = None                 # son du clap : origine de la règle
        self.claps: list[tuple[float, bool]] = []     # (instant, clap courant ?)
        self.frames: list[float] = []                 # instants des images réelles
        self.playhead: float | None = None
        self.chosen: float | None = None
        self.dragging = False
        self._hover: float | None = None
        self._env: tuple[np.ndarray, np.ndarray] | None = None

    def set_data(self, sig: np.ndarray, rate: int, t_sig: float, ta: float, tb: float,
                 claps: list[tuple[float, bool]], frames: list[float] | None = None,
                 ref: float | None = None) -> None:
        self.sig, self.rate, self.t_sig = sig, rate, t_sig
        self.ta, self.tb = ta, max(tb, ta + 1e-3)
        self.claps = claps
        self.frames = list(frames or [])
        self.ref = ref
        self._env = None
        self.update()

    def set_marks(self, playhead: float | None, chosen: float | None) -> None:
        self.playhead, self.chosen = playhead, chosen
        self.update()

    # --- axe ---------------------------------------------------------------------------
    def _w(self) -> int:
        return max(1, self.width() - 2 * self.MARGIN)

    def x_of(self, t: float) -> float:
        return self.MARGIN + (t - self.ta) / (self.tb - self.ta) * self._w()

    def t_of(self, x: float) -> float:
        x = min(max(x, self.MARGIN), self.MARGIN + self._w())
        return self.ta + (x - self.MARGIN) / self._w() * (self.tb - self.ta)

    def _track(self) -> QRectF:
        return QRectF(self.MARGIN, self.RULER, self._w(), self.height() - self.RULER - self.FLAGS)

    def _envelope(self) -> tuple[np.ndarray, np.ndarray]:
        w = self._w()
        if self._env is not None and self._env[0].size == w:
            return self._env
        lo = np.zeros(w, np.float32)
        hi = np.zeros(w, np.float32)
        if self.sig.size:
            edges = self.ta + np.arange(w + 1) / w * (self.tb - self.ta)
            idx = np.clip(np.round((edges - self.t_sig) * self.rate).astype(int), 0, self.sig.size)
            for i in range(w):
                seg = self.sig[idx[i]:max(idx[i] + 1, idx[i + 1])]
                if seg.size:
                    lo[i], hi[i] = float(seg.min()), float(seg.max())
            peak = float(max(np.abs(lo).max(), np.abs(hi).max())) or 1.0
            lo, hi = lo / peak, hi / peak
        self._env = (lo, hi)
        return self._env

    # --- dessin ----------------------------------------------------------------------
    def _chip(self, p: QPainter, x: float, y: float, text: str, color: QColor) -> None:
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(text) + 12
        x0 = min(max(x - w / 2, 0), self.width() - w)
        r = QRectF(x0, y, w, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QColor("#16161A"))
        p.drawText(r, Qt.AlignCenter, text)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), BG)
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        tr = self._track()
        p.setPen(QPen(QColor("#2A2A30"), 1))
        p.setBrush(QColor("#1C1C21"))
        p.drawRoundedRect(tr, 6, 6)
        # règle : secondes par rapport au son du clap (graduations dans le haut de la piste)
        ref = self.ref if self.ref is not None else self.ta
        span = self.tb - self.ta
        minor, major = (0.1, 0.5) if span <= 4.5 else (0.5, 1.0)
        per_major = int(round(major / minor))
        k0 = int(np.ceil((self.ta - ref) / minor - 1e-6))
        k1 = int(np.floor((self.tb - ref) / minor + 1e-6))
        for k in range(k0, k1 + 1):
            x = self.x_of(ref + k * minor)
            is_major = k % per_major == 0
            p.setPen(QPen(QColor("#4A4A52") if is_major else QColor("#303037"), 1))
            p.drawLine(QPointF(x, tr.top() + 1), QPointF(x, tr.top() + (7 if is_major else 4)))
            if is_major and k != 0:
                lab = f"{k * minor:+g} s".replace(".", ",").replace("-", "−")
                p.setPen(QColor("#6E6B66"))
                p.drawText(QRectF(x - 30, tr.top() + 7, 60, 13), Qt.AlignCenter, lab)
        # son
        lo, hi = self._envelope()
        mid = tr.center().y() + 4
        amp = tr.height() / 2 - 10
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(QPen(WAVE, 1))
        for i in range(lo.size):
            x = self.MARGIN + i
            p.drawLine(QPointF(x, mid - hi[i] * amp), QPointF(x, mid - lo[i] * amp))
        # images réelles : petits repères au bas de la piste, s'il y a la place
        if len(self.frames) > 1 and self._w() / len(self.frames) >= 4:
            p.setPen(QPen(QColor("#3A3A42"), 1))
            for t in self.frames:
                x = round(self.x_of(t))
                p.drawLine(QPointF(x, tr.bottom() - 5), QPointF(x, tr.bottom() - 1))
        p.setRenderHint(QPainter.Antialiasing, True)
        # sons de clap détectés : le courant en plein, les autres en pointillé
        for t, current in self.claps:
            if not (self.ta <= t <= self.tb):
                continue
            x = self.x_of(t)
            p.setPen(QPen(ORANGE, 2 if current else 1, Qt.SolidLine if current else Qt.DashLine))
            p.drawLine(QPointF(x, tr.top()), QPointF(x, tr.bottom()))
            if current:
                self._chip(p, x, 1, "son du clap", ORANGE)
        if self.chosen is not None and self.ta <= self.chosen <= self.tb:
            x = self.x_of(self.chosen)
            p.setPen(QPen(GREEN, 2))
            p.drawLine(QPointF(x, tr.top()), QPointF(x, tr.bottom()))
            self._chip(p, x, tr.bottom() + 2, "image choisie", GREEN)
        if self._hover is not None and not self.dragging:
            x = self.x_of(self._hover)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.drawLine(QPointF(x, tr.top()), QPointF(x, tr.bottom()))
        # tête de lecture : trait blanc et poignée ronde
        if self.playhead is not None and self.ta <= self.playhead <= self.tb:
            x = self.x_of(self.playhead)
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.drawLine(QPointF(x, tr.top()), QPointF(x, tr.bottom()))
            r = 7.5 if self.dragging else 6.0
            p.setPen(QPen(BG, 2))
            p.setBrush(QColor("#FFFFFF"))
            p.drawEllipse(QPointF(x, tr.center().y()), r, r)
        p.end()

    def resizeEvent(self, e):
        self._env = None
        super().resizeEvent(e)

    # --- souris ------------------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.dragging = True
            self.setCursor(Qt.SizeHorCursor)
            self.scrub.emit(self.t_of(e.position().x()))

    def mouseMoveEvent(self, e):
        self._hover = self.t_of(e.position().x())
        if self.dragging and e.buttons() & Qt.LeftButton:
            self.scrub.emit(self._hover)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            self.setCursor(Qt.PointingHandCursor)
            self.released.emit(self.t_of(e.position().x()))
            self.update()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if dy and not self.dragging:
            self.step.emit(-1 if dy > 0 else 1)
        e.accept()

    def leaveEvent(self, e):
        self._hover = None
        self.update()
        super().leaveEvent(e)
