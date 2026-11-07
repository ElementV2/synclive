# SPDX-License-Identifier: GPL-3.0-or-later
"""Onglet « Montage » : ouvrir un live entier (plusieurs heures, un ou plusieurs fichiers), enlever
le début, la fin, les écrans d'attente ou de pause (repérés automatiquement), réordonner des
morceaux, et exporter sans perte, à la vitesse du disque. Le montage est enregistré tout seul et
repris à la réouverture."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QVBoxLayout, QWidget,
)

from . import montage as M
from .ui_threads import release, stop
from .applog import error as log_error, log
from .ui_edit import EditorPanel, Source, fmt_t, kept

MUTED = "#8A8780"
GREEN = "#3FB950"
ORANGE = "#FF5A1F"
RED = "#F85149"
CARD = ("QFrame#card QLabel, QFrame#card QRadioButton { background: transparent; }"
        "QRadioButton { spacing: 8px; }"
        "QRadioButton::indicator { width: 14px; height: 14px; border-radius: 8px; border: 1px solid #5A5A63;"
        " background: #1B1B20; }"
        "QRadioButton::indicator:checked { background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5,"
        " fy:0.5, stop:0 #FF5A1F, stop:0.45 #FF5A1F, stop:0.55 #1B1B20, stop:1 #1B1B20); border-color: #FF5A1F; }")
KINDS = {"fixe": "écran fixe", "pareil": "comme l'exemple", "piste": "musique des pauses"}


def fmt_size(n: int) -> str:
    return (f"{n / 1e9:.1f} Go" if n >= 1e9 else f"{n / 1e6:.0f} Mo").replace(".", ",")


class NoWheelCombo(QComboBox):
    def wheelEvent(self, e):
        e.ignore()


class ExportWorker(QThread):
    progress = Signal(float)
    done = Signal(str, float)
    failed = Signal(str)

    def __init__(self, medias, segments, dst: Path, exact: bool):
        super().__init__()
        self.medias, self.segments, self.dst, self.exact = medias, segments, dst, exact
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        t0 = time.perf_counter()
        try:
            log.info(f"Montage : export de {len(self.segments)} morceau(x), {len(self.medias)} fichier(s) "
                     f"({'son recalculé' if self.exact else 'recopie'}) -> {self.dst}")
            M.export(self.medias, self.segments, self.dst, self.progress.emit, lambda: self._cancel, self.exact)
            log.info(f"Montage : exporté en {time.perf_counter() - t0:.1f} s")
            self.done.emit(str(self.dst), time.perf_counter() - t0)
        except InterruptedError:
            log.info("Montage : export annulé")
            self.failed.emit("Export annulé.")
        except ValueError as e:                  # fichiers incompatibles, etc. : message clair
            log.warning(f"Montage : export refusé : {e}")
            self.failed.emit(str(e))
        except Exception as e:
            log_error(f"Montage : export impossible -> {self.dst}")
            self.failed.emit(f"{type(e).__name__} : {e}")


class PauseWorker(QThread):
    """Repérage des pauses de tous les fichiers du montage : vignettes des images clés (une fois),
    puis écrans fixes, piste « musique des pauses », et écran d'exemple si demandé."""
    progress = Signal(str, float)
    done = Signal(object, object)                 # pauses [(fichier, Pause)], piste suggérée par fichier
    failed = Signal(str)

    def __init__(self, srcs: list[Source], cache: dict, min_duration: float, track: int | None,
                 example: tuple[int, float] | None):
        super().__init__()
        self.srcs, self.cache, self.min_d, self.track, self.example = srcs, cache, min_duration, track, example
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            out, suggested = [], {}
            ref = None
            n = len(self.srcs)
            for k, src in enumerate(self.srcs):
                if k not in self.cache:
                    self.cache[k] = M.key_thumbs(src.media, lambda f, k=k, nm=src.name: self.progress.emit(
                        f"images clés de {nm}", (k + 0.8 * f) / n), lambda: self._cancel)
                ov = src.overview
                while ov is not None and not ov.finished and not self._cancel:   # son de toutes les pistes
                    self.progress.emit(f"son de {src.name}", (k + 0.8 + 0.2 * (ov.done_until - src.start)
                                                              / max(1e-6, src.end - src.start)) / n)
                    self.msleep(200)
                if self._cancel:
                    raise InterruptedError
            if self.example is not None:
                s, t = self.example
                ref = M.reference_thumbs(self.srcs[s].media, t)
            for k, src in enumerate(self.srcs):
                kt, ov = self.cache[k], src.overview
                groups = [M.detect_still(kt, src.media, self.min_d, ov)]
                if ov is not None:
                    suggested[k] = M.suggest_pause_track(ov, self.min_d)
                    if self.track is not None and self.track < len(ov.tracks):
                        groups.append(M.detect_track(ov, src.media, self.track, kt.times, self.min_d))
                if ref is not None:
                    groups.append(M.detect_like(kt, src.media, ref, self.min_d, ov))
                out += [(k, p) for p in M.merge_pauses(*groups)]
            self.done.emit(out, suggested)
        except InterruptedError:
            self.failed.emit("Repérage annulé.")
        except Exception as e:
            log_error("Montage : repérage des pauses impossible")
            self.failed.emit(f"{type(e).__name__} : {e}")


class LiveDrop(QFrame):
    dropped = Signal(str)
    reopen = Signal(object)

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
                   "fin, les écrans d'attente ou de pause, et on exporte sans perte de qualité. Le montage est "
                   "enregistré tout seul et repris à la réouverture.")
        h.setAlignment(Qt.AlignCenter)
        h.setWordWrap(True)
        h.setStyleSheet(f"color:{MUTED}; background:transparent;")
        b = QPushButton("Ouvrir un fichier…")
        b.clicked.connect(self._pick)
        lay.addWidget(t)
        lay.addWidget(h)
        lay.addSpacing(8)
        lay.addWidget(b, alignment=Qt.AlignCenter)
        self.recent_box = QVBoxLayout()
        lay.addSpacing(10)
        lay.addLayout(self.recent_box)
        lay.addStretch(1)
        self._style()
        self.fill_recent()

    def fill_recent(self) -> None:
        while self.recent_box.count():
            w = self.recent_box.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        rec = M.recent_projects()
        if not rec:
            return
        lab = QLabel("Montages récents :")
        lab.setAlignment(Qt.AlignCenter)
        lab.setStyleSheet(f"color:{MUTED}; background:transparent;")
        self.recent_box.addWidget(lab)
        for data in rec:
            f0 = Path(data["files"][0]["path"])
            n = len(data.get("segments", []))
            more = f" + {len(data['files']) - 1} fichier(s)" if len(data["files"]) > 1 else ""
            when = datetime.fromtimestamp(data.get("saved", 0)).strftime("%d/%m %H:%M")
            b = QPushButton(f"{f0.name}{more} — {n} morceau{'x' if n > 1 else ''}, enregistré le {when}")
            b.setStyleSheet("QPushButton { background: transparent; border: none; color: #58A6FF; padding: 2px; }"
                            "QPushButton:hover { color: #F2F0EB; text-decoration: underline; }")
            b.clicked.connect(lambda _=False, d=data: self.reopen.emit(d))
            self.recent_box.addWidget(b, alignment=Qt.AlignCenter)

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
        self.srcs: list[Source] = []
        self.editor: EditorPanel | None = None
        self.worker: ExportWorker | None = None
        self.pworker: PauseWorker | None = None
        self.out: Path | None = None
        self.last_out: Path | None = None
        self.pauses: list[tuple[int, M.Pause]] = []
        self._ignored: list[tuple[int, float, float]] = []
        self._forgotten = False                  # montage oublié : ne pas le réenregistrer en fermant
        self._thumbs: dict[int, M.KeyThumbs] = {}

        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(18, 14, 18, 14)
        self.v.setSpacing(8)
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
        self.add_btn = QPushButton("Ajouter un fichier à la suite…")
        self.add_btn.setToolTip("Met un autre fichier au bout du montage (enregistrement découpé, générique…). "
                                "Pour l'export sans perte, il doit avoir le même format que le premier.")
        self.add_btn.clicked.connect(self._pick_add)
        self.new_btn = QPushButton("Nouveau montage…")
        self.new_btn.setToolTip("Ferme ce montage (il reste enregistré) et en ouvre un autre")
        self.new_btn.clicked.connect(self.drop_pick)
        for b in (self.add_btn, self.new_btn):
            b.setVisible(False)
            head.addWidget(b, 0, Qt.AlignTop)
        self.v.addLayout(head)

        self.drop = LiveDrop()
        self.drop.dropped.connect(self.open_file)
        self.drop.reopen.connect(self._reopen)
        self.v.addWidget(self.drop, 1)

        self.banner = QFrame()
        self.banner.setObjectName("card")
        self.banner.setStyleSheet(CARD)
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 6, 12, 6)
        self.banner_lbl = QLabel()
        bl.addWidget(self.banner_lbl, 1)
        self.fresh_btn = QPushButton("Repartir de zéro")
        self.fresh_btn.setToolTip("Oublie le montage enregistré et repart du fichier entier")
        self.fresh_btn.clicked.connect(self._start_fresh)
        bl.addWidget(self.fresh_btn)
        self.banner.setVisible(False)
        self.v.addWidget(self.banner)

        self.info = QLabel()
        self.info.setStyleSheet(f"color:{MUTED};")
        self.info.setVisible(False)
        self.v.addWidget(self.info)
        self.editor_box = QVBoxLayout()
        self.v.addLayout(self.editor_box, 1)

        # --- pauses ----------------------------------------------------------------------------
        self.pause_bar = QFrame()
        self.pause_bar.setObjectName("card")
        self.pause_bar.setStyleSheet(CARD)
        pl = QVBoxLayout(self.pause_bar)
        pl.setContentsMargins(12, 8, 12, 8)
        pl.setSpacing(6)
        r1 = QHBoxLayout()
        lab = QLabel("Pauses :")
        lab.setStyleSheet("font-weight:600;")
        r1.addWidget(lab)
        self.find_btn = QPushButton("Repérer les pauses")
        self.find_btn.setToolTip("Cherche les écrans fixes (attente, pause, image figée, noir) et, si une piste est "
                                 "choisie, les passages où la musique des pauses joue")
        self.find_btn.clicked.connect(lambda: self._find_pauses(None))
        self.like_btn = QPushButton("Écran comme celui-ci")
        self.like_btn.setToolTip("Place la tête sur un écran d'attente ou de pause (même animé, avec de la musique) : "
                                 "l'app retrouve tous les passages qui lui ressemblent dans le montage")
        self.like_btn.clicked.connect(self._find_like)
        r1.addWidget(self.find_btn)
        r1.addWidget(self.like_btn)
        r1.addSpacing(10)
        r1.addWidget(QLabel("au moins"))
        self.min_box = NoWheelCombo()
        for sec in (10, 20, 30, 60, 120):
            self.min_box.addItem(f"{sec} s", float(sec))
        self.min_box.setCurrentIndex(1)
        r1.addWidget(self.min_box)
        r1.addSpacing(10)
        r1.addWidget(QLabel("Musique des pauses sur :"))
        self.track_box = NoWheelCombo()
        self.track_box.setToolTip("Si une piste son ne porte que la musique des pauses, ses passages actifs sont "
                                  "les pauses (souvent le plus fiable)")
        r1.addWidget(self.track_box)
        r1.addSpacing(10)
        r1.addWidget(QLabel("Écouter :"))
        self.listen_box = NoWheelCombo()
        self.listen_box.setToolTip("Piste son jouée par le lecteur (et dessinée dans la timeline)")
        self.listen_box.currentIndexChanged.connect(self._listen_track)
        r1.addWidget(self.listen_box)
        r1.addStretch(1)
        pl.addLayout(r1)
        r2 = QHBoxLayout()
        self.pause_lbl = QLabel("Aucun repérage pour l'instant.")
        self.pause_lbl.setStyleSheet(f"color:{MUTED};")
        r2.addWidget(self.pause_lbl, 1)
        self.pbar = QProgressBar()
        self.pbar.setRange(0, 1000)
        self.pbar.setFixedWidth(200)
        self.pbar.setVisible(False)
        r2.addWidget(self.pbar)
        self.pcancel = QPushButton("Annuler")
        self.pcancel.setVisible(False)
        self.pcancel.clicked.connect(lambda: self.pworker and self.pworker.cancel())
        r2.addWidget(self.pcancel)
        self.prev_p = QPushButton("◀ Précédente")
        self.prev_p.clicked.connect(lambda: self._goto_pause(-1))
        self.next_p = QPushButton("Suivante ▶")
        self.next_p.clicked.connect(lambda: self._goto_pause(1))
        self.rm_p = QPushButton("Enlever celle-ci")
        self.rm_p.setToolTip("Enlève du montage la pause sous la tête de lecture")
        self.rm_p.clicked.connect(self._remove_current_pause)
        self.skip_p = QPushButton("Ignorer celle-ci")
        self.skip_p.setToolTip("Ce n'est pas une pause : on la garde et on ne la propose plus")
        self.skip_p.clicked.connect(self._ignore_current_pause)
        self.rm_all = QPushButton("Enlever toutes")
        self.rm_all.setObjectName("primary")
        self.rm_all.clicked.connect(self._remove_all_pauses)
        for b in (self.prev_p, self.next_p, self.rm_p, self.skip_p, self.rm_all):
            r2.addWidget(b)
        pl.addLayout(r2)
        self.pause_bar.setVisible(False)
        self.v.addWidget(self.pause_bar)

        # --- export ----------------------------------------------------------------------------
        self.export_bar = QFrame()
        self.export_bar.setObjectName("card")
        self.export_bar.setStyleSheet(CARD)
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
        self.fast.setToolTip("Le son est recopié comme la vidéo (toutes les pistes) : rien n'est réencodé. Il ne "
                             "peut être coupé qu'entre ses paquets (≈ 21 ms) : à chaque raccord l'écart reste sous "
                             "½ paquet et ne s'accumule pas.")
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
        self._hist: list = []
        self._eta = None
        self._overview_tick.connect(self._on_overview)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self.save)

    def drop_pick(self) -> None:
        self.drop._pick()

    # --- fichiers ------------------------------------------------------------------------
    def _make_source(self, path: str) -> Source | None:
        try:
            media = M.open_media(path)
        except Exception as e:
            log_error(f"Montage : ouverture impossible : {path}")
            QMessageBox.warning(self, "Montage", f"Impossible d'ouvrir ce fichier :\n{e}")
            return None
        if media.duration <= 0:
            QMessageBox.warning(self, "Montage", "Ce fichier n'a pas de durée lisible.")
            return None
        ov = M.Overview(media, on_progress=self._overview_tick.emit)
        src = Source(media, ov)
        ov.start()
        return src

    def open_file(self, path: str) -> None:
        """Nouveau montage à partir de ce fichier (le montage enregistré est repris s'il existe)."""
        if self.worker is not None:
            return
        data = M.load_project(path)
        if data is not None:
            self._reopen(data)
            return
        self._open([path], None)

    def _reopen(self, data: dict) -> None:
        self._open([f["path"] for f in data["files"]], data)

    def _open(self, paths: list[str], data: dict | None) -> None:
        self.close_file()
        srcs = []
        for p in paths:
            src = self._make_source(p)
            if src is None:
                for s in srcs:
                    s.close()
                return
            srcs.append(src)
        self.srcs = srcs
        segs = [tuple(x) for x in data["segments"]] if data and data.get("segments") else None
        if segs and any(int(s) >= len(srcs) for s, _a, _b in segs):
            segs = None
        d = data or {}
        self.editor = EditorPanel(self.srcs, segs, int(d.get("clip", 0)), d.get("t"))
        self.editor.changed.connect(self._changed)
        self.editor.meter.picked.connect(self._pick_listen)
        self.editor_box.addWidget(self.editor)
        self.out = Path(d["out"]) if d.get("out") else M.output_path(paths[0])
        self.exact.setChecked(bool(d.get("exact")))
        self.fast.setChecked(not self.exact.isChecked())
        self.pauses = [(int(s), M.Pause(a, b, k, bool(q))) for s, a, b, k, q in d.get("pauses", [])]
        self._ignored = [tuple(x) for x in d.get("ignored", [])]
        self.drop.setVisible(False)
        for w in (self.add_btn, self.new_btn, self.info, self.pause_bar, self.export_bar):
            w.setVisible(True)
        if data:
            when = datetime.fromtimestamp(d.get("saved", time.time())).strftime("%d/%m/%Y à %H:%M")
            n = len(self.editor.segments)
            self.banner_lbl.setText(f"Montage repris : {n} morceau{'x' if n > 1 else ''}, enregistré le {when}. "
                                    "Il est enregistré tout seul à chaque modification.")
            self.banner.setVisible(True)
            log.info(f"Montage repris : {paths[0]} ({n} morceaux)")
        else:
            self.banner.setVisible(False)
            log.info(f"Montage : ouverture de {paths[0]}")
        self.result.setText("")
        self.open_btn.setEnabled(False)
        self._fill_tracks()
        self._show_pauses()
        self._update()
        self.save()

    def _pick_add(self) -> None:
        exts = " ".join("*" + e for e in sorted(M.VIDEO_EXT))
        p, _ = QFileDialog.getOpenFileName(self, "Fichier à mettre à la suite", str(Path(self.srcs[0].path).parent),
                                           f"Vidéos ({exts});;Tous (*)")
        if p:
            self.add_file(p)

    def add_file(self, path: str) -> None:
        if self.editor is None or self.worker is not None:
            return
        src = self._make_source(path)
        if src is None:
            return
        why = M.incompatibility([self.srcs[0].media, src.media])
        if why:
            QMessageBox.warning(self, "Montage", f"Ce fichier ne peut pas être mis à la suite sans perte :\n{why}")
            src.close()
            return
        self.srcs.append(src)
        self.editor.add_source(src)
        log.info(f"Montage : fichier ajouté à la suite : {path}")
        self._fill_tracks()
        self._update()

    def close_file(self) -> None:
        if self.pworker is not None:
            self.pworker.cancel()
            stop(self.pworker)
            self.pworker = None
        if self.editor is not None:
            self.save()
            self.editor.shutdown()
            self.editor_box.removeWidget(self.editor)
            self.editor.deleteLater()
            self.editor = None
        for s in self.srcs:
            s.close()
        self.srcs = []
        self.pauses = []
        self._ignored = []
        self._thumbs = {}

    def _start_fresh(self) -> None:
        if not self.srcs:
            return
        if QMessageBox.question(self, "Montage", "Oublier le montage enregistré et repartir du fichier entier ?") \
                != QMessageBox.Yes:
            return
        first = self.srcs[0].path
        M.forget_project(first)
        self._forgotten = True                    # rien à réenregistrer en fermant
        self._open([first], None)

    # --- enregistrement automatique ----------------------------------------------------------
    def _changed(self) -> None:
        self._update()
        self._show_pauses()
        self._save_timer.start()

    def save(self) -> None:
        if self._forgotten:                       # « Repartir de zéro » : l'ancien montage est oublié
            self._forgotten = False
            return
        if self.editor is None or not self.srcs or not self.editor.segments:
            return
        state = self.editor.state()
        state.update(out=str(self.out) if self.out else "", exact=self.exact.isChecked(),
                     pauses=[[s, p.start, p.end, p.kind, p.quiet] for s, p in self.pauses],
                     ignored=[list(x) for x in self._ignored])
        M.save_project([s.path for s in self.srcs], state)

    # --- son -------------------------------------------------------------------------------
    def _fill_tracks(self) -> None:
        n = max((len(s.media.audio) for s in self.srcs), default=0)
        cur_t = self.track_box.currentData()
        self.track_box.blockSignals(True)
        self.track_box.clear()
        self.track_box.addItem("aucune", None)
        for k in range(n):
            self.track_box.addItem(f"piste {k + 1}", k)
        i = self.track_box.findData(cur_t)
        self.track_box.setCurrentIndex(i if i >= 0 else 0)
        self.track_box.blockSignals(False)
        cur_l = self.listen_box.currentData()
        self.listen_box.blockSignals(True)
        self.listen_box.clear()
        for k in range(n):
            self.listen_box.addItem(f"piste {k + 1}", k)
        i = self.listen_box.findData(cur_l)
        self.listen_box.setCurrentIndex(i if i >= 0 else 0)
        self.listen_box.blockSignals(False)
        self.listen_box.setEnabled(n > 1)
        self.track_box.setEnabled(n > 1)
        if self.editor is not None:
            self.editor.set_listened(int(self.listen_box.currentData() or 0))

    def _pick_listen(self, k: int) -> None:
        """Clic sur une barre du vumètre : écouter cette piste."""
        i = self.listen_box.findData(k)
        if i >= 0:
            self.listen_box.setCurrentIndex(i)

    def _listen_track(self) -> None:
        k = self.listen_box.currentData()
        if self.editor is None or k is None:
            return
        try:
            self.editor.player.setActiveAudioTrack(int(k))
        except Exception:
            pass
        self.editor.set_listened(int(k))
        for s in self.srcs:
            if s.overview is not None:
                s.overview.shown = int(k)
        self.editor.timeline.update()

    # --- pauses ----------------------------------------------------------------------------
    def _find_pauses(self, example) -> None:
        if self.editor is None or self.pworker is not None:
            return
        self.pworker = PauseWorker(self.srcs, self._thumbs, float(self.min_box.currentData()),
                                   self.track_box.currentData(), example)
        self.pworker.progress.connect(self._on_pause_progress)
        self.pworker.done.connect(self._on_pauses)
        self.pworker.failed.connect(self._on_pause_failed)
        self.pbar.setValue(0)
        self.pbar.setVisible(True)
        self.pcancel.setVisible(True)
        self.pause_lbl.setStyleSheet(f"color:{MUTED};")
        self.pause_lbl.setText("Repérage…")
        self.pworker.start()
        self._update()

    def _find_like(self) -> None:
        if self.editor is not None:
            self._find_pauses((self.editor._src_index(), self.editor._t))

    def _on_pause_progress(self, what: str, f: float) -> None:
        self.pbar.setValue(int(1000 * f))
        self.pause_lbl.setText(f"Repérage… {what} ({int(f * 100)} %)")

    def _end_pauses(self) -> None:
        release(self.pworker)                    # il finit son « run » : le garder jusqu'au bout
        self.pworker = None
        self.pbar.setVisible(False)
        self.pcancel.setVisible(False)

    def _on_pauses(self, found, suggested) -> None:
        self._end_pauses()
        ign = {(s, round(a, 2), round(b, 2)) for s, a, b in self._ignored}
        # on garde les pauses déjà repérées (exemple précédent…) et on ajoute les nouvelles
        by_src: dict[int, list] = {}
        for s, p in self.pauses + list(found):
            by_src.setdefault(s, []).append(p)
        merged = [(s, p) for s, ps in by_src.items() for p in M.merge_pauses(ps)]
        self.pauses = [(s, p) for s, p in merged if (s, round(p.start, 2), round(p.end, 2)) not in ign]
        # piste de la musique des pauses : proposée si l'app en trouve une et qu'aucune n'est choisie
        sug = next((v for v in suggested.values() if v is not None), None)
        if sug is not None and self.track_box.currentData() is None:
            self.track_box.setCurrentIndex(self.track_box.findData(sug))
            log.info(f"Montage : piste {sug + 1} proposée pour la musique des pauses")
            self.pause_lbl.setText(f"La piste {sug + 1} ressemble à la musique des pauses : elle est choisie, "
                                   "nouveau repérage…")
            QTimer.singleShot(50, lambda: self._find_pauses(None))
            return
        log.info(f"Montage : {len(self.pauses)} pause(s) repérée(s)")
        self._show_pauses()
        self.save()

    def _on_pause_failed(self, msg: str) -> None:
        self._end_pauses()
        self.pause_lbl.setText(msg)
        self._update()

    def _active_pauses(self) -> list[tuple[int, M.Pause]]:
        """Pauses encore dans le montage (en partie au moins), dans l'ordre du montage."""
        if self.editor is None:
            return []
        found = []
        for (s, a, b), o in zip(self.editor.segments, self.editor.timeline.starts()):
            for ps, p in self.pauses:
                if ps == s and p.end > a + 0.5 and p.start < b - 0.5:
                    found.append((o + max(p.start, a) - a, ps, p))
        found.sort(key=lambda x: x[0])
        seen, res = set(), []
        for _o, ps, p in found:
            if id(p) not in seen:
                seen.add(id(p))
                res.append((ps, p))
        return res

    def _show_pauses(self) -> None:
        if self.editor is None:
            return
        act = self._active_pauses()
        self.editor.timeline.pauses = [(s, p.start, p.end) for s, p in act]
        self.editor.timeline.update()
        if self.pworker is not None:
            pass
        elif act:
            tot = sum(p.duration for _s, p in act)
            detail = ", ".join(sorted({KINDS.get(k, k) for _s, p in act for k in p.kind.split("+")}))
            n = len(act)
            self.pause_lbl.setText(f"{n} pause{'s' if n > 1 else ''} repérée{'s' if n > 1 else ''} dans le montage · "
                                   f"{fmt_t(tot, False)} au total ({detail}), hachurées en orange")
            self.pause_lbl.setStyleSheet(f"color:{ORANGE};")
        elif self.pauses:
            self.pause_lbl.setText("Toutes les pauses repérées sont enlevées du montage.")
            self.pause_lbl.setStyleSheet(f"color:{GREEN};")
        elif self._thumbs:
            self.pause_lbl.setText("Aucune pause repérée. Pour un écran d'attente animé : place la tête dessus et "
                                   "« Écran comme celui-ci ».")
            self.pause_lbl.setStyleSheet(f"color:{MUTED};")
        self._update()

    def _pause_at_head(self) -> tuple[int, M.Pause] | None:
        ed = self.editor
        if ed is None:
            return None
        s = ed._src_index()
        for ps, p in self._active_pauses():
            if ps == s and p.start - 0.5 <= ed._t <= p.end + 0.5:
                return ps, p
        return None

    def _goto_pause(self, d: int) -> None:
        ed = self.editor
        act = self._active_pauses()
        if ed is None or not act:
            return
        out = ed._out()
        pos = []
        for ps, p in act:
            for (s, a, b), o in zip(ed.segments, ed.timeline.starts()):
                if s == ps and p.end > a + 0.5 and p.start < b - 0.5:
                    pos.append(o + max(p.start, a) - a)
                    break
        if d > 0:
            nxt = [x for x in pos if x > out + 0.6]
            target = nxt[0] if nxt else pos[0]
        else:
            prv = [x for x in pos if x < out - 0.6]
            target = prv[-1] if prv else pos[-1]
        ed._hold()
        ed._goto_out(target + 0.5, False)

    def _remove_current_pause(self) -> None:
        cur = self._pause_at_head()
        if cur is None:
            self._goto_pause(1)
            cur = self._pause_at_head()
            if cur is None:
                return
        s, p = cur
        self.editor.remove_range(s, p.start, p.end)

    def _ignore_current_pause(self) -> None:
        cur = self._pause_at_head()
        if cur is None:
            return
        s, p = cur
        self._ignored.append((s, p.start, p.end))
        self.pauses = [(ps, q) for ps, q in self.pauses if q is not p]
        self._show_pauses()
        self.save()

    def _remove_all_pauses(self) -> None:
        act = self._active_pauses()
        if self.editor is None or not act:
            return
        tot = sum(p.duration for _s, p in act)
        if QMessageBox.question(self, "Montage", f"Enlever les {len(act)} pauses repérées ({fmt_t(tot, False)}) du "
                                "montage ?\n(Ctrl+Z pour revenir en arrière)") != QMessageBox.Yes:
            return
        self.editor.remove_ranges([(s, p.start, p.end) for s, p in act])

    # --- affichage ---------------------------------------------------------------------------
    def _on_overview(self) -> None:
        if self.editor is not None:
            self.editor.timeline.update()
            self._update_info()

    def _update_info(self) -> None:
        if not self.srcs:
            return
        m = self.srcs[0].media
        bits = [Path(m.path).name + (f" + {len(self.srcs) - 1} fichier(s)" if len(self.srcs) > 1 else ""),
                fmt_t(sum(s.end - s.start for s in self.srcs), False),
                fmt_size(sum(s.media.size for s in self.srcs))]
        if m.has_video:
            bits.append(f"{m.width}×{m.height} · {m.fps:.2f} i/s · {m.video_codec}".replace(".", ","))
        if m.audio:
            a = m.audio[0]
            bits.append(f"son {a['codec']} {a['rate'] // 1000} kHz {a['layout']}"
                        + (f" · {len(m.audio)} pistes" if len(m.audio) > 1 else ""))
        pend = [s for s in self.srcs if s.overview is not None and not s.overview.finished]
        if pend:
            s = pend[0]
            bits.append(f"aperçu du son : {int(100 * (s.overview.done_until - s.start) / max(1e-6, s.end - s.start))} %")
        self.info.setText("  ·  ".join(bits))

    def _update(self) -> None:
        self._update_info()
        if self.out is not None:
            self.out_lbl.setText(str(self.out))
        busy = self.worker is not None
        has = self.editor is not None
        full = has and self.editor.is_full() and len(self.srcs) == 1
        self.go_btn.setEnabled(has and not busy and not full)
        self.go_btn.setToolTip("Rien n'est coupé : utilise « Début ici », « Fin ici » ou « Couper ici »" if full else "")
        multi = has and len({s for s, _a, _b in self.editor.segments}) > 1
        self.exact.setEnabled(not multi)
        if multi and self.exact.isChecked():
            self.fast.setChecked(True)
        self.exact.setToolTip("Un seul fichier à la fois (plusieurs fichiers : son recopié)" if multi else
                              "Le son est décodé et réencodé dans le même format, coupé à l'échantillon près "
                              "(synchro exacte). La vidéo reste recopiée telle quelle.")
        on = has and not busy and bool(self._active_pauses())
        for b in (self.prev_p, self.next_p, self.rm_p, self.skip_p, self.rm_all):
            b.setEnabled(on)
        idle = has and not busy and self.pworker is None
        self.find_btn.setEnabled(idle)
        self.like_btn.setEnabled(idle and self.editor.src.has_video)
        self.add_btn.setEnabled(has and not busy)

    def _choose_out(self) -> None:
        if self.out is None:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Fichier du montage", str(self.out),
                                           f"Même format (*{self.out.suffix})")
        if p:
            q = Path(p)
            if q.suffix.lower() != self.out.suffix.lower():
                q = q.with_suffix(self.out.suffix)          # même conteneur : recopie sans perte
            if any(q.resolve() == Path(s.path).resolve() for s in self.srcs):
                QMessageBox.warning(self, "Montage", "Choisis un autre nom que les fichiers d'origine.")
                return
            self.out = q
            self._update()
            self.save()

    # --- export -------------------------------------------------------------------------
    def export(self) -> None:
        if self.editor is None or self.worker is not None:
            return
        segs = list(self.editor.segments)
        used = sorted({s for s, _a, _b in segs})
        why = M.incompatibility([self.srcs[i].media for i in used])
        if why:
            QMessageBox.warning(self, "Montage", f"Export sans perte impossible :\n{why}")
            return
        if self.out.exists():
            if QMessageBox.question(self, "Montage", f"{self.out.name} existe déjà. Le remplacer ?") \
                    != QMessageBox.Yes:
                return
        self.editor.player.pause()
        for s in self.srcs:
            if s.overview is not None:
                s.overview.pause()            # l'export d'abord (sinon 3 fois plus lent)
        self.worker = ExportWorker([s.media for s in self.srcs], segs, self.out, self.exact.isChecked())
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
        """Avancement et temps restant, estimé sur la vitesse des 8 dernières secondes."""
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
        self._eta = left if self._eta is None else 0.7 * self._eta + 0.3 * left
        self.result.setText(f"Export… {int(f * 100)} % — encore {fmt_t(max(0.0, self._eta), False)} environ")

    def _end(self) -> None:
        release(self.worker)
        self.worker = None
        for s in self.srcs:
            if s.overview is not None:
                s.overview.resume()
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
        self.out = M.output_path(self.srcs[0].path)        # prochain export : un nouveau nom
        self._update()
        self.save()

    def _on_failed(self, msg: str) -> None:
        self._end()
        self.result.setStyleSheet(f"color:{RED if 'annulé' not in msg else MUTED};")
        self.result.setText(msg)

    # --- glisser-déposer sur toute la page ---------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self.worker is None:
            e.acceptProposedAction()

    def dropEvent(self, e):
        """Pas de montage ouvert : le fichier est ouvert. Montage ouvert : il est mis à la suite."""
        for u in e.mimeData().urls():
            if u.toLocalFile():
                e.acceptProposedAction()
                if self.editor is None:
                    self.open_file(u.toLocalFile())
                else:
                    self.add_file(u.toLocalFile())
                return

    def shutdown(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            stop(self.worker, 10000)
        self.close_file()
