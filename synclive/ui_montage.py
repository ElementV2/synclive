# SPDX-License-Identifier: GPL-3.0-or-later
"""Onglet « Montage » : ouvrir un live entier (plusieurs heures), enlever le début, la fin, les
écrans d'attente ou de pause, réordonner des morceaux, et exporter sans perte, à la vitesse du
disque."""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QRadioButton,
    QVBoxLayout, QWidget,
)

from . import montage as M
from .ui_edit import EditorPanel, Source, fmt_t, kept

MUTED = "#8A8780"
GREEN = "#3FB950"
ORANGE = "#FF5A1F"
RED = "#F85149"


def fmt_size(n: int) -> str:
    return (f"{n / 1e9:.1f} Go" if n >= 1e9 else f"{n / 1e6:.0f} Mo").replace(".", ",")


class ExportWorker(QThread):
    progress = Signal(float)
    done = Signal(str, float)
    failed = Signal(str)

    def __init__(self, media: M.Media, segments, dst: Path, exact: bool):
        super().__init__()
        self.media, self.segments, self.dst, self.exact = media, segments, dst, exact
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        t0 = time.perf_counter()
        try:
            M.export(self.media, self.segments, self.dst, self.progress.emit, lambda: self._cancel, self.exact)
            self.done.emit(str(self.dst), time.perf_counter() - t0)
        except InterruptedError:
            self.failed.emit("Export annulé.")
        except Exception as e:
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__} : {e}")


class LiveDrop(QFrame):
    dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self._hover = False
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        t = QLabel("Glisse ici le live à monter")
        t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet("font-size:17px; font-weight:600; background:transparent;")
        h = QLabel("Un fichier vidéo, même de plusieurs heures : il s'ouvre tout de suite. On enlève le début, la "
                   "fin, les écrans d'attente ou de pause, et on exporte sans perte de qualité.")
        h.setAlignment(Qt.AlignCenter)
        h.setWordWrap(True)
        h.setStyleSheet(f"color:{MUTED}; background:transparent;")
        b = QPushButton("Ouvrir un fichier…")
        b.clicked.connect(self._pick)
        lay.addWidget(t)
        lay.addWidget(h)
        lay.addSpacing(8)
        lay.addWidget(b, alignment=Qt.AlignCenter)
        lay.addStretch(1)
        self._style()

    def _style(self) -> None:
        c = ORANGE if self._hover else "#3A3A42"
        bg = "#201A18" if self._hover else "#19191D"
        self.setStyleSheet(f"LiveDrop {{ border: 2px dashed {c}; border-radius: 10px; background: {bg}; }}")

    def _pick(self) -> None:
        exts = " ".join("*" + e for e in sorted(M.VIDEO_EXT))
        p, _ = QFileDialog.getOpenFileName(self, "Ouvrir le live à monter", "", f"Vidéos ({exts});;Tous (*)")
        if p:
            self.dropped.emit(p)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._hover = True
            self._style()

    def dragLeaveEvent(self, e):
        self._hover = False
        self._style()

    def dropEvent(self, e):
        self._hover = False
        self._style()
        for u in e.mimeData().urls():
            if u.toLocalFile():
                e.acceptProposedAction()
                self.dropped.emit(u.toLocalFile())
                return


class MontagePage(QWidget):
    _overview_tick = Signal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.src: Source | None = None
        self.editor: EditorPanel | None = None
        self.worker: ExportWorker | None = None
        self.out: Path | None = None
        self.last_out: Path | None = None

        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(18, 14, 18, 14)
        self.v.setSpacing(10)
        head = QHBoxLayout()
        tv = QVBoxLayout()
        tv.setSpacing(2)
        title = QLabel("Montage")
        title.setStyleSheet("font-size:18px; font-weight:600;")
        self.sub = QLabel("Couper un live entier sans perte : début, fin, écrans d'attente ou de pause. "
                          "La vidéo et le son sont recopiés tels quels, l'export va à la vitesse du disque.")
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(f"color:{MUTED};")
        tv.addWidget(title)
        tv.addWidget(self.sub)
        head.addLayout(tv, 1)
        self.change_btn = QPushButton("Ouvrir un autre fichier…")
        self.change_btn.clicked.connect(lambda: self.drop._pick())
        self.change_btn.setVisible(False)
        head.addWidget(self.change_btn, 0, Qt.AlignTop)
        self.v.addLayout(head)

        self.drop = LiveDrop()
        self.drop.dropped.connect(self.open_file)
        self.v.addWidget(self.drop, 1)

        self.info = QLabel()
        self.info.setStyleSheet(f"color:{MUTED};")
        self.info.setVisible(False)
        self.v.addWidget(self.info)
        self.editor_box = QVBoxLayout()
        self.v.addLayout(self.editor_box, 1)

        # export
        self.export_bar = QFrame()
        self.export_bar.setObjectName("card")
        self.export_bar.setStyleSheet("QFrame#card QLabel, QFrame#card QRadioButton { background: transparent; }"
                                      "QRadioButton { spacing: 8px; }"
                                      "QRadioButton::indicator { width: 14px; height: 14px; border-radius: 8px;"
                                      " border: 1px solid #5A5A63; background: #1B1B20; }"
                                      "QRadioButton::indicator:checked { background: qradialgradient(cx:0.5, cy:0.5,"
                                      " radius:0.5, fx:0.5, fy:0.5, stop:0 #FF5A1F, stop:0.45 #FF5A1F, stop:0.55"
                                      " #1B1B20, stop:1 #1B1B20); border-color: #FF5A1F; }")
        el = QVBoxLayout(self.export_bar)
        el.setContentsMargins(14, 10, 14, 10)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Fichier écrit :"))
        self.out_lbl = QLabel()
        self.out_lbl.setStyleSheet("font-weight:600;")
        r1.addWidget(self.out_lbl, 1)
        choose = QPushButton("Choisir…")
        choose.clicked.connect(self._choose_out)
        r1.addWidget(choose)
        el.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Son :"))
        self.fast = QRadioButton("recopié tel quel — export immédiat, synchro à ±10 ms aux raccords")
        self.exact = QRadioButton("recalculé à l'échantillon près — plus lent")
        self.fast.setChecked(True)
        self.fast.setToolTip("Le son est recopié comme la vidéo : rien n'est réencodé. Il ne peut être coupé "
                             "qu'entre ses paquets (≈ 21 ms) : à chaque raccord l'écart reste sous ½ paquet et "
                             "ne s'accumule pas.")
        self.exact.setToolTip("Le son est décodé et réencodé dans le même format, coupé à l'échantillon près "
                              "(synchro exacte). La vidéo reste recopiée telle quelle.")
        r2.addWidget(self.fast)
        r2.addSpacing(12)
        r2.addWidget(self.exact)
        r2.addStretch(1)
        el.addLayout(r2)
        r3 = QHBoxLayout()
        self.result = QLabel()
        self.result.setWordWrap(True)
        r3.addWidget(self.result, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(260)
        self.bar.setVisible(False)
        r3.addWidget(self.bar)
        self.cancel_btn = QPushButton("Annuler")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(lambda: self.worker and self.worker.cancel())
        self.open_btn = QPushButton("Ouvrir le dossier")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: self.last_out and QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.last_out.parent))))
        self.go_btn = QPushButton("Exporter le montage")
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.export)
        for b in (self.cancel_btn, self.open_btn, self.go_btn):
            r3.addWidget(b)
        el.addLayout(r3)
        self.export_bar.setVisible(False)
        self.v.addWidget(self.export_bar)
        self._t_start = 0.0
        self._overview_tick.connect(self._on_overview)

    # --- fichier ------------------------------------------------------------------------
    def open_file(self, path: str) -> None:
        if self.worker is not None:
            return
        try:
            media = M.open_media(path)
        except Exception as e:
            QMessageBox.warning(self, "Montage", f"Impossible d'ouvrir ce fichier :\n{e}")
            return
        if media.duration <= 0:
            QMessageBox.warning(self, "Montage", "Ce fichier n'a pas de durée lisible.")
            return
        self.close_file()
        ov = M.Overview(media, on_progress=self._overview_tick.emit)
        self.src = Source(media, ov)
        self.editor = EditorPanel(self.src)
        self.editor.changed.connect(self._update)
        self.editor_box.addWidget(self.editor)
        ov.start()
        self.out = M.output_path(path)
        self.drop.setVisible(False)
        self.change_btn.setVisible(True)
        self.info.setVisible(True)
        self.export_bar.setVisible(True)
        self.result.setText("")
        self.open_btn.setEnabled(False)
        self._update()

    def close_file(self) -> None:
        if self.editor is not None:
            self.editor.shutdown()
            self.editor_box.removeWidget(self.editor)
            self.editor.deleteLater()
            self.editor = None
        if self.src is not None:
            self.src.close()
            self.src = None

    def _on_overview(self) -> None:
        if self.editor is not None:
            self.editor.timeline.update()
            self._update_info()

    def _update_info(self) -> None:
        if self.src is None:
            return
        m = self.src.media
        bits = [Path(m.path).name, fmt_t(m.duration, False), fmt_size(m.size)]
        if m.has_video:
            bits.append(f"{m.width}×{m.height} · {m.fps:.2f} i/s · {m.video_codec}".replace(".", ","))
        if m.audio:
            a = m.audio[0]
            bits.append(f"son {a['codec']} {a['rate'] // 1000} kHz {a['layout']}"
                        + (f" (+{len(m.audio) - 1} piste{'s' if len(m.audio) > 2 else ''})" if len(m.audio) > 1 else ""))
        ov = self.src.overview
        if ov is not None and ov.done_until < m.end - 0.5:
            bits.append(f"aperçu du son : {int(100 * (ov.done_until - m.start) / max(1e-6, m.duration))} %")
        self.info.setText("  ·  ".join(bits))

    def _update(self) -> None:
        self._update_info()
        if self.out is not None:
            self.out_lbl.setText(str(self.out))
        busy = self.worker is not None
        has = self.editor is not None
        full = has and self.src.is_full(self.editor.segments)
        self.go_btn.setEnabled(has and not busy and not full)
        self.go_btn.setToolTip("Rien n'est coupé : utilise « Début ici », « Fin ici » ou « Couper ici »" if full else "")

    def _choose_out(self) -> None:
        if self.out is None:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Fichier du montage", str(self.out),
                                           f"Même format (*{self.out.suffix})")
        if p:
            q = Path(p)
            if q.suffix.lower() != self.out.suffix.lower():
                q = q.with_suffix(self.out.suffix)          # même conteneur : recopie sans perte
            if self.src is not None and q.resolve() == Path(self.src.path).resolve():
                QMessageBox.warning(self, "Montage", "Choisis un autre nom que le fichier d'origine.")
                return
            self.out = q
            self._update()

    # --- export -------------------------------------------------------------------------
    def export(self) -> None:
        if self.editor is None or self.worker is not None:
            return
        segs = self.editor.segments
        if self.out.exists():
            if QMessageBox.question(self, "Montage", f"{self.out.name} existe déjà. Le remplacer ?") \
                    != QMessageBox.Yes:
                return
        self.editor.player.pause()
        if self.src.overview is not None:
            self.src.overview.pause()            # l'export d'abord (sinon 3 fois plus lent)
        self.worker = ExportWorker(self.src.media, list(segs), self.out, self.exact.isChecked())
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self._t_start = time.perf_counter()
        self._hist = []
        self._eta = None
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.cancel_btn.setVisible(True)
        self.result.setStyleSheet("")
        self.result.setText(f"Export de {fmt_t(kept(segs), False)}…")
        self.worker.start()
        self._update()

    def _on_progress(self, f: float) -> None:
        """Avancement et temps restant, estimé sur la vitesse des 8 dernières secondes (la vitesse
        moyenne depuis le début faisait monter l'estimation en cours de route)."""
        now = time.perf_counter()
        self.bar.setValue(int(1000 * f))
        hist = self._hist
        hist.append((now, f))
        while len(hist) > 2 and now - hist[0][0] > 8.0:
            hist.pop(0)
        el = now - self._t_start
        if f <= 0.0 or el < 2.0 or len(hist) < 2 or hist[-1][1] <= hist[0][1]:
            self.result.setText(f"Export… {int(f * 100)} %")
            return
        rate = (hist[-1][1] - hist[0][1]) / max(1e-6, hist[-1][0] - hist[0][0])
        left = (1.0 - f) / rate
        # pas de saut d'une mise à jour à l'autre : moyenne glissante de l'estimation
        self._eta = left if self._eta is None else 0.7 * self._eta + 0.3 * left
        self.result.setText(f"Export… {int(f * 100)} % — encore {fmt_t(max(0.0, self._eta), False)} environ")

    def _end(self) -> None:
        self.worker = None
        if self.src is not None and self.src.overview is not None:
            self.src.overview.resume()
        self.bar.setVisible(False)
        self.cancel_btn.setVisible(False)
        self._update()

    def _on_done(self, path: str, secs: float) -> None:
        self._end()
        self.last_out = Path(path)
        self.open_btn.setEnabled(True)
        self.result.setStyleSheet(f"color:{GREEN};")
        self.result.setText(f"✓ Exporté en {fmt_t(secs, False)} : {Path(path).name} "
                            f"({fmt_t(kept(self.editor.segments), False)}, {fmt_size(Path(path).stat().st_size)})")
        self.out = M.output_path(self.src.path)        # prochain export : un nouveau nom
        self._update()

    def _on_failed(self, msg: str) -> None:
        self._end()
        self.result.setStyleSheet(f"color:{RED if 'annulé' not in msg else MUTED};")
        self.result.setText(msg)

    # --- glisser-déposer sur toute la page ---------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self.worker is None:
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile():
                e.acceptProposedAction()
                self.open_file(u.toLocalFile())
                return

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(10000)
        self.close_file()
