# SPDX-License-Identifier: GPL-3.0-or-later
"""Prépare une version distribuable dans release/ :

  SyncLive-windows-x64.zip   exécutable + licences + offre de source
  SHA256SUMS.txt

Nom sans numéro de version : le lien
  https://github.com/ElementV2/synclive/releases/latest/download/SyncLive-windows-x64.zip
télécharge toujours la dernière version (le numéro est porté par le tag de la release).
Le code source de la version est l'archive « Source code » que GitHub joint à chaque release
(git archive du tag) : c'est l'offre de source exigée par la GPL.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from synclive import __version__  # noqa: E402

DIST = ROOT / "dist" / "SyncLive"
OUT = ROOT / "release"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not (DIST / "SyncLive.exe").exists():
        print("dist/SyncLive/SyncLive.exe introuvable : lance d'abord build.bat", file=sys.stderr)
        return 1
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout
    if dirty.strip():
        print("Attention : des modifications ne sont pas commitées ; le code source publié (tag) ne "
              "correspondra pas à l'exe. Commite avant de publier.", file=sys.stderr)
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*"):          # fichiers des versions précédentes
        if old.is_file():
            old.unlink()

    exe_zip = OUT / "SyncLive-windows-x64.zip"
    with zipfile.ZipFile(exe_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(DIST.rglob("*")):
            if f.is_file():
                z.write(f, Path("SyncLive") / f.relative_to(DIST))

    sums = OUT / "SHA256SUMS.txt"
    sums.write_text(f"{sha256(exe_zip)}  {exe_zip.name}\n", encoding="utf-8")
    for p in (exe_zip, sums):
        print(f"{p.name:40s} {p.stat().st_size / 1e6:8.1f} Mo")
    print(f"\nSyncLive {__version__} prêt. Lien permanent : "
          "https://github.com/ElementV2/synclive/releases/latest/download/SyncLive-windows-x64.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
