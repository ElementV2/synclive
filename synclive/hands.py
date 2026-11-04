# SPDX-License-Identifier: GPL-3.0-or-later
"""Localisation des mains (MediaPipe) pour cibler la zone du clap. Optionnel."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np


def model_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "models" / "hand_landmarker.task"


class HandLocator:
    def __init__(self) -> None:
        self._lm = None
        self.error: str | None = None
        p = model_path()
        if not p.exists():
            self.error = f"modèle introuvable : {p}"
            return
        try:
            os.environ.setdefault("GLOG_minloglevel", "2")
            from mediapipe.tasks.python import BaseOptions, vision
            opts = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(p)),
                num_hands=2,
                running_mode=vision.RunningMode.IMAGE,
                min_hand_detection_confidence=0.35,
                min_hand_presence_confidence=0.35,
            )
            self._lm = vision.HandLandmarker.create_from_options(opts)
        except Exception as e:  # pragma: no cover - dépend de l'install
            self.error = str(e)
            self._lm = None

    @property
    def available(self) -> bool:
        return self._lm is not None

    def boxes(self, rgb: np.ndarray) -> list[tuple[float, float, float, float]]:
        """Boîtes (x0, y0, x1, y1) normalisées 0-1 de chaque main détectée."""
        if self._lm is None:
            return []
        import mediapipe as mp
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        res = self._lm.detect(img)
        out = []
        for hand in res.hand_landmarks or []:
            xs = [p.x for p in hand]
            ys = [p.y for p in hand]
            out.append((min(xs), min(ys), max(xs), max(ys)))
        return out

    def close(self) -> None:
        if self._lm is not None:
            try:
                self._lm.close()
            except Exception:
                pass
            self._lm = None
