# Banque de vidéos de test

Vidéos réelles pour tester SyncLive, et mosaïques multicam fabriquées à partir d'elles.
Les vidéos ne sont pas dans git (trop lourdes) : seuls ce README et `sources.json` peuvent l'être.

```
videos_test/
  sources/        les clips (ajoute les tiens ici)
  sources.json    description + mesure de référence de chaque clip (généré par « scan »)
  compos/         mosaïques générées (.mp4) et leur vérité terrain (.json) — se régénèrent
```

## Utilisation

```
.venv\Scripts\python tools\videos_test.py scan       # nouveaux clips -> sources.json
.venv\Scripts\python tools\videos_test.py compose    # fabrique les mosaïques
.venv\Scripts\python tools\videos_test.py check      # analyse les mosaïques, compare à l'attendu
.venv\Scripts\python tools\videos_test.py all        # les trois
.venv\Scripts\python tools\videos_test.py check grille_2x2   # une seule disposition
```

## Ajouter un clip

1. Dépose la vidéo dans `sources/` (nom parlant : `camera_resolution_fps_particularite.mp4`).
2. Lance `scan` : le clip est analysé et ajouté à `sources.json`.
3. Complète sa `description` dans `sources.json`. Mets `"utiliser": false` pour l'écarter des mosaïques.

Un clip sert de vignette s'il contient **une seule caméra plein écran** dont les claps sont mesurés.

## Mosaïques

Du multicam simulé : un clip par vignette, toutes les vignettes clappent **en même temps**, et la
bande son ne contient **qu'un seul clap** par événement (pris dans un des clips). Chaque vignette
reçoit un retard d'image connu (−300 à +300 ms).
Décalage attendu = mesure de référence du clap dans le clip seul − retard ajouté.

Les dispositions sont dans `LAYOUTS` (`tools/videos_test.py`) : grilles bord à bord, grande +
petites, bande, vignettes libres sur fond noir / blanc / gris / bleu, incrustation.

## Pour de bons clips de test

- assez de lumière pour que la caméra tienne sa cadence (pas d'images répétées) ;
- mains bien visibles, claps près de la caméra, **mains gardées jointes ½ s** après chaque clap ;
- au moins 5 claps espacés d'**au moins 1 s**.
