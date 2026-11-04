# SPDX-License-Identifier: GPL-3.0-or-later
"""Charte SyncLive (logo 05C « Peak ») et accès aux fichiers de marque."""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

INK = (22, 22, 26)          # #16161A
CREAM = (242, 240, 235)     # #F2F0EB
ORANGE = (255, 90, 31)      # #FF5A1F

INK_HEX, CREAM_HEX, ORANGE_HEX = "#16161A", "#F2F0EB", "#FF5A1F"


def asset(name: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "synclive" / "assets" / name
    return Path(__file__).resolve().parent / "assets" / name


@lru_cache(maxsize=8)
def _lockup_rgba():
    from PIL import Image
    return Image.open(asset("lockup-dark-bg.png")).convert("RGBA")


def lockup(height: int):
    """Logo horizontal (symbole + « synclive ») pour fond sombre, à la hauteur demandée."""
    from PIL import Image
    im = _lockup_rgba()
    w = max(1, round(im.width * height / im.height))
    return im.resize((w, max(1, height)), Image.LANCZOS)
