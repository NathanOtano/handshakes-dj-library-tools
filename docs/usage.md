# Utiliser les outils en ligne de commande

## Configurer uniquement l’outil choisi

Les commandes s’exécutent depuis la racine du dépôt. Elles n’ont pas toutes les
mêmes dépendances ni les mêmes options. Lire le bloc `param(...)` de la commande
PowerShell ou le parseur d’arguments du script Python avant utilisation.

| Modèle versionné | Configuration locale | Usage |
| --- | --- | --- |
| `config/dj-library.paths.json.example` | `config/dj-library.paths.json` | Chemins, extensions et outils optionnels |
| `config/dj-genre-taxonomy.json.example` | `config/dj-genre-taxonomy.json` | Alias de genres et bacs |

Créer uniquement les fichiers absents ; conserver toute configuration existante :

```powershell
if (-not (Test-Path .\config\dj-library.paths.json)) {
    Copy-Item .\config\dj-library.paths.json.example .\config\dj-library.paths.json
}
if (-not (Test-Path .\config\dj-genre-taxonomy.json)) {
    Copy-Item .\config\dj-genre-taxonomy.json.example .\config\dj-genre-taxonomy.json
}
```

Adapter ensuite les chemins fictifs `C:/DJ_Music` et les extensions. La clé
historique `postProcessed_Library_RootRoot` désigne le dossier de fichiers traités ;
son nom est conservé pour correspondre aux scripts. Les chemins d’outils vides
laissent les diagnostics tenter leur découverte dans le `PATH`.
Le modèle de chemins ne configure pas les comptes de services tiers.

Une première vérification des dossiers peut se lancer sans les créer :

```powershell
pwsh -NoProfile -File .\scripts\Test-DjLibraryReadiness.ps1 -Json
```

Un résultat négatif avec les chemins fictifs est attendu tant qu’ils ne sont pas
adaptés. Ce diagnostic ne prouve ni la cohérence d’une collection ni la lecture
d’une clé sur un appareil.

## Comprendre les écritures

| Mode ou opération | Effet à vérifier |
| --- | --- |
| Audit | Peut produire des copies temporaires et des rapports ; consulter chaque script |
| `Plan` | Prépare les changements proposés, sans appliquer la modification à la collection originale |
| `CopyApply -Apply` | Teste l’écriture sur une copie, pour les commandes qui proposent ce mode |
| `LiveApply -Apply -ConfirmLiveWrite` | Modifie la collection originale, après sauvegarde et fermeture des logiciels DJ |

Ces trois modes concernent notamment l’ajout de fichiers, la taxonomie et les
playlists intelligentes. D’autres commandes ont un contrat différent, par exemple
`CopySmoke`, un simple `-Apply` ou des options propres au script. Aucun mode
d’écriture réelle ne doit être ajouté automatiquement par un agent.

Pour un essai sur copie, Git ne sauvegarde pas la base musicale : conserver
séparément la base et ses fichiers associés. Les wrappers Rekordbox de ce dépôt
cherchent par défaut la collection du profil Windows courant ; utiliser leur
option `-RekordboxRoot` pour désigner une autre collection, dont une copie de test.

## Exemple : préparer l’ajout de fichiers traités

Configurer d’abord le dossier source et les chemins locaux. Avec les dépendances
Rekordbox disponibles et le logiciel fermé :

```powershell
pwsh -NoProfile -File .\scripts\Add-RekordboxProcessedContent.ps1 -Mode Plan -Json
```

Examiner le plan et les doublons avant de tester `CopyApply`. L’option
`-DuplicateCsv` peut fournir un rapport de doublons existant ; elle ne doit pas
pointer vers un fichier d’exemple inexistant. `-PrepareRuntime`, lorsqu’il est
proposé, installe des dépendances dans un environnement local ignoré : cette
installation doit être un choix explicite.

## Exemple : préparer la taxonomie

Adapter la configuration, puis auditer et préparer le plan :

```powershell
pwsh -NoProfile -File .\scripts\Audit-RekordboxGenreTaxonomy.ps1 -Json
pwsh -NoProfile -File .\scripts\Sync-RekordboxGenreTaxonomy.ps1 -Mode Plan -Json
```

La taxonomie et les bacs provisoires servent au rangement. Un bac provisoire
n’est pas un genre précis prouvé et ne doit pas devenir automatiquement un tag
audio. Voir le [registre de bacs et les conditions de sortie USB](usb-export-reliability.md).

## Préparer une clé USB et vérifier le résultat

Le dépôt fournit des helpers et une [méthode de vérification](usb-export-reliability.md),
pas une commande autonome de synchronisation universelle. Une intégration doit
fournir son lecteur/éditeur de base native, ses analyses existantes, son plan de
copie, ses contrôles d’identité du volume et son retour arrière.

Après construction locale et validation, copier les audios et analyses avant les
catalogues, relire le support, éjecter proprement puis tester sur le lecteur.
Conserver les preuves de chaque lot : sélection, ordre, plafonds, analyses,
compteurs, index et hashes. L’acceptation matérielle est distincte des tests Python.

## Contribuer ou signaler un problème

Exécuter les tests synthétiques avant une proposition de modification. Décrire
la commande, le système, les versions et le résultat attendu. Fournir un cas
fictif minimal plutôt qu’une base réelle, une capture personnelle ou des secrets.
Les signalements sensibles suivent la [politique de sécurité](../SECURITY.md).
