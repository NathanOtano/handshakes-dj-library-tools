# Handshakes DJ — Library Tools

**Outils en ligne de commande pour utilisateurs avancés et agents supervisés.**

Scripts Python et PowerShell pour auditer une bibliothèque DJ, préparer des
modifications Rekordbox/Serato et contrôler le classement ainsi que les données
d’un export USB. Le dépôt distribue du code source, sans installateur ni
exporteur USB universel prêt à l’emploi. La ligne de commande reste la référence.

Une [interface locale expérimentale](docs/local-ui.md) propose trois étapes :
vérifier, préparer, synchroniser. Elle exige des commandes configurées localement
et conserve les contrôles de l’exporteur. Elle n’est pas un installateur graphique
validé pour toutes les bibliothèques et tous les lecteurs.

## Choisir un point d’entrée

| Besoin | Point d’entrée | Portée |
| --- | --- | --- |
| Vérifier le dépôt sans bibliothèque musicale | `python -m unittest discover -s tests -v` | Tests synthétiques des helpers de bacs et USB |
| Ouvrir l’interface locale | [Guide de l’interface](docs/local-ui.md) | Vérification, préparation et copie via des commandes configurées localement |
| Vérifier les chemins de sa configuration | `scripts/Test-DjLibraryReadiness.ps1` | Diagnostic local, sans création de dossiers par défaut |
| Auditer fichiers et doublons | `scripts/Audit-DjLibraryCleanup.ps1` | Copie de travail et rapports ; aucune modification des originaux |
| Préparer l’ajout de fichiers traités | `scripts/Add-RekordboxProcessedContent.ps1 -Mode Plan` | Plan avant toute écriture de collection |
| Auditer ou synchroniser la taxonomie | `scripts/Audit-RekordboxGenreTaxonomy.ps1`, `scripts/Sync-RekordboxGenreTaxonomy.ps1` | Audit, plan, essai sur copie puis application explicite |
| Contrôler les bacs et une sélection USB | [Méthode USB](docs/usb-export-reliability.md) | Couverture, plafonds, analyses, index et compteur natif |

Le [guide d’utilisation](docs/usage.md) explique la configuration et les modes
d’écriture. Le [catalogue des scripts](scripts/README.md) détaille les autres
opérations : métadonnées, relocalisation, playlists, rapprochement de métadonnées
Spotify/TIDAL et diagnostics de contrôleur.

## Démarrage sans données privées

Python **3.10 ou supérieur** suffit pour les tests synthétiques ; la dernière
validation a été exécutée avec Python 3.12. Aucun compte ni fichier musical
n’est nécessaire.

```powershell
git clone https://github.com/NathanOtano/handshakes-dj-library-tools.git
cd handshakes-dj-library-tools
python -m unittest discover -s tests -v
```

Pour ouvrir l’interface, copier `config/local-ui.json.example` vers
`config/local-ui.json`, puis lancer :

```powershell
python scripts/dj_local_ui.py --config config/local-ui.json
```

Ouvrir l’adresse `127.0.0.1` affichée. Le modèle permet de visiter l’interface,
mais les opérations restent désactivées tant que les commandes DJ et leurs
rapports ne sont pas configurés. Voir le [contrat des commandes](docs/local-ui.md#contrat-des-commandes).

Pour les commandes `.ps1`, utiliser **PowerShell 7 ou supérieur** sur Windows.
Les dépendances supplémentaires varient selon l’outil : `pyrekordbox` et son
pilote SQLCipher pour les bases Rekordbox, `mutagen` pour certains tags,
Chromaprint/`fpcalc` pour certains rapprochements audio. Elles ne sont pas
nécessaires aux tests ci-dessus. Les commandes Python des helpers peuvent être
portables ; l’ensemble des scripts n’est pas validé sur macOS ou Linux.

## Préparer sa bibliothèque

1. Lire le [guide](docs/usage.md), choisir un seul outil et examiner ses paramètres.
2. Copier les modèles de `config/` vers des fichiers locaux puis adapter leurs
   chemins. Les modèles contiennent des valeurs fictives.
3. Fermer les logiciels DJ et conserver une sauvegarde cohérente avant toute
   opération sur une collection.
4. Examiner le rapport ou le plan, tester sur une copie lorsque le mode existe,
   puis appliquer uniquement le lot validé. Les scripts ont des contrats
   différents : ne pas supposer que chaque commande possède un mode `Plan`.

Un agent doit respecter les mêmes étapes : périmètre explicite, lecture du script,
prévisualisation, autorisation avant écriture réelle et vérification après coup.
Un code de sortie réussi ne suffit pas à prouver la lecture sur un appareil DJ.

## Ce qui est vérifié

- **33 tests Python** couvrent les helpers de bacs et USB, le profil natif,
  l’extraction de la sélection courante et les garde-fous du serveur local.
- Le parcours graphique a été vérifié sur bureau et mobile, avec une cible
  temporaire : confirmation, annulation, arrêt et panne réseau. Cela ne valide
  pas une copie physique sur clé ni sa lecture sur un appareil.
- Des essais ciblés sur **XDJ-AERO** ont confirmé un compteur corrigé et la
  recherche par artiste d’un titre avec son analyse reconnue. Les détails et
  limites sont dans la [méthode USB](docs/usb-export-reliability.md).
- Les autres scripts sont des outils spécialisés à adapter et à vérifier sur
  des copies. La suite de tests ne certifie pas toutes leurs opérations.

Les helpers USB nécessitent une intégration avec un lecteur/éditeur de base
natif et une bibliothèque analysée. Ils ne génèrent pas seuls un export complet.
Les essais AERO ne prouvent ni la lecture de chaque titre, ni la compatibilité
universelle avec les autres lecteurs ou versions de Rekordbox.

## Organisation et documentation

- `scripts/` : commandes et helpers ; [catalogue](scripts/README.md).
- `tests/` : cas synthétiques sans bibliothèque réelle.
- `local_ui/` : interface locale ; [lancement et limites](docs/local-ui.md).
- `config/` : modèles de configuration ; `schemas/` : schéma de taxonomie.
- [Utilisation et configuration](docs/usage.md).
- [Fiabilité du classement et de l’export USB](docs/usb-export-reliability.md).
- [Étude expérimentale USB/MIDI de contrôleurs](docs/USB_MIDI_MAPPING_GUIDE.md) :
  sujet distinct du classement musical et de l’export USB.
- [Historique](CHANGELOG.md), [sécurité](SECURITY.md), [licence MIT](LICENSE).

Conserver les bases, fichiers audio, registres de bacs, rapports, identifiants et
captures dans des emplacements locaux ignorés par Git. Ne jamais joindre une
bibliothèque réelle ou un identifiant de connexion à une issue.

## Licence et services tiers

Le code est distribué sous licence MIT. Utiliser uniquement des fichiers audio
dont on détient les droits nécessaires et respecter les conditions des services
tiers. Aucun audio, identifiant de service ou clé de base n’est distribué.
Ce projet indépendant n’est affilié ni à AlphaTheta/Pioneer DJ, ni à Serato,
Spotify, TIDAL ou aux autres marques citées. Une modification directe de base
peut endommager une collection : garder des sauvegardes et valider les copies.
