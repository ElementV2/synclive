# SPDX-License-Identifier: GPL-3.0-or-later
"""Publie la version courante sur GitHub : release vX.Y.Z, notes tirées de CHANGELOG.md,
fichiers de release/ (exe zip, sources zip, SHA256SUMS.txt). Voir RELEASING.md.

Jeton : variable GITHUB_TOKEN, sinon gestionnaire d'identifiants de Git (jamais affiché).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from synclive import __url__, __version__  # noqa: E402

REPO = __url__.replace("https://github.com/", "").strip("/")
TAG = f"v{__version__}"
REL = ROOT / "release"
ASSETS = [(f"SyncLive-{__version__}-windows-x64.zip", "application/zip"),
          (f"SyncLive-{__version__}-source.zip", "application/zip"),
          ("SHA256SUMS.txt", "text/plain")]

INSTALL = """
## Installation (Windows 10 / 11, 64 bits)

1. Télécharger **`SyncLive-{v}-windows-x64.zip`** ci-dessous.
2. Clic droit sur le zip > **Extraire tout** (ne pas lancer l'exe depuis l'intérieur du zip).
3. Ouvrir le dossier `SyncLive` et lancer **`SyncLive.exe`**. Rien d'autre à installer.

Déjà installé (1.1.0 ou plus récent) : SyncLive propose la mise à jour tout seul au démarrage.

L'exécutable n'est pas signé : si Windows SmartScreen affiche « Windows a protégé votre ordinateur »,
cliquer sur **Informations complémentaires** puis **Exécuter quand même**.

| Fichier | Contenu |
|---|---|
| `SyncLive-{v}-windows-x64.zip` | application prête à l'emploi |
| `SyncLive-{v}-source.zip` | code source complet de cette version (GPL) |
| `SHA256SUMS.txt` | empreintes SHA-256 |

Logiciel libre sous GNU GPL v3.0 ou ultérieure. Historique complet : [CHANGELOG.md]({url}/blob/main/CHANGELOG.md).
"""


def token() -> str:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, cwd=ROOT).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    sys.exit("Aucun jeton GitHub : définir GITHUB_TOKEN.")


def notes() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(__version__)}\][^\n]*\n(.*?)(?=^## \[|^\[\d)", text, re.S | re.M)
    if not m:
        sys.exit(f"Section [{__version__}] absente de CHANGELOG.md")
    return m.group(1).strip() + "\n" + INSTALL.format(v=__version__, url=__url__)


def main() -> int:
    for name, _ in ASSETS:
        if not (REL / name).exists():
            sys.exit(f"{name} absent de release/ : lancer release.bat")
    H = {"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "synclive-release"}

    def api(method, url, data=None, raw=None, headers=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        req = urllib.request.Request(url, data=body, method=method, headers={**H, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    st, rel = api("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}")
    payload = {"tag_name": TAG, "name": f"SyncLive {__version__}", "body": notes(),
               "draft": False, "prerelease": False, "make_latest": "true"}
    if st == 404:
        st, rel = api("POST", f"https://api.github.com/repos/{REPO}/releases", payload)
    else:
        st, rel = api("PATCH", rel["url"], payload)
    if st >= 300:
        sys.exit(f"Erreur GitHub {st} : {rel}")
    print(f"release {TAG} : {rel['html_url']}")
    existing = {a["name"]: a["url"] for a in rel.get("assets", [])}
    upload = rel["upload_url"].split("{")[0]
    for name, ctype in ASSETS:
        if name in existing:
            api("DELETE", existing[name])
        data = (REL / name).read_bytes()
        st, a = api("POST", f"{upload}?name={name}", raw=data,
                    headers={"Content-Type": ctype, "Content-Length": str(len(data))})
        print(f"  {name:40s} {'ok' if st < 300 else st}  {len(data) / 1e6:.1f} Mo")
        if st >= 300:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
