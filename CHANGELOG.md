# Journal des modifications

Toutes les évolutions notables de SyncLive sont listées ici.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), numérotation [SemVer](https://semver.org/lang/fr/) :
`MAJEUR.MINEUR.CORRECTIF` (correctif = corrections, mineur = nouveautés compatibles, majeur = changements incompatibles).

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

Première version publique.

### Ajouté
- Analyse d'une vidéo de synchro par glisser-déposer : découpe en plans, regroupement par source, claps (arrêt net des mains, aide MediaPipe) et flashs de mire, toutes fréquences d'images (timestamps réels).
- Sources à l'image identique séparées par leur QR et leur décalage.
- Plan de correction global en ms et en images : son retardé une fois pour tout le mix, retard d'image par source, reste après arrondi.
- Mires SyncLive : toutes résolutions et fréquences, identifiant et QR par source, bips et flashs numérotés sur un cycle de 8 (décalages jusqu'à ± 4 s).
- Partage réseau : chaque machine ouvre un lien, reçoit son identifiant, lance la boucle ou télécharge le MP4.
- Export : images annotées, récapitulatif, CSV, JSON.
- Exécutable Windows autonome (licence GPL-3.0, composants tiers documentés).

[1.1.0]: https://github.com/ElementV2/synclive/releases/tag/v1.1.0
[1.0.0]: https://github.com/ElementV2/synclive/releases/tag/v1.0.0
