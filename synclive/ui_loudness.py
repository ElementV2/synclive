# SPDX-License-Identifier: GPL-3.0-or-later
"""Onglet « Normalisation » : mettre tous les médias reçus en régie au même niveau (EBU R128).

On dépose des fichiers ou des dossiers ; chacun est mesuré aussitôt (niveau intégré, crête
vraie, dynamique), on voit l'écart à la cible, puis « Normaliser » écrit une copie corrigée de
chaque fichier : vidéo intacte (pas de réencodage), son au niveau cible, synchro gardée."""
from __future__ import annotations

import math
import queue
import traceback
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMenu, QProgressBar, QPushButton, QRadioButton, QSizePolicy,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import loudness as L

ORANGE = "#FF5A1F"
GREEN = "#3FB950"
YELLOW = "#E3B341"
RED = "#F85149"
MUTED = "#8A8780"

COLS = ["Fichier", "Durée", "Niveau", "Crête", "Dynamique", "Écart à la cible", "Correction", "Après", "État"]
C_FILE, C_DUR, C_LVL, C_PEAK, C_LRA, C_BAR, C_GAIN, C_AFTER, C_STATE = range(len(COLS))
BAR_MIN, BAR_MAX = -40.0, -6.0      # échelle de la colonne « Écart à la cible » (LUFS)


def fmt_db(v: float, unit: str, sign: bool = False) -> str:
    if v is None or not math.isfinite(v):
        return "—"
    s = f"{v:+.1f}" if sign else f"{v:.1f}"
    return f"{s} {unit}".replace(".", ",").replace("-", "−")


def fmt_dur(sec: float) -> str:
    if not sec or sec <= 0:
        return "—"
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def media_files(paths: list[str]) -> list[str]:
    """Fichiers médias d'une liste de fichiers et de dossiers (dossiers parcourus en entier)."""
    out = []
    for p in paths:
        q = Path(p)
        if q.is_dir():
            for f in sorted(q.rglob("*")):
                if f.is_file() and f.suffix.lower() in L.MEDIA_EXT and "normalisé" not in f.parts:
                    out.append(str(f))
        elif q.is_file():
            out.append(str(q))
    return out


def settings() -> QSettings:
    """Réglages de l'onglet. SYNCLIVE_SETTINGS_INI (tests) : un fichier à part, sans toucher à ceux
    de l'utilisateur."""
    import os
    ini = os.environ.get("SYNCLIVE_SETTINGS_INI")
    return QSettings(ini, QSettings.IniFormat) if ini else QSettings("SyncLive", "SyncLive")


class Item:
    """Un fichier de la liste."""

    def __init__(self, path: str):
        self.path = path
        self.info: L.FileLoudness | None = None
        self.plan: L.Plan | None = None
        self.state = "attente"          # attente, mesure, prêt, travail, fait, erreur, vide
        self.progress = 0.0
        self.error = ""
        self.out: Path | None = None
        self.after: L.FileLoudness | None = None
        self.segments: list[L.Segment] | None = None     # montage (None : fichier entier)

    def main_kept(self) -> L.TrackLoudness | None:
        """Piste principale mesurée sur les seuls morceaux gardés."""
        if self.info is None or self.info.main is None:
            return None
        return L.restrict(self.info.main, None if L.is_full(self.info, self.segments) else self.segments)

    def replan(self, target: L.Target) -> None:
        if self.info is not None and self.info.tracks:
            self.plan = L.plans_for(self.info, target, self.segments)[0]


# --- travail en arrière-plan ---------------------------------------------------------------
class MeasureWorker(QThread):
    """Mesure les fichiers un par un, au fur et à mesure qu'on les ajoute."""
    progress = Signal(str, float)
    measured = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.q: queue.Queue[str | None] = queue.Queue()
        self.dropped: set[str] = set()

    def add(self, path: str) -> None:
        self.dropped.discard(path)
        self.q.put(path)

    def drop(self, path: str) -> None:
        self.dropped.add(path)

    def stop(self) -> None:
        self.q.put(None)

    def run(self) -> None:
        while True:
            p = self.q.get()
            if p is None:
                return
            if p in self.dropped:
                continue
            try:
                info = L.measure(p, lambda f, p=p: self.progress.emit(p, f), lambda p=p: p in self.dropped)
                self.measured.emit(p, info)
            except InterruptedError:
                pass
            except Exception as e:
                traceback.print_exc()
                self.failed.emit(p, _reason(e))


class PlanWorker(QThread):
    """Plans (gain, limiteur) calculés en tâche de fond : avec le limiteur, ~0,15 s par fichier de
    3 min ; sur le fil de l'interface, changer la cible d'une longue liste la figeait."""
    planned = Signal(int, str, object)       # génération, chemin, plan

    def __init__(self):
        super().__init__()
        self.q: queue.Queue = queue.Queue()
        self.gen = 0

    def submit(self, gen: int, jobs: list[tuple[str, object]], target: L.Target) -> None:
        self.q.put((gen, jobs, target))

    def stop(self) -> None:
        self.q.put(None)

    def run(self) -> None:
        while True:
            job = self.q.get()
            if job is None:
                return
            gen, jobs, target = job
            for path, info in jobs:
                if gen != self.gen:              # cible changée entre-temps : travail obsolète
                    break
                try:
                    pl = L.plans_for(info, target, None)[0]
                except Exception:
                    traceback.print_exc()
                    continue
                self.planned.emit(gen, path, pl)


class NormalizeWorker(QThread):
    progress = Signal(str, float)
    finished_one = Signal(str, str, object, object)     # chemin, sortie, plans, mesure après
    failed = Signal(str, str)
    all_done = Signal(bool)                              # True : annulé

    def __init__(self, jobs: list[tuple[str, L.FileLoudness, Path, list | None]], target: L.Target):
        super().__init__()
        self.jobs, self.target, self._cancel = jobs, target, False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        for path, info, dst, segs in self.jobs:
            if self._cancel:
                break
            try:
                # écrit, remesure le fichier écrit, et refait si l'encodeur a fait dépasser la crête
                plans, after = L.normalize_checked(path, dst, info, self.target,
                                                   lambda f, p=path: self.progress.emit(p, f), lambda: self._cancel,
                                                   segs)
                self.finished_one.emit(path, str(dst), plans, after)
            except InterruptedError:
                break
            except Exception as e:
                traceback.print_exc()
                self.failed.emit(path, _reason(e))
        self.all_done.emit(self._cancel)


def _reason(e: Exception) -> str:
    msg = str(e) or type(e).__name__
    if "Invalid data" in msg or "could not find codec" in msg.lower():
        return "fichier illisible ou format non pris en charge"
    return msg[:200]


# --- affichage ------------------------------------------------------------------------------
class LevelBar(QStyledItemDelegate):
    """Colonne « Écart à la cible » : échelle −40…−6 LUFS, la cible en orange, le niveau mesuré
    (point gris relié à la cible) et le niveau obtenu (point vert)."""

    def __init__(self, page: "LoudnessPage"):
        super().__init__(page)
        self.page = page

    def paint(self, p: QPainter, opt, index):
        it = self.page.item_at(index.row())
        r = QRectF(opt.rect).adjusted(10, 0, -10, 0)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        y = r.center().y()
        x_of = lambda v: r.left() + (min(max(v, BAR_MIN), BAR_MAX) - BAR_MIN) / (BAR_MAX - BAR_MIN) * r.width()
        p.setPen(QPen(QColor("#2E2E35"), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(r.left(), y, r.right(), y)
        tgt = self.page.target().lufs
        xt = x_of(tgt)
        if it is not None and it.info is not None and it.info.main is not None and not it.info.main.silent:
            lvl = it.info.main.integrated
            xl = x_of(lvl)
            d = abs(lvl - tgt)
            col = QColor(GREEN if d <= 1.0 else (YELLOW if d <= 3.0 else RED))
            p.setPen(QPen(col, 4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(min(xl, xt), y, max(xl, xt), y)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#B8B5AE"))
            p.drawEllipse(QRectF(xl - 5, y - 5, 10, 10))
            done = it.after.main.integrated if it.after is not None and it.after.main else None
            if done is not None and math.isfinite(done):
                p.setBrush(QColor(GREEN))
                xd = x_of(done)
                p.drawEllipse(QRectF(xd - 5, y - 5, 10, 10))
        p.setPen(QPen(QColor(ORANGE), 2))
        p.drawLine(xt, y - 9, xt, y + 9)
        p.restore()


class DropArea(QFrame):
    """Zone de dépôt : grande tant que la liste est vide, bandeau ensuite."""
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self._hover = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        self.title = QLabel("Glisse ici les vidéos ou les dossiers à mettre au même niveau")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-size:17px; font-weight:600; background:transparent;")
        self.hint = QLabel("MP4, MOV, MKV, MXF, WAV, MP3… · les dossiers sont parcourus en entier · "
                           "la vidéo n'est jamais réencodée")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(f"color:{MUTED}; background:transparent;")
        row = QHBoxLayout()
        row.addStretch(1)
        self.add_files = QPushButton("Ajouter des fichiers…")
        self.add_dir = QPushButton("Ajouter un dossier…")
        row.addWidget(self.add_files)
        row.addWidget(self.add_dir)
        row.addStretch(1)
        lay.addStretch(1)
        lay.addWidget(self.title)
        lay.addWidget(self.hint)
        lay.addSpacing(6)
        lay.addLayout(row)
        lay.addStretch(1)
        self.add_files.clicked.connect(self._pick_files)
        self.add_dir.clicked.connect(self._pick_dir)
        self.set_compact(False)

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        self.title.setText("Déposer d'autres fichiers ou dossiers" if compact
                           else "Glisse ici les vidéos ou les dossiers à mettre au même niveau")
        self.title.setStyleSheet(f"font-size:{14 if compact else 17}px; font-weight:600; background:transparent;")
        self.hint.setVisible(not compact)
        self.setMinimumHeight(86 if compact else 220)
        self.setMaximumHeight(96 if compact else 16777215)
        self._style()

    def _style(self) -> None:
        c = ORANGE if self._hover else "#3A3A42"
        bg = "#201A18" if self._hover else "#19191D"
        self.setStyleSheet(f"DropArea {{ border: 2px dashed {c}; border-radius: 10px; background: {bg}; }}")

    def _pick_files(self) -> None:
        exts = " ".join("*" + e for e in sorted(L.MEDIA_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Ajouter des médias", "", f"Médias ({exts});;Tous (*)")
        if files:
            self.dropped.emit(files)

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Ajouter un dossier")
        if d:
            self.dropped.emit([d])

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
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.dropped.emit(paths)


class NoWheelCombo(QComboBox):
    def wheelEvent(self, e):
        e.ignore()


class NoWheelSpin(QDoubleSpinBox):
    """Champ numérique sans flèches (saisie, ↑ ↓ au clavier) et insensible à la molette."""

    def __init__(self):
        super().__init__()
        self.setStyleSheet("QDoubleSpinBox { padding: 5px 10px; }"
                           "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 0; border: none; }")

    def wheelEvent(self, e):
        e.ignore()


# --- la page ---------------------------------------------------------------------------------
class LoudnessPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.items: list[Item] = []
        self.norm: NormalizeWorker | None = None
        self.meter = MeasureWorker()
        self.meter.progress.connect(self._on_progress)
        self.meter.measured.connect(self._on_measured)
        self.meter.failed.connect(self._on_failed)
        self.meter.start()
        self.planner = PlanWorker()
        self.planner.planned.connect(self._on_planned)
        self.planner.start()
        self._settings = settings()

        self.setStyleSheet(
            "QRadioButton { spacing: 8px; }"
            "QRadioButton::indicator { width: 14px; height: 14px; border-radius: 8px; border: 1px solid #5A5A63;"
            " background: #1B1B20; }"
            "QRadioButton::indicator:checked { background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5,"
            " fy:0.5, stop:0 #FF5A1F, stop:0.45 #FF5A1F, stop:0.55 #1B1B20, stop:1 #1B1B20); border-color: #FF5A1F; }"
            "QTableWidget::item { padding: 0 8px; }"
            "QTableWidget::item:selected { background: #26262C; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Normaliser le son")
        title.setStyleSheet("font-size:18px; font-weight:600;")
        sub = QLabel("Toutes les vidéos au même niveau sonore (mesure EBU R128) : l'ingé son n'a plus "
                     "d'écart de volume d'un média à l'autre. La vidéo est recopiée sans réencodage et la "
                     "synchro son / image est gardée.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{MUTED};")
        tv = QVBoxLayout()
        tv.setSpacing(2)
        tv.addWidget(title)
        tv.addWidget(sub)
        head.addLayout(tv, 1)
        v.addLayout(head)

        # réglages
        bar = QFrame()
        bar.setObjectName("card")
        bar.setStyleSheet("QFrame#card QLabel, QFrame#card QCheckBox { background: transparent; }")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(10)
        bl.addWidget(QLabel("Cible :"))
        self.preset = NoWheelCombo()
        for name, t in L.PRESETS:
            self.preset.addItem(name, (t.lufs, t.peak))
        self.preset.addItem("Personnalisée", None)
        self.preset.setMinimumContentsLength(30)
        bl.addWidget(self.preset)
        self.lufs = NoWheelSpin()
        self.lufs.setRange(-40.0, -5.0)
        self.lufs.setDecimals(1)
        self.lufs.setSingleStep(0.5)
        self.lufs.setSuffix(" LUFS")
        self.lufs.setFixedWidth(96)
        self.lufs.setAlignment(Qt.AlignRight)
        self.lufs.setToolTip("Niveau intégré visé pour chaque fichier (loudness EBU R128)")
        bl.addWidget(self.lufs)
        bl.addSpacing(8)
        bl.addWidget(QLabel("Crête max :"))
        self.peak = NoWheelSpin()
        self.peak.setRange(-9.0, 0.0)
        self.peak.setDecimals(1)
        self.peak.setSingleStep(0.5)
        self.peak.setSuffix(" dBTP")
        self.peak.setFixedWidth(96)
        self.peak.setAlignment(Qt.AlignRight)
        self.peak.setToolTip("Crête vraie maximale (entre échantillons comprise) après correction")
        bl.addWidget(self.peak)
        bl.addSpacing(8)
        self.limiter = QCheckBox("Limiteur de crête")
        self.limiter.setChecked(True)
        self.limiter.setToolTip("Si le gain ferait dépasser la crête max, seuls ces passages sont abaissés "
                                f"(au plus {L.MAX_LIMIT_DB:.0f} dB). Sans limiteur, le gain est réduit et le "
                                "fichier reste sous la cible.")
        bl.addWidget(self.limiter)
        bl.addStretch(1)
        self.common_btn = QPushButton("Cible commune")
        self.common_btn.setToolTip("Règle la cible au niveau le plus haut que TOUS les fichiers de la liste "
                                   "peuvent atteindre sans écraser le son : tout le lot finit au même niveau.")
        self.common_btn.clicked.connect(self._common_target)
        bl.addWidget(self.common_btn)
        v.addWidget(bar)

        self.drop = DropArea()
        self.drop.dropped.connect(self.add_paths)
        v.addWidget(self.drop, 1)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_FILE, QHeaderView.Stretch)
        hh.setDefaultAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.table.horizontalHeaderItem(C_BAR).setToolTip(
            f"Échelle {BAR_MIN:.0f} à {BAR_MAX:.0f} LUFS · trait orange : la cible · point gris : le niveau "
            "du fichier · point vert : le niveau obtenu".replace("-", "−"))
        for c in (C_DUR, C_LVL, C_PEAK, C_LRA, C_GAIN, C_AFTER):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(C_BAR, QHeaderView.Fixed)
        self.table.setColumnWidth(C_BAR, 220)
        hh.setSectionResizeMode(C_STATE, QHeaderView.Fixed)
        self.table.setColumnWidth(C_STATE, 250)
        self.table.setItemDelegateForColumn(C_BAR, LevelBar(self))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.setVisible(False)
        v.addWidget(self.table, 3)
        QShortcut(QKeySequence(Qt.Key_Delete), self.table, activated=self._remove_selected)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size:14px;")
        v.addWidget(self.summary)

        out = QHBoxLayout()
        out.addWidget(QLabel("Fichiers corrigés :"))
        self.out_beside = QRadioButton("dans un dossier « normalisé » à côté de chaque original")
        self.out_choose = QRadioButton("tous dans :")
        self.out_dir_lbl = QLabel()
        self.out_dir_lbl.setStyleSheet(f"color:{MUTED};")
        self.out_dir_btn = QPushButton("Choisir…")
        self.out_dir_btn.clicked.connect(self._choose_dir)
        self.out_beside.setChecked(True)
        out.addWidget(self.out_beside)
        out.addSpacing(10)
        out.addWidget(self.out_choose)
        out.addWidget(self.out_dir_lbl)
        out.addWidget(self.out_dir_btn)
        out.addStretch(1)
        v.addLayout(out)

        act = QHBoxLayout()
        self.rm_btn = QPushButton("Retirer la sélection")
        self.rm_btn.clicked.connect(self._remove_selected)
        self.clear_btn = QPushButton("Tout retirer")
        self.clear_btn.clicked.connect(self._clear)
        act.addWidget(self.rm_btn)
        act.addWidget(self.clear_btn)
        act.addStretch(1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(220)
        self.bar.setVisible(False)
        self.bar_lbl = QLabel()
        self.bar_lbl.setStyleSheet(f"color:{MUTED};")
        act.addWidget(self.bar_lbl)
        act.addWidget(self.bar)
        self.cancel_btn = QPushButton("Annuler")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.setVisible(False)
        self.open_btn = QPushButton("Ouvrir le dossier")
        self.open_btn.clicked.connect(self._open_out)
        self.open_btn.setEnabled(False)
        self.go_btn = QPushButton("Normaliser")
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self._start)
        act.addWidget(self.cancel_btn)
        act.addWidget(self.open_btn)
        act.addWidget(self.go_btn)
        v.addLayout(act)

        self._load_settings()
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.lufs.valueChanged.connect(self._target_changed)
        self.peak.valueChanged.connect(self._target_changed)
        self.limiter.toggled.connect(self._target_changed)
        self.out_beside.toggled.connect(self._save_settings)
        self._replan_timer = QTimer(self)
        self._replan_timer.setSingleShot(True)
        self._replan_timer.setInterval(250)
        self._replan_timer.timeout.connect(self._replan)
        self._refresh()

    # --- réglages ---------------------------------------------------------------------------
    def target(self) -> L.Target:
        return L.Target(round(self.lufs.value(), 1), round(self.peak.value(), 1), self.limiter.isChecked())

    def _load_settings(self) -> None:
        s = self._settings
        idx = int(s.value("loud/preset", 0))
        self.preset.setCurrentIndex(min(max(idx, 0), self.preset.count() - 1))
        data = self.preset.currentData()
        lufs = float(s.value("loud/lufs", data[0] if data else -23.0))
        peak = float(s.value("loud/peak", data[1] if data else -1.0))
        self.lufs.setValue(lufs)
        self.peak.setValue(peak)
        self.limiter.setChecked(str(s.value("loud/limiter", "true")).lower() == "true")
        self.out_dir = str(s.value("loud/out_dir", "")) or None
        self.out_choose.setChecked(bool(self.out_dir) and str(s.value("loud/beside", "true")).lower() != "true")
        self.out_beside.setChecked(not self.out_choose.isChecked())
        self.out_dir_lbl.setText(self.out_dir or "(aucun dossier choisi)")

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("loud/preset", self.preset.currentIndex())
        s.setValue("loud/lufs", self.lufs.value())
        s.setValue("loud/peak", self.peak.value())
        s.setValue("loud/limiter", "true" if self.limiter.isChecked() else "false")
        s.setValue("loud/out_dir", self.out_dir or "")
        s.setValue("loud/beside", "true" if self.out_beside.isChecked() else "false")

    def _preset_changed(self) -> None:
        data = self.preset.currentData()
        if data:
            for w, val in ((self.lufs, data[0]), (self.peak, data[1])):
                w.blockSignals(True)
                w.setValue(val)
                w.blockSignals(False)
        self._target_changed()

    def _target_changed(self) -> None:
        # une valeur qui ne correspond plus au préréglage : « Personnalisée »
        data = self.preset.currentData()
        if data and (abs(data[0] - self.lufs.value()) > 1e-6 or abs(data[1] - self.peak.value()) > 1e-6):
            match = next((i for i in range(self.preset.count())
                          if (d := self.preset.itemData(i)) and abs(d[0] - self.lufs.value()) < 1e-6
                          and abs(d[1] - self.peak.value()) < 1e-6), self.preset.count() - 1)
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(match)
            self.preset.blockSignals(False)
        self._save_settings()
        # plans de l'ancienne cible : périmés tout de suite (« Calcul… », bouton grisé), le recalcul
        # part 250 ms après le dernier changement (pas à chaque clic)
        self.planner.gen += 1
        for it in self.items:
            it.after = None if it.state != "fait" else it.after
            if it.state in ("prêt", "fait") and it.info is not None:
                it.plan = None
        self._replan_timer.start()
        self._refresh()

    def _choose_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Dossier des fichiers corrigés", self.out_dir or "")
        if d:
            self.out_dir = d
            self.out_dir_lbl.setText(d)
            self.out_choose.setChecked(True)
            self._save_settings()

    def _common_target(self) -> None:
        tracks = [it.main_kept() for it in self.items if it.info is not None and it.info.main is not None]
        val = L.common_target(tracks, self.target())
        if val is None:
            return
        val = min(val, -5.0)
        self.lufs.setValue(max(-40.0, val))

    # --- liste --------------------------------------------------------------------------------
    def item_at(self, row: int) -> Item | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    def add_paths(self, paths: list[str]) -> None:
        known = {it.path for it in self.items}
        added = 0
        for p in media_files(paths):
            if p in known:
                continue
            it = Item(p)
            self.items.append(it)
            known.add(p)
            self.meter.add(p)
            it.state = "mesure"
            added += 1
        if added:
            self._refresh()

    def _remove(self, rows: list[int]) -> None:
        if self.norm is not None:
            return
        for r in sorted(set(rows), reverse=True):
            it = self.items.pop(r)
            self.meter.drop(it.path)
        self._refresh()

    def _remove_selected(self) -> None:
        self._remove([i.row() for i in self.table.selectionModel().selectedRows()])

    def _clear(self) -> None:
        self._remove(list(range(len(self.items))))

    def _menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        it = self.item_at(row)
        if it is None:
            return
        m = QMenu(self)
        m.addAction("Ouvrir l'emplacement de l'original",
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(it.path).parent))))
        if it.out is not None:
            m.addAction("Lire le fichier corrigé", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(it.out))))
            m.addAction("Ouvrir l'emplacement du fichier corrigé",
                        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(it.out.parent))))
        m.addSeparator()
        m.addAction("Retirer de la liste", lambda: self._remove([row]))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _todo(self, it: Item) -> bool:
        return True

    # --- mesures --------------------------------------------------------------------------------
    def _find(self, path: str) -> int:
        return next((i for i, it in enumerate(self.items) if it.path == path), -1)

    def _on_progress(self, path: str, f: float) -> None:
        r = self._find(path)
        if r >= 0:
            self.items[r].progress = f
            self._row(r)

    def _on_measured(self, path: str, info: L.FileLoudness) -> None:
        r = self._find(path)
        if r < 0:
            return
        it = self.items[r]
        it.info = info
        if not info.tracks:
            it.state, it.error = "vide", "pas de son dans ce fichier"
        elif all(t.silent for t in info.tracks):
            it.state, it.error = "vide", "son muet : rien à corriger"
        else:
            it.state = "prêt"
            it.plan = None
            self._plan_items([it])
        self._row(r)
        self._summary()
        self._update_buttons()

    def _on_failed(self, path: str, msg: str) -> None:
        r = self._find(path)
        if r >= 0:
            self.items[r].state, self.items[r].error = "erreur", msg
            self._row(r)
            self._summary()
            self._update_buttons()

    def _replan(self) -> None:
        """Cible changée : tous les plans sont recalculés en tâche de fond (nouvelle génération :
        un calcul en cours pour l'ancienne cible est abandonné)."""
        self.planner.gen += 1
        todo = []
        for it in self.items:
            if it.info is not None and it.info.main is not None and not it.info.main.silent:
                it.plan = None
                if it.state == "fait":
                    it.state = "prêt"      # cible changée : à refaire
                todo.append(it)
        self._plan_items(todo)
        self._refresh()

    def _plan_items(self, items) -> None:
        if items:
            self.planner.submit(self.planner.gen, [(it.path, it.info) for it in items], self.target())

    def _on_planned(self, gen: int, path: str, plan) -> None:
        if gen != self.planner.gen:
            return
        r = self._find(path)
        if r < 0:
            return
        self.items[r].plan = plan
        self._row(r)
        self._summary()
        self._update_buttons()

    # --- normalisation ------------------------------------------------------------------------
    def _start(self) -> None:
        if self.norm is not None:
            return
        t = self.target()
        jobs = []
        for it in self.items:
            if it.state == "prêt" and it.info is not None and self._todo(it):
                it.out = L.output_path(it.path, self.out_dir if self.out_choose.isChecked() else None, t)
                jobs.append((it.path, it.info, it.out, it.segments))
                it.state, it.progress, it.after = "travail", 0.0, None
        if not jobs:
            return
        self._jobs_total = len(jobs)
        self._jobs_done = 0
        self.norm = NormalizeWorker(jobs, t)
        self.norm.progress.connect(self._on_norm_progress)
        self.norm.finished_one.connect(self._on_norm_done)
        self.norm.failed.connect(self._on_norm_failed)
        self.norm.all_done.connect(self._on_all_done)
        self.norm.start()
        self._refresh()

    def _cancel(self) -> None:
        if self.norm is not None:
            self.norm.cancel()

    def _on_norm_progress(self, path: str, f: float) -> None:
        r = self._find(path)
        if r >= 0:
            self.items[r].progress = f
            self._row(r)
        self.bar.setValue(int(1000 * (self._jobs_done + f) / max(1, self._jobs_total)))

    def _on_norm_done(self, path: str, out: str, plans, after) -> None:
        r = self._find(path)
        self._jobs_done += 1
        if r >= 0:
            it = self.items[r]
            it.state, it.out, it.after = "fait", Path(out), after
            it.plan = plans[0] if plans else it.plan
            self._row(r)
        self.open_btn.setEnabled(True)
        self._summary()

    def _on_norm_failed(self, path: str, msg: str) -> None:
        self._jobs_done += 1
        r = self._find(path)
        if r >= 0:
            self.items[r].state, self.items[r].error = "erreur", msg
            self._row(r)

    def _on_all_done(self, cancelled: bool) -> None:
        for it in self.items:
            if it.state == "travail":
                it.state = "prêt"
        self.norm = None
        self._refresh()
        if cancelled:
            self.bar_lbl.setText("Normalisation annulée.")

    def _open_out(self) -> None:
        outs = [it.out for it in self.items if it.out is not None and it.state == "fait"]
        if outs:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(outs[-1].parent)))

    # --- rendu ----------------------------------------------------------------------------------
    def _refresh(self) -> None:
        has = bool(self.items)
        self.drop.set_compact(has)
        self.table.setVisible(has)
        layout = self.layout()
        layout.setStretchFactor(self.drop, 0 if has else 1)
        self.table.setRowCount(len(self.items))
        for r in range(len(self.items)):
            self._row(r)
        self._summary()
        self._update_buttons()

    def _cell(self, r: int, c: int, text: str, color: str | None = None, tip: str = "",
              align=Qt.AlignVCenter | Qt.AlignRight) -> None:
        w = self.table.item(r, c)
        if w is None:
            w = QTableWidgetItem()
            self.table.setItem(r, c, w)
        w.setText(text)
        w.setTextAlignment(align)
        w.setForeground(QColor(color or "#F2F0EB"))
        w.setToolTip(tip)

    def _row(self, r: int) -> None:
        it = self.items[r]
        tgt = self.target()
        name = Path(it.path).name
        info = it.info
        tr = it.main_kept() if info is not None else None
        tip = it.path
        if info is not None and len(info.tracks) > 1:
            tip += "\n" + "\n".join(f"piste {t.index + 1} : {fmt_db(t.integrated, 'LUFS')}, crête "
                                    f"{fmt_db(t.true_peak, 'dBTP')}, {t.layout}" for t in info.tracks)
            tip += "\nchaque piste est corrigée séparément"
        self._cell(r, C_FILE, name + (f"   ({len(info.tracks)} pistes son)" if info and len(info.tracks) > 1 else ""),
                   tip=tip, align=Qt.AlignVCenter | Qt.AlignLeft)
        self._cell(r, C_DUR, fmt_dur(info.duration) if info else "—", MUTED)
        if tr is not None and not tr.silent:
            d = tr.integrated - tgt.lufs
            col = GREEN if abs(d) <= 1.0 else (YELLOW if abs(d) <= 3.0 else RED)
            self._cell(r, C_LVL, fmt_db(tr.integrated, "LUFS"), col,
                       f"{fmt_db(d, 'LU', True)} par rapport à la cible")
            self._cell(r, C_PEAK, fmt_db(tr.true_peak, "dBTP"),
                       RED if tr.true_peak > -0.05 else None, "crête vraie (entre échantillons comprise)")
            self._cell(r, C_LRA, fmt_db(tr.lra, "LU"), MUTED,
                       "plage de dynamique (LRA) : écart entre passages calmes et forts")
        else:
            for c in (C_LVL, C_PEAK, C_LRA):
                self._cell(r, c, "—", MUTED)
        pl = it.plan
        if pl is not None and tr is not None and not tr.silent:
            gtxt = fmt_db(pl.gain_db, "dB", True)
            if pl.limited_db >= 0.05:
                gtxt += f" · lim. −{pl.limited_db:.1f}".replace(".", ",")
            self._cell(r, C_GAIN, gtxt, None, pl.note or "gain appliqué à tout le fichier")
            if it.after is not None and it.after.main is not None:
                a = it.after.main
                ok = abs(a.integrated - tgt.lufs) <= 0.5 and a.true_peak <= tgt.peak + 0.05
                self._cell(r, C_AFTER, fmt_db(a.integrated, "LUFS") + ("  ✓" if ok else ""), GREEN if ok else YELLOW,
                           f"mesuré sur le fichier écrit : {fmt_db(a.integrated, 'LUFS')}, crête "
                           f"{fmt_db(a.true_peak, 'dBTP')}")
            else:
                short = pl.expected_lufs < tgt.lufs - 0.25
                self._cell(r, C_AFTER, fmt_db(pl.expected_lufs, "LUFS"), YELLOW if short else MUTED,
                           pl.note or "niveau attendu après correction")
        else:
            self._cell(r, C_GAIN, "—", MUTED)
            self._cell(r, C_AFTER, "—", MUTED)
        st, col = self._state_text(it)
        self._cell(r, C_STATE, st, col, it.error or (it.plan.note if it.plan else ""),
                   Qt.AlignVCenter | Qt.AlignLeft)
        self.table.viewport().update(self.table.visualRect(self.table.model().index(r, C_BAR)))

    def _state_text(self, it: Item) -> tuple[str, str]:
        if it.state == "mesure":
            return f"Mesure… {int(it.progress * 100)} %", MUTED
        if it.state == "travail":
            return f"Normalisation… {int(it.progress * 100)} %", ORANGE
        if it.state == "fait":
            return f"✓ Écrit : {it.out.name if it.out else ''}", GREEN
        if it.state == "erreur":
            return f"Erreur : {it.error}", RED
        if it.state == "vide":
            return it.error.capitalize(), MUTED
        if it.state == "prêt" and it.plan is None:
            return "Calcul…", MUTED
        if it.state == "prêt":
            pl = it.plan
            if pl is not None and pl.expected_lufs < self.target().lufs - 0.25:
                return (f"Prêt · montera à {pl.expected_lufs:.1f} au lieu de {self.target().lufs:g} "
                        "(pics trop forts)").replace(".", ","), YELLOW
            if pl is not None and abs(pl.gain_db) < 0.05 and pl.limited_db < 0.05 and not it.segments:
                return "Déjà au niveau", GREEN
            return "Prêt", None
        return "En attente", MUTED

    def _summary(self) -> None:
        done = [it for it in self.items if it.info is not None and it.info.main is not None
                and not it.info.main.silent]
        if not self.items:
            self.summary.setText("")
            return
        measuring = sum(1 for it in self.items if it.state == "mesure")
        parts = [f"{len(self.items)} fichier{'s' if len(self.items) > 1 else ''}"]
        if measuring:
            parts.append(f"{measuring} en cours de mesure")
        if len(done) >= 2:
            lv = [it.main_kept().integrated for it in done]
            ex = [it.plan.expected_lufs for it in done if it.plan is not None]
            spread = max(lv) - min(lv)
            txt = f"écart de volume entre le plus fort et le plus faible : <b>{spread:.1f} LU</b>"
            if ex:
                after = [it.after.main.integrated if it.after is not None and it.after.main else e
                         for it, e in zip([d for d in done if d.plan is not None], ex)]
                txt += f" → après : <b style='color:{GREEN}'>{max(after) - min(after):.1f} LU</b>"
            parts.append(txt.replace(".", ","))
        short = [it for it in done if it.plan is not None and it.plan.expected_lufs < self.target().lufs - 0.25]
        if short:
            one = len(short) == 1
            parts.append(f"<span style='color:{YELLOW}'>{len(short)} fichier{'' if one else 's'} "
                         f"{'a' if one else 'ont'} des pics trop forts pour monter jusqu'à la cible sans "
                         "abîmer le son (survole la ligne pour le détail). Pour que tout soit au même "
                         "niveau : « Cible commune »</span>")
        self.summary.setText(" · ".join(parts))

    def _update_buttons(self) -> None:
        busy = self.norm is not None
        ready = sum(1 for it in self.items if it.state == "prêt" and self._todo(it))
        pending = any(it.state == "prêt" and it.plan is None for it in self.items)
        self.go_btn.setText(f"Normaliser {ready} fichier{'s' if ready > 1 else ''}" if ready else "Normaliser")
        self.go_btn.setEnabled(ready > 0 and not busy and not pending)
        self.rm_btn.setEnabled(not busy and bool(self.table.selectionModel().selectedRows()))
        self.clear_btn.setEnabled(not busy and bool(self.items))
        self.common_btn.setEnabled(not busy and sum(1 for it in self.items if it.info is not None) >= 1)
        for w in (self.preset, self.lufs, self.peak, self.limiter, self.out_beside, self.out_choose,
                  self.out_dir_btn):
            w.setEnabled(not busy)
        self.drop.setEnabled(not busy)
        self.cancel_btn.setVisible(busy)
        self.bar.setVisible(busy)
        if busy:
            self.bar_lbl.setText("Normalisation en cours…")
        elif not self.bar_lbl.text().startswith("Normalisation annulée"):
            n = sum(1 for it in self.items if it.state == "fait")
            self.bar_lbl.setText(f"{n} fichier{'s' if n > 1 else ''} écrit{'s' if n > 1 else ''}" if n else "")

    # --- glisser-déposer sur toute la page ---------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self.norm is None:
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.add_paths(paths)

    def shutdown(self) -> None:
        if self.norm is not None:
            self.norm.cancel()
            self.norm.wait(5000)
        for it in self.items:
            self.meter.drop(it.path)
        self.meter.stop()
        self.meter.wait(5000)
        self.planner.gen += 1
        self.planner.stop()
        self.planner.wait(5000)
