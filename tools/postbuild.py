# SPDX-License-Identifier: GPL-3.0-or-later
"""Étapes après PyInstaller :

1. Runtime Visual C++ (msvcp140*.dll, vcruntime140*.dll, concrt140.dll) copié à la racine de
   _internal : sans ça, OpenCV / MediaPipe / SciPy ne démarrent pas sur un PC où le
   redistribuable Visual C++ n'est pas installé (PyInstaller ne les place que dans PySide6/).
2. Licences des composants (tools/collect_licenses.py).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTERNAL = ROOT / "dist" / "SyncLive" / "_internal"
RUNTIME = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
           "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"]


def main() -> int:
    if not INTERNAL.exists():
        print(f"{INTERNAL} introuvable : lance d'abord PyInstaller", file=sys.stderr)
        return 1
    present = {p.name.lower(): p for p in INTERNAL.rglob("*.dll")}
    copied = []
    for name in RUNTIME:
        if (INTERNAL / name).exists() or any(p.parent == INTERNAL and p.name.lower() == name
                                             for p in INTERNAL.iterdir()):
            continue
        src = present.get(name)
        if src is None:
            sysdir = Path(r"C:\Windows\System32") / name
            src = sysdir if sysdir.exists() else None
        if src is not None:
            shutil.copy2(src, INTERNAL / src.name)
            copied.append(src.name)
    print("Runtime VC++ copié :", ", ".join(copied) if copied else "déjà présent")
    sys.path.insert(0, str(ROOT / "tools"))
    import collect_licenses
    return collect_licenses.main()


if __name__ == "__main__":
    raise SystemExit(main())
