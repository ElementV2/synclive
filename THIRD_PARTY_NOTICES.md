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
| Python 3.11 (embarqué dans l'exécutable) | interpréteur | PSF License |
| Runtime Microsoft Visual C++ (msvcp140, vcruntime140…) | exécution | redistribuable Microsoft Visual C++ |

Les binaires FFmpeg fournis avec PyAV sont compilés avec x264 et x265 (GPL) et opencore-amr
(Apache-2.0) : l'exécutable est donc distribué sous **GNU GPL version 3**, comme SyncLive
(voir `LICENSE`). `release.bat` livre avec l'exécutable le dossier `licenses/` (textes de licence
de chaque composant), `SOURCE.txt` (offre de code source) ; les sources de SyncLive sont l'archive « Source code » de chaque release.
Le détail des bibliothèques FFmpeg embarquées est dans `licenses/FFmpeg-components.md`.
