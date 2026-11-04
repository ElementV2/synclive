# Composants tiers

SyncLive s'appuie sur les composants suivants. Ils ne sont pas couverts par la licence de SyncLive
et restent soumis à leur propre licence.

| Composant | Usage | Licence |
|---|---|---|
| [MediaPipe](https://github.com/google-ai-edge/mediapipe) et le modèle `models/hand_landmarker.task` | localisation des mains | Apache-2.0 |
| [PyAV](https://github.com/PyAV-Org/PyAV) / [FFmpeg](https://ffmpeg.org) | décodage et encodage audio/vidéo | BSD-3-Clause (PyAV) ; FFmpeg LGPL/GPL selon la compilation |
| [x264](https://www.videolan.org/developers/x264.html) (inclus dans les binaires PyAV) | encodage H.264 des mires | GPL-2.0-or-later |
| [Qt for Python (PySide6)](https://www.qt.io/qt-for-python) | interface | LGPL-3.0 |
| [OpenCV](https://opencv.org) | traitement d'image, QR codes | Apache-2.0 |
| [NumPy](https://numpy.org), [SciPy](https://scipy.org) | calcul | BSD-3-Clause |
| [Pillow](https://python-pillow.org) | images | MIT-CMU |
| Police *Unbounded* (logo, vectorisée dans les SVG) | identité visuelle | SIL OFL 1.1 |

Les binaires FFmpeg fournis avec PyAV sont compilés avec x264 (GPL). Avant de **redistribuer
l'exécutable** produit par `build.bat`, vérifier les obligations de ces licences (mise à disposition
des sources, mentions, etc.).
