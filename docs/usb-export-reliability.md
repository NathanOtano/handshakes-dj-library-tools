# Fiabilité du classement et de l’export USB

## Classement sans inventer un genre

Un bac DJ est un rangement de mix ; il n’est pas forcément un genre précis.
La taxonomie reste prioritaire. Un choix provisoire peut compléter les titres
qu’elle ne classe pas, à partir de voisins audio et d’une décision de rangement.
Compter une voix par famille de modèles, sans multiplier le poids de variantes
corrélées. Exclure les voisins de même titre et conserver les alternatives,
l’accord et la provenance. Un score de proximité n’est pas une probabilité.

Le registre local `runtime/dj-bucket-assignments.json`, version 1, contient une
table `assignments` indexée par ID natif. Chaque entrée fournit `path`, `title`,
`bucket` et `families`. Il ne doit contenir que des choix approuvés. L’audit et la
synchronisation appliquent le registre uniquement en l’absence de bac taxonomique.
Une divergence ID/chemin/titre ou un bac inconnu provoque une erreur. Un genre
précis reste prioritaire. Ne jamais versionner le registre réel ni des bases DJ.

Exemple fictif de structure, à créer localement et adapter à ses propres données :

```json
{"version":1,"assignments":{"7":{"path":"C:/DJ_Music/example.aif","title":"Example","bucket":"Example bucket","families":2}}}
```

Le nom du bac doit exister dans la taxonomie utilisée. Sans voisin exploitable,
recueillir une décision musicale avant export ; ne pas fabriquer un genre.

## Conditions de sortie

La sélection USB doit avoir zéro titre sans bac, sans imposer ce classement aux
samplers internes ou entrées streaming volontairement exclus de l’export.
`audit_dj_usb_readiness.py` contrôle une copie DB cohérente, ses fichiers locaux,
la couverture des bacs et leurs appartenances dans les parties USB. Son profil
de navigation attend `DJ Bacs/<bac>/Tous` et les vues USB correspondantes.
La commande écrit seulement le rapport demandé, aucune base ni audio :

```text
python scripts/audit_dj_usb_readiness.py --master <snapshot>/master.db --db-dir <snapshot> --taxonomy <taxonomy.json> --output <report.json>
```

Le runtime de ce contrôle nécessite `pyrekordbox` ; les tests synthétiques des
helpers n’en ont pas besoin. Copier la configuration d’exemple avant utilisation.
L’export ne remplace pas l’analyse native préalable des grilles et repères.

`usb_playlist_limits.py` regroupe les BPM par dizaines, conserve l’inconnu et
supprime seulement une plage entièrement couverte. Les parties gardent l’ordre
et contiennent au plus 1 000 titres. Compter ensuite les dossiers **et** playlists
dans la limite de 1 000 éléments ; ne pas supprimer des titres pour la respecter.
`usb_export_policy.verify_bucket_coverage` contrôle la sélection XML correspondante.

## Trois incohérences distinctes dans un export natif

1. **Compteur périmé** : les lignes peuvent être présentes alors qu’un compteur
   de gestion indique encore le nombre de l’ancien export. Le helper ne recale
   ce champ que pour le profil observé : table 19, une ligne, marqueur `0x280`,
   champ réservé nul et IDs contigus de 1 à N. Tout autre profil est refusé.
2. **Index détachés** : une lecture linéaire peut retrouver les nouvelles lignes
   tandis que les anciens index titres/playlists pointent encore vers des pages
   détachées. Un exporteur doit reconstruire ou retirer ces index selon son
   profil vérifié. Le vérificateur fourni exige des index retirés vides ; il ne
   prétend pas que tous les exports officiels doivent avoir des index vides.
3. **Analyse liée à l’ancien chemin** : après déplacement physique de l’audio,
   changer seulement son chemin dans les catalogues ou le bloc PPTH ne suffit
   pas. Le dossier USBANLZ dépend du chemin audio USB final. `analysis_directory`
   reproduit le hash UTF-16 observé ; `allocate_analysis_path` réserve les noms
   occupés et attribue un nom distinct lors d’une collision.

Les tests reproduisent compteur périmé, dossier incorrect, index encore peuplé,
profil inconnu et collisions, uniquement avec des données fictives. Les autres
champs opaques ne sont pas réputés décodés. Ces helpers utilisent le protocole
d’un lecteur/éditeur PDB fourni par l’intégrateur, sans embarquer de clé de base,
de fichier audio, de bibliothèque privée ou de décodeur tiers.

## Ordre d’une synchronisation contrôlée

1. Fermer les logiciels DJ ; identifier le volume par son identité réelle, pas
   seulement une lettre. Faire une copie cohérente DB/WAL/SHM/XML et les préimages.
2. Stabiliser les noms et destinations physiques avant le calcul des analyses.
   Une catégorie de rangement peut être directement sous `Contents/` ; les
   playlists et le rangement physique sont deux structures différentes.
3. Construire localement les catalogues legacy et OneLibrary ; comparer IDs,
   dictionnaires, fichiers, playlists et ordre, état d’analyse, grilles, cues,
   commentaires/couleurs, My Tags, index et compteur natif.
4. Lier le reçu de vérification aux hashes du manifeste et des catalogues. Un
   changement après vérification invalide le reçu. Refuser une version ancienne.
5. Copier les nouveaux audios et analyses, puis les catalogues en dernier.
   Hacher intégralement les nouvelles copies et les relire sur le volume.
   Pour l’audio déjà présent, un contrôle taille/extrémités doit être décrit
   comme tel, jamais comme un hash intégral.
6. Relire le support réel, vider les caches puis éjecter proprement. Prévoir
   arrêt, reprise et retour arrière : plusieurs renommages sur FAT32 ne sont
   pas une transaction atomique. Conserver les anciennes analyses pendant une
   réparation et ne retirer que les alias nouveaux prouvés lors d’un rollback.

Des essais ciblés sur XDJ-AERO ont montré le compteur attendu et la reconnaissance
d’un titre par Artiste sans avertissement d’analyse après correction de ces
incohérences. Ils ne prouvent ni tous les morceaux, ni CDJ-350, ni un lecteur
OneLibrary, ni l’alternance avec l’exporteur officiel. Chaque nouveau lot nécessite
sa propre vérification ; aucun résultat universel de compatibilité n’est promis.
