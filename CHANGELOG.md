# Journal des modifications

Toutes les évolutions notables de SyncLive sont listées ici.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), numérotation [SemVer](https://semver.org/lang/fr/) :
`MAJEUR.MINEUR.CORRECTIF` (correctif = corrections, mineur = nouveautés compatibles, majeur = changements incompatibles).

## [1.5.0] - 2026-11-07

### Ajouté
- **Montage : repérer les pauses.** Écrans d'attente fixes trouvés tout seuls ; écran animé
  (logo qui bouge, boucle) retrouvé partout avec « Écran comme celui-ci » ; piste son de la
  musique des pauses reconnue et proposée (ex. piste 1 = live, piste 6 = musique) et ses passages
  repérés. Pauses hachurées dans la timeline : précédente / suivante, enlever celle-ci, ignorer,
  tout enlever. « Écouter » choisit la piste son du lecteur.
- **Montage : vumètre de toutes les pistes son** à côté de l'image, en même temps (crête,
  valeur, piste écoutée en orange) ; clic sur une barre = écouter cette piste.
- **Montage : plusieurs fichiers bout à bout** (live en plusieurs parties), lecture qui enchaîne
  d'un fichier à l'autre, export sans perte ; un fichier d'un autre format est refusé avec la raison.
- **Montage enregistré tout seul** et repris à la réouverture de la vidéo ; derniers montages
  proposés.
- **Rapport de niveaux** dans l'onglet Normalisation : page HTML ou tableau CSV pour Excel.
- **Journal** : bouton « Journal » pour voir les actions et les erreurs, les copier ou ouvrir
  leur dossier ; les erreurs donnent le détail dans le journal.

## [1.4.0] - 2026-11-06

### Ajouté
- **Onglet « Normalisation » : tous les médias au même niveau sonore** (régie : plus d'écart de volume
  d'une vidéo à l'autre pour l'ingé son). Glisser-déposer de fichiers et de dossiers, mesure
  aussitôt (EBU R128 : niveau intégré, crête vraie, dynamique), écart à la cible visible sur
  chaque ligne, cibles −23 / −16 / −14 LUFS ou personnalisée, « Cible commune » pour tout le
  lot. Gain linéaire et limiteur de crête vraie (6 dB au plus) ; vidéo recopiée sans réencodage,
  son dans sa famille de codec d'origine, synchro gardée ; chaque fichier écrit est remesuré et
  refait si besoin. Formats : MP4, MOV, MKV, WebM, AVI, MXF, TS, M2TS, MPG, FLV, WMV, 3GP, WAV,
  AIFF, FLAC, MP3, M4A, OGG, WMA.
- **Onglet « Montage » : couper un live entier sans perte.** Ouverture immédiate même pour 6 h.
  La timeline est le montage, comme dans un logiciel de montage : morceaux bout à bout, supprimer
  un morceau recolle les suivants, la lecture enchaîne les morceaux ; marqueurs DÉBUT / FIN du
  montage (instant dans le montage et dans le fichier) et raccords. Lecteur avec le son, aperçu de
  l'image en temps réel dans la timeline, aperçu du son rempli en tâche de fond. « Début ici »,
  « Fin ici », « Couper ici », « Supprimer le morceau », déplacer les morceaux, Ctrl+Z ; flèches
  ← → image par image. Coupes calées sur les images clés ; export en recopie (vidéo et son tels
  quels) à la vitesse du disque : un live de 3 h dont on enlève 45 min en 10 s. Option « son
  recalculé à l'échantillon près ».
- **Réglage de l'image du clap : nouveau lecteur.** Bouton lecture / pause (Espace) qui joue la
  vidéo avec son son, à vitesse normale, ×1/2 ou ×1/4, sans sauter d'image. Une seule timeline :
  le son, une règle en secondes autour du son du clap, les sons détectés comme claps, l'image
  choisie et la tête de lecture. L'image où l'on clique ou relâche, celle où l'on met pause, une
  vignette ou les flèches ← → deviennent l'image du clap (plus de bouton « Choisir cette image »).
  La molette sur la vidéo ou la timeline passe d'une image à l'autre.

### Modifié
- Fenêtre « Régler l'image du clap » : ouverte à la hauteur de l'écran et juste à la largeur de
  la vidéo (elle n'est plus étirée sur un écran large) ; la vidéo est toujours ajustée à sa zone.
- Conseil de tournage en clair : « tape 3 à 5 fois dans tes mains, bien fort et bien net… ».
- Messages de l'onglet Normalisation en clair quand un fichier ne peut pas monter jusqu'à la cible
  (« montera à −21,8 au lieu de −16 (pics trop forts) »).

### Corrigé
- Analyse : « Annuler » est pris en compte tout de suite (en moins d'¼ s), à toutes les étapes,
  même pendant la lecture du son d'un fichier de 3 h (24 s d'attente auparavant), et n'affiche plus
  d'alerte « échec ». La recherche des claps traite le son par tranches : un live de plusieurs
  heures ne réclame plus un tableau de plusieurs Go en mémoire.
- Réglage du clap : la vidéo ne change plus de taille au premier clic, la molette ne modifie plus
  les listes (clap, bande, durée) par accident, et une image rattachée au son d'un autre clap
  (claps rapprochés) garde exactement l'image affichée.

## [1.3.2] - 2026-11-05

### Ajouté
- **Supprimer une source** où il ne se passe rien (bouton sur chaque carte) : elle disparaît des
  résultats, du plan de correction et de l'export ; « Annuler la dernière modification » la remet.
- **Réglage de l'image du clap plus lisible** : « Éclaircir » et « Zoom sur les mains » (à cocher
  au besoin), la bande ne montre que les vraies images d'une caméra plus lente que le fichier
  (pas ses copies), bande de 9, 15 ou 21 images, ± 1, 2 ou 4 s autour du son.
- **Lecteur dans le réglage du clap** : timeline à faire glisser, lecture / pause (Espace) à
  vitesse normale, ×1/2 ou ×1/4, en boucle sur la séquence chargée, et « Choisir cette image ».
- **Son du clap visible et audible** : forme d'onde au-dessus de la timeline, avec les sons
  détectés comme claps (en orange, le clap courant en trait plein), l'image affichée et l'image
  choisie ; clic sur la forme d'onde pour s'y placer. Le son est joué avec la vidéo (ralenti en
  ×1/2 et ×1/4) et « Écouter le clap » rejoue image et son de 0,5 s avant à 0,5 s après.
- **L'aperçu de la source suit le réglage** : après un réglage à la main (ou un autre candidat
  choisi, un clap écarté, une annulation), la carte et l'export montrent l'image retenue, plus
  celle de l'automatique.

### Modifié
- **Page réseau des mires** : le MP4 du format choisi est préparé dès l'ouverture de la page (et
  à chaque changement de format), avec l'avancement affiché ; « Télécharger le MP4 » et « Lancer
  en plein écran » montrent « Préparation… 42 % » puis démarrent tout seuls. Format par défaut :
  Full HD 59.94 (page et onglet Mires).
- Onglet Mires : « N° de la 1re source » retiré (les sources sont toujours numérotées à partir
  de 1) et la durée de la boucle s'affiche en secondes (« 16 s »), par pas de 8 s.

### Corrigé
- Réglage du clap : déplacer la timeline (ou la forme d'onde) choisit maintenant l'image, comme les
  flèches. Pendant un glisser l'image suit, et c'est celle où on relâche qui est retenue.
- Image du clap réglée à la main : c'est maintenant la mesure de la source, à ± ½ image réelle. Les
  claps automatiques de cette source (souvent mal appariés quand on a dû régler) ne la brouillent
  plus : un réglage à −34 ms n'est plus noyé dans une marge de ± 35 ms et donne bien sa correction.
- Réglage du clap : après avoir déplacé la timeline, un clic dans la bande pouvait rattacher
  l'image au mauvais son (référence prise sur les claps automatiques d'une source ambiguë) ou
  retenir une image voisine. L'image cliquée est maintenant celle retenue, et la vignette
  affiche la valeur exacte obtenue (avec le son auquel elle sera rattachée). La vidéo ne change
  plus de taille quand le texte ou les vignettes changent, et la lecture ×1 avec « Éclaircir »
  affiche bien toutes les images.
- « Régler l'image du clap… » s'ouvre sur le même clap et la même image que la carte (elle
  s'ouvrait sur le premier clap de la liste, une autre image que celle montrée).
- Réglage à la main avec des claps rapprochés : l'image de contact choisie était rattachée au son
  sélectionné dans la liste, même si elle appartenait au clap suivant (−963 ms au lieu de +22 ms).
  Elle est maintenant rattachée au son de clap qui lui correspond, et la fenêtre le signale.
- Caméra lente (20 i/s réels dans un fichier à 60) : chaque image réelle passait pour l'arrêt
  d'un clap, si bien que n'importe quel bruit trouvait une image à associer. Résultat : fausses
  caméras mesurées, plan coupé en deux en plein clap. Seules les images réellement nouvelles
  sont maintenant analysées, à la précision de la caméra (vidéo OBS réelle : caméra du bureau
  mesurée à +4 ms au lieu de « ambigu »).
- Claps trop rapprochés pour être appariés : « À RÉGLER » au lieu de « AMBIGU : +490 ms ou
  −117 ms » (deux valeurs dont l'une est fausse), et le bouton « Régler l'image du clap… » est mis
  en avant sur la carte.
- Source avec un seul clap et plusieurs mouvements aussi nets (décor animé, projection) :
  « À VÉRIFIER » au lieu d'un décalage inventé.

## [1.3.1] - 2026-11-05

### Modifié
- **Un seul fichier à télécharger** : `SyncLive-windows-x64.zip`, sans numéro de version. Le lien
  https://github.com/ElementV2/synclive/releases/latest/download/SyncLive-windows-x64.zip donne
  toujours la dernière version. Le code source de chaque version est l'archive « Source code » de
  la release (`SOURCE.txt` et « À propos » mis à jour).

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

[1.5.0]: https://github.com/ElementV2/synclive/releases/tag/v1.5.0
[1.4.0]: https://github.com/ElementV2/synclive/releases/tag/v1.4.0
[1.3.2]: https://github.com/ElementV2/synclive/releases/tag/v1.3.2
[1.3.1]: https://github.com/ElementV2/synclive/releases/tag/v1.3.1
[1.3.0]: https://github.com/ElementV2/synclive/releases/tag/v1.3.0
[1.2.0]: https://github.com/ElementV2/synclive/releases/tag/v1.2.0
[1.1.1]: https://github.com/ElementV2/synclive/releases/tag/v1.1.1
[1.1.0]: https://github.com/ElementV2/synclive/releases/tag/v1.1.0
[1.0.0]: https://github.com/ElementV2/synclive/tree/b64643b
