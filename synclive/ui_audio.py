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
    (blanc, poignée ronde). Appuyer ou glisser : `scrub` (on affiche) ; relâcher : `released` (on reste là)
    (l'image à cet instant est choisie)."""

    scrub = Signal(float)
    released = Signal(float)
    zoomed = Signal(bool)   # la vue a changé : zoom sur une zone (True) ou tout le plan (False)

    MARGIN = 12         # px de chaque côté : la poignée reste entière aux extrémités
    RULER = 20          # px : étiquette du son du clap et règle des secondes au-dessus de la piste
    FLAGS = 18          # px : étiquette « image choisie » sous la piste

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip("Clic ou glisser : y aller, l'image suit · molette : zoom · double-clic : tout le plan")
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
        self.norm: float | None = None     # crête de référence (celle du plan) : même hauteur partout
        self.peaks: np.ndarray | None = None   # aperçu du son de tout le plan (crêtes), à la place de sig
        self.fa, self.fb = 0.0, 1.0            # tout le plan ; [ta, tb] est la partie montrée (zoom)

    def set_data(self, sig: np.ndarray, rate: int, t_sig: float, ta: float, tb: float,
                 claps: list[tuple[float, bool]], frames: list[float] | None = None,
                 ref: float | None = None) -> None:
        self.sig, self.rate, self.t_sig = sig, rate, t_sig
        tb = max(tb, ta + 1e-3)
        if (abs(ta - self.fa) > 1e-6 or abs(tb - self.fb) > 1e-6) or not self.zoomed_in():
            self.ta, self.tb = ta, tb          # autre plan : tout le plan ; même plan : le zoom reste
        self.fa, self.fb = ta, tb
        self._tell_zoom()
        self.claps = claps
        self.frames = list(frames or [])
        self.ref = ref
        self._env = None
        self.update()

    def set_marks(self, playhead: float | None, chosen: float | None) -> None:
        self.playhead, self.chosen = playhead, chosen
        self.update()

    # --- zoom --------------------------------------------------------------------------
    def zoomed_in(self) -> bool:
        return bool(self.tb - self.ta < (self.fb - self.fa) - 1e-6)

    def _tell_zoom(self) -> None:
        self.zoomed.emit(self.zoomed_in())

    def zoom_to(self, a: float, b: float) -> None:
        a, b = sorted((a, b))
        if b - a < 0.2:                          # au plus près : 0,2 s sur toute la largeur
            c = (a + b) / 2
            a, b = c - 0.1, c + 0.1
        a, b = max(self.fa, a), min(self.fb, b)
        self.ta, self.tb = a, max(b, a + 1e-3)
        self._env = None
        self.update()
        self._tell_zoom()

    def reset_zoom(self) -> None:
        if self.zoomed_in():
            self.ta, self.tb = self.fa, self.fb
            self._env = None
            self.update()
            self._tell_zoom()

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
        if self.peaks is not None and self.peaks.size:
            # tout le plan : crêtes en décibels (48 dB sous la plus forte), la parole et les claps
            # faibles restent visibles à côté d'un choc très fort
            n = self.peaks.size
            full = max(1e-6, self.fb - self.fa)
            edges = self.ta + np.arange(w + 1) / w * (self.tb - self.ta)          # partie montrée
            idx = np.clip(((edges - self.fa) / full * n).astype(int), 0, n)
            top = float(self.peaks.max()) or 1.0
            for i in range(w):
                seg = self.peaks[idx[i]:max(idx[i] + 1, idx[i + 1])]
                pk = float(seg.max()) if seg.size else 0.0
                hi[i] = float(np.clip(1.0 + 20.0 * np.log10(max(pk, 1e-9) / top) / 48.0, 0.0, 1.0))
            lo = -hi
            self._env = (lo, hi)
            return self._env
        if self.sig.size:
            edges = self.ta + np.arange(w + 1) / w * (self.tb - self.ta)
            idx = np.clip(np.round((edges - self.t_sig) * self.rate).astype(int), 0, self.sig.size)
            for i in range(w):
                seg = self.sig[idx[i]:max(idx[i] + 1, idx[i + 1])]
                if seg.size:
                    lo[i], hi[i] = float(seg.min()), float(seg.max())
            peak = self.norm or float(max(np.abs(lo).max(), np.abs(hi).max())) or 1.0
            lo, hi = np.clip(lo / peak, -1, 1) * peak, np.clip(hi / peak, -1, 1) * peak
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
        # règle : secondes par rapport au son du clap (court passage) ou temps du fichier (tout un
        # plan), graduations dans le haut de la piste
        span = self.tb - self.ta
        ref = self.ref if self.ref is not None and span <= 12 else 0.0
        steps = ((4.5, 0.1, 0.5), (12, 0.5, 1.0), (40, 1.0, 5.0), (130, 5.0, 10.0), (400, 10.0, 60.0),
                 (1500, 60.0, 300.0))
        minor, major = next(((a, b) for lim, a, b in steps if span <= lim), (300.0, 1800.0))
        absolute = ref == 0.0
        per_major = int(round(major / minor))
        k0 = int(np.ceil((self.ta - ref) / minor - 1e-6))
        k1 = int(np.floor((self.tb - ref) / minor + 1e-6))
        for k in range(k0, k1 + 1):
            x = self.x_of(ref + k * minor)
            is_major = k % per_major == 0
            p.setPen(QPen(QColor("#4A4A52") if is_major else QColor("#303037"), 1))
            p.drawLine(QPointF(x, tr.top() + 1), QPointF(x, tr.top() + (7 if is_major else 4)))
            if is_major and (k != 0 or absolute):
                if absolute:
                    tt = k * minor
                    m_, s_ = divmod(tt, 60.0)
                    lab = (f"{int(m_)}:{s_:04.1f}".replace(".", ",") if major < 1.0
                           else f"{int(m_)}:{int(round(s_)):02d}")
                else:
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
        """Clic ou glisser n'importe où : la tête va là, l'image suit (comme au Montage)."""
        if e.button() != Qt.LeftButton:
            return
        x = e.position().x()
        self.dragging = True
        self.setCursor(Qt.SizeHorCursor)
        self.scrub.emit(self.t_of(x))

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

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.reset_zoom()                      # double-clic : tout le plan

    def wheelEvent(self, e):
        """Molette : zoom autour de la souris (comme au Montage)."""
        dy = e.angleDelta().y()
        if dy and not self.dragging:
            t = self.t_of(e.position().x())
            span = self.tb - self.ta
            full = self.fb - self.fa
            new = min(full, max(0.2, span * (0.8 if dy > 0 else 1.25)))
            frac = (t - self.ta) / max(1e-6, span)
            a = min(max(self.fa, t - frac * new), self.fb - new)
            self.ta, self.tb = a, a + new
            self._env = None
            self.update()
            self._tell_zoom()
        e.accept()

    def leaveEvent(self, e):
        self._hover = None
        self.update()
        super().leaveEvent(e)

