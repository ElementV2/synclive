# SPDX-License-Identifier: GPL-3.0-or-later
"""Point d'entrée de SyncLive.exe.

    SyncLive.exe                       interface
    SyncLive.exe video.mp4             interface + analyse de la vidéo
    SyncLive.exe --cli video.mp4 -o D  analyse sans interface
"""
import os
import sys
import tempfile
from pathlib import Path


def _log_file():
    """Exe « fenêtré » : pas de console. Les messages vont dans un journal au lieu de faire planter
    le programme (écrire sur une sortie inexistante lève une erreur)."""
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "SyncLive"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return open(base / "synclive.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        return open(os.devnull, "w", encoding="utf-8")


# journal : erreurs et événements dans %LOCALAPPDATA%\SyncLive\journal (bouton « Journal » de l'app) ;
# sans console (exe fenêtré), ce qui serait écrit sur la console y va aussi
try:
    from synclive import applog

    applog.setup()
except Exception:
    if sys.stdout is None or sys.stderr is None:
        log = _log_file()
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log

if len(sys.argv) > 1 and sys.argv[1] == "--cli":
    from synclive.cli import main as cli_main

    raise SystemExit(cli_main(sys.argv[2:]))

from synclive.ui import main  # noqa: E402

raise SystemExit(main())
