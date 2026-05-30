# Scripts

## `Test-DjLibraryReadiness.ps1`

Vérifie les chemins et les outils configurés. Ne modifie rien, sauf avec `-CreateMissingFolders` pour créer les dossiers de travail non critiques.

## `Export-DjControllerWindowsDiagnostics.ps1`

Capture l'état Windows USB/MIDI/driver du DJ-Controller en lecture seule. Le rapport JSON va dans `reports/`, hors Git.

```powershell
pwsh -NoProfile -File .\scripts\Export-DjControllerWindowsDiagnostics.ps1
pwsh -NoProfile -File .\scripts\Export-DjControllerWindowsDiagnostics.ps1 -Json
```

## `Install-DjControllerRekordboxMidiMapping.ps1`

Restaure dans le profil rekordbox le mapping MIDI d'usine `PIONEER DJ-Controller MIDI`. Le script est en dry-run par défaut et ne remplace le fichier utilisateur qu'avec `-Apply`.

```powershell
pwsh -NoProfile -File .\scripts\Install-DjControllerRekordboxMidiMapping.ps1
pwsh -NoProfile -File .\scripts\Install-DjControllerRekordboxMidiMapping.ps1 -Apply -CloseRekordbox
```

## `Test-DjControllerMidiInput.ps1`

Capture les messages MIDI WinMM bruts envoyés par le DJ-Controller. Fermer rekordbox pendant le test, sinon rekordbox peut monopoliser l'entrée MIDI.

```powershell
pwsh -NoProfile -File .\scripts\Test-DjControllerMidiInput.ps1 -Seconds 20 -CloseRekordbox -Json
```

## `Export-DjControllerWifiDiagnostics.ps1`

Capture l'état Wi-Fi Windows pour le DJ-Controller : SSID visible, connexion, IP, ping de l'AERO, profil réseau, pare-feu, adaptateurs VPN et statut rekordbox. Le rapport JSON va dans `reports/`, hors Git.

```powershell
pwsh -NoProfile -File .\scripts\Export-DjControllerWifiDiagnostics.ps1
pwsh -NoProfile -File .\scripts\Export-DjControllerWifiDiagnostics.ps1 -Json
```

## `Export-DjLibraryInventory.ps1`

Scanne la bibliothèque et écrit un CSV + JSONL dans `reports/`.

`-Hash` calcule un SHA-256 par fichier et peut être long sur une grosse bibliothèque.

## `Sync-DjControlDatabase.ps1`

Crée et alimente la base de contrôle Codex sous `runtime/dj-control/control.sqlite`.

Le script reste en dry-run par défaut. Ajouter `-Apply` écrit uniquement dans la base de contrôle locale ; il ne modifie ni fichiers audio ni base Rekordbox.

```powershell
pwsh -NoProfile -File .\scripts\Sync-DjControlDatabase.ps1 -Mode init -Apply
pwsh -NoProfile -File .\scripts\Sync-DjControlDatabase.ps1 -Mode snapshot -Apply
pwsh -NoProfile -File .\scripts\Sync-DjControlDatabase.ps1 -Mode status
pwsh -NoProfile -File .\scripts\Sync-DjControlDatabase.ps1 -Mode plan-operation -OperationType relink -TargetKind file -TargetRef "C:\DJ_Music\Playlists\ALL_TRACKS\track.wav"
pwsh -NoProfile -File .\scripts\Sync-DjControlDatabase.ps1 -Mode list-operations
```

## `Invoke-RekordboxControlledWrite.ps1`

Teste et encadre la voie Rekordbox/SQLCipher avec `pyrekordbox`.

Le mode par défaut écrit seulement dans une copie temporaire de `master.db`, puis supprime le faux morceau et la copie :

```powershell
pwsh -NoProfile -File .\scripts\Invoke-RekordboxControlledWrite.ps1 -Mode CopySmoke -PrepareRuntime -Json
```

Le mode live reste bloqué sans `-Apply`, `-ConfirmLiveWrite`, sauvegarde préalable et Rekordbox fermé :

```powershell
pwsh -NoProfile -File .\scripts\Invoke-RekordboxControlledWrite.ps1 -Mode AddContent -TrackPath "C:\DJ_Music\Playlists\ALL_TRACKS\track.wav" -Title "Track" -Json
```

Ne lancer le mode live avec `-Apply -ConfirmLiveWrite` qu’après validation manuelle du dry-run, sauvegarde et fermeture complète de Rekordbox.

## `Add-RekordboxProcessed_Library_RootContent.ps1`

Ajoute à la collection Rekordbox les fichiers présents dans `C:\DJ_Music\Processed_Library_Root` qui ne sont pas déjà référencés par chemin exact normalisé. Le script écrit d’abord un plan sous `reports/`, peut valider l’ajout sur une copie de `master.db`, puis applique en live seulement avec `-Apply -ConfirmLiveWrite`, Rekordbox fermé, et une sauvegarde automatique.

Après un audit de doublons, passer le CSV `local-duplicate-candidates-*.csv` pour éviter d’ajouter les chemins marqués `delete_candidate`.

```powershell
pwsh -NoProfile -File .\scripts\Add-RekordboxProcessed_Library_RootContent.ps1 -Mode Plan -DuplicateCsv reports\local-duplicate-candidates-YYYYMMDD-HHMMSS.csv -Json
pwsh -NoProfile -File .\scripts\Add-RekordboxProcessed_Library_RootContent.ps1 -Mode CopyApply -Apply -DuplicateCsv reports\local-duplicate-candidates-YYYYMMDD-HHMMSS.csv -Json
pwsh -NoProfile -File .\scripts\Add-RekordboxProcessed_Library_RootContent.ps1 -Mode LiveApply -Apply -ConfirmLiveWrite -DuplicateCsv reports\local-duplicate-candidates-YYYYMMDD-HHMMSS.csv -Json
```

## `Sync-RekordboxSmartPlaylists.ps1`

Synchronise les playlists intelligentes Rekordbox suffixées `_` vers les playlists normales du même nom sans suffixe, puis réécrit l’ordre des playlists normales par BPM croissant.

Par défaut, le script prépare seulement un plan depuis une copie de `master.db` :

```powershell
pwsh -NoProfile -File .\scripts\Sync-RekordboxSmartPlaylists.ps1 -Mode Plan -Json
```

Tester l’écriture sur une copie runtime :

```powershell
pwsh -NoProfile -File .\scripts\Sync-RekordboxSmartPlaylists.ps1 -Mode CopyApply -Apply -Json
```

Appliquer à Rekordbox live exige Rekordbox fermé, une sauvegarde automatique et une confirmation explicite :

```powershell
pwsh -NoProfile -File .\scripts\Sync-RekordboxSmartPlaylists.ps1 -Mode LiveApply -Apply -ConfirmLiveWrite -Json
```

Le mode par défaut ajoute les nouveaux morceaux et conserve les morceaux déjà présents dans la playlist normale. Ajouter `-RemoveStale` seulement si la playlist normale doit devenir un miroir exact de la playlist intelligente.

## `Audit-DjLibraryCleanup.ps1`

Audite en lecture seule les fichiers locaux, les chemins Rekordbox, les doublons binaires exacts et la couverture locale des playlists ciblées. Le script copie `master.db` dans `runtime/`, écrit les rapports sous `reports/`, et ne modifie ni Rekordbox ni les fichiers.

```powershell
pwsh -NoProfile -File .\scripts\Audit-DjLibraryCleanup.ps1 -AudioRoot C:\DJ_Music -AudioHashMode none -Json
```

## `Sync-RekordboxMountedDrivePaths.ps1`

Relie les chemins Rekordbox d’une ancienne racine montée vers la racine active quand le fichier cible existe, par exemple `D:/DJ_Music` vers `C:/DJ_Music`. Toujours valider `CopyApply` avant `LiveApply`.

```powershell
pwsh -NoProfile -File .\scripts\Sync-RekordboxMountedDrivePaths.ps1 -Mode CopyApply -Apply -Json
pwsh -NoProfile -File .\scripts\Sync-RekordboxMountedDrivePaths.ps1 -Mode LiveApply -Apply -ConfirmLiveWrite -Json
```

## `Remove-DjExactDuplicates.ps1`

Nettoie les doublons confirmés par un rapport `local-duplicate-candidates-*.csv` : doublons binaires exacts ou doublons audio décodés exacts, fusion des memberships Rekordbox, réaffectation des historiques de lecture au morceau conservé, fusion des tags utilisateur, suppression de la ligne Rekordbox doublon, puis déplacement du fichier doublon en quarantaine récupérable sous `C:\DJ_Music\_DUPLICATE_QUARANTINE\...`. Toujours valider `CopyApply` avant `LiveApply`.

```powershell
pwsh -NoProfile -File .\scripts\Remove-DjExactDuplicates.ps1 -Mode CopyApply -Apply -DuplicateCsv reports\local-duplicate-candidates-YYYYMMDD-HHMMSS.csv -Json
pwsh -NoProfile -File .\scripts\Remove-DjExactDuplicates.ps1 -Mode LiveApply -Apply -ConfirmLiveWrite -DuplicateCsv reports\local-duplicate-candidates-YYYYMMDD-HHMMSS.csv -Json
```

## `Invoke-DjFileOperation.ps1`

Applique une opération fichier déjà enregistrée dans la base de contrôle. Le script reste en dry-run par défaut : il valide les chemins, l’extension audio, la destination et le hash source, mais ne déplace rien.

```powershell
pwsh -NoProfile -File .\scripts\Invoke-DjFileOperation.ps1 -OperationId "op-..." -Json
pwsh -NoProfile -File .\scripts\Invoke-DjFileOperation.ps1 -OperationId "op-..." -Apply -Json
```

`-Apply` déplace un seul fichier, vérifie le hash après déplacement, écrit un événement dans `operation_event`, puis marque l’action fichier comme `applied`. La répercussion Rekordbox reste séparée.

## `Test-DjDuplicateSanity.ps1`

Compare les lots autorisés avec la bibliothèque et écrit un rapport JSON + CSV sous `reports/`. Le script est en lecture seule.

```powershell
pwsh -NoProfile -File .\scripts\Test-DjDuplicateSanity.ps1
pwsh -NoProfile -File .\scripts\Test-DjDuplicateSanity.ps1 -AudioHash
pwsh -NoProfile -File .\scripts\Test-DjDuplicateSanity.ps1 -Fingerprint
```

`-Fingerprint` exige `fpcalc` / Chromaprint dans le `PATH`.

## `Measure-AuthorizedAudioQuality.ps1`

Mesure les fichiers locaux autorisés avec `ffprobe` : codec, fréquence, bit depth, bitrate, durée et candidat qualité CD.

```powershell
pwsh -NoProfile -File .\scripts\Measure-AuthorizedAudioQuality.ps1 -RootPath "C:\DJ_Music\Playlists\your-playlist"
```

Après conversion Processed_Library_Root, mesurer plutôt la sortie finale :

```powershell
pwsh -NoProfile -File .\scripts\Measure-AuthorizedAudioQuality.ps1 -RootPath "C:\DJ_Music\Processed_Library_Root"
```

## `Audit-DjChromaprintOverlap.ps1`

Audite en lecture seule la sortie Processed_Library_Root avec `fpcalc` / Chromaprint. Le script calcule ou réutilise un cache sous `runtime/`, cherche les empreintes qui se superposent avec un décalage possible, puis écrit les groupes candidats sous `reports/` avec le fichier recommandé à garder selon la règle “qualité CD ou moins, master seulement si aucun autre candidat”. Par défaut, il calcule Chromaprint seulement sur les fichiers dont le nom ressemble déjà à un doublon ; ajouter `-CandidateMode All` pour une passe exhaustive, nettement plus longue.

```powershell
pwsh -NoProfile -File .\scripts\Audit-DjChromaprintOverlap.ps1 -RootPath "C:\DJ_Music\Processed_Library_Root" -Json
pwsh -NoProfile -File .\scripts\Audit-DjChromaprintOverlap.ps1 -RootPath "C:\DJ_Music\Processed_Library_Root" -Limit 100 -Json
```

## `Resolve-DjGenreCandidates.ps1`

Construit des propositions de genre pour les échecs AutoTagger sans modifier les fichiers audio. Le script lit les rapports dérivés AutoTagger, garde les succès historiques par plateforme, peut relire les tags `genre` / `style` déjà présents avec `ffprobe`, et écrit les rapports sous `reports/`.

```powershell
pwsh -NoProfile -File .\scripts\Resolve-DjGenreCandidates.ps1 -Mode Worklist -Json
pwsh -NoProfile -File .\scripts\Resolve-DjGenreCandidates.ps1 -Mode Verify -VerifyAudioTags -Json
pwsh -NoProfile -File .\scripts\Resolve-DjGenreCandidates.ps1 -Mode Resolve -Limit 20 -Sources itunes,deezer -Json
pwsh -NoProfile -File .\scripts\Resolve-DjGenreCandidates.ps1 -Mode Resolve -InputM3uPath reports\genre-resolver-YYYYMMDD-HHMMSS-true-unresolved-missing-existing-genre.m3u -Sources itunes,deezer -Json
pwsh -NoProfile -File .\scripts\Resolve-DjGenreCandidates.ps1 -Mode Resolve -WorklistScope MissingExistingGenre -Sources itunes,deezer -Json
pwsh -NoProfile -File .\scripts\Resolve-DjGenreCandidates.ps1 -Mode AudioReadiness -Json
```

Les sources API et les règles de scraping désactivées par défaut sont dans `config/dj-genre-resolver.sources.json`.
La source `spotify_direct` utilise l’API Web Spotify directement, avec plusieurs requêtes fuzzy artiste/titre, puis les genres d’artistes Spotify comme indice. Elle utilise `SPOTIFY_CLIENT_ID` et `SPOTIFY_CLIENT_SECRET` si ces variables existent, sinon elle relit les identifiants Spotify du `settings.json` local AutoTagger. Elle reste en lecture seule comme les autres sources :

```powershell
pwsh -NoProfile -File .\scripts\Resolve-DjGenreCandidates.ps1 -Mode Resolve -InputM3uPath reports\AutoTagger-platform-coverage-20260526-191348-final-spotify-complete-spotify-error-no-ok.m3u -Sources spotify_direct -Limit 20 -Json
```

Le mode `Resolve` refuse un run API non borné sans `-InputM3uPath`, `-Limit` ou `-WorklistScope`.

## `Start-AutoTaggerCoverageRun.ps1`

Lance AutoTagger en mode serveur avec un runner traçable pour compléter une couverture de plateforme. Par défaut, le script garde `overwrite=false` pour éviter de remplacer les genres existants, mais AutoTagger reste un outil qui écrit dans les fichiers audio. AutoTagger 1.7.0 ne traite pas un `.m3u` passé comme `path` dans cette voie : utiliser un vrai dossier audio.

```powershell
pwsh -NoProfile -File .\scripts\Start-AutoTaggerCoverageRun.ps1 -TargetPath "C:\DJ_Music\Processed_Library_Root\260517_DL" -Platforms spotify -Threads 2 -RunInForeground -Json
```

## `Update-AutoTaggerPlatformCoverage.ps1`

Recalcule la couverture par plateforme depuis les logs JSONL AutoTagger et écrit un résumé + des M3U de reprise sous `reports/`. Les M3U sont des listes de contrôle/reprise, pas des cibles directes AutoTagger dans le mode serveur actuel.

```powershell
pwsh -NoProfile -File .\scripts\Update-AutoTaggerPlatformCoverage.ps1 -Label latest -Json
```

## `Invoke-AutoTaggerSpotifyRetry.ps1`

Contrôleur de reprise Spotify. Il recalcule la couverture, respecte le cooldown Spotify détecté dans les logs, saute si AutoTagger tourne déjà, puis relance une passe `spotify` avec `overwrite=false` quand la limite est expirée. Il cible d’abord `C:\DJ_Music\Processed_Library_Root\_ALL`, puis les fichiers restants un par un si seuls des chemins hors `_ALL` manquent encore.

```powershell
pwsh -NoProfile -File .\scripts\Invoke-AutoTaggerSpotifyRetry.ps1 -Json
pwsh -NoProfile -File .\scripts\Invoke-AutoTaggerSpotifyRetry.ps1 -Apply -Json
```

## `Register-AutoTaggerSpotifyRetryTask.ps1`

Enregistre la tâche planifiée Windows `DJ Library AutoTagger Spotify Retry`, qui appelle le contrôleur de reprise régulièrement en fenêtre cachée et écrit son état sous `runtime/AutoTagger-spotify-retry/`.

```powershell
pwsh -NoProfile -File .\scripts\Register-AutoTaggerSpotifyRetryTask.ps1 -IntervalMinutes 120 -Apply -Json
```

## `Resolve-DjChromaprintDuplicates.ps1`

Applique seulement les doublons Chromaprint qui passent les garde-fous sûrs : similarité de groupe minimale, durée compatible, clé de titre compatible, fichier à garder présent, fichier doublon présent. Le script conserve en priorité la ligne Rekordbox du fichier à garder, fusionne les memberships, réaffecte les historiques de lecture à cette ligne conservée, fusionne les tags utilisateur, supprime les lignes doublons, déplace les fichiers en quarantaine, et convertit les masters haute résolution restants en qualité CD quand le fichier local est le seul exemplaire à conserver.

Le mode `Plan` travaille sur une copie de `master.db`. `CopyApply` valide l’écriture sur une copie. `LiveApply` exige `-Apply -ConfirmLiveWrite`, Rekordbox fermé, une sauvegarde live et des vérifications après coup.

```powershell
pwsh -NoProfile -File .\scripts\Resolve-DjChromaprintDuplicates.ps1 -Mode Plan -ReportCsv reports\chromaprint-overlap-groups-YYYYMMDD-HHMMSS.csv -MinGroupSimilarity 0.98 -DurationToleranceSeconds 1.0 -TitleMatchMode Safe -Json
pwsh -NoProfile -File .\scripts\Resolve-DjChromaprintDuplicates.ps1 -Mode CopyApply -Apply -ReportCsv reports\chromaprint-overlap-groups-YYYYMMDD-HHMMSS.csv -MinGroupSimilarity 0.98 -DurationToleranceSeconds 1.0 -TitleMatchMode Safe -Json
pwsh -NoProfile -File .\scripts\Resolve-DjChromaprintDuplicates.ps1 -Mode LiveApply -Apply -ConfirmLiveWrite -ReportCsv reports\chromaprint-overlap-groups-YYYYMMDD-HHMMSS.csv -MinGroupSimilarity 0.98 -DurationToleranceSeconds 1.0 -TitleMatchMode Safe -Json
```

## `Import-AuthorizedAudio.ps1`

Prépare un plan d’import depuis une source autorisée vers `C:\DJ_Music\Playlists\ALL_TRACKS`.

Par défaut, le script est un dry-run. Il ne copie ou déplace les fichiers qu’avec `-Apply`.

## `New-PlaylistJob.ps1`

Crée une fiche de suivi pour une playlist. Il sert à cadrer la voie officielle Rekordbox/Serato, la voie `tiddl` locale explicite, ou l’import de fichiers locaux autorisés.

```powershell
pwsh -NoProfile -File .\scripts\New-PlaylistJob.ps1 -Name "nom-playlist" -Source tidal-rekordbox-official -PlaylistUrl "https://tidal.com/browse/playlist/..." -CreateFolders
pwsh -NoProfile -File .\scripts\New-PlaylistJob.ps1 -Name "nom-playlist" -Source tidal-tiddl-local -PlaylistUrl "https://tidal.com/playlist/..." -CreateFolders
```

## `New-PlaylistAcquisitionPlan.ps1`

Crée un plan d’acquisition pour une playlist dont la destination finale est une clé USB Rekordbox/Serato. La playlist TIDAL est la référence ; `tiddl` peut remplir le dossier local de playlist quand User le demande explicitement, puis le repo reprend avec les contrôles locaux.

```powershell
pwsh -NoProfile -File .\scripts\New-PlaylistAcquisitionPlan.ps1 -PlaylistUrl "https://tidal.com/playlist/..." -CreateFolders
```
