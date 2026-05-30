[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [switch] $CreateMissingFolders,
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

function Test-ConfiguredTool {
    param(
        [Parameter(Mandatory)][string] $Key,
        [Parameter(Mandatory)][string] $DisplayName,
        [AllowNull()][string] $ConfiguredPath,
        [Parameter(Mandatory)][string[]] $CommandNames
    )

    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        $toolPath = Resolve-ConfiguredPath -Path $ConfiguredPath
        return [pscustomobject]@{
            key = $Key
            displayName = $DisplayName
            found = Test-Path -LiteralPath $toolPath -PathType Leaf
            source = 'configuredPath'
            path = $toolPath
        }
    }

    foreach ($commandName in $CommandNames) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return [pscustomobject]@{
                key = $Key
                displayName = $DisplayName
                found = $true
                source = 'PATH'
                path = $command.Source
            }
        }
    }

    return [pscustomobject]@{
        key = $Key
        displayName = $DisplayName
        found = $false
        source = 'not-found'
        path = $null
    }
}

function Get-LibraryRootCandidate {
    param(
        [AllowNull()][string] $PlaylistRoot,
        [Parameter(Mandatory)][string[]] $AudioExtensions
    )

    $root = Resolve-ConfiguredPath $PlaylistRoot
    if ([string]::IsNullOrWhiteSpace($root) -or -not (Test-Path -LiteralPath $root -PathType Container)) {
        return @()
    }

    Get-ChildItem -LiteralPath $root -Force -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name |
        ForEach-Object {
            $audioCount = 0
            foreach ($extension in $AudioExtensions) {
                $matches = @(Get-ChildItem -LiteralPath $_.FullName -File -Filter "*$extension" -ErrorAction SilentlyContinue | Select-Object -First 3)
                $audioCount += $matches.Count
            }

            [pscustomobject]@{
                name = $_.Name
                path = $_.FullName
                topLevelAudioSampleCount = $audioCount
                lastWriteTime = $_.LastWriteTime.ToString('o')
            }
        }
}

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}

$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json

$folderSpecs = @(
    [pscustomobject]@{ key = 'musicRoot'; role = 'Racine musique'; path = Resolve-ConfiguredPath $config.musicRoot; required = $true; canCreate = $false },
    [pscustomobject]@{ key = 'playlistRoot'; role = 'Racine playlists'; path = Resolve-ConfiguredPath $config.playlistRoot; required = $false; canCreate = $true },
    [pscustomobject]@{ key = 'libraryRoot'; role = 'Depot source ALL_TRACKS'; path = Resolve-ConfiguredPath $config.libraryRoot; required = $true; canCreate = $false },
    [pscustomobject]@{ key = 'intakeRoot'; role = 'Racine lots playlists et tiddl'; path = Resolve-ConfiguredPath $config.intakeRoot; required = $false; canCreate = $true },
    [pscustomobject]@{ key = 'postProcessed_Library_RootRoot'; role = 'Sortie convertie Processed_Library_Root'; path = Resolve-ConfiguredPath $config.postProcessed_Library_RootRoot; required = $false; canCreate = $true },
    [pscustomobject]@{ key = 'reportsRoot'; role = 'Rapports locaux'; path = Resolve-ConfiguredPath $config.reportsRoot; required = $false; canCreate = $true }
)

$folders = foreach ($folder in $folderSpecs) {
    $exists = Test-Path -LiteralPath $folder.path -PathType Container
    $created = $false

    if (-not $exists -and $CreateMissingFolders -and $folder.canCreate) {
        New-Item -ItemType Directory -Path $folder.path -Force | Out-Null
        $exists = $true
        $created = $true
    }

    [pscustomobject]@{
        key = $folder.key
        role = $folder.role
        path = $folder.path
        exists = $exists
        required = $folder.required
        created = $created
    }
}

$tools = @(
    Test-ConfiguredTool -Key 'processed_library_root' -DisplayName $config.toolHints.processed_library_root.displayName -ConfiguredPath $config.toolHints.processed_library_root.configuredPath -CommandNames @('Processed_Library_Root Notes', 'Processed_Library_Root Notes.exe', 'Processed_Library_RootNotes', 'Processed_Library_RootNotes.exe')
    Test-ConfiguredTool -Key 'AutoTagger' -DisplayName $config.toolHints.AutoTagger.displayName -ConfiguredPath $config.toolHints.AutoTagger.configuredPath -CommandNames @('AutoTagger', 'AutoTagger.exe', 'AutoTagger', 'AutoTagger.exe')
    Test-ConfiguredTool -Key 'fpcalc' -DisplayName $config.toolHints.chromaprint.displayName -ConfiguredPath $config.toolHints.chromaprint.configuredPath -CommandNames @('fpcalc', 'fpcalc.exe')
    Test-ConfiguredTool -Key 'tidalDl' -DisplayName 'tidal-dl (documented only)' -ConfiguredPath $null -CommandNames @('tidal-dl', 'tidal-dl.exe')
)

$blockingFolders = @($folders | Where-Object { $_.required -and -not $_.exists })
$libraryRootCandidates = @()
if ($blockingFolders | Where-Object { $_.key -eq 'libraryRoot' }) {
    $libraryRootCandidates = @(Get-LibraryRootCandidate -PlaylistRoot $config.playlistRoot -AudioExtensions @($config.audioExtensions))
}

$result = [pscustomobject]@{
    repoRoot = $repoRoot
    configPath = $configFullPath
    generatedAt = (Get-Date).ToString('o')
    ready = ($blockingFolders.Count -eq 0)
    folders = $folders
    libraryRootCandidates = $libraryRootCandidates
    tools = $tools
    guardrails = [pscustomobject]@{
        audioFilesIgnoredByGit = $true
        tidalThirdPartyDownloaderEnabledByDefault = $false
        importRequiresApply = $true
    }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 8
    return
}

$result
