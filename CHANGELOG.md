# Journal des modifications

Toutes les évolutions notables de SyncLive sont listées ici.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), numérotation [SemVer](https://semver.org/lang/fr/) :
`MAJEUR.MINEUR.CORRECTIF` (correctif = corrections, mineur = nouveautés compatibles, majeur = changements incompatibles).

## [1.3.0] - 2026-11-05

### Ajouté
- **Régler l'image du clap à la main** : bouton sur chaque source, bande d'images autour du clap
  (zoom automatique sur les mains), on clique l'image où les mains se touchent, flèches ← → pour
  avancer ou reculer d'une image. Le décalage est recalculé tout de suite, à ± ½ image près ;
  « Revenir à l'automatique » et « Annuler la dernière modification » restent possibles. Les images
  répétées d'une caméra lente sont grisées.

### Modifié
- **Interface adaptée à la taille de la fenêtre** : les sources s'affichent sur 1 à 4 colonnes
  selon la largeur et leurs images s'agrandissent avec la fenêtre ; le plan de correction occupe
  toute la largeur et les textes longs passent à la ligne. La fenêtre peut être réduite à ~900 px.

## [1.2.0] - 2026-11-05

### Ajouté
- **Marge de mesure** affichée pour chaque source (« ± 39 ms »), avec la **cadence réelle** de la
  caméra quand elle répète ses images (webcam qui tombe à 15 i/s en faible lumière). Un écart plus
  petit que la marge donne « SYNCHRO » et aucune correction n'est proposée.
- **Décalage ambigu** signalé (« AMBIGU : +557 ms ou −165 ms ») quand des claps trop rapprochés
  permettent deux appariements, au lieu d'annoncer un résultat faux.
- **Mosaïques sur fond uni** (noir, blanc, couleur) : vignettes de tailles et positions libres,
  mal alignées, qui se touchent par un coin, ou collées en bande au milieu du fond.
- **Analyse fine des vignettes** : autour de chaque clap, le mouvement de chaque vignette est
  réanalysé à pleine résolution (une vignette de 640 px ne couvrait que 10 × 6 cellules).
- **Banque de vidéos de test** (`videos_test/`, `tools/videos_test.py`) : clips réels et mosaïques
  multicam synthétiques (claps simultanés, un seul son, retards connus de −300 à +300 ms).

### Corrigé
- Une caméra qui répète ses images (15 i/s réels dans un fichier à 30 ou 60) était découpée en
  plusieurs fausses caméras à chaque clap fait près de l'objectif.
- Image très en retard (au-delà de ~200 ms) : le clap était rejeté car l'arrêt des mains tombait
  en bout de fenêtre de recherche.
- Mire à l'écran dont le son n'a pas été enregistré : un bruit du plateau pouvait être pris pour un
  bip et donner une mesure fausse ; la mire est maintenant « non mesurée ».
- Un faux clap audio (son coupé en plein bruit en début de fichier) pouvait fausser ou invalider
  la mesure d'une caméra.
- Une mire SyncLive sur fond uni n'est pas prise pour une mosaïque.

## [1.1.1] - 2026-11-04

### Corrigé
- Clap précédé d'un geste plus lent (personne qui se met en place) : le clap pouvait être « avalé »
  par ce geste et un faux décalage était donné (constaté sur un multiview réel : erreur de 262 ms,
  désormais 1 ms).
- Un mouvement qui ne s'arrête pas dans la fenêtre de recherche interrompait toute la recherche
  au lieu de passer au mouvement suivant.

## [1.1.0] - 2026-11-04

### Ajouté
- **Multiview maison** : une mosaïque de caméras bord à bord (sans séparateur) est découpée automatiquement en vignettes ; chaque vignette devient une source (« CAM 2 · vignette haut centre »). Un seul clap vu par toutes les caméras donne le décalage de chacune ; les mires dans une vignette sont aussi reconnues (QR compris).
- **Mises à jour automatiques** : au démarrage, SyncLive vérifie discrètement la dernière version publiée sur GitHub. Un bandeau propose la mise à jour : téléchargement avec progression, contrôle SHA-256, remplacement des fichiers et redémarrage automatique. Bouton « Rechercher les mises à jour » dans « À propos ».
- **Numéro des mires à distance** : depuis le partage réseau, le n° de chaque machine se change directement sur sa ligne ; la machine l'applique en moins de 3 s et sa boucle redémarre avec le nouveau QR (échange automatique si le n° est déjà pris).
- **Annuler la dernière modification** (fusion, déplacement de plan, renommage, exclusion de clap, choix de candidat).
- **Mires d'autres marques** : les bips longs (note tenue de 100 à 300 ms) sont reconnus, début mesuré à la milliseconde.
- Mode développement `dev.bat` : l'interface se relance à chaque modification du code.

### Modifié
- **Plus aucun réglage** : piste audio et sensibilité choisies automatiquement (si la 1re piste ne contient ni clap ni bip, les autres pistes sont essayées).
- Accueil simplifié : une grande zone de dépôt ; les résultats s'affichent après l'analyse.
- Panneau réseau sur toute la hauteur, une ligne par machine.
- « Écart restant » devient « Reste après correction », avec explication.
- Les claps (bruit large bande) et les bips de mire (note pure) sont distingués : une même bande son peut contenir les deux.
- Grille d'analyse du mouvement plus fine (32 × 18).
- Aperçu des mires retiré de l'onglet Mires.

### Corrigé
- Faire défiler la liste des sources à la molette pouvait déclencher une fusion par accident (« Fusionner avec… » est maintenant un bouton à menu ; les listes ignorent la molette).
- Un décor qui scintille (flamme animée, écran) pouvait être pris pour une mire.
- Libellé « mire sans son ? » qui renvoyait à un réglage supprimé.

## [1.0.0] - 2026-11-04

Première version (remplacée par la 1.1.0, qui apporte la mise à jour automatique).

### Ajouté
- Analyse d'une vidéo de synchro par glisser-déposer : découpe en plans, regroupement par source, claps (arrêt net des mains, aide MediaPipe) et flashs de mire, toutes fréquences d'images (timestamps réels).
- Sources à l'image identique séparées par leur QR et leur décalage.
- Plan de correction global en ms et en images : son retardé une fois pour tout le mix, retard d'image par source, reste après arrondi.
- Mires SyncLive : toutes résolutions et fréquences, identifiant et QR par source, bips et flashs numérotés sur un cycle de 8 (décalages jusqu'à ± 4 s).
- Partage réseau : chaque machine ouvre un lien, reçoit son identifiant, lance la boucle ou télécharge le MP4.
- Export : images annotées, récapitulatif, CSV, JSON.
- Exécutable Windows autonome (licence GPL-3.0, composants tiers documentés).

[1.3.0]: https://github.com/ElementV2/synclive/releases/tag/v1.3.0
[1.2.0]: https://github.com/ElementV2/synclive/releases/tag/v1.2.0
[1.1.1]: https://github.com/ElementV2/synclive/releases/tag/v1.1.1
[1.1.0]: https://github.com/ElementV2/synclive/releases/tag/v1.1.0
[1.0.0]: https://github.com/ElementV2/synclive/tree/b64643b
