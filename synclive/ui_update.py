# SPDX-License-Identifier: GPL-3.0-or-later
"""Interface des mises à jour : vérification au démarrage, bandeau, téléchargement, installation."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton, QWidget,
)

from . import __version__, updater


class CheckThread(QThread):
    result = Signal(object)          # Release | None

    def run(self) -> None:
        self.result.emit(updater.check_for_update())


class DownloadThread(QThread):
    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, rel: updater.Release):
        super().__init__()
        self.rel, self._cancel = rel, False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            work = Path(tempfile.gettempdir()) / "synclive-download"
            z = updater.download(self.rel, work, lambda d, t: self.progress.emit(d, t), lambda: self._cancel)
            updater.verify(self.rel, z)
            self.done.emit(str(z))
        except InterruptedError:
            self.failed.emit("")
        except Exception as e:
            self.failed.emit(str(e))


class UpdateBanner(QFrame):
    """Bandeau discret affiché en haut de la fenêtre quand une nouvelle version existe."""

    def __init__(self, window: QWidget):
        super().__init__()
        self.window = window
        self.rel: updater.Release | None = None
        self.setVisible(False)
        self.setStyleSheet("UpdateBanner { background: #24170F; border-bottom: 1px solid #5A2A12; }"
                           "QLabel { background: transparent; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 12, 8)
        self.label = QLabel()
        lay.addWidget(self.label, 1)
        self.go = QPushButton("Mettre à jour")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self.update_now)
        later = QPushButton("Plus tard")
        later.clicked.connect(lambda: self.setVisible(False))
        skip = QPushButton("Ignorer cette version")
        skip.clicked.connect(self._skip)
        for b in (self.go, later, skip):
            lay.addWidget(b)
        self._thread: QThread | None = None
        self._dl: DownloadThread | None = None

    # --- vérification ------------------------------------------------------------
    def check(self, manual: bool = False) -> None:
        if self._thread and self._thread.isRunning():
            return
        self._manual = manual
        self._thread = CheckThread()
        self._thread.result.connect(self._on_result)
        self._thread.start()

    def _on_result(self, rel) -> None:
        if rel is None:
            if self._manual:
                QMessageBox.information(self.window, "Mises à jour",
                                        f"SyncLive {__version__} est à jour (ou GitHub est injoignable).")
            return
        if not self._manual and QSettings("SyncLive", "SyncLive").value("skip_version", "") == rel.version:
            return
        self.rel = rel
        how = "" if updater.can_self_update() else " — téléchargement depuis la page de la release"
        self.label.setText(f"<b>SyncLive {rel.version}</b> est disponible (tu as la {__version__}){how}.")
        self.go.setText("Mettre à jour" if updater.can_self_update() else "Voir la release")
        self.setVisible(True)

    def _skip(self) -> None:
        if self.rel:
            QSettings("SyncLive", "SyncLive").setValue("skip_version", self.rel.version)
        self.setVisible(False)

    # --- installation ------------------------------------------------------------
    def update_now(self) -> None:
        if self.rel is None:
            return
        if not updater.can_self_update():
            QDesktopServices.openUrl(QUrl(self.rel.url))
            return
        self.dialog = QProgressDialog("Téléchargement de la mise à jour…", "Annuler", 0, 1000, self.window)
        self.dialog.setWindowTitle(f"SyncLive {self.rel.version}")
        self.dialog.setWindowModality(Qt.WindowModal)
        self.dialog.setMinimumDuration(0)
        self.dialog.setValue(0)
        self._dl = DownloadThread(self.rel)
        self._dl.progress.connect(self._progress)
        self._dl.done.connect(self._install)
        self._dl.failed.connect(self._failed)
        self.dialog.canceled.connect(self._dl.cancel)
        self._dl.start()

    def _progress(self, done: int, total: int) -> None:
        if total:
            self.dialog.setValue(int(done / total * 1000))
            self.dialog.setLabelText(f"Téléchargement de la mise à jour… {done / 1e6:.0f} / {total / 1e6:.0f} Mo")

    def _failed(self, msg: str) -> None:
        self.dialog.close()
        if msg:
            QMessageBox.warning(self.window, "Mise à jour", f"La mise à jour a échoué : {msg}")

    def _install(self, zip_path: str) -> None:
        self.dialog.setLabelText("Installation… SyncLive va redémarrer.")
        try:
            updater.install_and_restart(Path(zip_path))
        except Exception as e:
            self._failed(str(e))
            return
        QApplication.quit()
