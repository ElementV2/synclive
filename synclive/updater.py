# SPDX-License-Identifier: GPL-3.0-or-later
"""Mises à jour depuis les releases GitHub.

1. `latest_release()` interroge l'API publique GitHub (pas de compte nécessaire).
2. `download()` récupère le zip Windows de la release, `verify()` contrôle son SHA-256
   avec le fichier SHA256SUMS.txt publié dans la même release.
3. `install_and_restart()` extrait le zip et lance un petit script PowerShell qui attend la
   fermeture de SyncLive, copie les nouveaux fichiers par-dessus l'installation, puis relance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __url__, __version__

REPO = re.sub(r"^https://github.com/", "", __url__).strip("/")
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
UA = f"SyncLive/{__version__} (+{__url__})"


@dataclass
class Release:
    version: str
    tag: str
    url: str                       # page de la release
    notes: str
    assets: dict[str, tuple[str, int]] = field(default_factory=dict)   # nom -> (url, taille)

    @property
    def windows_zip(self) -> tuple[str, str, int] | None:
        # le zip numéroté d'abord ; la copie au nom fixe (lien permanent) est identique
        names = sorted((n for n in self.assets if n.endswith("-windows-x64.zip")),
                       key=lambda n: not re.search(r"\d+\.\d+", n))
        if not names:
            return None
        u, size = self.assets[names[0]]
        return names[0], u, size


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - min(3, len(nums)))


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def latest_release(timeout: float = 6.0) -> Release | None:
    """Dernière release publiée, ou None (hors ligne, limite d'API, pas de release…)."""
    req = urllib.request.Request(API_LATEST, headers={"Accept": "application/vnd.github+json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name") or ""
    return Release(
        version=tag.lstrip("vV"), tag=tag, url=data.get("html_url") or __url__ + "/releases",
        notes=data.get("body") or "",
        assets={a["name"]: (a["browser_download_url"], int(a.get("size") or 0)) for a in data.get("assets", [])},
    )


def check_for_update() -> Release | None:
    rel = latest_release()
    return rel if rel and is_newer(rel.version) else None


def _fetch(url: str, dest: Path, progress: Callable[[int, int], None] | None = None,
           cancel: Callable[[], bool] | None = None, size_hint: int = 0) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r, dest.open("wb") as f:
        total = int(r.headers.get("Content-Length") or size_hint or 0)
        done = 0
        while True:
            if cancel and cancel():
                raise InterruptedError
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return dest


def download(rel: Release, workdir: Path, progress=None, cancel=None) -> Path:
    asset = rel.windows_zip
    if asset is None:
        raise RuntimeError("Cette release ne contient pas de version Windows.")
    name, url, size = asset
    workdir.mkdir(parents=True, exist_ok=True)
    return _fetch(url, workdir / name, progress, cancel, size)


def verify(rel: Release, zip_path: Path) -> None:
    """Contrôle SHA-256 avec SHA256SUMS.txt de la release (si présent)."""
    sums = rel.assets.get("SHA256SUMS.txt")
    if not sums:
        return
    txt = _fetch(sums[0], zip_path.with_name("SHA256SUMS.txt")).read_text(encoding="utf-8")
    expected = next((ln.split()[0] for ln in txt.splitlines() if ln.strip().endswith(zip_path.name)), None)
    if not expected:
        return
    h = hashlib.sha256()
    with zip_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest().lower() != expected.lower():
        raise RuntimeError("Le fichier téléchargé est corrompu (empreinte SHA-256 différente). Réessaie plus tard.")


def install_dir() -> Path | None:
    """Dossier de l'application installée (exe uniquement ; None en mode développement)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def can_self_update() -> bool:
    d = install_dir()
    if d is None:
        return False
    try:
        probe = d / ".synclive-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$pidToWait = {pid}
$src = '{src}'
$dst = '{dst}'
$exe = Join-Path $dst 'SyncLive.exe'
$log = Join-Path $env:LOCALAPPDATA 'SyncLive\update.log'
try {{ Wait-Process -Id $pidToWait -Timeout 60 }} catch {{}}
Start-Sleep -Milliseconds 500
"$(Get-Date -Format s) mise a jour : $src -> $dst" | Out-File -FilePath $log -Append -Encoding utf8
robocopy $src $dst /E /IS /IT /R:5 /W:1 /NFL /NDL /NJH /NJS /NP | Out-File -FilePath $log -Append -Encoding utf8
Start-Process -FilePath $exe
Start-Sleep -Seconds 2
Remove-Item -LiteralPath '{tmp}' -Recurse -Force
"""


def _launch_independent(cmd: list[str]) -> None:
    """Lance le script de mise à jour de façon totalement indépendante de SyncLive : il est créé par
    le service WMI de Windows, donc hors de tout groupe de processus qui le tuerait à la fermeture
    de l'application. Repli : lancement détaché classique."""
    line = subprocess.list2cmdline(cmd).replace("'", "''")
    ps = f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{line}'}}; exit $r.ReturnValue"
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=no_window, timeout=30)
        if r.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)


def install_and_restart(zip_path: Path, target: Path | None = None) -> Path:
    """Extrait la nouvelle version et lance le script qui l'installera après fermeture de l'app.
    Retourne le chemin du script (l'appelant doit ensuite quitter l'application)."""
    target = target or install_dir()
    if target is None:
        raise RuntimeError("Mise à jour automatique disponible uniquement dans la version exe.")
    tmp = Path(tempfile.mkdtemp(prefix="synclive-update-"))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    src = tmp / "SyncLive"
    if not (src / "SyncLive.exe").exists():
        raise RuntimeError("Archive de mise à jour inattendue (SyncLive.exe introuvable).")
    q = lambda p: str(p).replace("'", "''")
    script = tmp / "update.ps1"
    script.write_text(SCRIPT.format(pid=os.getpid(), src=q(src), dst=q(target), tmp=q(tmp)), encoding="utf-8-sig")
    _launch_independent(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                         "-File", str(script)])
    zip_path.unlink(missing_ok=True)
    return script
