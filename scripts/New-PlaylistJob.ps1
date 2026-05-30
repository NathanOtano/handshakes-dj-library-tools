[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $Name,

    [ValidateSet('tidal-rekordbox-official', 'tidal-serato-streaming', 'tidal-tiddl-local', 'authorized-local-files')]
    [string] $Source = 'tidal-rekordbox-official',

    [string] $PlaylistUrl,
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
        throw "Playlist name cannot be converted to a safe folder name."
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

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}

$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$slug = ConvertTo-Slug -Value $Name
$jobRoot = Join-Path $repoRoot (Join-Path 'staging\playlists' $slug)
$intakeRoot = Resolve-ConfiguredPath $config.intakeRoot
$playlistIntakePath = [System.IO.Path]::Combine($intakeRoot, $slug)
$libraryRoot = Resolve-ConfiguredPath $config.libraryRoot

$sourceGuidance = switch ($Source) {
    'tidal-rekordbox-official' {
        'Use TIDAL DJ Extension inside Rekordbox. Store Offline is managed by Rekordbox, not by this repo.'
    }
    'tidal-serato-streaming' {
        'Use TIDAL DJ Extension inside Serato. Serato requires an active connection for TIDAL playback.'
    }
    'tidal-tiddl-local' {
        'Use the TIDAL playlist as the reference, run tiddl explicitly into the local playlist folder, then treat only local files as available for USB/Rekordbox export.'
    }
    'authorized-local-files' {
        'Use local files from purchases, promos, record pools, productions, or another source with clear local-retention rights.'
    }
}

New-Item -ItemType Directory -Path $jobRoot -Force | Out-Null

$job = [ordered]@{
    version = '0.1.0'
    name = $Name
    slug = $slug
    source = $Source
    playlistUrl = $PlaylistUrl
    createdAt = (Get-Date).ToString('o')
    libraryRoot = $libraryRoot
    intakePath = $playlistIntakePath
    localGuardrail = 'TIDAL playlist entries can be a reference, but Rekordbox tidal:tracks:* entries are not local files. This repo does not store TIDAL secrets, downloader caches, or music files.'
    guidance = $sourceGuidance
    nextSteps = @(
        'Confirm the playlist source and target DJ app.',
        'For a USB-local workflow, use tiddl only as an explicit local command into the configured playlist folder.',
        'Keep tiddl output flat: one playlist folder, then Artist - Title files at the folder root.',
        'Ignore Rekordbox tidal:tracks:* entries when checking local availability.',
        'Run Processed_Library_Root / Processed_Library_Root Notes after local import when applicable.',
        'Run OneTagger for genre and metadata cleanup.'
    )
}

$jobPath = Join-Path $jobRoot 'playlist-job.json'
$readmePath = Join-Path $jobRoot 'README.md'
$jobJson = $job | ConvertTo-Json -Depth 8
Write-Utf8NoBom -Path $jobPath -Content ($jobJson + [Environment]::NewLine)

$readme = @"
# $Name

Source : `$Source`

Playlist : $PlaylistUrl

Bibliotheque cible : `$libraryRoot`

Lot local de playlist : `$playlistIntakePath`

## Garde-fou

Une playlist TIDAL peut servir de reference. Une entree Rekordbox `tidal:tracks:*` ne compte jamais comme fichier local disponible pour l'export USB.

Si la source est `tidal-tiddl-local`, executer `tiddl` explicitement vers le dossier de playlist ci-dessus, avec le modele `Artiste - Titre` compatible Processed_Library_Root, puis reprendre avec les controles locaux du repo. Ne stocker ni secrets TIDAL, ni cache downloader, ni fichiers audio dans Git.

## Prochaine action

$sourceGuidance

"@

Write-Utf8NoBom -Path $readmePath -Content $readme

$folderCreated = $false
$folderStatus = 'not-requested'
if ($CreateFolders) {
    $playlistIntakeParent = [System.IO.Path]::GetDirectoryName($playlistIntakePath)
    if (Test-Path -LiteralPath $playlistIntakeParent -PathType Container) {
        New-Item -ItemType Directory -Path $playlistIntakePath -Force | Out-Null
        $folderCreated = $true
        $folderStatus = 'created-or-existing'
    }
    else {
        $folderStatus = 'parent-missing'
        Write-Warning "Playlist parent folder not found: $playlistIntakeParent"
    }
}

$result = [pscustomobject]@{
    name = $Name
    slug = $slug
    source = $Source
    playlistUrl = $PlaylistUrl
    jobPath = $jobPath
    readmePath = $readmePath
    intakePath = $playlistIntakePath
    intakeFolderCreated = $folderCreated
    intakeFolderStatus = $folderStatus
}

if ($Json) {
    $result | ConvertTo-Json -Depth 4
    return
}

$result
