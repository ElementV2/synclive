# SPDX-License-Identifier: GPL-3.0-or-later
"""Journal de SyncLive : ce qui s'est passé et les erreurs, dans un fichier que l'utilisateur
retrouve depuis l'app (bouton « Journal »). Fichier tournant (1 Mo × 5) dans
%LOCALAPPDATA%\\SyncLive\\journal ; en-tête à chaque démarrage (version, système, bibliothèques) ;
les erreurs des tâches de fond, les plantages imprévus (y compris dans les fils) et les
avertissements de Qt y sont écrits avec leur trace."""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("synclive")
_ready = False


def log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "SyncLive" / "journal"


def log_file() -> Path:
    return log_dir() / "synclive.log"


class _StreamToLog:
    """Sortie console inexistante (exe fenêtré) : ce qui y serait écrit va dans le journal."""

    def __init__(self, level: int):
        self.level = level
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                log.log(self.level, line.rstrip())
        return len(s)

    def flush(self) -> None:
        pass


def setup() -> None:
    """À appeler une fois au démarrage (sans effet ensuite)."""
    global _ready
    if _ready:
        return
    _ready = True
    log.setLevel(logging.INFO)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(log_file(), maxBytes=1_000_000, backupCount=4, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(threadName)s  %(message)s",
                                         "%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
    except OSError:
        log.addHandler(logging.NullHandler())
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        con = logging.StreamHandler(sys.stderr)          # en développement : aussi dans la console
        con.setLevel(logging.WARNING)
        con.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(con)
    if sys.stdout is None:
        sys.stdout = _StreamToLog(logging.INFO)
    if sys.stderr is None:
        sys.stderr = _StreamToLog(logging.ERROR)

    def hook(et, ev, tb):
        log.critical("Erreur imprévue", exc_info=(et, ev, tb))
        if _prev_hook is not None and _prev_hook is not hook:
            _prev_hook(et, ev, tb)
    _prev_hook = sys.excepthook
    sys.excepthook = hook

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(f"Erreur imprévue dans le fil {args.thread.name if args.thread else '?'}",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_hook
    _header()


def _header() -> None:
    try:
        import synclive
        bits = [f"SyncLive {synclive.__version__}", f"Windows {platform.version()}" if sys.platform == "win32"
                else platform.platform(), f"Python {platform.python_version()}"]
        try:
            import PySide6
            bits.append(f"PySide6 {PySide6.__version__}")
        except Exception:
            pass
        try:
            import av
            bits.append(f"PyAV {av.__version__} (FFmpeg {av.library_versions.get('libavcodec')})")
        except Exception:
            pass
        bits.append("exe" if getattr(sys, "frozen", False) else "développement")
        log.info("──── démarrage : " + " · ".join(bits))
    except Exception:
        pass


def install_qt_handler() -> None:
    """Avertissements et erreurs de Qt dans le journal (les simples infos sont ignorées)."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        def handler(kind, _ctx, msg):
            if kind in (QtMsgType.QtWarningMsg,):
                if "propagateSizeHints" in msg:          # bruit connu de Qt sous Windows
                    return
                log.warning(f"Qt : {msg}")
            elif kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                log.error(f"Qt : {msg}")
        qInstallMessageHandler(handler)
    except Exception:
        pass


def error(context: str) -> None:
    """Erreur attrapée (dans un `except`) : écrite avec sa trace."""
    log.error(context, exc_info=True)


def tail(max_lines: int = 600) -> str:
    """Dernières lignes du journal (fichier courant)."""
    try:
        for h in log.handlers:
            h.flush()
        with open(log_file(), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return "(journal vide)"
