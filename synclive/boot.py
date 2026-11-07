# SPDX-License-Identifier: GPL-3.0-or-later
"""Démarrage de l'interface : une petite fenêtre « SyncLive démarre… » s'affiche tout de suite,
AVANT de charger les grosses bibliothèques (FFmpeg, OpenCV, scipy, lecteur vidéo de Qt : plusieurs
centaines de Mo). Au premier lancement sur un PC, Windows et l'antivirus lisent et analysent chacune
d'elles : sans cette fenêtre, l'app tournait plusieurs secondes sans rien montrer. Les étapes du
démarrage et leur durée sont écrites dans le journal.

Ce module n'importe que la partie fenêtres de Qt (rapide) : rien d'autre avant la fenêtre."""
from __future__ import annotations

import sys
import threading
import time

T0 = time.perf_counter()


def _splash(app):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    from . import __version__, brand
    w = QWidget(None, Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    w.setStyleSheet("QWidget { background:#16161A; color:#E6E6E6; } QLabel#small { color:#8A8780; }")
    v = QVBoxLayout(w)
    v.setContentsMargins(36, 28, 36, 24)
    v.setSpacing(10)
    logo = QLabel()
    pm = QPixmap(str(brand.asset("lockup-dark-bg.png")))
    if not pm.isNull():
        logo.setPixmap(pm.scaledToWidth(300, Qt.SmoothTransformation))
    else:
        logo.setText("SyncLive")
    logo.setAlignment(Qt.AlignCenter)
    v.addWidget(logo)
    msg = QLabel(f"SyncLive {__version__} démarre…")
    msg.setAlignment(Qt.AlignCenter)
    msg.setStyleSheet("font-size:15px; font-weight:600;")
    v.addWidget(msg)
    small = QLabel("La première ouverture sur ce PC peut prendre un moment\n"
                   "(Windows vérifie les fichiers de l'application).")
    small.setObjectName("small")
    small.setAlignment(Qt.AlignCenter)
    v.addWidget(small)
    w.adjustSize()
    scr = app.primaryScreen()
    if scr is not None:
        g = scr.availableGeometry()
        w.move(g.center() - w.rect().center())
    w.show()
    w.raise_()
    return w


def run() -> int:
    from . import applog
    applog.setup()
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
    applog.install_qt_handler()
    app.setApplicationName("SyncLive")
    splash = _splash(app)
    for _ in range(3):                       # qu'elle soit vraiment dessinée avant le chargement
        app.processEvents()
    t_splash = time.perf_counter() - T0

    # chargement du reste du programme pendant que la petite fenêtre est affichée
    err: list = []
    done = threading.Event()

    def load():
        try:
            from . import ui  # noqa: F401  (FFmpeg, OpenCV, scipy, lecteur vidéo…)
        except BaseException as e:  # noqa: BLE001
            err.append(e)
        finally:
            done.set()
    threading.Thread(target=load, name="chargement", daemon=True).start()
    while not done.wait(0.03):
        app.processEvents()
    if err:
        splash.close()
        raise err[0]
    t_code = time.perf_counter() - T0

    from . import ui
    w = ui.start(app)
    splash.close()
    app.processEvents()
    t_win = time.perf_counter() - T0
    applog.log.info(f"Démarrage : petite fenêtre à {t_splash:.1f} s, programme chargé à {t_code:.1f} s, "
                    f"fenêtre affichée à {t_win:.1f} s")
    return app.exec()
