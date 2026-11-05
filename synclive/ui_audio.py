# SPDX-License-Identifier: GPL-3.0-or-later
"""Son dans « Régler l'image du clap » : forme d'onde alignée sur la timeline (sons de clap
détectés, image affichée, image choisie) et lecture du son avec la vidéo, ralentie au besoin."""
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


class WaveView(QWidget):
    """Forme d'onde de l'extrait chargé, sur le même axe que la timeline. Clic / glisser : se
    placer à cet instant (signal `seek`)."""

    seek = Signal(float)

    MARGIN = 9          # px : aligné sur la course de la poignée de la timeline

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(70)
        self.setMaximumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Son : en orange les sons détectés comme claps. Clic ou glisser pour s'y placer.")
        self.sig = np.zeros(0, np.float32)
        self.rate = 48000
        self.t_sig = 0.0
        self.ta, self.tb = 0.0, 1.0
        self.claps: list[tuple[float, bool]] = []     # (instant, clap courant ?)
        self.playhead: float | None = None
        self.chosen: float | None = None
        self._env: tuple[np.ndarray, np.ndarray] | None = None

    def set_data(self, sig: np.ndarray, rate: int, t_sig: float, ta: float, tb: float,
                 claps: list[tuple[float, bool]]) -> None:
        self.sig, self.rate, self.t_sig = sig, rate, t_sig
        self.ta, self.tb = ta, max(tb, ta + 1e-3)
        self.claps = claps
        self._env = None
        self.update()

    def set_marks(self, playhead: float | None, chosen: float | None) -> None:
        self.playhead, self.chosen = playhead, chosen
        self.update()

    # --- axe ---------------------------------------------------------------------------
    def x_of(self, t: float) -> float:
        w = max(1, self.width() - 2 * self.MARGIN)
        return self.MARGIN + (t - self.ta) / (self.tb - self.ta) * w

    def t_of(self, x: float) -> float:
        w = max(1, self.width() - 2 * self.MARGIN)
        return self.ta + (x - self.MARGIN) / w * (self.tb - self.ta)

    def _envelope(self) -> tuple[np.ndarray, np.ndarray]:
        w = max(1, self.width() - 2 * self.MARGIN)
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
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        r = self.rect()
        p.fillRect(r, BG)
        mid = r.height() / 2
        amp = r.height() / 2 - 4
        lo, hi = self._envelope()
        p.setPen(QPen(WAVE, 1))
        for i in range(lo.size):
            x = self.MARGIN + i
            p.drawLine(QPointF(x, mid - hi[i] * amp), QPointF(x, mid - lo[i] * amp))
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        for t, current in self.claps:
            if not (self.ta <= t <= self.tb):
                continue
            x = self.x_of(t)
            p.setPen(QPen(ORANGE, 3 if current else 1, Qt.SolidLine if current else Qt.DashLine))
            p.drawLine(QPointF(x, 0), QPointF(x, r.height()))
            if current:
                p.drawText(QRectF(x + 4, 2, 140, 14), Qt.AlignLeft, "son du clap")
        if self.chosen is not None and self.ta <= self.chosen <= self.tb:
            x = self.x_of(self.chosen)
            p.setPen(QPen(GREEN, 2))
            p.drawLine(QPointF(x, 0), QPointF(x, r.height()))
            p.drawText(QRectF(x + 4, r.height() - 16, 140, 14), Qt.AlignLeft, "image choisie")
        if self.playhead is not None and self.ta <= self.playhead <= self.tb:
            x = self.x_of(self.playhead)
            p.setPen(QPen(QColor("#FFFFFF"), 1))
            p.drawLine(QPointF(x, 0), QPointF(x, r.height()))
        p.end()

    def resizeEvent(self, e):
        self._env = None
        super().resizeEvent(e)

    def mousePressEvent(self, e):
        self.seek.emit(self.t_of(e.position().x()))

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self.seek.emit(self.t_of(e.position().x()))
