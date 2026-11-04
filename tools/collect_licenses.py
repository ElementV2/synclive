# SPDX-License-Identifier: GPL-3.0-or-later
"""Copie dans dist/SyncLive/licenses/ la licence de SyncLive et celles de tous les composants
embarqués dans l'exécutable (obligatoire pour redistribuer le binaire)."""
from __future__ import annotations

import shutil
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "SyncLive"

# paquets effectivement embarqués par PyInstaller
PACKAGES = ["av", "PySide6", "PySide6_Essentials", "shiboken6", "mediapipe", "opencv-python",
            "numpy", "scipy", "pillow", "pyinstaller"]

FFMPEG_COMPONENTS = """\
# FFmpeg et bibliothèques associées (embarquées via PyAV {av})

Les DLL du dossier `_internal/av.libs` proviennent des binaires officiels de PyAV {av},
compilés par le projet PyAV : https://github.com/PyAV-Org/pyav-ffmpeg

FFmpeg {ffmpeg} est compilé avec des composants GPL (x264, x265) et Apache-2.0 (opencore-amr) :
l'ensemble est distribué sous **GNU GPL version 3** (texte : `SyncLive/LICENSE`).

| Composant | Licence | Sources |
|---|---|---|
| FFmpeg {ffmpeg} | GPL-3.0-or-later (cette compilation) | https://ffmpeg.org/download.html |
| x264 | GPL-2.0-or-later | https://code.videolan.org/videolan/x264 |
| x265 | GPL-2.0-or-later | https://bitbucket.org/multicoreware/x265_git |
| LAME (libmp3lame) | LGPL-2.0-or-later | https://lame.sourceforge.io |
| opencore-amr | Apache-2.0 | https://sourceforge.net/projects/opencore-amr |
| Opus | BSD-3-Clause | https://opus-codec.org |
| libvpx | BSD-3-Clause | https://chromium.googlesource.com/webm/libvpx |
| dav1d | BSD-2-Clause | https://code.videolan.org/videolan/dav1d |
| SVT-AV1 | BSD-3-Clause-Clear | https://gitlab.com/AOMediaCodec/SVT-AV1 |
| libwebp | BSD-3-Clause | https://chromium.googlesource.com/webm/libwebp |
| libvpl | MIT | https://github.com/intel/libvpl |
| libiconv | LGPL-2.1-or-later | https://www.gnu.org/software/libiconv |
| zlib | Zlib | https://zlib.net |
| Runtime MinGW (libgcc, libstdc++, winpthreads) | GPL-3.0 + exception GCC Runtime / MIT | https://www.mingw-w64.org |

Les scripts et versions exactes de compilation sont publiés par le projet pyav-ffmpeg
(lien ci-dessus). Voir aussi `SOURCE.txt` pour l'offre de code source.
"""


def _copy_dist_licenses(name: str, dest: Path) -> int:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return 0
    n = 0
    for f in dist.files or []:
        s = str(f).replace("\\", "/")
        low = s.lower()
        if not any(k in low for k in ("licen", "copying", "notice", "authors")):
            continue
        if low.endswith((".py", ".pyc", ".dll", ".pyd", ".so")):
            continue
        src = Path(f.locate())
        if not src.is_file():
            continue
        out = dest / f"{dist.metadata['Name']}-{dist.version}" / Path(s).name
        if out.exists():
            out = out.with_name(f"{out.stem}-{n}{out.suffix}")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        n += 1
    return n


def main() -> int:
    if not DIST.exists():
        print(f"{DIST} introuvable : lance d'abord build.bat", file=sys.stderr)
        return 1
    lic = DIST / "licenses"
    if lic.exists():
        shutil.rmtree(lic)
    lic.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", DIST / "LICENSE.txt")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", DIST / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(ROOT / "SOURCE.txt", DIST / "SOURCE.txt")
    shutil.copy2(ROOT / "tools" / "licenses" / "LGPL-3.0.txt", lic / "LGPL-3.0.txt")
    py_lic = Path(sys.base_prefix) / "LICENSE.txt"
    if py_lic.exists():
        shutil.copy2(py_lic, lic / f"Python-{sys.version_info.major}.{sys.version_info.minor}-LICENSE.txt")
    total = 0
    for name in PACKAGES:
        total += _copy_dist_licenses(name, lic)
    import av
    (lic / "FFmpeg-components.md").write_text(
        FFMPEG_COMPONENTS.format(av=av.__version__, ffmpeg=getattr(av, "ffmpeg_version_info", "?")),
        encoding="utf-8")
    print(f"Licences copiées : {total} fichiers de paquets + textes GPL/LGPL/Python -> {lic}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
