<p align="center">
  <img src="synclive/assets/lockup-dark-bg.svg" alt="SyncLive" height="64">
</p>

<p align="center">
  Mesure le décalage son / image de chaque source d'une vidéo (caméras, PC, consoles)<br>
  et indique quoi corriger, en millisecondes et en images.
</p>

---

## Principe

1. Devant chaque caméra, on fait quelques **claps**. Sur chaque PC ou console, on lance une **mire SyncLive**.
2. On enregistre le programme (OBS, mélangeur, enregistreur…).
3. On glisse l'enregistrement dans SyncLive.

SyncLive retrouve chaque source, mesure son décalage et produit un **plan de correction** :

```
SON : retarder de 118 ms · 3,5 img (réglage global, une seule fois pour toutes les sources)
CAM 1       IMAGE EN RETARD de 118 ms · 3,5 img   ->  IMAGE : aucun retard à mettre
CAM 3       IMAGE EN RETARD de 98 ms · 2,9 img    ->  IMAGE : retarder de 1 img · 33 ms
07 · ATLAS  SON EN RETARD de 148 ms · 4,4 img     ->  IMAGE : retarder de 8 img · 267 ms
```

Le son étant commun à toutes les sources, on ne peut que le retarder, une seule fois, de la plus grande
avance mesurée. Chaque source d'image reçoit ensuite le retard qui lui manque, en images entières, avec
l'écart restant après arrondi.

## Fonctionnalités

**Analyse**
- Glisser-déposer d'une vidéo (MP4, MOV, MKV, MXF, TS…), toutes fréquences d'images
  (23.976 à 120 i/s, VFR) : les calculs utilisent les timestamps réels du fichier.
- Découpe en plans et regroupement des plans par source. Deux sources qui affichent la même image
  (même mire sur deux PC) sont séparées par leur QR code et par leur décalage, même sans coupe visible.
- **Claps** : instant où les mains s'arrêtent net, avec une précision meilleure qu'une image.
  La zone des mains est ciblée automatiquement, avec l'aide de MediaPipe quand les mains sont visibles.
- **Mires** : flash d'une image, mesuré à l'échantillon audio près.
- Résultats robustes (médiane, dispersion, indice de confiance) et corrections manuelles :
  renommer, fusionner, déplacer un plan, exclure un clap, choisir un autre candidat.
- Export : une image annotée par source, un récapitulatif, `plan_correction.csv`,
  `rapport_claps.csv` et `rapport.json`.

**Mires SyncLive**
- Toutes résolutions (SD PAL/NTSC, 720p, 1080p, 1440p, UHD, DCI 4K, vertical, carré) et
  fréquences (23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 100, 119.88, 120).
- Un flash d'une seule image et un bip à chaque période (≈ 1 s).
- Chaque source porte un identifiant (`07 · ATLAS`, jusqu'à 99) en clair et dans un QR code.
- Flashs et bips numérotés sur un cycle de 8 (le numéro est dans le QR et dans la note du bip) :
  un décalage supérieur à une période est mesuré sans ambiguïté, jusqu'à ± 4 s.
- Règle −15…+15 images pour une lecture à l'œil.
- Boucle sans couture, en MP4 (H.264 + AAC) ou MOV (H.264 + PCM 24 bits).

**Multiview maison**
- Une mosaïque de caméras bord à bord, sans séparateur, est découpée automatiquement en vignettes :
  chaque vignette devient une source. Un seul clap vu par toutes les caméras donne le décalage de
  chacune ; claps et bips de mire peuvent se suivre dans la même bande son.

**Partage réseau**
- SyncLive affiche un lien et un QR code. Chaque machine ouvre le lien dans son navigateur, reçoit
  son identifiant, puis lance la boucle en plein écran ou télécharge le MP4. Aucune installation.
- Le n° de chaque machine se change à distance, directement sur sa ligne.

**Sans réglage, à jour tout seul**
- Aucun réglage : piste audio et sensibilité sont choisies automatiquement.
- Au démarrage, SyncLive vérifie la dernière version publiée et propose la mise à jour
  (téléchargement, contrôle SHA-256, installation et redémarrage automatiques).

<p align="center"><img src="docs/mire.png" alt="Mire SyncLive" width="900"></p>
<p align="center"><img src="docs/app-mires.png" alt="Onglet Mires" width="900"></p>

## Installation

**Utilisateurs** : télécharger `SyncLive-<version>-windows-x64.zip` depuis la
[dernière release](https://github.com/ElementV2/synclive/releases/latest), extraire, lancer
`SyncLive.exe`. Rien d'autre à installer. Historique : [CHANGELOG.md](CHANGELOG.md).

**Développement** : Windows 10/11, Python 3.11.

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python main.py
dev.bat                       :: mode dev : l'interface se relance à chaque modification du code
```

Exécutable autonome (`dist\SyncLive\SyncLive.exe`) :

```bat
build.bat
```

## Utilisation en ligne de commande

```bat
python -m synclive.cli video.mp4 -o dossier_sortie
SyncLive.exe --cli video.mp4 -o dossier_sortie
```

Options : `--max-offset` (ms), `--audio-track`, `--sensitivity`, `--group-threshold`,
`--cut-threshold`, `--no-hands`.

## Conventions

- `décalage = t_son − t_image`
  - négatif : **image en retard** (le son arrive avant l'image) ;
  - positif : **son en retard** (le son arrive après l'image).
- Les retards d'image sont exprimés en images entières à la fréquence de l'enregistrement, et en ms
  (OBS : filtre « Délai de rendu »).

## Conseils de tournage

- 3 à 5 claps secs par caméra, mains visibles et éclairées, environ 1 s entre deux claps.
- Au moins 2 s de plan stable par source.
- Avec un seul clap par caméra, la mesure est donnée en confiance « faible » : vérifie-la dans
  l'onglet *Détail*.
- Le navigateur ajoute sa propre latence audio : si une machine lit ses médias avec un autre lecteur
  en production, télécharge le MP4 et lis-le en boucle avec ce lecteur.

## Tests

```bat
.venv\Scripts\python -m pytest
```

Les tests génèrent des vidéos synthétiques à décalage connu : claps à 25, 29.97 et 60 i/s ; mires
avec sources identiques, latences différentes et décalages supérieurs à une période ; MP4/AAC ;
partage réseau ; plan de correction.

## Structure

| Fichier | Rôle |
|---|---|
| `synclive/media.py` | décodage audio/vidéo (PyAV), analyse du mouvement |
| `synclive/audio_claps.py` | détection des claps et des bips |
| `synclive/shots.py` | coupes et regroupement par apparence |
| `synclive/visual.py` | instant visuel des claps et des flashs |
| `synclive/hands.py` | localisation des mains (MediaPipe) |
| `synclive/analysis.py` | pipeline, sources, QR, numérotation des bips |
| `synclive/export.py` | plan de correction, images annotées, rapports |
| `synclive/pattern.py` | générateur de mires |
| `synclive/server.py` | partage réseau |
| `synclive/multiview.py` | découpe des mosaïques en vignettes |
| `synclive/updater.py`, `synclive/ui_update.py` | mises à jour depuis les releases GitHub |
| `synclive/ui.py`, `synclive/ui_pattern.py` | interface (PySide6) |

## Distribuer une version

Procédure complète et conventions indispensables à la mise à jour automatique : [RELEASING.md](RELEASING.md).

```bat
release.bat
```

`release\` contient alors, à publier **ensemble** :

- `SyncLive-<version>-windows-x64.zip` : l'exécutable, `LICENSE.txt`, `SOURCE.txt` et `licenses/`
  (licence de chaque composant embarqué) ;
- `SyncLive-<version>-source.zip` : le code source complet de cette version ;
- `SHA256SUMS.txt` : les empreintes des deux archives.

L'exécutable n'est pas signé : au premier lancement, Windows SmartScreen peut afficher un
avertissement (« Informations complémentaires » puis « Exécuter quand même »).

## Licence

SyncLive est un logiciel libre distribué sous **GNU General Public License v3.0 ou ultérieure**
([LICENSE](LICENSE)). Copyright (C) 2026 ElementV2.

Ce choix découle des composants embarqués : FFmpeg, tel que fourni par PyAV, est compilé avec x264
et x265 (GPL). Les composants tiers et leurs licences sont listés dans
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
