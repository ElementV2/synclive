# SPDX-License-Identifier: GPL-3.0-or-later
"""Mode dev : lance l'interface depuis le code source et la relance automatiquement à chaque
modification d'un fichier de synclive/ (pas besoin de recompiler l'exe).

    python tools/dev.py                 interface
    python tools/dev.py video.mp4       interface + analyse de la vidéo à chaque relance
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH = [ROOT / "synclive", ROOT / "main.py"]


def snapshot() -> dict[Path, float]:
    files = []
    for w in WATCH:
        files += [w] if w.is_file() else [p for p in w.rglob("*") if p.suffix in (".py", ".svg", ".png")]
    return {p: p.stat().st_mtime for p in files if p.exists()}


def start() -> subprocess.Popen:
    env = dict(os.environ, GLOG_minloglevel="2", PYTHONIOENCODING="utf-8")
    return subprocess.Popen([sys.executable, str(ROOT / "main.py"), *sys.argv[1:]], cwd=ROOT, env=env)


def main() -> int:
    print("SyncLive — mode dev : relance automatique à chaque modification (Ctrl+C pour quitter)")
    proc, snap = start(), snapshot()
    try:
        while True:
            time.sleep(0.6)
            cur = snapshot()
            if cur != snap:
                changed = [p.name for p in cur if cur.get(p) != snap.get(p)]
                print(f"modifié : {', '.join(changed[:5])} -> relance")
                snap = cur
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc = start()
            elif proc.poll() is not None:
                # fenêtre fermée par l'utilisateur : on attend la prochaine modification pour relancer
                print("fenêtre fermée : relance à la prochaine modification (Ctrl+C pour quitter)")
                while snapshot() == snap:
                    time.sleep(0.6)
                snap = snapshot()
                proc = start()
    except KeyboardInterrupt:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
