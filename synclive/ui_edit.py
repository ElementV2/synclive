# SPDX-License-Identifier: GPL-3.0-or-later
"""Banc de montage (onglet Montage) : enlever le début, la fin, les écrans d'attente ou de pause
d'un live entier, couper, supprimer et réordonner des morceaux, sans perte de qualité.

Fait pour des fichiers de plusieurs heures : rien n'est lu en entier à l'ouverture (les images
clés sont cherchées à la demande, l'aperçu du son se remplit en tâche de fond) ; l'aperçu de
l'image suit la timeline en temps réel. Les coupes tombent sur les images clés : la vidéo est
recopiée telle quelle. Le résultat est une liste de morceaux (instants du fichier) dans l'ordre
de sortie."""
from __future__ import annotations

import math
import threading

import av
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import montage as M
from .ui_clap import FrameView

Segment = tuple[float, float]

ORANGE = QColor("#FF5A1F")
GREEN = QColor("#3FB950")
CUT = QColor(20, 20, 24, 210)
BG = QColor("#16161A")
MUTED = "#8A8780"
SEG_COLORS = ["#3FB950", "#58A6FF", "#D2A8FF", "#E3B341", "#FF7B72", "#79C0FF", "#56D364", "#F0883E"]


def fmt_t(t: float, ms: bool = True) -> str:
    t = max(0.0, t)
    m, s = divmod(t, 60.0)
    h, m = divmod(int(m), 60)
    sec = f"{s:06.3f}" if ms else f"{int(s):02d}"
    return (f"{h}:{m:02d}:{sec}" if h else f"{m}:{sec}").replace(".", ",")


def kept(segs: list[Segment]) -> float:
    return float(sum(b - a for a, b in segs))


class Source:
    """Le fichier monté : bornes, images clés (cherchées à la demande), aperçu du son."""

    def __init__(self, media: M.Media, overview: M.Overview | None = None):
        self.media = media
        self.path = media.path
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

    def full(self) -> list[Segment]:
        return [(self.start, self.end)]

    def is_full(self, segs: list[Segment] | None) -> bool:
        return not segs or (len(segs) == 1 and abs(segs[0][0] - self.start) < 1e-3
                            and abs(segs[0][1] - self.end) < 1e-3)

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
        self.wait(3000)

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
    recolle les suivants. Chaque morceau montre son son, ses marqueurs début / fin et son n°.
    Clic / glisser : se placer. Molette : zoom autour de la souris ; « Tout voir » : tout le montage."""

    seek = Signal(float)          # instant du montage (s depuis le début du montage)
    picked = Signal(int)          # morceau cliqué
    released = Signal()           # fin du clic / glisser

    MARGIN = 12

    def __init__(self, src: Source, parent=None):
        super().__init__(parent)
        self.src = src
        self.segments: list[Segment] = []
        self.selected: int | None = None
        self.playhead = 0.0
        self.v0, self.v1 = 0.0, 1.0
        self._fit = True              # vue : tout le montage (suit les modifications)
        self.hover: float | None = None
        self._drag = False
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
        for a, b in self.segments:
            out.append(pos)
            pos += b - a
        return out

    def locate(self, p: float) -> tuple[int, float]:
        """Instant du montage -> (morceau, instant du fichier)."""
        p = min(max(p, 0.0), self.total())
        pos = 0.0
        for i, (a, b) in enumerate(self.segments):
            if p < pos + (b - a) or i == len(self.segments) - 1:
                return i, min(b, a + (p - pos))
            pos += b - a
        return 0, self.segments[0][0] if self.segments else 0.0

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
        if not self.src.has_video or self.v1 - self.v0 > 180:
            return
        for (a, b), o in zip(self.segments, self.starts()):
            lo, hi = max(a, a + self.v0 - o), min(b, a + self.v1 - o)
            t = lo
            while t < hi:
                _b, nx = self.src.keys.around(t)
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
        ov = self.src.overview
        starts = self.starts()
        w = int(self._w())
        mid = tr.center().y()
        amp = tr.height() / 2 - 8
        for i, ((a, b), o) in enumerate(zip(self.segments, starts)):
            x0, x1 = self.x_of(o), self.x_of(o + (b - a))
            if x1 < tr.left() or x0 > tr.right():
                continue
            cx0, cx1 = max(x0, tr.left()), min(x1, tr.right())
            col = QColor(SEG_COLORS[i % len(SEG_COLORS)])
            fill = QColor(col)
            fill.setAlpha(70 if i == self.selected else 40)
            p.fillRect(QRectF(cx0, tr.top(), cx1 - cx0, tr.height()), fill)
            # son du morceau (aperçu lu en tâche de fond)
            if ov is not None:
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
                    i0 = int((t0 - self.src.start) / ov.STEP)
                    i1 = max(i0 + 1, int((t1 - self.src.start) / ov.STEP))
                    seg = lv[max(0, i0):min(lv.size, i1)]
                    if seg.size == 0:
                        continue
                    v = float(np.clip((seg.max() + 60.0) / 60.0, 0.0, 1.0))
                    p.drawLine(QPointF(px, mid - v * amp), QPointF(px, mid + v * amp))
                p.setRenderHint(QPainter.Antialiasing, True)
            # n° du morceau (petit), s'il y a la place
            if cx1 - cx0 > 22 and tr.left() <= x0 + 4:
                p.setPen(col)
                p.drawText(QRectF(max(cx0, x0) + 6, tr.bottom() - 17, 30, 14), Qt.AlignLeft, str(i + 1))
        # raccords entre morceaux : trait blanc et ciseaux
        for o in starts[1:]:
            x = self.x_of(o)
            if tr.left() <= x <= tr.right():
                p.setPen(QPen(QColor(255, 255, 255, 220), 2))
                p.drawLine(QPointF(x, tr.top() + 2), QPointF(x, tr.bottom() - 2))
                p.setPen(QColor("#F2F0EB"))
                p.drawText(QRectF(x - 8, tr.top() + 2, 16, 14), Qt.AlignCenter, "✂")
        # début et fin du montage : marqueurs, avec l'instant correspondant dans le fichier
        if self.segments:
            fm = p.fontMetrics()
            a0 = self.segments[0][0]
            b1 = self.segments[-1][1]
            for x, start, t_file in ((self.x_of(0.0), True, a0), (self.x_of(self.total()), False, b1)):
                t_mont = 0.0 if start else self.total()
                if not (tr.left() - 1 <= x <= tr.right() + 1):
                    continue
                trimmed = (abs(t_file - self.src.start) > 0.05) if start else (abs(t_file - self.src.end) > 0.05)
                col = ORANGE if trimmed else QColor("#B8B5AE")
                p.setPen(QPen(col, 3))
                p.drawLine(QPointF(x, tr.top()), QPointF(x, tr.bottom()))
                d = 8 if start else -8
                p.drawLine(QPointF(x, tr.top() + 1.5), QPointF(x + d, tr.top() + 1.5))
                p.drawLine(QPointF(x, tr.bottom() - 1.5), QPointF(x + d, tr.bottom() - 1.5))
                # instant dans le montage (il change à chaque coupe) et dans le fichier d'origine
                what = "DÉBUT" if start else "FIN"
                lab = f"{what} {fmt_t(t_mont, False)} · fichier {fmt_t(t_file - self.src.start, False)}"
                wl = fm.horizontalAdvance(lab) + 12
                r = QRectF(x + 5 if start else x - 5 - wl, tr.top() + 4, wl, 16)
                self._chip(p, r, col, lab)
        # images clés connues (zoomé) : points de coupe possibles
        keys = self.src.known()
        if keys and span <= 600:
            p.setPen(QPen(QColor("#B8B5AE"), 1))
            for (a, b), o in zip(self.segments, starts):
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
            a, b = self.segments[i]
            ts = min(max(self.src.snap(t), a), b)
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
            fm = p.fontMetrics()
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


class EditorPanel(QWidget):
    """Couper, supprimer, déplacer des morceaux d'un fichier. `self.segments` : le montage."""

    changed = Signal()

    def __init__(self, src: Source, segments: list[Segment] | None = None, parent=None):
        super().__init__(parent)
        self.src = src
        path = src.path
        self.segments: list[Segment] = list(segments) if segments else src.full()
        self._undo: list = []
        self.selected: int | None = 0
        self._clip = 0                          # morceau où est la tête de lecture
        self._jumping = False                   # saut d'un morceau au suivant en cours (lecture)
        self._t = self.segments[0][0]           # instant du fichier sous la tête
        self._frame_t: float | None = None
        self._held = False

        v = QVBoxLayout(self)
        v.setSpacing(10)
        if src.has_video:
            how = ("Coupes calées sur les images clés (la tête de lecture y saute pour montrer l'image exacte "
                   "de la coupe) : la vidéo est recopiée telle quelle, sans aucune perte de qualité.")
        else:
            how = "Fichier son : coupes au paquet près."
        help_ = QLabel(how + "  La timeline est le montage : supprimer un morceau recolle les suivants. "
                       "Espace : lecture · ← → : image par image · Maj+← → : 1 s · , . : image clé · I : début ici · "
                       "O : fin ici · C : couper ici · Suppr : supprimer le morceau · glisser les morceaux en bas "
                       "pour changer l'ordre · Ctrl+Z : annuler.")
        help_.setWordWrap(True)
        help_.setStyleSheet(f"color:{MUTED};")
        v.addWidget(help_)

        # un seul afficheur, en lecture comme à l'arrêt : les images du lecteur Qt (lecture) et celles
        # de notre aperçu (arrêt, glisser) sont dessinées au même endroit, à la même échelle, sur le
        # même fond noir (passer d'un afficheur à l'autre faisait changer le zoom et le fond)
        self.still = FrameView(background="#000000")
        self.still.setMinimumSize(480, 270)
        v.addWidget(self.still, 1)
        self.scrub = None
        if src.has_video:
            self.scrub = ScrubDecoder(path)
            self.scrub.frame.connect(self._on_still)
            self.scrub.start()
        else:
            self.still.set_message("Fichier son seul")
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_video_frame)
        self.player.setVideoSink(self.sink)
        self.player.positionChanged.connect(self._on_pos)
        self.player.positionChanged.connect(self._on_seeked)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.setSource(QUrl.fromLocalFile(path))

        self.timeline = EditTimeline(src)
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
        self._seek(self._t)                       # image du début du montage, tout de suite

    # --- montage <-> fichier --------------------------------------------------------------
    def _out(self) -> float:
        """Position de la tête dans le montage."""
        if not self.segments:
            return 0.0
        i = min(self._clip, len(self.segments) - 1)
        a, b = self.segments[i]
        return sum(bb - aa for aa, bb in self.segments[:i]) + (min(max(self._t, a), b) - a)

    def _goto_out(self, p: float, resume: bool = True) -> None:
        """Se placer à un instant du montage (clic, glisser, pas à pas qui change de morceau)."""
        i, t = self.timeline.locate(p)
        self._clip = i
        self._seek(t, resume)

    def _clip_bounds(self) -> tuple[float, float]:
        return self.segments[min(self._clip, len(self.segments) - 1)]

    def _relocate(self) -> None:
        """Après une modification : la tête reste dans un morceau existant."""
        for i, (a, b) in enumerate(self.segments):
            if a - 1e-6 <= self._t < b - 1e-6 or (i == len(self.segments) - 1 and abs(self._t - b) < 1e-6):
                self._clip = i
                return
        self._clip = min(self._clip, len(self.segments) - 1)
        self._t = self.segments[self._clip][0]

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
        if self.scrub is not None:
            self.scrub.request(t)                  # aperçu immédiat
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

    def _ms(self, t: float) -> int:
        return int(round((t - self.src.start) * 1000))

    def _on_still(self, ft: float, img, exact: bool) -> None:
        if exact:
            self._frame_t = ft                     # instant exact de l'image affichée
        if not self._playing():
            self.still.set_frame(img)

    def _on_video_frame(self, frame) -> None:
        if self._playing() and frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self.still.set_qimage(img)

    def _on_pos(self, ms: int) -> None:
        if not self._playing() or self._jumping:
            return                                 # à l'arrêt, la position est la nôtre
        t = self.src.start + ms / 1000.0
        a, b = self._clip_bounds()
        if t >= b - 0.02:                          # fin du morceau : on enchaîne sur le suivant
            nxt = self._clip + 1
            if nxt >= len(self.segments):
                self._t = b
                self.player.pause()
            else:
                self._clip = nxt
                self._t = self.segments[nxt][0]
                self._jumping = True               # ignorer les positions d'avant le saut
                self.player.setPosition(self._ms(self._t))
                self._select(nxt)
            self._show_pos()
            return
        if a - 0.05 <= t < b:
            self._t = t
            self.timeline.follow(self._out())
            self._show_pos()

    def _on_seeked(self, ms: int) -> None:
        if self._jumping:
            t = self.src.start + ms / 1000.0
            a, b = self._clip_bounds()
            if a - 0.25 <= t < b:
                self._jumping = False

    def _on_state(self, st) -> None:
        playing = st == QMediaPlayer.PlayingState
        held = self._held
        if not playing:
            self._held = False
            self._jumping = False
        self.play_btn.setText("❚❚" if playing or self._resume else "▶")
        if not playing:
            t = self.src.start + self.player.position() / 1000.0
            a, b = self._clip_bounds()
            if not self._resume and not held and a <= t <= b:   # vraie pause (Espace)
                self._t = t
            if self.scrub is not None:
                self.scrub.request(self._t)        # l'image exacte où l'on s'est arrêté
            self._show_pos()

    def _start(self, t: float) -> None:
        self._held = False
        a, b = self._clip_bounds()
        if t >= b - 0.05:                          # en fin de morceau : on part du suivant
            if self._clip + 1 < len(self.segments):
                self._clip += 1
                t = self.segments[self._clip][0]
            else:                                  # fin du montage : on repart du début
                self._clip, t = 0, self.segments[0][0]
            self._t = t
        self.player.setPosition(self._ms(t))
        self.player.play()

    def _toggle(self) -> None:
        if self._resume:                           # pause pendant un déplacement : Espace = rester en pause
            self._resume = False
            self._resume_timer.stop()
            return
        if self._playing():
            self.player.pause()
        else:
            self._start(self._t)

    def _key_step(self, d: int) -> None:
        """Image clé précédente / suivante, dans le montage (passe au morceau voisin aux bords)."""
        self._hold()
        a, b = self._clip_bounds()
        if not self.src.has_video:
            self._nudge(d * 1.0)
            return
        t2 = self.src.step(self._t, d)
        if a <= t2 <= b and abs(t2 - self._t) > 1e-6:
            self._seek(t2, False)
        elif d > 0:
            self._goto_out(sum(bb - aa for aa, bb in self.segments[:self._clip + 1]), False)
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
            self._seek(self.segments[self._clip][0], False)
        elif t2 < a - 0.25 / fps and self._clip > 0:
            self._clip -= 1
            pa, pb = self._clip_bounds()
            self._seek(max(pa, pb - 1.0 / fps), False)
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
        segs = [(a, b) for a, b in segs if b - a > 1e-3]
        if not segs:
            return                  # on ne vide pas tout le montage
        self._push()
        self.segments = segs
        self.selected = select if select is not None and select < len(segs) else min(self.selected or 0, len(segs) - 1)
        if clip is not None:
            self._clip = min(clip, len(segs) - 1)
            self._t = t if t is not None else segs[self._clip][0]
        else:
            self._relocate()
        self._refresh()
        if self.scrub is not None:
            self.scrub.request(self._t)

    def start_here(self) -> None:
        """Enlève tout ce qui est avant la tête, dans le montage."""
        t = self._cut_point()
        i = self._clip
        a, b = self.segments[i]
        self._apply([(t, b)] + self.segments[i + 1:], 0, 0, t)

    def end_here(self) -> None:
        """Enlève tout ce qui est après la tête, dans le montage."""
        t = self._cut_point()
        i = self._clip
        a, b = self.segments[i]
        segs = self.segments[:i] + [(a, t)]
        self._apply(segs, len(segs) - 1, len(segs) - 1, t)

    def split_here(self) -> None:
        t = self._cut_point()
        i = self._clip
        a, b = self.segments[i]
        if a + 1e-3 < t < b - 1e-3:
            self._apply(self.segments[:i] + [(a, t), (t, b)] + self.segments[i + 1:], i + 1, i + 1, t)

    def delete_selected(self) -> None:
        """Supprime le morceau sélectionné : les suivants se recollent (la tête va sur le morceau
        qui prend sa place)."""
        i = self.selected if self.selected is not None else self._clip
        if i is None or len(self.segments) <= 1:
            return
        segs = self.segments[:i] + self.segments[i + 1:]
        j = min(i, len(segs) - 1)
        self._apply(segs, j, j, segs[j][0] if j == i else segs[j][1])

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
            if self.scrub is not None:
                self.scrub.request(self._t)

    def reset(self) -> None:
        self._apply(self.src.full(), 0, 0, self._t)

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

    # --- affichage ----------------------------------------------------------------------
    def _refresh(self) -> None:
        self._filling = True            # (pas de blocage du modèle : la vue doit voir les lignes)
        self.list.blockSignals(True)
        self.list.clear()
        for i, (a, b) in enumerate(self.segments):
            it = QListWidgetItem(f"{i + 1}   {fmt_t(b - a, False)}   (fichier {fmt_t(a - self.src.start, False)} → "
                                 f"{fmt_t(b - self.src.start, False)})")
            it.setData(Qt.UserRole, i)
            it.setForeground(QColor(SEG_COLORS[i % len(SEG_COLORS)]))
            it.setToolTip("Glisser pour changer l'ordre · double-clic : aller au début du morceau")
            self.list.addItem(it)
        if self.selected is not None and self.selected < self.list.count():
            self.list.setCurrentRow(self.selected)
        self.list.blockSignals(False)
        self._filling = False
        total = self.src.end - self.src.start
        k = kept(self.segments)
        n = len(self.segments)
        self.summary.setText(
            f"{n} morceau{'x' if n > 1 else ''} · durée du montage <b>{fmt_t(k, False)}</b> "
            f"(fichier d'origine {fmt_t(total, False)}, enlevé {fmt_t(total - k, False)})")
        self.undo_btn.setEnabled(bool(self._undo))
        self.del_btn.setEnabled(n > 1)
        sel = self.selected if self.selected is not None else -1
        self.left_btn.setEnabled(sel > 0)
        self.right_btn.setEnabled(0 <= sel < n - 1)
        self._show_pos()
        self.changed.emit()

    def _show_pos(self) -> None:
        out = self._out()
        self.time_lbl.setText(f"{fmt_t(out)} / {fmt_t(kept(self.segments), False)}")
        self.time_lbl.setToolTip(f"Montage {fmt_t(out)} · fichier d'origine {fmt_t(self._t - self.src.start)}")
        self.src_lbl.setText(f"fichier {fmt_t(self._t - self.src.start)}")
        self.timeline.set_state(self.segments, self.selected, out)

    def shutdown(self) -> None:
        if self.scrub is not None:
            self.scrub.stop()
            self.scrub = None
        self.player.stop()
        self.player.setSource(QUrl())

    def result_segments(self) -> list[Segment] | None:
        """None si le fichier est gardé en entier."""
        return None if self.src.is_full(self.segments) else list(self.segments)
