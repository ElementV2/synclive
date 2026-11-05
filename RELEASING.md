# Publier une version

La mise à jour automatique des utilisateurs repose sur ces conventions : les respecter à chaque version.

| Convention | Pourquoi |
|---|---|
| Tag `vX.Y.Z` (SemVer) et release GitHub **publiée** (ni brouillon ni pré-version) | l'app lit `releases/latest` et compare les numéros |
| Fichier `SyncLive-windows-x64.zip` (sans numéro) contenant un dossier `SyncLive/` avec `SyncLive.exe` | archive installée par la mise à jour ; le lien `releases/latest/download/SyncLive-windows-x64.zip` donne toujours la dernière version |
| Fichier `SHA256SUMS.txt` dans la release | l'app vérifie l'empreinte avant d'installer |
| Tag poussé **après** le commit de la version | l'archive « Source code » de GitHub (obligation GPL) est tirée du tag |

## Étapes

1. Mettre à jour la version dans `synclive/__init__.py` et `pyproject.toml`.
2. Ajouter la section `## [X.Y.Z] - AAAA-MM-JJ` en tête de `CHANGELOG.md` (+ le lien en bas).
3. Lancer les tests : `.venv\Scripts\python -m pytest`.
4. Commiter, puis construire : `release.bat` (exe, licences, zip, empreinte).
5. Vérifier l'exe extrait du zip sur une machine propre (ou un dossier avec accents et un PATH minimal).
6. Taguer et pousser : `git tag -a vX.Y.Z -m "SyncLive X.Y.Z"` puis `git push origin main vX.Y.Z`.
7. Publier : `.venv\Scripts\python tools\publish_release.py` (crée la release GitHub, notes tirées du
   CHANGELOG, envoie les deux fichiers). Jeton lu dans `GITHUB_TOKEN` ou, à défaut, dans le
   gestionnaire d'identifiants de Git.

Les utilisateurs à partir de la 1.1.0 voient alors le bandeau de mise à jour au démarrage suivant.
