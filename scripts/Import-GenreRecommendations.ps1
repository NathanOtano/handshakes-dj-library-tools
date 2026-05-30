[CmdletBinding()]
param(
    [ValidateSet('Plan', 'CopyApply', 'LiveApply')]
    [string] $Mode = 'Plan',

    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $RekordboxRoot = (Join-Path $env:APPDATA 'Pioneer\rekordbox'),
    [string] $MasterDbPath = '',
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\genre-recommendation-apply'),
    [Parameter(Mandatory)][string] $InputCsv,
    [ValidateSet('low', 'medium', 'high')]
    [string] $ConfidenceCutoff = 'low',
    [string] $ReportsRoot,
    [int] $Limit = 0,

    [switch] $IncludeReview,
    [switch] $ExcludeToolsSamples,
    [switch] $OverwriteExisting,
    [switch] $IncludeStreaming,
    [switch] $ApplyFileTags,
    [string] $FfmpegPath,
    [string] $FfprobePath,

    [switch] $PrepareRuntime,
    [switch] $Apply,
    [switch] $ConfirmLiveWrite,
    [switch] $KeepRuntime,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'genre_recommendation_apply.py'))
$repoRootLower = $repoRoot.ToLowerInvariant()

function Write-Result {
    param([Parameter(Mandatory)] [pscustomobject] $Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 30
        return
    }
    $Result
}

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

function Assert-UnderRepoRuntime {
    param([Parameter(Mandatory)] [string] $Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing runtime path outside repo: $resolved"
    }
    return $resolved
}

function Get-RekordboxProcess {
    $names = @('rekordbox', 'rekordboxAgent', 'rbinit', 'rbcloudagent')
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $names -contains $_.ProcessName } |
        Select-Object ProcessName, Id, Path
}

function Resolve-PythonRuntime {
    $venvRoot = Assert-UnderRepoRuntime -Path (Join-Path $repoRoot 'runtime\rekordbox-controlled-write\pyrekordbox-venv')
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        $python = Get-Command py -ErrorAction SilentlyContinue
    }
    if ($null -eq $python) {
        throw 'Python 3 is required.'
    }

    $probe = & $python.Source -c "import importlib.util; print('yes' if importlib.util.find_spec('pyrekordbox') and importlib.util.find_spec('sqlcipher3') else 'no')"
    if ($LASTEXITCODE -eq 0 -and ($probe | Select-Object -Last 1) -eq 'yes') {
        return $python.Source
    }

    if (-not $PrepareRuntime) {
        throw 'pyrekordbox + sqlcipher3 are required. Re-run with -PrepareRuntime to create an ignored repo-local venv.'
    }

    New-Item -ItemType Directory -Path $venvRoot -Force | Out-Null
    & $python.Source -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create pyrekordbox virtual environment.'
    }
    & $venvPython -m pip install --upgrade pip --quiet
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to upgrade pip in pyrekordbox virtual environment.'
    }
    & $venvPython -m pip install pyrekordbox --quiet
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install pyrekordbox in repo-local virtual environment.'
    }
    return $venvPython
}

function Resolve-Tool {
    param(
        [string] $ExplicitPath,
        [string] $ConfiguredPath,
        [Parameter(Mandatory)] [string] $CommandName,
        [switch] $Optional
    )

    foreach ($candidate in @($ExplicitPath, $ConfiguredPath)) {
        $resolved = Resolve-ConfiguredPath $candidate
        if (-not [string]::IsNullOrWhiteSpace($resolved) -and (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            return $resolved
        }
    }

    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    if ($Optional) {
        return $null
    }

    throw "$CommandName not found. Configure it in config or pass an explicit path."
}

function Copy-RekordboxDatabaseSet {
    param([Parameter(Mandatory)] [string] $DestinationRoot)
    New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
    $names = @('master.db', 'master.db-wal', 'master.db-shm', 'masterPlaylists6.xml', 'automixPlaylist6.xml')
    foreach ($name in $names) {
        $src = Join-Path $RekordboxRoot $name
        $dst = Join-Path $DestinationRoot $name
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
            [pscustomobject]@{
                name = $name
                copied = $false
                reason = 'missing'
                source = $src
                dest = $dst
                bytes = 0
                sha256 = $null
            }
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
            [pscustomobject]@{
                name = $name
                copied = $true
                reason = ''
                source = $src
                dest = $dst
                bytes = (Get-Item -LiteralPath $dst).Length
                sha256 = $hash
            }
        }
        catch {
            [pscustomobject]@{
                name = $name
                copied = $false
                reason = $_.Exception.Message
                source = $src
                dest = $dst
                bytes = 0
                sha256 = $null
            }
        }
        finally {
            if ($null -ne $outputStream) { $outputStream.Dispose() }
            if ($null -ne $inputStream) { $inputStream.Dispose() }
        }
    }
}

function Invoke-RecommendationHelper {
    param(
        [Parameter(Mandatory)] [string] $PythonPath,
        [Parameter(Mandatory)] [string] $DatabasePath,
        [Parameter(Mandatory)] [string] $DatabaseDir,
        [Parameter(Mandatory)] [string] $RuntimeMode
    )

    $arguments = @(
        $helperPath,
        '--repo-root', $repoRoot,
        '--config', $configFullPath,
        '--master', $DatabasePath,
        '--db-dir', $DatabaseDir,
        '--input-csv', $inputFullPath,
        '--runtime-mode', $RuntimeMode.ToLowerInvariant(),
        '--confidence-cutoff', $ConfidenceCutoff.ToLowerInvariant(),
        '--reports-root', $reportsRoot
    )

    if ($Limit -gt 0) {
        $arguments += @('--limit', $Limit.ToString([System.Globalization.CultureInfo]::InvariantCulture))
    }
    if ($IncludeReview) {
        $arguments += '--include-review'
    }
    if ($ExcludeToolsSamples) {
        $arguments += '--exclude-tools-samples'
    }
    if ($OverwriteExisting) {
        $arguments += '--overwrite-existing'
    }
    if ($IncludeStreaming) {
        $arguments += '--include-streaming'
    }
    if ($Apply) {
        $arguments += '--apply'
    }
    if ($ApplyFileTags) {
        $arguments += '--apply-file-tags'
        if (-not [string]::IsNullOrWhiteSpace($ffmpeg)) {
            $arguments += @('--ffmpeg', $ffmpeg)
        }
        if (-not [string]::IsNullOrWhiteSpace($ffprobe)) {
            $arguments += @('--ffprobe', $ffprobe)
        }
    }

    $env:PYTHONIOENCODING = 'utf-8'
    $raw = & $PythonPath @arguments
    $exit = $LASTEXITCODE
    $payload = $raw | Out-String
    if ($exit -ne 0) {
        throw "genre_recommendation_apply.py failed with code ${exit}: ${payload}"
    }

    $parsed = $payload | ConvertFrom-Json
    return [pscustomobject]@{ exitCode = $exit; payload = $parsed }
}

if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Helper not found: $helperPath"
}

$configFullPath = Resolve-ConfiguredPath $ConfigPath
if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}
if (-not (Test-Path -LiteralPath $InputCsv -PathType Leaf)) {
    throw "InputCsv not found: $InputCsv"
}

$runtimeFullPath = Assert-UnderRepoRuntime -Path $runtimeFullPath
$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$reportsRoot = Resolve-ConfiguredPath $ReportsRoot
if ([string]::IsNullOrWhiteSpace($reportsRoot)) {
    $reportsRoot = Resolve-ConfiguredPath $config.reportsRoot
    if ([string]::IsNullOrWhiteSpace($reportsRoot)) {
        $reportsRoot = Join-Path $repoRoot 'reports'
    }
}

New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $runtimeFullPath -Force | Out-Null
$inputFullPath = Resolve-ConfiguredPath $InputCsv

if ([string]::IsNullOrWhiteSpace($MasterDbPath)) {
    $masterPath = Join-Path $RekordboxRoot 'master.db'
} else {
    $masterPath = Resolve-ConfiguredPath $MasterDbPath
}

if (-not (Test-Path -LiteralPath $RekordboxRoot -PathType Container)) {
    throw "RekordboxRoot folder not found: $RekordboxRoot"
}
if (-not (Test-Path -LiteralPath $masterPath -PathType Leaf)) {
    throw "master.db not found: $masterPath"
}

$ffmpeg = Resolve-Tool -ExplicitPath $FfmpegPath -ConfiguredPath $null -CommandName 'ffmpeg' -Optional
$ffprobe = Resolve-Tool -ExplicitPath $FfprobePath -ConfiguredPath $null -CommandName 'ffprobe' -Optional
if ($ApplyFileTags -and [string]::IsNullOrWhiteSpace($ffmpeg)) {
    throw 'ffmpeg is required for -ApplyFileTags.'
}
if ($ApplyFileTags -and [string]::IsNullOrWhiteSpace($ffprobe)) {
    throw 'ffprobe is required for -ApplyFileTags.'
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$rekordboxProcesses = @(Get-RekordboxProcess)
$warnings = New-Object System.Collections.Generic.List[string]
if ($rekordboxProcesses.Count -gt 0) {
    $warnings.Add('Rekordbox-related processes are running. LiveApply is blocked while they are running.')
}

$pythonPath = Resolve-PythonRuntime

if ($Mode -eq 'Plan' -or ($Mode -eq 'LiveApply' -and -not $Apply)) {
    $planRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "plan\$timestamp")
    $copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $planRoot)
    $masterCopy = Join-Path $planRoot 'master.db'
    if (-not (Test-Path -LiteralPath $masterCopy -PathType Leaf)) {
        throw "master.db copy missing: $masterCopy"
    }

    $helper = Invoke-RecommendationHelper -PythonPath $pythonPath -DatabasePath $masterCopy -DatabaseDir $planRoot -RuntimeMode 'plan'
    if (-not $KeepRuntime) {
        Remove-Item -LiteralPath $planRoot -Recurse -Force
    }

    $result = [pscustomobject]@{
        mode = $Mode
        status = 'plan'
        success = ($helper.exitCode -eq 0 -and [bool] $helper.payload.success)
        generatedAt = (Get-Date).ToString('o')
        rekordboxRoot = $RekordboxRoot
        runtimeRoot = $planRoot
        runtimeRootExistsAfterCleanup = (Test-Path -LiteralPath $planRoot)
        warnings = @($warnings)
        copies = $copies
        recommendation = $helper.payload
    }
    Write-Result -Result $result
    if (-not $result.success) { exit 1 }
    exit 0
}

if ($Mode -eq 'CopyApply') {
    if (-not $Apply) {
        throw 'CopyApply requires -Apply. Use -Mode Plan for preview.'
    }
    $copyRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "copy-apply\$timestamp")
    $copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $copyRoot)
    $masterCopy = Join-Path $copyRoot 'master.db'
    if (-not (Test-Path -LiteralPath $masterCopy -PathType Leaf)) {
        throw "master.db copy missing: $masterCopy"
    }

    $helper = Invoke-RecommendationHelper -PythonPath $pythonPath -DatabasePath $masterCopy -DatabaseDir $copyRoot -RuntimeMode 'copyapply'
    if (-not $KeepRuntime) {
        Remove-Item -LiteralPath $copyRoot -Recurse -Force
    }

    $result = [pscustomobject]@{
        mode = $Mode
        status = 'applied-to-copy'
        success = ($helper.exitCode -eq 0 -and [bool] $helper.payload.success)
        generatedAt = (Get-Date).ToString('o')
        rekordboxRoot = $RekordboxRoot
        runtimeRoot = $copyRoot
        runtimeRootExistsAfterCleanup = (Test-Path -LiteralPath $copyRoot)
        warnings = @($warnings)
        copies = $copies
        recommendation = $helper.payload
    }
    Write-Result -Result $result
    if (-not $result.success) { exit 1 }
    exit 0
}

if (-not $Apply) {
    throw 'LiveApply requires -Apply. Run without -Apply first to preview the live write.'
}
if (-not $ConfirmLiveWrite) {
    throw 'LiveApply requires -ConfirmLiveWrite.'
}
if ($rekordboxProcesses.Count -gt 0) {
    throw 'LiveApply is blocked while Rekordbox or rekordboxAgent processes are running. Close Rekordbox completely, then rerun.'
}

$backupRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "live-backup\$timestamp")
$backup = @(Copy-RekordboxDatabaseSet -DestinationRoot $backupRoot)
$backupManifestPath = Join-Path $backupRoot 'manifest.json'
$backupManifest = [pscustomobject]@{
    createdAt = (Get-Date).ToString('o')
    purpose = 'pre-live-genre-recommendation-apply-backup'
    rekordboxRoot = $RekordboxRoot
    inputCsv = $inputFullPath
    copied = $backup
}
$backupManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $backupManifestPath -Encoding UTF8

$helperLive = Invoke-RecommendationHelper -PythonPath $pythonPath -DatabasePath $masterPath -DatabaseDir $RekordboxRoot -RuntimeMode 'liveapply'
$result = [pscustomobject]@{
    mode = $Mode
    status = 'applied-to-live-rekordbox'
    success = ($helperLive.exitCode -eq 0 -and [bool] $helperLive.payload.success)
    generatedAt = (Get-Date).ToString('o')
    rekordboxRoot = $RekordboxRoot
    backupRoot = $backupRoot
    backupManifestPath = $backupManifestPath
    warnings = @($warnings)
    recommendation = $helperLive.payload
}
Write-Result -Result $result
if (-not $result.success) { exit 1 }
