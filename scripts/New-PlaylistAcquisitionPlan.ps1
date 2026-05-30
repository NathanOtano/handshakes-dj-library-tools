[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $PlaylistUrl,

    [string] $Name,
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [switch] $CreateFolders,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configFullPath = [System.IO.Path]::GetFullPath($ConfigPath)

function Test-IsAbsolutePath {
    param([Parameter(Mandatory)][string] $Path)
    $root = [System.IO.Path]::GetPathRoot($Path)
    return -not [string]::IsNullOrWhiteSpace($root)
}

function Resolve-ConfiguredPath {
    param([AllowNull()][string] $Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    if (Test-IsAbsolutePath -Path $Path) {
        return [System.IO.Path]::GetFullPath($Path)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
}

function ConvertTo-Slug {
    param([Parameter(Mandatory)][string] $Value)
    $normalized = $Value.Trim().ToLowerInvariant()
    $slug = [regex]::Replace($normalized, '[^a-z0-9]+', '-').Trim('-')
    if ([string]::IsNullOrWhiteSpace($slug)) {
        throw "Value cannot be converted to a safe folder name."
    }
    return $slug
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Content
    )
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Get-TidalPlaylistId {
    param([Parameter(Mandatory)][string] $Url)

    $match = [regex]::Match($Url, '(?i)(?:tidal\.com/(?:browse/)?playlist/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')
    if ($match.Success) {
        return $match.Groups[1].Value.ToLowerInvariant()
    }

    return $null
}

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}

$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$libraryRoot = Resolve-ConfiguredPath $config.libraryRoot
$intakeRoot = Resolve-ConfiguredPath $config.intakeRoot
$playlistId = Get-TidalPlaylistId -Url $PlaylistUrl

if ([string]::IsNullOrWhiteSpace($Name)) {
    if (-not [string]::IsNullOrWhiteSpace($playlistId)) {
        $Name = "tidal-$($playlistId.Substring(0, 8))"
    }
    else {
        $Name = "playlist-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    }
}

$slug = ConvertTo-Slug -Value $Name
$jobRoot = Join-Path $repoRoot (Join-Path 'staging\playlists' $slug)
$trackCsvPath = Join-Path $jobRoot 'tracks.csv'
$jobPath = Join-Path $jobRoot 'acquisition-plan.json'
$readmePath = Join-Path $jobRoot 'README.md'
$authorizedInboxPath = [System.IO.Path]::Combine($intakeRoot, $slug)

New-Item -ItemType Directory -Path $jobRoot -Force | Out-Null

$trackCsvHeader = 'position,tidal_track_url,title,artists,version,isrc,duration,preferred_source,purchase_url,authorized_file_path,acquisition_status,import_status,notes'
if (-not (Test-Path -LiteralPath $trackCsvPath -PathType Leaf)) {
    Write-Utf8NoBom -Path $trackCsvPath -Content ($trackCsvHeader + [Environment]::NewLine)
}

$importPreviewCommand = "pwsh -NoProfile -File .\scripts\Import-AuthorizedAudio.ps1 -SourcePath `"$authorizedInboxPath`""
$importApplyCommand = "$importPreviewCommand -Apply"

$plan = [ordered]@{
    version = '0.1.0'
    kind = 'playlist-acquisition-plan'
    name = $Name
    slug = $slug
    sourceService = 'TIDAL'
    playlistUrl = $PlaylistUrl
    playlistId = $playlistId
    createdAt = (Get-Date).ToString('o')
    target = [ordered]@{
        libraryRoot = $libraryRoot
        intakePath = $authorizedInboxPath
        finalUse = 'rekordbox-serato-usb-local-files'
    }
    files = [ordered]@{
        trackCsv = $trackCsvPath
        readme = $readmePath
    }
    allowedWorkflow = @(
        'Use the TIDAL playlist as a reference list.',
        'Fill tracks.csv with track metadata and legal acquisition status.',
        'When User chooses the tiddl workflow, run tiddl explicitly into the playlist folder under C:\DJ_Music\Playlist.',
        'Keep tiddl files flat in that playlist folder, named Artist - Title or Artist - Title (Version), matching the Processed_Library_Root convention.',
        'Place authorized files in the playlist folder.',
        'Ignore Rekordbox tidal:tracks:* entries when checking local availability.',
        'Run Import-AuthorizedAudio.ps1 to copy them into ALL_TRACKS.',
        'Run Processed_Library_Root / Processed_Library_Root Notes.',
        'Run AutoTagger.',
        'Export local files from rekordbox to USB.'
    )
    blockedWorkflow = @(
        'No token, cookie, account secret, or encrypted stream capture in this repo.',
        'No implicit or recurring TIDAL downloader launched by repo scripts.',
        'No tiddl output written directly to ALL_TRACKS or Processed_Library_Root.',
        'No audio file committed to Git.'
    )
    commands = [ordered]@{
        importPreview = $importPreviewCommand
        importApply = $importApplyCommand
    }
}

Write-Utf8NoBom -Path $jobPath -Content (($plan | ConvertTo-Json -Depth 8) + [Environment]::NewLine)

$readme = @"
# $Name

Playlist TIDAL : $PlaylistUrl

Playlist ID : $playlistId

Bibliotheque cible : `$libraryRoot`

Lot local de playlist : `$authorizedInboxPath`

## Ce que ce plan fait

- suit la playlist comme reference de preparation ;
- garde un CSV de tracks a completer ;
- prepare le dossier de playlist ou deposer ou recuperer les fichiers locaux ;
- donne les commandes d'import vers `ALL_TRACKS`, puis le flux Processed_Library_Root et AutoTagger.

## Ce que ce plan ne fait pas

- pas d'appel TIDAL implicite ou recurrent depuis les scripts du repo ;
- pas de jeton, cookie, session ou secret TIDAL dans le repo ;
- pas de sortie `tiddl` directement dans `ALL_TRACKS` ou `Processed_Library_Root` ;
- pas de sous-dossiers album/artiste pour `tiddl` : un dossier par playlist, fichiers a plat ;
- pas de fichier audio dans Git.

## Commandes

Preview import :

```powershell
$importPreviewCommand
```

Apply import :

```powershell
$importApplyCommand
```

"@

Write-Utf8NoBom -Path $readmePath -Content $readme

$folderCreated = $false
$folderStatus = 'not-requested'
if ($CreateFolders) {
    $authorizedInboxParent = [System.IO.Path]::GetDirectoryName($authorizedInboxPath)
    if (Test-Path -LiteralPath $authorizedInboxParent -PathType Container) {
        New-Item -ItemType Directory -Path $authorizedInboxPath -Force | Out-Null
        $folderCreated = $true
        $folderStatus = 'created-or-existing'
    }
    else {
        $folderStatus = 'parent-missing'
        Write-Warning "Playlist parent folder not found: $authorizedInboxParent"
    }
}

$result = [pscustomobject]@{
    name = $Name
    slug = $slug
    playlistUrl = $PlaylistUrl
    playlistId = $playlistId
    jobPath = $jobPath
    readmePath = $readmePath
    trackCsvPath = $trackCsvPath
    intakePath = $authorizedInboxPath
    intakeFolderCreated = $folderCreated
    intakeFolderStatus = $folderStatus
}

if ($Json) {
    $result | ConvertTo-Json -Depth 5
    return
}

$result
