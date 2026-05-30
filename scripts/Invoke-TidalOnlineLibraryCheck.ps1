[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\tidal-online-check.json'),
    [string] $PathsConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $ReportsRoot = (Join-Path $PSScriptRoot '..\reports'),
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\tidal-online-check'),
    [string] $RekordboxRoot = (Join-Path $env:APPDATA 'Pioneer\rekordbox'),
    [string] $TiddlPython = (Join-Path $env:APPDATA 'uv\tools\tiddl\Scripts\python.exe'),
    [string] $TiddlExe = (Join-Path $HOME '.local\bin\tiddl.exe'),
    [string] $Since = '',
    [ValidateSet('', 'low', 'normal', 'high', 'max')]
    [string] $TrackQuality = '',
    [ValidateSet('', 'none', 'allow', 'only')]
    [string] $DolbyAtmos = '',
    [switch] $ApplyDownloads,
    [switch] $ApplyPlaylistMissingDownloads,
    [switch] $UpdateState,
    [switch] $SkipRekordbox,
    [switch] $KeepRuntime,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$reportsFullPath = [System.IO.Path]::GetFullPath($ReportsRoot)
$configFullPath = [System.IO.Path]::GetFullPath($ConfigPath)
$pathsConfigFullPath = [System.IO.Path]::GetFullPath($PathsConfigPath)
$tidalHelper = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'tidal_online_library_check.py'))
$rekordboxHelper = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'rekordbox_local_playlist_export.py'))
$statePath = Join-Path $runtimeFullPath 'state.json'

function Write-Result {
    param([Parameter(Mandatory)] [pscustomobject] $Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 30
        return
    }
    $Result
}

function Assert-UnderRepo {
    param([Parameter(Mandatory)] [string] $Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing repo runtime/report path outside repo: $resolved"
    }
    return $resolved
}

function Copy-RekordboxDatabaseSet {
    param(
        [Parameter(Mandatory)] [string] $SourceRoot,
        [Parameter(Mandatory)] [string] $DestinationRoot
    )
    New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
    $names = @('master.db', 'master.db-wal', 'master.db-shm', 'masterPlaylists6.xml', 'automixPlaylist6.xml')
    foreach ($name in $names) {
        $src = Join-Path $SourceRoot $name
        $dst = Join-Path $DestinationRoot $name
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
            [pscustomobject]@{ name = $name; copied = $false; reason = 'missing'; source = $src; dest = $dst; bytes = 0; sha256 = $null }
            continue
        }

        $inputStream = $null
        $outputStream = $null
        try {
            $inputStream = [System.IO.File]::Open($src, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $outputStream = [System.IO.File]::Open($dst, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $inputStream.CopyTo($outputStream)
            $outputStream.Dispose()
            $outputStream = $null
            $hash = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash
            [pscustomobject]@{ name = $name; copied = $true; reason = ''; source = $src; dest = $dst; bytes = (Get-Item -LiteralPath $dst).Length; sha256 = $hash }
        }
        catch {
            [pscustomobject]@{ name = $name; copied = $false; reason = $_.Exception.Message; source = $src; dest = $dst; bytes = 0; sha256 = $null }
        }
        finally {
            if ($null -ne $outputStream) { $outputStream.Dispose() }
            if ($null -ne $inputStream) { $inputStream.Dispose() }
        }
    }
}

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}
if (-not (Test-Path -LiteralPath $pathsConfigFullPath -PathType Leaf)) {
    throw "Paths config not found: $pathsConfigFullPath"
}
if (-not (Test-Path -LiteralPath $tidalHelper -PathType Leaf)) {
    throw "TIDAL helper not found: $tidalHelper"
}
if (-not (Test-Path -LiteralPath $TiddlPython -PathType Leaf)) {
    throw "tiddl Python runtime not found: $TiddlPython"
}
if (-not (Test-Path -LiteralPath $TiddlExe -PathType Leaf)) {
    throw "tiddl executable not found: $TiddlExe"
}

$runtimeFullPath = Assert-UnderRepo -Path $runtimeFullPath
$reportsFullPath = Assert-UnderRepo -Path $reportsFullPath
New-Item -ItemType Directory -Path $runtimeFullPath -Force | Out-Null
New-Item -ItemType Directory -Path $reportsFullPath -Force | Out-Null

$warnings = New-Object System.Collections.Generic.List[string]
$rekordboxExportPath = ''
$rekordboxSnapshot = $null

if (-not $SkipRekordbox) {
    $pyrekordboxPython = Join-Path $repoRoot 'runtime\rekordbox-controlled-write\pyrekordbox-venv\Scripts\python.exe'
    $masterPath = Join-Path $RekordboxRoot 'master.db'
    if (-not (Test-Path -LiteralPath $pyrekordboxPython -PathType Leaf)) {
        $warnings.Add('pyrekordbox runtime is missing; Rekordbox playlist coverage was skipped.')
    }
    elseif (-not (Test-Path -LiteralPath $masterPath -PathType Leaf)) {
        $warnings.Add("Rekordbox master.db not found; Rekordbox playlist coverage was skipped: $masterPath")
    }
    elseif (-not (Test-Path -LiteralPath $rekordboxHelper -PathType Leaf)) {
        $warnings.Add("Rekordbox helper not found; Rekordbox playlist coverage was skipped: $rekordboxHelper")
    }
    else {
        $snapshotRoot = Assert-UnderRepo -Path (Join-Path $runtimeFullPath "rekordbox-snapshot\$timestamp")
        $copies = @(Copy-RekordboxDatabaseSet -SourceRoot $RekordboxRoot -DestinationRoot $snapshotRoot)
        $masterCopy = Join-Path $snapshotRoot 'master.db'
        $rekordboxSnapshot = [pscustomobject]@{
            path = $snapshotRoot
            copies = $copies
        }
        if (Test-Path -LiteralPath $masterCopy -PathType Leaf) {
            $env:PYTHONIOENCODING = 'utf-8'
            $rawExport = & $pyrekordboxPython $rekordboxHelper --master $masterCopy --db-dir $snapshotRoot
            $exitCode = $LASTEXITCODE
            $exportText = $rawExport | Out-String
            $exportPayload = $exportText | ConvertFrom-Json
            if ($exitCode -eq 0 -and [bool] $exportPayload.success) {
                $rekordboxExportPath = Join-Path $snapshotRoot 'rekordbox-local-playlists.json'
                $exportText | Set-Content -LiteralPath $rekordboxExportPath -Encoding UTF8
            }
            else {
                $warnings.Add("Rekordbox export failed; playlist coverage was skipped: $($exportPayload.error)")
            }
        }
        else {
            $warnings.Add("Rekordbox master copy missing; playlist coverage was skipped: $masterCopy")
        }
    }
}

$arguments = @(
    $tidalHelper,
    '--config', $configFullPath,
    '--paths-config', $pathsConfigFullPath,
    '--runtime-root', $runtimeFullPath,
    '--reports-root', $reportsFullPath,
    '--state-path', $statePath,
    '--timestamp', $timestamp,
    '--tiddl-exe', $TiddlExe
)
if (-not [string]::IsNullOrWhiteSpace($Since)) {
    $arguments += @('--since', $Since)
}
if (-not [string]::IsNullOrWhiteSpace($rekordboxExportPath)) {
    $arguments += @('--rekordbox-export', $rekordboxExportPath)
}
if ($ApplyDownloads) {
    $arguments += '--apply-downloads'
}
if ($ApplyPlaylistMissingDownloads) {
    $arguments += '--apply-playlist-missing-downloads'
}
if (-not [string]::IsNullOrWhiteSpace($TrackQuality)) {
    $arguments += @('--track-quality', $TrackQuality)
}
if (-not [string]::IsNullOrWhiteSpace($DolbyAtmos)) {
    $arguments += @('--dolby-atmos', $DolbyAtmos)
}
if ($UpdateState) {
    $arguments += '--update-state'
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:NO_COLOR = '1'
$raw = & $TiddlPython @arguments
$exit = $LASTEXITCODE
$payloadText = $raw | Out-String
$payload = $payloadText | ConvertFrom-Json

$result = [pscustomobject]@{
    success = ($exit -eq 0 -and [bool] $payload.success)
    mode = if ($ApplyDownloads -and $ApplyPlaylistMissingDownloads) {
        'apply-liked-and-playlist-missing-downloads'
    }
    elseif ($ApplyDownloads) {
        'apply-liked-downloads'
    }
    elseif ($ApplyPlaylistMissingDownloads) {
        'apply-playlist-missing-downloads'
    }
    else {
        'plan'
    }
    generatedAt = (Get-Date).ToString('o')
    warnings = @($warnings)
    rekordboxSnapshot = $rekordboxSnapshot
    tidal = $payload
}

Write-Result -Result $result
if (-not $result.success) { exit 1 }
