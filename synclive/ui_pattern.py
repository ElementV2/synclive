# SPDX-License-Identifier: GPL-3.0-or-later
"""Onglet « Générer des mires » : boucles de synchro SyncLive dans tous les formats."""
from __future__ import annotations

import time
import traceback
from pathlib import Path

import cv2
from PIL import Image
from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from . import pattern, server


def pil_to_pixmap(im) -> QPixmap:
    im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    return QPixmap.fromImage(QImage(data, im.width, im.height, 3 * im.width, QImage.Format_RGB888).copy())


def titled(title: str, w: QWidget) -> QWidget:
    box = QWidget()
    lay = QVBoxLayout(box)
    lab = QLabel(title)
    lab.setStyleSheet("color:#8A8780;")
    lay.addWidget(lab)
    lay.addWidget(w)
    return box


class PatternWorker(QThread):
    progress = Signal(str, float)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, specs: list[pattern.PatternSpec], out_dir: Path):
        super().__init__()
        self.specs, self.out_dir, self._cancel = specs, out_dir, False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        files = []
        try:
            n = len(self.specs)
            for i, sp in enumerate(self.specs):
                label = f"{i + 1}/{n} · {sp.filename()}"
                files.append(pattern.render(
                    sp, self.out_dir / sp.filename(),
                    lambda f, i=i, label=label: self.progress.emit(label, (i + f) / n),
                    lambda: self._cancel))
            self.done.emit(files)
        except InterruptedError:
            self.failed.emit("Génération annulée.")
        except Exception as e:
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__} : {e}")


class PatternPage(QWidget):
    def __init__(self):
        super().__init__()
        self.worker: PatternWorker | None = None
        self.out_dir = Path.home() / "Videos" / "SyncLive mires"
        lay = QHBoxLayout(self)
        left = QVBoxLayout()

        g_res = QGroupBox("Résolutions")
        gl = QGridLayout(g_res)
        self.res_checks: dict[str, QCheckBox] = {}
        for i, name in enumerate(pattern.RESOLUTIONS):
            cb = QCheckBox(name)
            cb.setChecked(name == "Full HD 1920×1080")
            self.res_checks[name] = cb
            gl.addWidget(cb, i // 2, i % 2)
        left.addWidget(g_res)

        g_fps = QGroupBox("Fréquences d'images (i/s)")
        gf = QGridLayout(g_fps)
        self.fps_checks: dict[str, QCheckBox] = {}
        for i, name in enumerate(pattern.FRAME_RATES):
            cb = QCheckBox(name)
            cb.setChecked(name in ("25", "29.97", "50", "59.94", "60"))
            self.fps_checks[name] = cb
            gf.addWidget(cb, i // 4, i % 4)
        quick = QHBoxLayout()
        for lab, sel in (("Tout", list(pattern.FRAME_RATES)), ("PAL", ["25", "50"]),
                         ("NTSC", ["23.976", "29.97", "59.94", "119.88"]), ("Entières", ["24", "30", "60", "120"])):
            b = QPushButton(lab)
            b.setObjectName("flat")
            b.clicked.connect(lambda _=False, sel=sel: [c.setChecked(n in sel) for n, c in self.fps_checks.items()])
            quick.addWidget(b)
        gf.addLayout(quick, 3, 0, 1, 4)
        left.addWidget(g_fps)

        g_opt = QGroupBox("Options")
        go = QGridLayout(g_opt)
        self.n_sources = QSpinBox()
        self.n_sources.setRange(1, pattern.MAX_SOURCES)
        self.n_sources.setToolTip("Une boucle par source (PC, console…), chacune avec son numéro, son nom\n"
                                  "et son QR code : SyncLive lit le QR pour savoir quelle machine est à l'image.")
        self.first_source = QSpinBox()
        self.first_source.setRange(1, pattern.MAX_SOURCES)
        self.periods = QSpinBox()
        self.periods.setRange(8, 600)
        self.periods.setSingleStep(8)
        self.periods.setValue(16)
        self.periods.setSuffix(" périodes (≈ s, multiple de 8)")
        self.periods.setToolTip("Les bips sont numérotés sur un cycle de 8 (note + QR) : la boucle dure "
                                "un multiple de 8 périodes pour rester sans couture.")
        self.container = QComboBox()
        self.container.addItem("MP4 (H.264 + AAC) — universel", "mp4")
        self.container.addItem("MOV (H.264 + PCM 24 bits) — régie / montage", "mov")
        self.out_label = QLabel(str(self.out_dir))
        self.out_label.setWordWrap(True)
        choose = QPushButton("Dossier…")
        choose.setObjectName("flat")
        choose.clicked.connect(self._choose)
        rows = [("Nombre de sources", self.n_sources), ("N° de la 1re source", self.first_source),
                ("Durée de la boucle", self.periods), ("Format", self.container),
                ("Dossier de sortie", choose)]
        for i, (lab, w) in enumerate(rows):
            go.addWidget(QLabel(lab), i, 0)
            go.addWidget(w, i, 1)
        go.addWidget(self.out_label, len(rows), 0, 1, 2)
        left.addWidget(g_opt)

        self.count = QLabel("")
        self.count.setStyleSheet("color:#8A8780;")
        self.status = QLabel("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        for w in (self.count, self.status, self.bar):
            left.addWidget(w)
        btns = QHBoxLayout()
        self.go = QPushButton("Générer les mires")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self.generate)
        self.stop = QPushButton("Annuler")
        self.stop.setObjectName("flat")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(lambda: self.worker and self.worker.cancel())
        opn = QPushButton("Ouvrir le dossier")
        opn.setObjectName("flat")
        opn.clicked.connect(self._open)
        for b in (self.go, self.stop, opn):
            btns.addWidget(b)
        left.addLayout(btns)
        left.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(self._network_box())
        prev = QHBoxLayout()
        self.prev_normal, self.prev_flash = QLabel(), QLabel()
        for w in (self.prev_normal, self.prev_flash):
            w.setAlignment(Qt.AlignCenter)
        prev.addWidget(titled("Aperçu", self.prev_normal))
        prev.addWidget(titled("Image 0 : flash et bip", self.prev_flash))
        right.addLayout(prev)
        tip = QLabel("Chaque boucle porte un numéro, un nom et un QR code (ex. 07 · ATLAS) : à l'analyse, "
                     "SyncLive lit le QR pour savoir quelle machine est à l'image, même si toutes affichent "
                     "la même mire.\n"
                     "Les flashs et les bips sont numérotés de 1 à 8 (QR + note) : un décalage supérieur à une "
                     "période est mesuré sans se caler sur le mauvais flash, jusqu'à ± 4 s.\n"
                     "Lecture à l'œil : la position du curseur sur la règle au moment du bip donne le décalage "
                     "en images (+ = son en retard).")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8A8780;")
        right.addWidget(tip)
        right.addStretch(1)
        lay.addLayout(left, 2)
        lay.addLayout(right, 3)

        for cb in list(self.fps_checks.values()) + list(self.res_checks.values()):
            cb.toggled.connect(self._count)
        for cb in self.res_checks.values():
            cb.toggled.connect(self.update_preview)
        self.n_sources.valueChanged.connect(self._count)
        self.first_source.valueChanged.connect(self.update_preview)
        self.update_preview()
        self._count()

    # --- partage réseau ------------------------------------------------------------
    def _network_box(self) -> QGroupBox:
        g = QGroupBox("Partage réseau")
        lay = QHBoxLayout(g)
        self.qr_label = QLabel()
        self.qr_label.setFixedSize(170, 170)
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setStyleSheet("background:#1B1B20; border:1px solid #2A2A30; border-radius:6px;")
        lay.addWidget(self.qr_label)
        col = QVBoxLayout()
        row = QHBoxLayout()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(server.DEFAULT_PORT)
        self.port.setPrefix("port ")
        self.share_btn = QPushButton("Démarrer le partage")
        self.share_btn.setObjectName("primary")
        self.share_btn.clicked.connect(self._toggle_share)
        row.addWidget(self.share_btn)
        row.addWidget(self.port)
        col.addLayout(row)
        self.url_label = QLabel("Chaque machine ouvre un lien et reçoit son identifiant. Aucune installation.")
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.url_label)
        self.clients = QListWidget()
        self.clients.setMaximumHeight(110)
        col.addWidget(self.clients)
        lay.addLayout(col, 1)
        self.server: server.LoopServer | None = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_clients)
        return g

    def _toggle_share(self) -> None:
        if self.server and self.server.running:
            self.server.stop()
            self.server = None
            self.timer.stop()
            self.share_btn.setText("Démarrer le partage")
            self.url_label.setText("Arrêté.")
            self.qr_label.clear()
            self.clients.clear()
            self.port.setEnabled(True)
            return
        srv = server.LoopServer(port=self.port.value(), periods=self.periods.value())
        try:
            srv.start()
        except OSError as e:
            self.url_label.setText(f"Impossible d'ouvrir le port {self.port.value()} : {e}")
            return
        self.server = srv
        url = srv.url
        self.share_btn.setText("Arrêter le partage")
        self.port.setEnabled(False)
        self.url_label.setText(f"<span style='font-size:20px; font-weight:600'>{url}</span><br>"
                               f"<span style='color:#8A8780'>À ouvrir dans le navigateur de chaque machine du "
                               f"réseau. Au premier lancement, autorise SyncLive dans le pare-feu Windows "
                               f"(réseaux privés).</span>")
        m = pattern.qr_matrix(url)
        m = cv2.copyMakeBorder(m, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
        big = cv2.resize(m, (160, 160), interpolation=cv2.INTER_NEAREST)
        self.qr_label.setPixmap(pil_to_pixmap(Image.fromarray(big)))
        self.timer.start(2000)
        self._refresh_clients()

    def _refresh_clients(self) -> None:
        if not self.server:
            return
        now = time.time()
        items = []
        for c in self.server.registry.clients():
            online = now - c["seen"] < 15
            state = "en ligne" if online else "hors ligne"
            play = f"   lecture {c['playing']}" if c.get("playing") else ""
            items.append(f"{c['label']:<16}  {state:<10}  {c['ip']:<15}  {c['agent']}{play}")
        if [self.clients.item(i).text() for i in range(self.clients.count())] != items:
            self.clients.clear()
            self.clients.addItems(items or ["Aucune machine connectée pour l'instant."])

    def shutdown(self) -> None:
        if self.server:
            self.server.stop()

    def _choose(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Dossier de sortie des mires", str(self.out_dir))
        if d:
            self.out_dir = Path(d)
            self.out_label.setText(d)

    def _open(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.out_dir)))

    def specs(self) -> list[pattern.PatternSpec]:
        out = []
        for rname, cb in self.res_checks.items():
            if not cb.isChecked():
                continue
            w, h = pattern.RESOLUTIONS[rname]
            for fname, fc in self.fps_checks.items():
                if not fc.isChecked():
                    continue
                for k in range(self.n_sources.value()):
                    src = (self.first_source.value() - 1 + k) % pattern.MAX_SOURCES + 1
                    out.append(pattern.PatternSpec(w, h, pattern.FRAME_RATES[fname], source=src,
                                                   periods=self.periods.value(),
                                                   container=self.container.currentData()))
        return out

    def _count(self) -> None:
        n = len(self.specs())
        self.count.setText(f"{n} fichier(s) à générer")
        self.go.setEnabled(n > 0 and not (self.worker and self.worker.isRunning()))

    def update_preview(self) -> None:
        sel = [n for n, cb in self.res_checks.items() if cb.isChecked()] or ["Full HD 1920×1080"]
        w, h = pattern.RESOLUTIONS[sel[0]]
        sp = pattern.PatternSpec(w, h, pattern.FRAME_RATES["29.97"], source=self.first_source.value())
        for lab, i in ((self.prev_normal, 5), (self.prev_flash, 0)):
            im = Image.fromarray(pattern.preview(sp, i))
            im.thumbnail((430, 260))
            lab.setPixmap(pil_to_pixmap(im))

    def generate(self) -> None:
        specs = self.specs()
        if not specs:
            return
        self.go.setEnabled(False)
        self.stop.setEnabled(True)
        self.worker = PatternWorker(specs, self.out_dir)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _progress(self, label: str, f: float) -> None:
        self.status.setText(label)
        self.bar.setValue(int(f * 1000))

    def _done(self, files: list) -> None:
        self.stop.setEnabled(False)
        self._count()
        self.bar.setValue(1000)
        self.status.setText(f"{len(files)} mire(s) générée(s) dans {self.out_dir}")

    def _failed(self, msg: str) -> None:
        self.stop.setEnabled(False)
        self._count()
        self.status.setText(msg)
