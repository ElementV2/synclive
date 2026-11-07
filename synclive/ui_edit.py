# SPDX-License-Identifier: GPL-3.0-or-later
"""Banc de montage (onglet Montage) : enlever le début, la fin, les écrans d'attente ou de pause
d'un live entier, couper, supprimer et réordonner des morceaux, mettre plusieurs fichiers bout à
bout (enregistrement découpé, générique…), sans perte de qualité.

Fait pour des fichiers de plusieurs heures : rien n'est lu en entier à l'ouverture (les images
clés sont cherchées à la demande, l'aperçu du son se remplit en tâche de fond) ; l'aperçu de
l'image suit la timeline en temps réel. Les coupes tombent sur les images clés : la vidéo est
recopiée telle quelle. Le montage est une liste de morceaux (fichier, début, fin) dans l'ordre
de sortie."""
from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import av
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from . import montage as M
from .ui_clap import FrameView
from .ui_threads import release

Segment = tuple[int, float, float]        # (n° du fichier, début, fin) — instants du fichier

ORANGE = QColor("#FF5A1F")
GREEN = QColor("#3FB950")
BG = QColor("#16161A")
MUTED = "#8A8780"
SEG_COLORS = ["#3FB950", "#58A6FF", "#D2A8FF", "#E3B341", "#FF7B72", "#79C0FF", "#56D364", "#F0883E"]


def fmt_t(t: float, ms: bool = True) -> str:
    t = max(0.0, t)
    m, s = divmod(t, 60.0)
    h, m = divmod(int(m), 60)
    sec = f"{s:06.3f}" if ms else f"{int(s):02d}"
    return (f"{h}:{m:02d}:{sec}" if h else f"{m}:{sec}").replace(".", ",")


def kept(segs) -> float:
    return float(sum(seg[-1] - seg[-2] for seg in segs))


class Source:
    """Un fichier du montage : bornes, images clés (cherchées à la demande), aperçu du son."""

    def __init__(self, media: M.Media, overview: M.Overview | None = None):
        self.media = media
        self.path = media.path
        self.name = Path(media.path).name
        self.start, self.end = media.start, media.end
        self.has_video = media.has_video
        self.keys = M.KeyIndex(media)
        self.overview = overview

    def snap(self, t: float) -> float:
        return self.keys.snap(t)

    def step(self, t: float, d: int) -> float:
        return self.keys.step(t, d)

    def known(self) -> list[float]:
        return sorted(self.keys.known)

    def close(self) -> None:
        if self.overview is not None:
            self.overview.stop()
        self.keys.close()


class ScrubDecoder(QThread):
    """Aperçu en temps réel pendant qu'on se déplace (le lecteur Qt ne suit pas un glisser) :
    pour chaque position demandée, on montre aussitôt l'image clé qui la précède (une seule image
    à décoder), puis l'image exacte si aucune nouvelle position n'est arrivée entre-temps."""

    frame = Signal(float, object, bool)      # instant de l'image, RGB, image exacte ?

    MAX_W = 1280

    def __init__(self, path: str, keyframes: list[float] | None = None):
        super().__init__()
        self.path = path
        self.keys = np.asarray(sorted(keyframes or []), dtype=np.float64)
        self._cond = threading.Condition()
        self._target: float | None = None
        self._gen = 0
        self._stop = False

    def request(self, t: float) -> None:
        with self._cond:
            self._target = t
            self._gen += 1
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        if not self.wait(3000):
            release(self)                        # image en cours de décodage : gardé jusqu'au bout

    def _newer(self, gen: int) -> bool:
        return self._gen != gen or self._stop

    BUDGET = 0.12      # s : au-delà, pendant un glisser, on montre l'image la plus proche obtenue

    def run(self) -> None:
        import time
        try:
            c = av.open(self.path)
        except Exception:
            return
        vs = next((s for s in c.streams.video), None)
        if vs is None:
            c.close()
            return
        vs.thread_type = "AUTO"
        cc = vs.codec_context
        w = min(self.MAX_W, cc.width or self.MAX_W)
        h = max(2, int(round((cc.height or 720) * w / max(1, cc.width or w))) // 2 * 2)
        rate = float(vs.average_rate or 30)
        half = 0.5 / rate
        frames = None          # décodage en cours (on continue sans ressauter quand on avance)
        cur = None             # dernière image décodée (instant, image)
        ahead = None           # image décodée en trop (elle dépasse la position précédente)
        try:
            while True:
                with self._cond:
                    while self._target is None and not self._stop:
                        self._cond.wait()
                    if self._stop:
                        return
                    t = self._target
                    self._target = None
                # un peu plus loin, sans image clé entre les deux : on poursuit le décodage ;
                # sinon on saute (repartir de l'image clé coûte moins que tout décoder)
                near = cur is not None and cur[0] - half <= t <= cur[0] + 3.0
                if near and self.keys.size:
                    i = int(np.searchsorted(self.keys, cur[0] + half, side="right"))
                    near = not (i < self.keys.size and self.keys[i] <= t + half)
                if not near or frames is None:
                    try:
                        c.seek(int(max(0.0, t) / vs.time_base), stream=vs, backward=True, any_frame=False)
                    except Exception:
                        continue
                    frames, cur, ahead = c.decode(vs), None, None
                t0 = time.perf_counter()
                exact = True
                while True:
                    if ahead is not None:
                        nxt, ahead = ahead, None
                    else:
                        try:
                            fr = next(frames)
                        except (StopIteration, av.error.EOFError):
                            frames = None
                            break
                        except Exception:
                            frames = None
                            break
                        nxt = (float(fr.time) if fr.time is not None else t, fr)
                    if nxt[0] > t + half:
                        ahead = nxt                    # dépasse : la précédente est la bonne
                        break
                    cur = nxt
                    if time.perf_counter() - t0 > self.BUDGET and self._target is not None:
                        exact = False                  # on bouge encore : on montre celle-ci
                        break
                if cur is not None:
                    self.frame.emit(cur[0], cur[1].to_ndarray(format="rgb24", width=w, height=h), exact)
        finally:
            c.close()



class EditTimeline(QWidget):
    """La timeline EST le montage (comme dans un logiciel de montage) : les morceaux gardés bout à
    bout, dans l'ordre, en temps du montage. Ce qui est enlevé disparaît ; supprimer un morceau
    recolle les suivants. Chaque morceau montre son son et son n° (et son fichier s'il y en a
    plusieurs) ; DÉBUT et FIN du montage, raccords ✂. Les zones repérées comme pauses sont hachurées.
    Clic / glisser : se placer. Molette : zoom autour de la souris ; « Tout voir » : tout le montage."""

    seek = Signal(float)          # instant du montage (s depuis le début du montage)
    picked = Signal(int)          # morceau cliqué
    released = Signal()           # fin du clic / glisser

    MARGIN = 12

    def __init__(self, srcs: list[Source], parent=None):
        super().__init__(parent)
        self.srcs = srcs
        self.segments: list[Segment] = []
        self.selected: int | None = None
        self.playhead = 0.0
        self.v0, self.v1 = 0.0, 1.0
        self._fit = True              # vue : tout le montage (suit les modifications)
        self.hover: float | None = None
        self._drag = False
        self.pauses: list[tuple[int, float, float]] = []     # pauses repérées (fichier, début, fin)
        self.setMinimumHeight(118)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._discover = QTimer(self)
        self._discover.setSingleShot(True)
        self._discover.setInterval(120)
        self._discover.timeout.connect(self._find_keys)

    # --- montage <-> fichier -----------------------------------------------------------
    def total(self) -> float:
        return max(1e-6, kept(self.segments))

    def starts(self) -> list[float]:
        out, pos = [], 0.0
        for _s, a, b in self.segments:
            out.append(pos)
            pos += b - a
        return out

    def locate(self, p: float) -> tuple[int, float]:
        """Instant du montage -> (morceau, instant de son fichier)."""
        p = min(max(p, 0.0), self.total())
        pos = 0.0
        for i, (_s, a, b) in enumerate(self.segments):
            if p < pos + (b - a) or i == len(self.segments) - 1:
                return i, min(b, a + (p - pos))
            pos += b - a
        return 0, self.segments[0][1] if self.segments else 0.0

    # --- axe ---------------------------------------------------------------------------
    def _w(self) -> float:
        return max(1.0, self.width() - 2 * self.MARGIN)

    def x_of(self, p: float) -> float:
        return self.MARGIN + (p - self.v0) / max(1e-6, self.v1 - self.v0) * self._w()

    def t_of(self, x: float) -> float:
        x = min(max(x, self.MARGIN), self.MARGIN + self._w())
        return self.v0 + (x - self.MARGIN) / self._w() * (self.v1 - self.v0)

    def show_all(self) -> None:
        self._fit = True
        self.v0, self.v1 = 0.0, self.total()
        self.update()

    def follow(self, p: float) -> None:
        """Garde la tête de lecture visible quand on est zoomé."""
        span = self.v1 - self.v0
        if p < self.v0 or p > self.v1:
            self.v0 = min(max(0.0, p - span * 0.1), max(0.0, self.total() - span))
            self.v1 = self.v0 + span

    def set_state(self, segments, selected, playhead) -> None:
        self.segments, self.selected, self.playhead = list(segments), selected, playhead
        if self._fit:
            self.v0, self.v1 = 0.0, self.total()
        else:
            self.v1 = min(self.v1, self.total())
            self.v0 = min(self.v0, max(0.0, self.v1 - 2.0))
        self.update()

    def _find_keys(self) -> None:
        """Zoomé : on va chercher les images clés de la zone visible (pour les montrer)."""
        if self.v1 - self.v0 > 180:
            return
        for (s, a, b), o in zip(self.segments, self.starts()):
            src = self.srcs[s]
            if not src.has_video:
                continue
            lo, hi = max(a, a + self.v0 - o), min(b, a + self.v1 - o)
            t = lo
            while t < hi:
                _b, nx = src.keys.around(t)
                if nx is None or nx <= t:
                    break
                t = nx + 1e-3
        self.update()

    # --- dessin ------------------------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        top, bottom = 22.0, self.height() - 18.0
        tr = QRectF(self.MARGIN, top, self._w(), bottom - top)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#2A2A30"), 1))
        p.setBrush(QColor("#1C1C21"))
        p.drawRoundedRect(tr, 6, 6)
        span = self.v1 - self.v0
        steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]
        step = next((s for s in steps if span / s <= self._w() / 90), steps[-1])
        k = math.ceil(self.v0 / step)
        p.setPen(QColor("#6E6B66"))
        while k * step <= self.v1:
            x = self.x_of(k * step)
            p.drawLine(QPointF(x, top - 5), QPointF(x, top))
            p.drawText(QRectF(x - 40, 2, 80, 14), Qt.AlignCenter, fmt_t(k * step, ms=step < 1))
            k += 1
        starts = self.starts()
        mid = tr.center().y()
        amp = tr.height() / 2 - 8
        multi = len({s for s, _a, _b in self.segments}) > 1
        fm = p.fontMetrics()
        for i, ((s, a, b), o) in enumerate(zip(self.segments, starts)):
            src = self.srcs[s]
            x0, x1 = self.x_of(o), self.x_of(o + (b - a))
            if x1 < tr.left() or x0 > tr.right():
                continue
            cx0, cx1 = max(x0, tr.left()), min(x1, tr.right())
            col = QColor(SEG_COLORS[i % len(SEG_COLORS)])
            fill = QColor(col)
            fill.setAlpha(70 if i == self.selected else 40)
            p.fillRect(QRectF(cx0, tr.top(), cx1 - cx0, tr.height()), fill)
            ov = src.overview
            if ov is not None:                              # son du morceau (aperçu en tâche de fond)
                p.setRenderHint(QPainter.Antialiasing, False)
                wave = QColor(col)
                wave.setAlpha(150)
                p.setPen(QPen(wave, 1))
                lv = ov.levels
                for px in range(int(cx0), int(cx1)):
                    t0 = a + (self.t_of(px) - o)
                    t1 = a + (self.t_of(px + 1) - o)
                    if t0 > ov.done_until:
                        break
                    i0 = int((t0 - src.start) / ov.STEP)
                    i1 = max(i0 + 1, int((t1 - src.start) / ov.STEP))
                    seg = lv[max(0, i0):min(lv.size, i1)]
                    if seg.size == 0:
                        continue
                    v = float(np.clip((seg.max() + 60.0) / 60.0, 0.0, 1.0))
                    p.drawLine(QPointF(px, mid - v * amp), QPointF(px, mid + v * amp))
                p.setRenderHint(QPainter.Antialiasing, True)
            # pauses repérées dans ce morceau : hachures orange
            for ps, pa, pb in self.pauses:
                if ps != s or pb <= a or pa >= b:
                    continue
                hx0, hx1 = self.x_of(o + max(pa, a) - a), self.x_of(o + min(pb, b) - a)
                hx0, hx1 = max(hx0, tr.left()), min(hx1, tr.right())
                if hx1 - hx0 < 1:
                    continue
                r = QRectF(hx0, tr.top() + 1, hx1 - hx0, tr.height() - 2)
                hatch = QColor(ORANGE)
                hatch.setAlpha(95)
                p.fillRect(r, hatch)
                p.setPen(QPen(QColor(255, 140, 80, 200), 1, Qt.DashLine))
                p.drawRect(r)
                if r.width() > 56:
                    p.setPen(QColor("#FFB38A"))
                    p.drawText(r.adjusted(4, 0, -2, 0), Qt.AlignBottom | Qt.AlignLeft, "pause ?")
            # n° du morceau (et son fichier quand le montage en a plusieurs)
            lab = f"{i + 1} · {src.name}" if multi else str(i + 1)
            if cx1 - cx0 > fm.horizontalAdvance(lab) + 12:
                p.setPen(col)
                p.drawText(QRectF(cx0 + 6, tr.bottom() - 17, cx1 - cx0 - 10, 14), Qt.AlignLeft, lab)
            elif cx1 - cx0 > 22:
                p.setPen(col)
                p.drawText(QRectF(cx0 + 6, tr.bottom() - 17, 30, 14), Qt.AlignLeft, str(i + 1))
        # raccords entre morceaux : trait blanc et ciseaux
        for o in starts[1:]:
            x = self.x_of(o)
            if tr.left() <= x <= tr.right():
                p.setPen(QPen(QColor(255, 255, 255, 220), 2))
                p.drawLine(QPointF(x, tr.top() + 2), QPointF(x, tr.bottom() - 2))
                p.setPen(QColor("#F2F0EB"))
                p.drawText(QRectF(x - 8, tr.top() + 2, 16, 14), Qt.AlignCenter, "✂")
        # début et fin du montage : instant dans le montage, et dans le fichier
        if self.segments:
            s0, a0, _b0 = self.segments[0]
            s1, _a1, b1 = self.segments[-1]
            for x, start, s, t_file in ((self.x_of(0.0), True, s0, a0), (self.x_of(self.total()), False, s1, b1)):
                if not (tr.left() - 1 <= x <= tr.right() + 1):
                    continue
                src = self.srcs[s]
                t_mont = 0.0 if start else self.total()
                trimmed = (abs(t_file - src.start) > 0.05) if start else (abs(t_file - src.end) > 0.05)
                col = ORANGE if trimmed else QColor("#B8B5AE")
                p.setPen(QPen(col, 3))
                p.drawLine(QPointF(x, tr.top()), QPointF(x, tr.bottom()))
                d = 8 if start else -8
                p.drawLine(QPointF(x, tr.top() + 1.5), QPointF(x + d, tr.top() + 1.5))
                p.drawLine(QPointF(x, tr.bottom() - 1.5), QPointF(x + d, tr.bottom() - 1.5))
                what = "DÉBUT" if start else "FIN"
                where = f"{src.name} " if multi else "fichier "
                lab = f"{what} {fmt_t(t_mont, False)} · {where}{fmt_t(t_file - src.start, False)}"
                wl = fm.horizontalAdvance(lab) + 12
                r = QRectF(x + 5 if start else x - 5 - wl, tr.top() + 4, wl, 16)
                self._chip(p, r, col, lab)
        # images clés connues (zoomé) : points de coupe possibles
        if span <= 600:
            p.setPen(QPen(QColor("#B8B5AE"), 1))
            for (s, a, b), o in zip(self.segments, starts):
                keys = self.srcs[s].known()
                lo = int(np.searchsorted(keys, a))
                hi = int(np.searchsorted(keys, b))
                for t in keys[lo:hi]:
                    x = self.x_of(o + t - a)
                    if tr.left() <= x <= tr.right():
                        p.drawLine(QPointF(x, tr.bottom() - 6), QPointF(x, tr.bottom() - 1))
        if span <= 180 and not self._discover.isActive():
            self._discover.start()
        # survol (zoomé) : où tomberait la coupe
        if self.hover is not None and not self._drag and span <= 600 and self.segments:
            i, t = self.locate(self.hover)
            s, a, b = self.segments[i]
            ts = min(max(self.srcs[s].snap(t), a), b)
            xs = self.x_of(starts[i] + ts - a)
            p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.DashLine))
            p.drawLine(QPointF(xs, tr.top()), QPointF(xs, tr.bottom()))
        # tête de lecture
        if self.v0 <= self.playhead <= self.v1:
            x = self.x_of(self.playhead)
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.drawLine(QPointF(x, tr.top() - 4), QPointF(x, tr.bottom() + 4))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#FFFFFF"))
            p.drawEllipse(QPointF(x, tr.top() - 4), 5, 5)
            lab = fmt_t(self.playhead)
            wl = fm.horizontalAdvance(lab) + 10
            r = QRectF(min(max(x - wl / 2, 0), self.width() - wl), bottom + 2, wl, 15)
            p.setBrush(QColor("#F2F0EB"))
            p.drawRoundedRect(r, 3, 3)
            p.setPen(QColor("#16161A"))
            p.drawText(r, Qt.AlignCenter, lab)
        p.end()

    @staticmethod
    def _chip(p: QPainter, r: QRectF, col: QColor, text: str) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(r, 3, 3)
        p.setPen(QColor("#16161A"))
        p.drawText(r, Qt.AlignCenter, text)

    # --- souris ------------------------------------------------------------------------
    def _seg_at(self, pos: float) -> int | None:
        if not self.segments:
            return None
        return self.locate(pos)[0]

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = True
            pos = self.t_of(e.position().x())
            i = self._seg_at(pos)
            if i is not None:
                self.picked.emit(i)
            self.seek.emit(pos)

    def mouseMoveEvent(self, e):
        self.hover = self.t_of(e.position().x())
        if self._drag and e.buttons() & Qt.LeftButton:
            self.seek.emit(self.hover)
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = False
        self.update()
        self.released.emit()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if not dy:
            return
        t = self.t_of(e.position().x())
        span = self.v1 - self.v0
        new = min(self.total(), max(2.0, span * (0.8 if dy > 0 else 1.25)))
        frac = (t - self.v0) / max(1e-6, span)
        self.v0 = min(max(0.0, t - frac * new), max(0.0, self.total() - new))
        self.v1 = self.v0 + new
        self._fit = new >= self.total() - 1e-6
        self.update()
        e.accept()


class TrackMeter(QWidget):
    """Vumètre de toutes les pistes son en même temps, à côté de l'image : une barre par piste
    (crête, −60 à 0 dBFS), un trait qui garde la crête 1,5 s, la valeur au-dessus, la piste écoutée
    en orange. Clic sur une barre : écouter cette piste."""

    picked = Signal(int)

    FLOOR = -60.0
    TICKS = (0, -6, -12, -20, -30, -40, -60)
    BAR, GAP, LEFT = 16, 8, 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.n = -1
        self.listened = 0
        self.shown: list[float | None] = []       # niveau affiché (avec retombée)
        self.hold: list[tuple[float, float]] = []  # (crête gardée, instant)
        self._last = time.perf_counter()
        self.setToolTip("Niveau de chaque piste son à la tête de lecture.\n"
                        "Clic sur une barre : écouter cette piste.")
        self.setCursor(Qt.PointingHandCursor)
        self.set_count(0)

    def set_count(self, n: int) -> None:
        if n == self.n:
            return
        self.n = n
        self.shown = [None] * n
        self.hold = [(self.FLOOR, 0.0)] * n
        self.setFixedWidth(self.LEFT + max(1, n) * (self.BAR + self.GAP) + 4)
        self.setVisible(n > 0)
        self.update()

    def set_levels(self, levels: list[float | None], playing: bool) -> None:
        """`levels` : crête de chaque piste (dBFS), None si inconnue. En lecture, la barre retombe
        doucement (24 dB/s) ; à l'arrêt elle montre le niveau à la tête, tel quel."""
        now = time.perf_counter()
        dt = min(0.2, now - self._last)
        self._last = now
        self.set_count(len(levels))
        for k, lv in enumerate(levels):
            old = self.shown[k]
            if lv is None:
                self.shown[k] = old - 24.0 * dt if playing and old is not None else None
            elif playing and old is not None:
                self.shown[k] = max(lv, old - 24.0 * dt)
            else:
                self.shown[k] = lv
            v = self.shown[k]
            h, ht = self.hold[k]
            if v is not None and (v >= h or now - ht > 1.5 or not playing):
                self.hold[k] = (v, now)
        self.update()

    def _y(self, db: float, top: float, h: float) -> float:
        f = (min(0.0, max(self.FLOOR, db)) - self.FLOOR) / -self.FLOOR
        return top + h * (1.0 - f)

    def _bar_x(self, k: int) -> float:
        return self.LEFT + k * (self.BAR + self.GAP)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        top, bottom = 22.0, self.height() - 22.0
        h = max(10.0, bottom - top)
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        for db in self.TICKS:                     # échelle
            y = self._y(db, top, h)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(0, y - 7, self.LEFT - 5, 14), Qt.AlignRight | Qt.AlignVCenter, str(db))
            p.setPen(QPen(QColor("#2A2A30"), 1))
            p.drawLine(QPointF(self.LEFT - 2, y), QPointF(self.width() - 2, y))
        for k in range(self.n):
            x = self._bar_x(k)
            p.fillRect(QRectF(x, top, self.BAR, h), QColor("#222228"))
            v = self.shown[k]
            if v is not None and v > self.FLOOR:
                for lo, hi, col in ((self.FLOOR, -18.0, "#3FB950"), (-18.0, -6.0, "#E3B341"),
                                    (-6.0, 0.0, "#F85149")):
                    if v <= lo:
                        break
                    y0, y1 = self._y(min(v, hi), top, h), self._y(lo, top, h)
                    p.fillRect(QRectF(x, y0, self.BAR, y1 - y0), QColor(col))
            hv, _ht = self.hold[k]
            if v is not None and hv > self.FLOOR:
                y = self._y(hv, top, h)
                p.fillRect(QRectF(x, y - 1, self.BAR, 2), QColor("#F85149") if hv > -1.0 else QColor("#E6E6E6"))
            p.setPen(QColor(MUTED))
            txt = "—" if v is None or hv <= self.FLOOR else f"{hv:.0f}"
            p.drawText(QRectF(x - 4, 2, self.BAR + 8, 16), Qt.AlignCenter, txt)
            on = k == self.listened
            fb = QFont(f)
            fb.setBold(on)
            fb.setPointSizeF(9)
            p.setFont(fb)
            p.setPen(ORANGE if on else QColor("#C9C7C1"))
            p.drawText(QRectF(x - 4, bottom + 3, self.BAR + 8, 18), Qt.AlignCenter, str(k + 1))
            p.setFont(f)
            if on:
                p.setPen(QPen(ORANGE, 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x - 1, top - 1, self.BAR + 2, h + 2))
        p.end()

    def mousePressEvent(self, e):
        x = e.position().x()
        for k in range(self.n):
            bx = self._bar_x(k)
            if bx - self.GAP / 2 <= x <= bx + self.BAR + self.GAP / 2:
                self.picked.emit(k)
                return


class EditorPanel(QWidget):
    """Couper, supprimer, déplacer des morceaux d'un ou plusieurs fichiers. `self.segments` : le
    montage, liste de (n° du fichier, début, fin)."""

    changed = Signal()

    def __init__(self, srcs: list[Source], segments: list[Segment] | None = None, clip: int = 0,
                 t: float | None = None, parent=None):
        super().__init__(parent)
        self.srcs: list[Source] = list(srcs)
        self.segments: list[Segment] = [tuple(x) for x in segments] if segments else \
            [(k, s.start, s.end) for k, s in enumerate(self.srcs)]
        self._undo: list = []
        self.selected: int | None = 0
        self._clip = min(max(clip, 0), len(self.segments) - 1)        # morceau sous la tête de lecture
        self._t = t if t is not None else self.segments[self._clip][1]  # instant de son fichier
        self._jumping = False                   # saut d'un morceau au suivant en cours (lecture)
        self._frame_t: float | None = None
        self._held = False
        self._psrc: int | None = None           # fichier chargé dans le lecteur
        self._pending: float | None = None      # lecture à lancer dès que le fichier est chargé
        self._seen_loading = True
        self._load_started = 0.0
        self._scrubs: dict[int, ScrubDecoder] = {}
        self._closed = False

        v = QVBoxLayout(self)
        v.setSpacing(10)
        help_ = QLabel("Coupes calées sur les images clés (la tête de lecture y saute pour montrer l'image exacte de "
                       "la coupe) : la vidéo est recopiée telle quelle, sans aucune perte de qualité. La timeline est "
                       "le montage : supprimer un morceau recolle les suivants.  Espace : lecture · ← → : image par "
                       "image · Maj+← → : 1 s · , . : image clé · I : début ici · O : fin ici · C : couper ici · "
                       "Suppr : supprimer le morceau · glisser les morceaux en bas pour changer l'ordre · Ctrl+Z.")
        help_.setWordWrap(True)
        help_.setStyleSheet(f"color:{MUTED};")
        v.addWidget(help_)

        # un seul afficheur, en lecture comme à l'arrêt : les images du lecteur Qt (lecture) et celles
        # de notre aperçu (arrêt, glisser) sont dessinées au même endroit, à la même échelle, sur le
        # même fond noir (passer d'un afficheur à l'autre faisait changer le zoom et le fond)
        self.still = FrameView(background="#000000")
        self.still.setMinimumSize(480, 270)
        pic = QHBoxLayout()
        pic.setSpacing(8)
        pic.addWidget(self.still, 1)
        self.meter = TrackMeter()                 # toutes les pistes son en même temps
        pic.addWidget(self.meter)
        v.addLayout(pic, 1)
        self._readers: dict[int, M.LevelReader] = {}
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(33)
        self._meter_timer.timeout.connect(self._update_meter)
        self._meter_timer.start()
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_video_frame)
        self.player.setVideoSink(self.sink)
        self.player.positionChanged.connect(self._on_pos)
        self.player.positionChanged.connect(self._on_seeked)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media)
        self._load_player(self._src_index())

        self.timeline = EditTimeline(self.srcs)
        self.timeline.seek.connect(self._goto_out)
        self.timeline.released.connect(self._resume_soon)
        # se déplacer pendant la lecture : pause, l'image suit (sans son), puis UNE reprise au
        # nouvel endroit (des sauts à chaque mouvement faisaient se superposer le son)
        self._resume = False
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.setInterval(300)
        self._resume_timer.timeout.connect(self._do_resume)
        self.timeline.picked.connect(self._select)
        v.addWidget(self.timeline)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(44, 40)
        self.play_btn.setObjectName("primary")
        self.play_btn.setToolTip("Lecture / pause (Espace)")
        self.play_btn.clicked.connect(self._toggle)
        row.addWidget(self.play_btn)
        prev_k = QPushButton("⏮")
        prev_k.setToolTip("Image clé précédente ( , )")
        prev_k.clicked.connect(lambda: self._key_step(-1))
        next_k = QPushButton("⏭")
        next_k.setToolTip("Image clé suivante ( . )")
        next_k.clicked.connect(lambda: self._key_step(1))
        row.addWidget(prev_k)
        row.addWidget(next_k)
        self.time_lbl = QLabel()
        self.time_lbl.setStyleSheet("font-family:Consolas,'Segoe UI'; font-size:15px; font-weight:600;")
        self.time_lbl.setMinimumWidth(190)
        row.addWidget(self.time_lbl)
        self.src_lbl = QLabel()
        self.src_lbl.setStyleSheet(f"color:{MUTED}; font-family:Consolas,'Segoe UI';")
        self.src_lbl.setToolTip("Instant correspondant dans le fichier d'origine")
        row.addWidget(self.src_lbl)
        row.addSpacing(12)
        self.start_btn = QPushButton("Début ici")
        self.start_btn.setToolTip("Enlève tout ce qui est avant la tête de lecture dans le montage (I)")
        self.start_btn.clicked.connect(self.start_here)
        self.end_btn = QPushButton("Fin ici")
        self.end_btn.setToolTip("Enlève tout ce qui est après la tête de lecture dans le montage (O)")
        self.end_btn.clicked.connect(self.end_here)
        self.split_btn = QPushButton("Couper ici")
        self.split_btn.setToolTip("Coupe le morceau en deux à la tête de lecture (C)")
        self.split_btn.clicked.connect(self.split_here)
        self.del_btn = QPushButton("Supprimer le morceau")
        self.del_btn.setToolTip("Enlève le morceau sélectionné, les suivants se recollent (Suppr)")
        self.del_btn.clicked.connect(self.delete_selected)
        for b in (self.start_btn, self.end_btn, self.split_btn, self.del_btn):
            row.addWidget(b)
        row.addStretch(1)
        self.undo_btn = QPushButton("Annuler")
        self.undo_btn.setToolTip("Annule la dernière modification (Ctrl+Z)")
        self.undo_btn.clicked.connect(self.undo)
        reset = QPushButton("Tout rétablir")
        reset.setToolTip("Revient aux fichiers entiers, bout à bout")
        reset.clicked.connect(self.reset)
        allv = QPushButton("Tout voir")
        allv.setToolTip("Tout le montage dans la timeline (molette : zoom)")
        allv.clicked.connect(self.timeline.show_all)
        row.addWidget(allv)
        row.addWidget(self.undo_btn)
        row.addWidget(reset)
        v.addLayout(row)

        seg_row = QHBoxLayout()
        lab = QLabel("Montage :")
        lab.setStyleSheet(f"color:{MUTED};")
        seg_row.addWidget(lab)
        self.list = QListWidget()
        self.list.setFlow(QListWidget.LeftToRight)
        self.list.setWrapping(False)
        self.list.setFixedHeight(58)
        self.list.setSpacing(4)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setStyleSheet("QListWidget { background:#19191D; border:1px solid #2A2A30; border-radius:6px; }"
                                "QListWidget::item { border-radius:5px; padding:6px 10px; margin:2px; }"
                                "QListWidget::item:selected { background:#2E2E35; }")
        self.list.model().rowsMoved.connect(self._reordered)
        self.list.currentRowChanged.connect(lambda r: self._select(r, from_list=True))
        self.list.itemDoubleClicked.connect(
            lambda it: self._goto_out(self.timeline.starts()[self.list.row(it)]))
        seg_row.addWidget(self.list, 1)
        self.left_btn = QPushButton("◀")
        self.left_btn.setToolTip("Déplacer le morceau sélectionné plus tôt dans le montage")
        self.left_btn.clicked.connect(lambda: self.move_segment(self.selected or 0, -1))
        self.right_btn = QPushButton("▶")
        self.right_btn.setToolTip("Déplacer le morceau sélectionné plus tard dans le montage")
        self.right_btn.clicked.connect(lambda: self.move_segment(self.selected or 0, 1))
        seg_row.addWidget(self.left_btn)
        seg_row.addWidget(self.right_btn)
        v.addLayout(seg_row)

        self.summary = QLabel()
        self.summary.setStyleSheet("font-size:14px;")
        v.addWidget(self.summary)

        for key, fn in ((Qt.Key_Space, self._toggle), (Qt.Key_I, self.start_here), (Qt.Key_O, self.end_here),
                        (Qt.Key_C, self.split_here), (Qt.Key_Delete, self.delete_selected),
                        (Qt.Key_Comma, lambda: self._key_step(-1)), (Qt.Key_Period, lambda: self._key_step(1)),
                        (Qt.Key_Left, lambda: self._frame_step(-1)), (Qt.Key_Right, lambda: self._frame_step(1))):
            QShortcut(QKeySequence(key), self, activated=fn)
        QShortcut(QKeySequence(Qt.SHIFT | Qt.Key_Left), self, activated=lambda: self._nudge(-1.0))
        QShortcut(QKeySequence(Qt.SHIFT | Qt.Key_Right), self, activated=lambda: self._nudge(1.0))
        # les raccourcis vont toujours au banc : un bouton cliqué ne doit pas prendre Espace (il se
        # relancerait), ni la liste des morceaux les flèches (elle changerait de morceau)
        for wdg in self.findChildren(QPushButton) + [self.list, self.timeline]:
            wdg.setFocusPolicy(Qt.NoFocus)
        self.setFocusPolicy(Qt.StrongFocus)
        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        self._refresh()
        self._seek(self._t)                       # image sous la tête, tout de suite

    # --- fichiers --------------------------------------------------------------------------
    @property
    def src(self) -> Source:
        """Fichier du morceau sous la tête de lecture."""
        return self.srcs[self._src_index()]

    def _src_index(self) -> int:
        return self.segments[min(self._clip, len(self.segments) - 1)][0]

    def _scrub(self, s: int) -> ScrubDecoder | None:
        if not self.srcs[s].has_video:
            return None
        if s not in self._scrubs:
            sd = ScrubDecoder(self.srcs[s].path)
            sd.frame.connect(lambda ft, img, ex, s=s: self._on_still(s, ft, img, ex))
            sd.start()
            self._scrubs[s] = sd
        return self._scrubs[s]

    def _preview(self) -> None:
        sd = self._scrub(self._src_index())
        if sd is not None:
            sd.request(self._t)
        elif not self.src.has_video:
            self.still.set_message("Fichier son seul")

    def _load_player(self, s: int) -> None:
        if self._psrc != s:
            self._psrc = s
            # au changement de fichier, le lecteur envoie d'abord un « chargé » qui concerne encore
            # l'ancien : on attend d'avoir vu le chargement du nouveau (filet : 2 s au plus)
            self._seen_loading = False
            self._load_started = time.perf_counter()
            self.player.setSource(QUrl.fromLocalFile(self.srcs[s].path))

    # --- vumètre ---------------------------------------------------------------------------
    def _head_time(self) -> float:
        """Instant du fichier sous la tête, au plus juste (en lecture, la position du lecteur)."""
        if self._playing() and not self._jumping and self._pending is None and self._psrc == self._src_index():
            t = self.src.start + self.player.position() / 1000.0
            a, b = self._clip_bounds()
            if a - 0.05 <= t <= b:
                return t
        return self._t

    def track_levels(self, s: int, t: float) -> list[float | None]:
        """Crête de chaque piste du fichier `s` sur les 100 ms qui précèdent `t` (dBFS) : d'après
        l'aperçu s'il est déjà passé par là, sinon d'après une lecture à la volée."""
        src = self.srcs[s]
        n = len(src.media.audio)
        ov = src.overview
        st = M.Overview.STEP
        if ov is not None and t + st < ov.done_until:
            i = int((t - src.start) / st)
            return [float(ov.tracks[k][max(0, i - 1):i + 1].max()) if 0 <= i < ov.tracks[k].size else None
                    for k in range(n)]
        r = self._readers.get(s)
        if r is None:
            r = self._readers[s] = M.LevelReader(src.media)
            r.start()
        r.request(t)
        out: list[float | None] = []
        for k in range(n):
            vals = [x for x in (r.level(k, t - st), r.level(k, t)) if x is not None]
            out.append(max(vals) if vals else None)
        return out

    def _update_meter(self) -> None:
        if self._closed or not self.isVisible():
            return
        n = max((len(x.media.audio) for x in self.srcs), default=0)
        s = self._src_index()
        lv = self.track_levels(s, self._head_time()) if self.srcs[s].media.audio else []
        lv = lv + [None] * (n - len(lv))
        self.meter.set_levels(lv, self._playing())

    def set_listened(self, k: int) -> None:
        self.meter.listened = k
        self.meter.update()

    def add_source(self, src: Source) -> None:
        """Ajoute un fichier à la fin du montage (en entier)."""
        self.srcs.append(src)
        k = len(self.srcs) - 1
        self._apply(self.segments + [(k, src.start, src.end)], len(self.segments), self._clip, self._t)

    # --- montage <-> fichier --------------------------------------------------------------
    def _out(self) -> float:
        """Position de la tête dans le montage."""
        if not self.segments:
            return 0.0
        i = min(self._clip, len(self.segments) - 1)
        _s, a, b = self.segments[i]
        return kept(self.segments[:i]) + (min(max(self._t, a), b) - a)

    def _goto_out(self, p: float, resume: bool = True) -> None:
        """Se placer à un instant du montage (clic, glisser, pas à pas qui change de morceau)."""
        i, t = self.timeline.locate(p)
        self._set_clip(i)
        self._seek(t, resume)

    def _set_clip(self, i: int) -> None:
        self._clip = i

    def _clip_bounds(self) -> tuple[float, float]:
        _s, a, b = self.segments[min(self._clip, len(self.segments) - 1)]
        return a, b

    def _relocate(self, s: int) -> None:
        """Après une modification : la tête reste dans un morceau existant (du même fichier)."""
        for i, (ss, a, b) in enumerate(self.segments):
            if ss == s and (a - 1e-6 <= self._t < b - 1e-6 or (i == len(self.segments) - 1 and abs(self._t - b) < 1e-6)):
                self._clip = i
                return
        self._clip = min(self._clip, len(self.segments) - 1)
        self._t = self.segments[self._clip][1]

    # --- lecture ------------------------------------------------------------------------
    def _playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def _seek(self, t: float, resume: bool = True) -> None:
        a, b = self._clip_bounds()
        t = min(max(t, a), b)
        self._t = t
        if self._playing() and resume and not self._held:
            self._resume = True                    # on reprendra la lecture une fois arrivé
            self.player.pause()
        if self._resume:
            self._resume_timer.start()             # (re)lancé à chaque déplacement
        self._preview()                            # aperçu immédiat
        self.timeline.follow(self._out())
        self._show_pos()

    def _resume_soon(self) -> None:
        if self._resume:
            self._resume_timer.start(80)

    def _do_resume(self) -> None:
        if not self._resume:
            return
        if self.timeline._drag:                    # on glisse encore : on attend le relâchement
            self._resume_timer.start(150)
            return
        self._resume = False
        self._start(self._t)

    def _ms(self, t: float, s: int | None = None) -> int:
        src = self.srcs[self._src_index() if s is None else s]
        return int(round((t - src.start) * 1000))

    def _on_still(self, s: int, ft: float, img, exact: bool) -> None:
        if self._closed or s != self._src_index():
            return                                 # image d'un autre fichier (ancienne demande)
        if exact:
            self._frame_t = ft                     # instant exact de l'image affichée
        if not self._playing():
            self.still.set_frame(img)

    def _on_video_frame(self, frame) -> None:
        if self._playing() and frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self.still.set_qimage(img)

    def _on_media(self, status) -> None:
        """Fichier chargé dans le lecteur : lancer la lecture demandée (changement de fichier)."""
        if status == QMediaPlayer.LoadingMedia:
            self._seen_loading = True
            return
        ready = status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia)
        fresh = self._seen_loading or time.perf_counter() - self._load_started > 2.0
        if self._pending is not None and ready and fresh:
            t, self._pending = self._pending, None
            self.player.setPosition(self._ms(t))
            self.player.play()
        elif self._pending is not None and ready:
            QTimer.singleShot(300, lambda: self._on_media(self.player.mediaStatus()))

    def _on_pos(self, ms: int) -> None:
        if not self._playing() or self._jumping or self._psrc != self._src_index():
            return                                 # à l'arrêt, la position est la nôtre
        src = self.src
        t = src.start + ms / 1000.0
        a, b = self._clip_bounds()
        if t >= b - 0.02:                          # fin du morceau : on enchaîne sur le suivant
            nxt = self._clip + 1
            if nxt >= len(self.segments):
                self._t = b
                self.player.pause()
            else:
                self._clip = nxt
                self._t = self.segments[nxt][1]
                self._jumping = True               # ignorer les positions d'avant le saut
                self._select(nxt)
                s = self.segments[nxt][0]
                if s != self._psrc:                # autre fichier : on le charge, la lecture suit
                    self._pending = self._t
                    self._load_player(s)
                else:
                    self.player.setPosition(self._ms(self._t))
            self._show_pos()
            return
        if a - 0.05 <= t < b:
            self._t = t
            self.timeline.follow(self._out())
            self._show_pos()

    def _on_seeked(self, ms: int) -> None:
        if self._jumping and self._psrc == self._src_index():
            t = self.src.start + ms / 1000.0
            a, b = self._clip_bounds()
            if a - 0.25 <= t < b:
                self._jumping = False

    def _on_state(self, st) -> None:
        playing = st == QMediaPlayer.PlayingState
        held = self._held
        if not playing:
            self._held = False
            if self._pending is None:
                self._jumping = False
        self.play_btn.setText("❚❚" if playing or self._resume or self._pending is not None else "▶")
        if not playing and self._pending is None:
            if self._psrc == self._src_index():
                t = self.src.start + self.player.position() / 1000.0
                a, b = self._clip_bounds()
                if not self._resume and not held and a <= t <= b:   # vraie pause (Espace)
                    self._t = t
            self._preview()                        # l'image exacte où l'on s'est arrêté
            self._show_pos()

    def _start(self, t: float) -> None:
        self._held = False
        a, b = self._clip_bounds()
        if t >= b - 0.05:                          # en fin de morceau : on part du suivant
            if self._clip + 1 < len(self.segments):
                self._clip += 1
                t = self.segments[self._clip][1]
            else:                                  # fin du montage : on repart du début
                self._clip, t = 0, self.segments[0][1]
            self._t = t
        s = self._src_index()
        if s != self._psrc or self.player.mediaStatus() in (QMediaPlayer.LoadingMedia, QMediaPlayer.NoMedia):
            self._pending = t                      # fichier à charger d'abord
            self._load_player(s)
            if self.player.mediaStatus() in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
                self._on_media(self.player.mediaStatus())
            return
        self.player.setPosition(self._ms(t))
        self.player.play()

    def _toggle(self) -> None:
        if self._resume:                           # pause pendant un déplacement : Espace = rester en pause
            self._resume = False
            self._resume_timer.stop()
            return
        if self._playing() or self._pending is not None:
            self._pending = None
            self.player.pause()
        else:
            self._start(self._t)

    def _key_step(self, d: int) -> None:
        """Image clé précédente / suivante, dans le montage (passe au morceau voisin aux bords)."""
        self._hold()
        a, b = self._clip_bounds()
        src = self.src
        if not src.has_video:
            self._nudge(d * 1.0)
            return
        t2 = src.step(self._t, d)
        if a <= t2 <= b and abs(t2 - self._t) > 1e-6:
            self._seek(t2, False)
        elif d > 0:
            self._goto_out(kept(self.segments[:self._clip + 1]), False)
        else:
            if self._t > a + 1e-3:
                self._seek(a, False)
            elif self._clip > 0:
                self._clip -= 1
                pa, pb = self._clip_bounds()
                self._seek(min(max(self.src.step(pb, -1), pa), pb), False)

    def _nudge(self, dt: float) -> None:
        self._hold()
        self._goto_out(self._out() + dt, False)

    def _hold(self) -> None:
        """Pas à pas : la lecture s'arrête et ne reprend pas toute seule (la pause du lecteur n'est
        pas immédiate : `_held` empêche qu'un déplacement juste après la relance)."""
        self._pending = None
        if self._playing():
            self._held = True
            self.player.pause()
        self._resume = False
        self._resume_timer.stop()

    def _frame_step(self, d: int) -> None:
        """Une image avant / après celle affichée, dans le montage (au bord d'un morceau, on passe
        à la 1re image du suivant ou à la dernière du précédent)."""
        fps = self.src.media.fps or 25.0
        self._hold()
        a, b = self._clip_bounds()
        base = self._frame_t if self._frame_t is not None and abs(self._frame_t - self._t) < 1.0 / fps else self._t
        t2 = base + d / fps
        if t2 >= b - 0.25 / fps and self._clip + 1 < len(self.segments):
            self._clip += 1
            self._seek(self.segments[self._clip][1], False)
        elif t2 < a - 0.25 / fps and self._clip > 0:
            self._clip -= 1
            pa, pb = self._clip_bounds()
            self._seek(max(pa, pb - 1.0 / (self.src.media.fps or 25.0)), False)
        else:
            self._seek(t2, False)

    # --- montage ------------------------------------------------------------------------
    def _push(self) -> None:
        self._undo.append((list(self.segments), self._clip, self._t))
        self._undo = self._undo[-100:]

    def _cut_point(self) -> float:
        """La coupe tombe sur l'image clé la plus proche (dans le morceau) ; on s'y place pour la voir."""
        a, b = self._clip_bounds()
        t = min(max(self.src.snap(self._t), a), b)
        if abs(t - self._t) > 1e-3:
            self._seek(t, False)
        return t

    def _apply(self, segs: list[Segment], select: int | None = None, clip: int | None = None,
               t: float | None = None) -> None:
        segs = [(s, a, b) for s, a, b in segs if b - a > 1e-3]
        if not segs:
            return                  # on ne vide pas tout le montage
        cur_s = self._src_index()
        self._push()
        self.segments = segs
        self.selected = select if select is not None and select < len(segs) else min(self.selected or 0, len(segs) - 1)
        if clip is not None:
            self._clip = min(clip, len(segs) - 1)
            self._t = t if t is not None else segs[self._clip][1]
        else:
            self._relocate(cur_s)
        self._refresh()
        self._preview()

    def start_here(self) -> None:
        """Enlève tout ce qui est avant la tête, dans le montage."""
        t = self._cut_point()
        i = self._clip
        s, a, b = self.segments[i]
        self._apply([(s, t, b)] + self.segments[i + 1:], 0, 0, t)

    def end_here(self) -> None:
        """Enlève tout ce qui est après la tête, dans le montage."""
        t = self._cut_point()
        i = self._clip
        s, a, b = self.segments[i]
        segs = self.segments[:i] + [(s, a, t)]
        self._apply(segs, len(segs) - 1, len(segs) - 1, t)

    def split_here(self) -> None:
        t = self._cut_point()
        i = self._clip
        s, a, b = self.segments[i]
        if a + 1e-3 < t < b - 1e-3:
            self._apply(self.segments[:i] + [(s, a, t), (s, t, b)] + self.segments[i + 1:], i + 1, i + 1, t)

    def delete_selected(self) -> None:
        """Supprime le morceau sélectionné : les suivants se recollent (la tête va sur le morceau
        qui prend sa place)."""
        i = self.selected if self.selected is not None else self._clip
        if i is None or len(self.segments) <= 1:
            return
        segs = self.segments[:i] + self.segments[i + 1:]
        j = min(i, len(segs) - 1)
        self._apply(segs, j, j, segs[j][1] if j == i else segs[j][2])

    def remove_range(self, s: int, pa: float, pb: float) -> None:
        """Enlève du montage un passage d'un fichier (pause repérée) : les morceaux qu'il touche
        sont coupés, le reste se recolle."""
        segs = []
        for ss, a, b in self.segments:
            if ss != s or pb <= a or pa >= b:
                segs.append((ss, a, b))
                continue
            if pa > a + 1e-3:
                segs.append((ss, a, pa))
            if pb < b - 1e-3:
                segs.append((ss, pb, b))
        if segs != self.segments:
            self._apply(segs)

    def remove_ranges(self, ranges: list[tuple[int, float, float]]) -> None:
        """Plusieurs passages d'un coup (une seule annulation)."""
        segs = list(self.segments)
        for s, pa, pb in ranges:
            out = []
            for ss, a, b in segs:
                if ss != s or pb <= a or pa >= b:
                    out.append((ss, a, b))
                    continue
                if pa > a + 1e-3:
                    out.append((ss, a, pa))
                if pb < b - 1e-3:
                    out.append((ss, pb, b))
            segs = out
        if segs != self.segments:
            self._apply(segs)

    def move_segment(self, i: int, d: int) -> None:
        j = i + d
        if not (0 <= i < len(self.segments) and 0 <= j < len(self.segments)):
            return
        segs = list(self.segments)
        segs[i], segs[j] = segs[j], segs[i]
        clip = {i: j, j: i}.get(self._clip, self._clip)      # la tête suit son morceau
        self._apply(segs, j, clip, self._t)

    def undo(self) -> None:
        if self._undo:
            self.segments, self._clip, self._t = self._undo.pop()
            self.selected = min(self.selected or 0, len(self.segments) - 1)
            self._refresh()
            self._preview()

    def reset(self) -> None:
        """Fichiers entiers, bout à bout."""
        self._apply([(k, s.start, s.end) for k, s in enumerate(self.srcs)], 0, 0, None)

    def _reordered(self, *_a) -> None:
        if getattr(self, "_filling", False):
            return
        order = [self.list.item(r).data(Qt.UserRole) for r in range(self.list.count())]
        segs = [self.segments[k] for k in order]
        if segs != self.segments:
            clip = order.index(self._clip) if self._clip in order else 0
            self._apply(segs, self.list.currentRow(), clip, self._t)

    def _select(self, i: int, from_list: bool = False) -> None:
        if i is None or i < 0 or i >= len(self.segments):
            return
        self.selected = i
        n = len(self.segments)
        self.left_btn.setEnabled(i > 0)
        self.right_btn.setEnabled(i < n - 1)
        if not from_list:
            self.list.blockSignals(True)
            self.list.setCurrentRow(i)
            self.list.blockSignals(False)
        self.timeline.set_state(self.segments, self.selected, self._out())

    # --- état (enregistrement automatique) ---------------------------------------------------
    def state(self) -> dict:
        return {"segments": [list(x) for x in self.segments], "clip": self._clip, "t": self._t}

    # --- affichage ----------------------------------------------------------------------
    def _refresh(self) -> None:
        self._filling = True            # (pas de blocage du modèle : la vue doit voir les lignes)
        self.list.blockSignals(True)
        self.list.clear()
        multi = len(self.srcs) > 1
        for i, (s, a, b) in enumerate(self.segments):
            src = self.srcs[s]
            where = src.name if multi else "fichier"
            it = QListWidgetItem(f"{i + 1}   {fmt_t(b - a, False)}   ({where} {fmt_t(a - src.start, False)} → "
                                 f"{fmt_t(b - src.start, False)})")
            it.setData(Qt.UserRole, i)
            it.setForeground(QColor(SEG_COLORS[i % len(SEG_COLORS)]))
            it.setToolTip(f"{src.path}\nGlisser pour changer l'ordre · double-clic : aller au début du morceau")
            self.list.addItem(it)
        if self.selected is not None and self.selected < self.list.count():
            self.list.setCurrentRow(self.selected)
        self.list.blockSignals(False)
        self._filling = False
        total = sum(s.end - s.start for s in self.srcs)
        k = kept(self.segments)
        n = len(self.segments)
        files = f"{len(self.srcs)} fichiers, " if multi else "fichier d'origine "
        self.summary.setText(
            f"{n} morceau{'x' if n > 1 else ''} · durée du montage <b>{fmt_t(k, False)}</b> "
            f"({files}{fmt_t(total, False)}, enlevé {fmt_t(max(0.0, total - k), False)})")
        self.undo_btn.setEnabled(bool(self._undo))
        self.del_btn.setEnabled(n > 1)
        sel = self.selected if self.selected is not None else -1
        self.left_btn.setEnabled(sel > 0)
        self.right_btn.setEnabled(0 <= sel < n - 1)
        self._show_pos()
        self.changed.emit()

    def _show_pos(self) -> None:
        out = self._out()
        src = self.src
        where = src.name if len(self.srcs) > 1 else "fichier"
        self.time_lbl.setText(f"{fmt_t(out)} / {fmt_t(kept(self.segments), False)}")
        self.time_lbl.setToolTip(f"Montage {fmt_t(out)} · {src.name} {fmt_t(self._t - src.start)}")
        self.src_lbl.setText(f"{where} {fmt_t(self._t - src.start)}")
        self.timeline.set_state(self.segments, self.selected, out)

    def shutdown(self) -> None:
        self._closed = True                      # les images encore en route sont ignorées
        self._meter_timer.stop()
        for r in self._readers.values():
            r.stop()
        self._readers.clear()
        for sd in self._scrubs.values():
            try:
                sd.frame.disconnect()
            except (RuntimeError, TypeError):
                pass
            sd.stop()
        self._scrubs.clear()
        self.player.stop()
        self.player.setSource(QUrl())

    def is_full(self) -> bool:
        return self.segments == [(k, s.start, s.end) for k, s in enumerate(self.srcs)]

    def result_segments(self) -> list[Segment] | None:
        """None si les fichiers sont gardés en entier."""
        return None if self.is_full() else list(self.segments)
