# SPDX-License-Identifier: GPL-3.0-or-later
"""Génère build/version_info.txt (propriétés Windows de SyncLive.exe) à partir de synclive.__version__."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import synclive  # noqa: E402

v = tuple(int(x) for x in synclive.__version__.split(".")[:3]) + (0,)
TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040C04B0', [
      StringStruct('CompanyName', '{synclive.__author__}'),
      StringStruct('FileDescription', 'SyncLive - mesure du décalage son/image'),
      StringStruct('FileVersion', '{synclive.__version__}'),
      StringStruct('InternalName', 'SyncLive'),
      StringStruct('LegalCopyright', '{synclive.__copyright__} - GPL-3.0-or-later'),
      StringStruct('OriginalFilename', 'SyncLive.exe'),
      StringStruct('ProductName', 'SyncLive'),
      StringStruct('ProductVersion', '{synclive.__version__}')])]),
    VarFileInfo([VarStruct('Translation', [0x040C, 1200])])
  ]
)
"""
out = ROOT / "build" / "version_info.txt"
out.parent.mkdir(exist_ok=True)
out.write_text(TEMPLATE, encoding="utf-8")
print(out)
