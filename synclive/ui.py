# SPDX-License-Identifier: GPL-3.0-or-later
"""Interface desktop SyncLive (PySide6)."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("GLOG_minloglevel", "2")

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__, analysis, brand, export
from .ui_threads import stop
from .ui_loudness import LoudnessPage
from .ui_montage import MontagePage
from .ui_pattern import PatternPage
from .ui_update import UpdateBanner
from .applog import error as log_error, log

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".m4v", ".ts", ".mts", ".m2ts", ".webm", ".flv"}

STYLE = """
QWidget { background: #16161A; color: #F2F0EB; font-family: "Segoe UI"; font-size: 13px; }
QTabWidget::pane { border: none; border-top: 1px solid #2A2A30; }
QTabBar::tab { background: transparent; color: #8A8780; padding: 10px 18px; border: none;
  border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #F2F0EB; border-bottom: 2px solid #FF5A1F; }
QTabBar::tab:hover { color: #D6D3CC; }
QGroupBox { border: 1px solid #2A2A30; border-radius: 6px; margin-top: 20px; padding: 14px 10px 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 2px; top: 2px; color: #8A8780; }
QPushButton { background: #1E1E23; border: 1px solid #303037; border-radius: 6px; padding: 7px 14px; color: #F2F0EB; }
QPushButton:hover { background: #24242A; border-color: #45454D; }
QPushButton:disabled { color: #5E5C58; border-color: #26262B; background: transparent; }
QPushButton#primary { background: #FF5A1F; color: #16161A; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #FF7240; }
QPushButton#primary:disabled { background: #2A2A30; color: #6E6B66; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #1B1B20; border: 1px solid #2E2E35; border-radius: 5px;
  padding: 5px 8px; selection-background-color: #45454D; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #FF5A1F; }
QComboBox QAbstractItemView { background: #1B1B20; border: 1px solid #2E2E35; selection-background-color: #2A2A30; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #45454D; border-radius: 3px; background: #1B1B20; }
QCheckBox::indicator:checked { background: #FF5A1F; border-color: #FF5A1F; }
QProgressBar { background: #24242A; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }
QProgressBar::chunk { background: #FF5A1F; border-radius: 2px; }
QTableWidget, QListWidget { background: #19191D; border: 1px solid #2A2A30; gridline-color: #222227; }
QHeaderView::section { background: #19191D; border: none; border-bottom: 1px solid #2A2A30; padding: 6px;
  color: #8A8780; }
QScrollArea { border: none; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #303037; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QFrame#card { background: #1B1B20; border: 1px solid #2A2A30; border-radius: 8px; }
QSplitter::handle { background: #16161A; }
"""


def pil_to_pixmap(im) -> QPixmap:
    im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    q = QImage(data, im.width, im.height, 3 * im.width, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(q)


def svg_pixmap(path, height: int) -> QPixmap:
    """Rendu vectoriel net : dessiné à la résolution réelle de l'écran, fond transparent."""
    from PySide6.QtGui import QGuiApplication, QPainter
    from PySide6.QtSvg import QSvgRenderer
    scr = QGuiApplication.primaryScreen()
    dpr = max(2.0, scr.devicePixelRatio() if scr else 1.0)
    r = QSvgRenderer(str(path))
    vb = r.viewBoxF()
    w = height * vb.width() / vb.height()
    pm = QPixmap(round(w * dpr), round(height * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    r.render(p)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


class Worker(QThread):
    progress = Signal(str, float)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str, settings: analysis.Settings):
        super().__init__()
        self.path, self.settings, self._cancel = path, settings, False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        log.info(f"Analyse : {self.path}")
        try:
            an = analysis.run(self.path, self.settings,
                              lambda s, f: self.progress.emit(s, f), lambda: self._cancel)
            self.done.emit(an)
        except InterruptedError:
            log.info("Analyse annulée")
            self.failed.emit("Analyse annulée.")
        except Exception as e:
            log_error(f"Analyse impossible : {self.path}")
            self.failed.emit(f"{type(e).__name__} : {e}")


class DropZone(QFrame):
    dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        self._set_style(False)
        lay = QVBoxLayout(self)
        self.label = QLabel("Déposer une vidéo de synchro")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("font-size: 17px; font-weight: 600; background: transparent;")
        self.hint = QLabel("Claps devant chaque caméra et/ou mires SyncLive · MP4, MOV, MKV, MXF…")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setWordWrap(True)
        self.label.setWordWrap(True)
        self.hint.setStyleSheet("color: #8A8780; background: transparent;")
        btn = QPushButton("Parcourir…")
        btn.setObjectName("flat")
        btn.clicked.connect(self._browse)
        lay.addStretch(1)
        lay.addWidget(self.label)
        lay.addWidget(self.hint)
        lay.addSpacing(8)
        lay.addWidget(btn, alignment=Qt.AlignCenter)
        lay.addStretch(1)

    def _set_style(self, hover: bool) -> None:
        c = "#8A8780" if hover else "#303037"
        bg = "#1B1B20" if hover else "#19191D"
        self.setStyleSheet(f"DropZone {{ border: 1px dashed {c}; border-radius: 8px; background: {bg}; }}")

    def _browse(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Choisir une vidéo", "",
                                           "Vidéos (" + " ".join("*" + e for e in sorted(VIDEO_EXT)) + ");;Tous (*)")
        if p:
            self.dropped.emit(p)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_style(True)

    def dragLeaveEvent(self, e):
        self._set_style(False)

    def dropEvent(self, e):
        self._set_style(False)
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p:
                self.dropped.emit(p)
                return


class ScaledImage(QLabel):
    """Image qui suit la largeur disponible en gardant ses proportions (cartes responsives)."""

    def __init__(self, pix: QPixmap):
        super().__init__()
        self._src = pix
        self.setMinimumWidth(240)
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setAlignment(Qt.AlignCenter)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return max(1, round(w * self._src.height() / max(1, self._src.width())))

    def sizeHint(self) -> QSize:
        return QSize(540, self.heightForWidth(540))

    def resizeEvent(self, e):
        w = self.width()
        if w > 0 and (self.pixmap() is None or self.pixmap().width() != w):
            self.setPixmap(self._src.scaledToWidth(w, Qt.SmoothTransformation))
        super().resizeEvent(e)


class CameraCard(QFrame):
    renamed = Signal(int, str)
    merge = Signal(int, int)
    adjust = Signal(int)
    remove = Signal(int)

    def __init__(self, cam: analysis.CameraResult, an: analysis.Analysis, others: list[tuple[int, str]],
                 plan: export.CorrectionPlan | None = None):
        super().__init__()
        self.setObjectName("card")
        self.cam = cam
        lay = QVBoxLayout(self)
        lay.addWidget(ScaledImage(pil_to_pixmap(export.annotate(cam, an, plan))))
        row = QHBoxLayout()
        row.addWidget(QLabel("Nom :"))
        self.name = QLineEdit(cam.name)
        self.name.setToolTip(export.display_name(cam))
        self.name.editingFinished.connect(lambda: self.renamed.emit(cam.id, self.name.text().strip() or cam.name))
        row.addWidget(self.name, 1)
        if others:
            # bouton + menu (et non une liste déroulante) : la molette de la souris ne doit jamais
            # pouvoir déclencher une fusion en faisant défiler la page
            btn = QPushButton("Fusionner avec…")
            menu = QMenu(btn)
            for oid, oname in others:
                act = menu.addAction(oname)
                act.triggered.connect(lambda _=False, d=oid: self.merge.emit(self.cam.id, d))
            btn.setMenu(menu)
            row.addWidget(btn)
        lay.addLayout(row)
        adj = QPushButton("Régler l'image du clap…" if not cam.manual else "Image du clap réglée à la main ✓ — modifier…")
        adj.setToolTip("Bande d'images autour du clap : clique l'image où les mains se touchent "
                       "si l'automatique s'est trompé")
        adj.clicked.connect(lambda: self.adjust.emit(self.cam.id))
        if cam.ambiguous and not cam.manual:
            adj.setObjectName("primary")      # pas de mesure sûre : c'est l'action à faire
        rm = QPushButton("Supprimer cette source")
        rm.setToolTip("Aucun clap n'est visible sur cette source : la retirer des résultats, du plan "
                      "de correction et de l'export (« Annuler la dernière modification » la remet)")
        rm.clicked.connect(lambda: self.remove.emit(self.cam.id))
        btns = QHBoxLayout()
        btns.addWidget(adj, 1)
        btns.addWidget(rm)
        lay.addLayout(btns)
        if not cam.claps:   # place gardée : toutes les cartes d'une ligne restent alignées
            sp = adj.sizePolicy()
            sp.setRetainSizeWhenHidden(True)
            adj.setSizePolicy(sp)
            adj.setVisible(False)
        lay.addStretch(1)


class NoWheelComboBox(QComboBox):
    """Liste déroulante qui ignore la molette : défiler la page ne modifie rien par accident."""

    def wheelEvent(self, e):
        e.ignore()


class LogDialog(QDialog):
    """Le journal : ce qui s'est passé et les erreurs, à copier ou à envoyer en cas de problème."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from . import applog
        self.setWindowTitle("Journal de SyncLive")
        self.resize(1000, 620)
        v = QVBoxLayout(self)
        where = QLabel(f"Fichier : {applog.log_file()}")
        where.setStyleSheet("color:#8A8780;")
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(where)
        hint = QLabel("En cas de problème, copie ce journal (ou envoie le fichier) : il dit ce qui s'est passé "
                      "et où.")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setStyleSheet("font-family: Consolas, 'Segoe UI'; font-size: 12px;")
        v.addWidget(self.text, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Actualiser")
        refresh.clicked.connect(self._load)
        copy = QPushButton("Copier tout")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        folder = QPushButton("Ouvrir le dossier du journal")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(applog.log_dir()))))
        close = QPushButton("Fermer")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        for b in (refresh, copy, folder):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)
        self._load()

    def _load(self) -> None:
        from . import applog
        self.text.setPlainText(applog.tail())
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SyncLive {__version__} — décalage son/image par caméra et par source")
        self.resize(1250, 860)
        self.an: analysis.Analysis | None = None
        self.worker: Worker | None = None
        self.last_export: Path | None = None

        self.setWindowIcon(QIcon(str(brand.asset("app-icon-256.png"))))
        self.main_tabs = QTabWidget()
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        self.update_banner = UpdateBanner(self)
        cv.addWidget(self.update_banner)
        cv.addWidget(self.main_tabs, 1)
        self.setCentralWidget(central)
        QTimer.singleShot(1500, self.update_banner.check)   # vérification discrète au démarrage
        corner = QWidget()
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(8, 4, 18, 4)
        cl.setSpacing(14)
        about = QPushButton("À propos")
        about.setStyleSheet("QPushButton { background: transparent; border: none; color: #8A8780; padding: 4px; }"
                            "QPushButton:hover { color: #F2F0EB; }")
        about.clicked.connect(self.show_about)
        journal = QPushButton("Journal")
        journal.setStyleSheet(about.styleSheet())
        journal.setToolTip("Ce qui s'est passé et les erreurs (à envoyer en cas de problème)")
        journal.clicked.connect(self.show_log)
        cl.addWidget(journal)
        logo = QLabel()
        logo.setPixmap(svg_pixmap(brand.asset("lockup-dark-bg.svg"), 24))
        logo.setStyleSheet("background: transparent;")
        cl.addWidget(about)
        cl.addWidget(logo)
        self.main_tabs.setCornerWidget(corner, Qt.TopRightCorner)
        root = QWidget()
        self.main_tabs.addTab(root, "Analyse")
        self.pattern_page = PatternPage()
        self.main_tabs.addTab(self.pattern_page, "Mires")
        self.loud_page = LoudnessPage()
        self.main_tabs.addTab(self.loud_page, "Normalisation")
        self.montage_page = MontagePage()
        self.main_tabs.addTab(self.montage_page, "Montage")
        v = QVBoxLayout(root)

        top = QHBoxLayout()
        self.drop = DropZone()
        self.drop.dropped.connect(self.start)
        top.addWidget(self.drop, 1)
        v.addLayout(top, 1)          # avant analyse : la zone de dépôt occupe tout l'écran
        self._root_layout = v

        pr = QHBoxLayout()
        self.status = QLabel("Prêt. Pour le tournage : devant chaque caméra, tape 3 à 5 fois dans tes mains, "
                             "bien fort et bien net, mains visibles à l'image, avec 1 seconde entre chaque.")
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.cancel_btn = QPushButton("Annuler")
        self.cancel_btn.setObjectName("flat")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(lambda: self.worker and self.worker.cancel())
        pr.addWidget(self.status, 2)
        pr.addWidget(self.bar, 3)
        pr.addWidget(self.cancel_btn)
        v.addLayout(pr)

        self.tabs = QTabWidget()
        # onglet caméras
        self.cards_host = QWidget()
        self.cards = QVBoxLayout(self.cards_host)
        self.cards_scroll = sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.cards_host)
        self.tabs.addTab(sc, "Sources")
        # onglet détail
        split = QSplitter(Qt.Vertical)
        self.shots_table = QTableWidget(0, 4)
        self.shots_table.setHorizontalHeaderLabels(["Plan", "Début", "Fin", "Caméra"])
        self.claps_table = QTableWidget(0, 8)
        self.claps_table.setHorizontalHeaderLabels(
            ["Utiliser", "Source", "Plan", "Son", "Événement image (choix)", "Décalage", "Images", "Méthode"])
        for t in (self.shots_table, self.claps_table):
            t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            t.verticalHeader().setVisible(False)
        self.claps_table.itemChanged.connect(self._clap_toggled)
        split.addWidget(self._titled("Plans détectés (change la caméra d'un plan si le regroupement s'est trompé)",
                                     self.shots_table))
        split.addWidget(self._titled("Claps / bips — décoche un événement douteux, ou choisis un autre candidat "
                                     "image si l'automatique s'est trompé", self.claps_table))
        self.tabs.addTab(split, "Détail")
        v.addWidget(self.tabs, 0)
        self.tabs.setVisible(False)

        bot = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setStyleSheet("color:#8A8780;")
        self.summary.setWordWrap(True)
        self.export_btn = QPushButton("Exporter les images et le rapport")
        self.export_btn.setObjectName("primary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.do_export)
        self.open_btn = QPushButton("Ouvrir le dossier")
        self.open_btn.setObjectName("flat")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: self.last_export and
                                      QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_export))))
        bot.addWidget(self.summary, 1)
        self.undo_btn = QPushButton("Annuler la dernière modification")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self.undo)
        self._undo: list = []
        bot.addWidget(self.undo_btn)
        bot.addWidget(self.open_btn)
        bot.addWidget(self.export_btn)
        v.addLayout(bot)

    # --- construction ------------------------------------------------------
    @staticmethod
    def _titled(title: str, w: QWidget) -> QWidget:
        box = QWidget()
        l = QVBoxLayout(box)
        lab = QLabel(title)
        lab.setWordWrap(True)
        lab.setStyleSheet("color:#8A8780;")
        l.addWidget(lab)
        l.addWidget(w)
        return box

    # --- analyse -------------------------------------------------------------
    def start(self, path: str) -> None:
        if self.worker and self.worker.isRunning():
            return
        if Path(path).suffix.lower() not in VIDEO_EXT:
            r = QMessageBox.question(self, "Format inconnu",
                                     f"« {Path(path).name} » n'a pas une extension vidéo connue. Essayer quand même ?")
            if r != QMessageBox.Yes:
                return
        st = analysis.Settings()   # aucun réglage : piste audio et sensibilité choisies automatiquement
        self.drop.label.setText(Path(path).name)
        self.export_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.worker = Worker(path, st)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _progress(self, stage: str, f: float) -> None:
        self.status.setText(stage + "…")
        self.bar.setValue(int(f * 1000))

    def _failed(self, msg: str) -> None:
        self.cancel_btn.setEnabled(False)
        self.bar.setValue(0)
        if msg == "Analyse annulée.":           # annuler n'est pas une erreur : pas d'alerte à fermer
            self.status.setText("Analyse annulée.")
            return
        self.status.setText("Échec.")
        QMessageBox.warning(self, "SyncLive", msg + "\n\nLe détail est dans le journal (bouton « Journal » "
                            "en haut à droite).")

    def _done(self, an: analysis.Analysis) -> None:
        log.info(f"Analyse terminée : {len(an.cameras())} source(s), {len(an.claps)} clap(s)")
        self.an = an
        self._undo.clear()
        self.undo_btn.setEnabled(False)
        # résultats : la zone de dépôt se réduit, les onglets prennent la place
        self.tabs.setVisible(True)
        self._root_layout.setStretch(0, 0)
        self._root_layout.setStretchFactor(self.tabs, 1)
        self.cancel_btn.setEnabled(False)
        self.export_btn.setEnabled(True)
        i = an.info
        self.status.setText("Analyse terminée.")
        self.summary.setText(f"{i.width}×{i.height} · {i.fps_label} i/s · {i.duration:.1f} s · "
                             f"{len(an.shots)} plan(s) · {len(an.claps)} clap(s)/bip(s) · piste audio {an.settings.audio_track + 1}")
        if not an.claps:
            QMessageBox.information(self, "SyncLive",
                                    "Aucun clap ni bip détecté, sur aucune piste audio de la vidéo. Vérifie que le son a bien été enregistré (claps nets, ou mire SyncLive avec le son activé).")
        self.refresh()

    # --- affichage & édition ----------------------------------------------------
    def refresh(self) -> None:
        an = self.an
        if an is None:
            return
        cams = an.cameras()
        plan = export.correction_plan(cams, an.info.fps_float)
        self.cards_host = QWidget()
        self.cards = QVBoxLayout(self.cards_host)
        self.cards.addWidget(self._plan_box(plan))
        self.card_grid = QGridLayout()
        self.card_grid.setSpacing(10)
        self.cards.addLayout(self.card_grid)
        self.card_widgets = []
        for cam in cams:
            others = [(c.id, c.name) for c in cams if c.id != cam.id]
            card = CameraCard(cam, an, others, plan)
            card.renamed.connect(self._rename)
            card.merge.connect(self._merge)
            card.adjust.connect(self._adjust)
            card.remove.connect(self._remove)
            self.card_widgets.append(card)
        self._grid_cols = 0
        self._layout_cards()
        self.cards.addStretch(1)
        self.cards_scroll.setWidget(self.cards_host)
        QTimer.singleShot(0, self._layout_cards)   # largeur réelle une fois la page affichée

        names = {c.id: export.display_name(c) for c in cams}
        self.shots_table.setRowCount(0)
        self.shots_table.setRowCount(len(an.shots))
        for r, s in enumerate(an.shots):
            for col, txt in enumerate([str(s.index + 1), f"{s.start:.2f} s", f"{s.end:.2f} s"]):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemIsEnabled)
                self.shots_table.setItem(r, col, it)
            cb = NoWheelComboBox()
            for cid, nm in names.items():
                cb.addItem(nm, cid)
            cb.addItem("Nouvelle source", -1)
            if s.camera in names:
                cb.setCurrentIndex(list(names).index(s.camera))
            else:   # source supprimée : choisir une source la remet dans les résultats
                cb.addItem(f"{an.camera_names.get(s.camera, 'source')} (supprimée)", s.camera)
                cb.setCurrentIndex(cb.count() - 1)
            cb.activated.connect(lambda i, si=s.index, c=cb: self._move_shot(si, c.itemData(i)))
            self.shots_table.setCellWidget(r, 3, cb)

        fps = an.info.fps_float
        cam_of_shot = {s.index: names.get(s.camera, f"{an.camera_names.get(s.camera, 'source')} (supprimée)")
                       for s in an.shots}
        self.claps_table.blockSignals(True)
        self.claps_table.setRowCount(0)
        self.claps_table.setRowCount(len(an.claps))
        for r, c in enumerate(an.claps):
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Unchecked if c.excluded else Qt.Checked)
            chk.setData(Qt.UserRole, c.id)
            self.claps_table.setItem(r, 0, chk)
            vals = [cam_of_shot[c.shot], str(c.shot + 1), f"{c.t_audio:.3f} s", "",
                    export.fmt_ms(c.offset), export.fmt_frames(c.offset, fps), c.method]
            for col, txt in enumerate(vals, start=1):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemIsEnabled)
                if c.selected is None:
                    it.setForeground(Qt.gray)
                self.claps_table.setItem(r, col, it)
            cb = NoWheelComboBox()
            cb.addItem("auto (cohérence)" if c.candidates else "aucun événement visible", None)
            for j, v in enumerate(c.candidates):
                mark = "   (retenu)" if c.selected is v else ""
                cb.addItem(f"{v.t:.3f} s   {export.fmt_ms(c.t_audio - v.t)}   force {v.strength:.2f}{mark}", j)
            if c.manual_t is not None:
                cb.addItem(f"{c.manual_t:.3f} s   {export.fmt_ms(c.t_audio - c.manual_t)}   à la main", "manual")
                cb.setCurrentIndex(cb.count() - 1)
            else:
                cb.setCurrentIndex(0 if c.forced is None else c.forced + 1)
            cb.activated.connect(lambda i, cid=c.id, box=cb: self._force(cid, box.itemData(i)))
            self.claps_table.setCellWidget(r, 4, cb)
        self.claps_table.blockSignals(False)

    CARD_MIN_W = 470      # px : en dessous, une colonne de moins

    def _layout_cards(self) -> None:
        """Autant de colonnes que la largeur le permet (1 à 4) ; les images suivent la largeur."""
        cards = getattr(self, "card_widgets", [])
        if not cards:
            return
        w = self.cards_scroll.viewport().width() - 24
        cols = int(max(1, min(4, w // self.CARD_MIN_W, len(cards))))
        if cols == self._grid_cols:
            return
        self._grid_cols = cols
        g = self.card_grid
        for c in cards:
            g.removeWidget(c)
        for k, c in enumerate(cards):
            g.addWidget(c, k // cols, k % cols, Qt.AlignTop)
        for i in range(4):
            g.setColumnStretch(i, 1 if i < cols else 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._layout_cards)

    def show_log(self) -> None:
        LogDialog(self).exec()

    def show_about(self) -> None:
        """Mentions légales (GPL-3.0, section 0 : « Appropriate Legal Notices »)."""
        import synclive
        lic_dir = Path(sys.executable).parent / "licenses" if getattr(sys, "frozen", False) else None
        lic_link = (f"<a href='{QUrl.fromLocalFile(str(lic_dir)).toString()}' style='color:#FF5A1F'>"
                    f"licences des composants tiers</a>" if lic_dir and lic_dir.exists()
                    else "licences des composants tiers : THIRD_PARTY_NOTICES.md")
        box = QMessageBox(self)
        box.setWindowTitle("À propos de SyncLive")
        box.setIconPixmap(svg_pixmap(brand.asset("lockup-dark-bg.svg"), 40))
        box.setTextFormat(Qt.RichText)
        box.setText(
            f"<p><b>SyncLive {synclive.__version__}</b><br>{synclive.__copyright__}</p>"
            "<p>Ce programme est un logiciel libre : vous pouvez le redistribuer et/ou le modifier selon les "
            "termes de la <b>GNU General Public License version 3</b> ou (à votre choix) toute version "
            "ultérieure, telle que publiée par la Free Software Foundation.</p>"
            "<p>Ce programme est distribué dans l'espoir qu'il sera utile, mais <b>SANS AUCUNE GARANTIE</b>, "
            "sans même la garantie implicite de qualité marchande ou d'adéquation à un usage particulier. "
            "Voir la licence pour plus de détails : "
            "<a href='https://www.gnu.org/licenses/gpl-3.0.html' style='color:#FF5A1F'>gnu.org/licenses/gpl-3.0</a>.</p>"
            f"<p>Code source : <a href='{synclive.__url__}' style='color:#FF5A1F'>{synclive.__url__}</a> "
            "(archive <i>Source code</i> jointe à chaque version).<br>"
            f"Composants tiers (FFmpeg, x264, Qt, MediaPipe, OpenCV…) : {lic_link}.</p>")
        box.setTextInteractionFlags(Qt.TextBrowserInteraction)
        box.setStandardButtons(QMessageBox.Ok)
        check = box.addButton("Rechercher les mises à jour", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is check:
            self.update_banner.check(manual=True)

    def _plan_box(self, plan: export.CorrectionPlan) -> QWidget:
        """Plan global : le son se décale une seule fois pour tout le mix, puis chaque image
        reçoit le retard complémentaire, en images entières. Toujours en ms ET en images."""
        fps = plan.fps
        box = QFrame()
        box.setObjectName("card")
        g = QGridLayout(box)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(28)
        g.setVerticalSpacing(6)
        title = QLabel("PLAN DE CORRECTION")
        title.setStyleSheet("color:#FF5A1F; font-weight:600; letter-spacing:1px; background:transparent;")
        g.addWidget(title, 0, 0, 1, 4)
        audio = QLabel(export.fmt_audio_delay(plan))
        audio.setStyleSheet("font-size:16px; font-weight:600; background:transparent;")
        audio.setWordWrap(True)
        g.addWidget(audio, 1, 0, 1, 4)
        heads = ["Source", "Constat (qui est en retard)", "Retard à mettre sur l'image", "Reste après correction"]
        for c, h in enumerate(heads):
            lab = QLabel(h)
            lab.setStyleSheet("color:#8A8780; background:transparent; padding-top:8px;")
            g.addWidget(lab, 2, c)
        for r, f in enumerate(plan.fixes, start=3):
            head, why = export.status(f.cam, fps)
            if f.video_delay is None:
                act, res = "rien de calculé", ""
            elif f.frames == 0:
                act, res = "aucun retard", export.both(f.residual, fps, signed=True)
            else:
                act = f"retarder de {f.frames} img · {round(f.frames / fps * 1000)} ms"
                res = export.both(f.residual, fps, signed=True)
            cells = [export.display_name(f.cam), f"{head}\n{why}", act, res]
            for c, v in enumerate(cells):
                lab = QLabel(v)
                lab.setWordWrap(c in (1, 3))
                st = "background:transparent;"
                if c == 1:
                    st += "color:#FF5A1F;" if head.startswith(("IMAGE", "SON")) else "color:#8A8780;"
                if c == 2:
                    st += "font-weight:600;"
                lab.setStyleSheet(st)
                g.addWidget(lab, r, c)
        note = QLabel("Le son est commun à toutes les sources : on ne peut que le retarder, une seule fois, de la "
                      "plus grande avance mesurée. Ensuite chaque source d'image reçoit le retard qui lui manque, "
                      f"en images entières à {self.an.info.fps_label} i/s (OBS : filtre « Délai de rendu », en ms). "
                      "Reste après correction : l'image ne se décale que par images entières (1 img = "
                      f"{round(1000 / fps)} ms), il reste donc ce petit décalage, invisible sous une demi-image.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8A8780; background:transparent; padding-top:8px;")
        g.addWidget(note, len(plan.fixes) + 3, 0, 1, 4)
        g.setColumnStretch(1, 1)
        return box

    def refresh_later(self) -> None:
        QTimer.singleShot(0, self.refresh)

    # --- annulation ------------------------------------------------------------
    def _snapshot(self) -> None:
        an = self.an
        self._undo.append(([s.camera for s in an.shots], dict(an.camera_names),
                           [(c.excluded, c.forced, c.manual_t) for c in an.claps], set(an.removed)))
        del self._undo[:-50]
        self.undo_btn.setEnabled(True)

    def undo(self) -> None:
        if not (self.an and self._undo):
            return
        cams, names, claps, removed = self._undo.pop()
        self.an.removed = removed
        for s, c in zip(self.an.shots, cams):
            s.camera = c
        self.an.camera_names = names
        changed = set()
        for c, (ex, fo, mt) in zip(self.an.claps, claps):
            if (c.excluded, c.forced, c.manual_t) != (ex, fo, mt):
                changed.add(c.shot)
            c.excluded, c.forced, c.manual_t = ex, fo, mt
        self._refresh_images(changed)
        self.undo_btn.setEnabled(bool(self._undo))
        self.refresh_later()

    def _rename(self, cid: int, name: str) -> None:
        if self.an and self.an.camera_names.get(cid) != name:
            self._snapshot()
            self.an.camera_names[cid] = name
            self.refresh_later()

    def _merge(self, src: int, dst: int) -> None:
        if self.an:
            self._snapshot()
            self.an.merge_cameras(src, dst)
            self.refresh_later()

    def _remove(self, cid: int) -> None:
        if self.an is not None:
            self._snapshot()
            self.an.remove_camera(cid)
            self.refresh_later()

    def _move_shot(self, shot: int, cid) -> None:
        if self.an is None or cid == self.an.shots[shot].camera:
            return
        self._snapshot()
        if cid != -1:
            self.an.removed.discard(cid)
        self.an.move_shot(shot, None if cid == -1 else cid)
        self.refresh_later()

    def _force(self, clap_id: int, cand) -> None:
        if self.an is not None and cand != "manual":
            self._snapshot()
            self.an.claps[clap_id].forced = cand
            self.an.claps[clap_id].manual_t = None
            self._refresh_images({self.an.claps[clap_id].shot})
            self.refresh_later()

    def _adjust(self, cid: int) -> None:
        """Bande d'images : l'utilisateur choisit l'image du contact pour chaque clap."""
        if self.an is None:
            return
        from .ui_clap import ClapFrameDialog
        cam = next((c for c in self.an.cameras() if c.id == cid), None)
        if cam is None or not cam.claps:
            return
        dlg = ClapFrameDialog(self.an, cam, self)
        # haute d'office, et large juste ce qu'il faut pour la vidéo : elle doit être lisible
        dlg.fit_to((self.screen() or QGuiApplication.primaryScreen()).availableGeometry())
        if dlg.exec() != ClapFrameDialog.Accepted:
            return
        ch = dlg.changes()
        if not ch:
            return
        self._snapshot()
        for clap_id, t in ch.items():
            c = self.an.claps[clap_id]
            c.manual_t = t
            if t is not None:
                c.excluded = False
                c.forced = None
        # l'aperçu montre l'image choisie (celle du clap réglé en dernier), pas celle de l'automatique
        last = dlg.last_changed if dlg.last_changed in ch else next(iter(ch))
        self.an.image_clap[self.an.claps[last].shot] = last
        self._refresh_images({self.an.claps[i].shot for i in ch})
        self.refresh_later()

    def _refresh_images(self, shots: set[int]) -> None:
        if not shots or self.an is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            analysis.refresh_images(self.an, sorted(shots))
        except Exception:
            pass        # l'aperçu garde l'ancienne image ; la mesure, elle, est à jour
        finally:
            QApplication.restoreOverrideCursor()

    def _clap_toggled(self, item: QTableWidgetItem) -> None:
        if self.an is None or item.column() != 0:
            return
        cid = item.data(Qt.UserRole)
        self._snapshot()
        self.an.claps[cid].excluded = item.checkState() != Qt.Checked
        self._refresh_images({self.an.claps[cid].shot})
        self.refresh_later()

    def do_export(self) -> None:
        if self.an is None:
            return
        default = str(Path(self.an.info.path).with_suffix("")) + "_synclive"
        d = QFileDialog.getExistingDirectory(self, "Dossier d'export", str(Path(default).parent))
        if not d:
            return
        out = Path(d) / Path(default).name if Path(d) == Path(default).parent else Path(d)
        try:
            files = export.export_all(self.an, out)
        except Exception as e:
            QMessageBox.warning(self, "Export", f"Échec de l'export : {e}")
            return
        self.last_export = out
        self.open_btn.setEnabled(True)
        self.status.setText(f"Exporté : {len(files)} fichiers dans {out}")

    def closeEvent(self, e):
        self.pattern_page.shutdown()
        self.loud_page.shutdown()
        self.montage_page.shutdown()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            stop(self.worker, 3000)
        super().closeEvent(e)


def install_french(app) -> None:
    """Boutons et fenêtres standard de Qt en français (Oui / Non, Annuler, ouvrir un fichier…)."""
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    tr = QTranslator(app)
    for d in (QLibraryInfo.path(QLibraryInfo.TranslationsPath),
              str(Path(getattr(sys, "_MEIPASS", "")) / "PySide6" / "translations")):
        if tr.load(QLocale(QLocale.French, QLocale.France), "qtbase", "_", d):
            app.installTranslator(tr)
            app._fr = tr                         # gardé en vie avec l'application
            return


def main() -> int:
    from . import applog
    applog.setup()
    app = QApplication(sys.argv)
    applog.install_qt_handler()
    app.setApplicationName("SyncLive")
    install_french(app)
    app.setStyleSheet(STYLE)
    app.setWindowIcon(QIcon(str(brand.asset("app-icon-256.png"))))
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        w.start(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
