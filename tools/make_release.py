# SPDX-License-Identifier: GPL-3.0-or-later
"""Prépare une version distribuable dans release/ :

  SyncLive-<version>-windows-x64.zip   exécutable + licences + offre de source
  SyncLive-<version>-source.zip        code source complet de cette version (git archive)
  SHA256SUMS.txt

La GPL impose de fournir le code source avec l'exécutable : publier les deux zips ensemble.
"""
from __future__ import annotations

import hashlib
import shutil
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
        print("Attention : des modifications ne sont pas commitées ; l'archive source correspond au dernier "
              "commit. Commite avant de publier.", file=sys.stderr)
    OUT.mkdir(exist_ok=True)
    base = f"SyncLive-{__version__}"

    exe_zip = OUT / f"{base}-windows-x64.zip"
    if exe_zip.exists():
        exe_zip.unlink()
    with zipfile.ZipFile(exe_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(DIST.rglob("*")):
            if f.is_file():
                z.write(f, Path("SyncLive") / f.relative_to(DIST))

    src_zip = OUT / f"{base}-source.zip"
    subprocess.run(["git", "archive", "--format=zip", f"--prefix={base}/", "-o", str(src_zip), "HEAD"],
                   cwd=ROOT, check=True)

    sums = OUT / "SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in (exe_zip, src_zip)), encoding="utf-8")
    for p in (exe_zip, src_zip, sums):
        print(f"{p.name:40s} {p.stat().st_size / 1e6:8.1f} Mo")
    print(f"\nÀ publier ensemble (GPL) : {exe_zip.name} + {src_zip.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
