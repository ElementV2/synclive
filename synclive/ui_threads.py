# SPDX-License-Identifier: GPL-3.0-or-later
"""Fils de travail Qt : ne jamais lâcher un QThread qui tourne encore.

Un fil qui annonce « fini » (signal émis depuis son `run`) n'a pas encore rendu la main : si l'on
oublie aussitôt sa référence, Python détruit l'objet Qt alors que le fil tourne (« QThread:
Destroyed while thread is still running », voire un plantage). `release` le garde en vie
jusqu'à sa vraie fin."""
from __future__ import annotations

from PySide6.QtCore import QThread

_alive: set[QThread] = set()


def release(th: QThread | None) -> None:
    """Oublier ce fil sans risque : il est gardé jusqu'à ce qu'il soit vraiment terminé."""
    if th is None or not th.isRunning():
        return
    _alive.add(th)
    th.finished.connect(lambda th=th: _alive.discard(th))
    if not th.isRunning():                       # fini entre-temps (avant la connexion)
        _alive.discard(th)


def stop(th: QThread | None, ms: int = 5000) -> None:
    """Attendre la fin d'un fil (déjà prié de s'arrêter) ; s'il tarde, il est gardé en vie."""
    if th is None:
        return
    if not th.wait(ms):
        release(th)
