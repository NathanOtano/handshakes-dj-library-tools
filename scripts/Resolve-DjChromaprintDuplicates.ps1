[CmdletBinding()]
param(
    [ValidateSet('Plan', 'CopyApply', 'LiveApply')]
    [string] $Mode = 'Plan',

    [string] $ReportCsv,
    [string] $MusicRoot = 'C:\DJ_Music',
    [string] $QuarantineRoot,
    [string] $CdBackupRoot,
    [string] $RekordboxRoot = (Join-Path $env:APPDATA 'Pioneer\rekordbox'),
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\dj-chromaprint-duplicate-apply'),
    [string] $ChromaprintCache = (Join-Path $PSScriptRoot '..\runtime\dj-chromaprint-overlap\fingerprint-cache.jsonl'),
    [string] $FfmpegPath,
    [string] $FfprobePath,

    [double] $MinGroupSimilarity = 0.98,
    [double] $DurationToleranceSeconds = 1.0,
    [ValidateSet('Safe', 'Loose', 'None')]
    [string] $TitleMatchMode = 'Safe',

    [switch] $Apply,
    [switch] $ConfirmLiveWrite,
    [switch] $SkipMasterConversion,
    [switch] $PrepareRuntime,
    [switch] $KeepRuntime,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'rekordbox_chromaprint_duplicate_cleanup.py'))
$masterPath = [System.IO.Path]::GetFullPath((Join-Path $RekordboxRoot 'master.db'))
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

if ([string]::IsNullOrWhiteSpace($ReportCsv)) {
    $latest = Get-ChildItem -LiteralPath (Join-Path $repoRoot 'reports') -Filter 'chromaprint-overlap-groups-*.csv' -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $latest) {
        throw 'ReportCsv was not provided and no chromaprint-overlap-groups report was found.'
    }
    $ReportCsv = $latest.FullName
}
if ([string]::IsNullOrWhiteSpace($QuarantineRoot)) {
    $QuarantineRoot = Join-Path $MusicRoot (Join-Path '_DUPLICATE_QUARANTINE' $timestamp)
}
if ([string]::IsNullOrWhiteSpace($CdBackupRoot)) {
    $CdBackupRoot = Join-Path $MusicRoot (Join-Path '_CD_QUALITY_BACKUP' $timestamp)
}

function Write-Result {
    param([Parameter(Mandatory)] [pscustomobject] $Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 30
        return
    }
    $Result
}

function Assert-UnderRepoRuntime {
    param([Parameter(Mandatory)] [string] $Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing runtime path outside repo: $resolved"
    }
    return $resolved
}

function Resolve-CommandPath {
    param(
        [string] $ExplicitPath,
        [Parameter(Mandatory)] [string] $CommandName
    )
    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        $resolved = [System.IO.Path]::GetFullPath($ExplicitPath)
        if (Test-Path -LiteralPath $resolved -PathType Leaf) {
            return $resolved
        }
    }
    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    throw "$CommandName not found. Pass -$($CommandName.Substring(0,1).ToUpperInvariant() + $CommandName.Substring(1))Path or add it to PATH."
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
    & $venvPython -m pip install --upgrade pip --quiet
    & $venvPython -m pip install pyrekordbox --quiet
    return $venvPython
}

function Copy-RekordboxDatabaseSet {
    param([Parameter(Mandatory)] [string] $DestinationRoot)
    New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
    $names = @('master.db', 'master.db-wal', 'master.db-shm', 'masterPlaylists6.xml', 'automixPlaylist6.xml')
    foreach ($name in $names) {
        $src = Join-Path $RekordboxRoot $name
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

function Invoke-ChromaprintHelper {
    param(
        [Parameter(Mandatory)] [string] $PythonPath,
        [Parameter(Mandatory)] [string] $DatabasePath,
        [Parameter(Mandatory)] [string] $DatabaseDir,
        [Parameter(Mandatory)] [string] $OperationReport,
        [Parameter(Mandatory)] [string] $ReviewCsv,
        [switch] $HelperApplyDb,
        [switch] $MoveFiles,
        [switch] $ConvertMasters,
        [switch] $AllowRekordboxRunningCommit
    )
    $env:PYTHONIOENCODING = 'utf-8'
    $arguments = @(
        $helperPath,
        '--master', $DatabasePath,
        '--db-dir', $DatabaseDir,
        '--report-csv', ([System.IO.Path]::GetFullPath($ReportCsv)),
        '--music-root', ([System.IO.Path]::GetFullPath($MusicRoot)),
        '--quarantine-root', ([System.IO.Path]::GetFullPath($QuarantineRoot)),
        '--cd-backup-root', ([System.IO.Path]::GetFullPath($CdBackupRoot)),
        '--chromaprint-cache', ([System.IO.Path]::GetFullPath($ChromaprintCache)),
        '--ffmpeg', $script:FfmpegResolved,
        '--ffprobe', $script:FfprobeResolved,
        '--operation-report', $OperationReport,
        '--review-csv', $ReviewCsv,
        '--min-group-similarity', $MinGroupSimilarity.ToString([System.Globalization.CultureInfo]::InvariantCulture),
        '--duration-tolerance-seconds', $DurationToleranceSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
        '--title-match-mode', $TitleMatchMode.ToLowerInvariant()
    )
    if ($HelperApplyDb) { $arguments += '--apply-db' }
    if ($MoveFiles) { $arguments += '--move-files' }
    if ($ConvertMasters) { $arguments += '--convert-masters' }
    if ($AllowRekordboxRunningCommit) { $arguments += '--allow-rekordbox-running-commit' }
    $raw = & $PythonPath @arguments
    $exit = $LASTEXITCODE
    $payload = $raw | Out-String
    [pscustomobject]@{ exitCode = $exit; payload = ($payload | ConvertFrom-Json) }
}

if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Helper not found: $helperPath"
}

$runtimeFullPath = Assert-UnderRepoRuntime -Path $runtimeFullPath
New-Item -ItemType Directory -Path $runtimeFullPath -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $repoRoot 'reports') -Force | Out-Null
$pythonPath = Resolve-PythonRuntime
$script:FfmpegResolved = Resolve-CommandPath -ExplicitPath $FfmpegPath -CommandName 'ffmpeg'
$script:FfprobeResolved = Resolve-CommandPath -ExplicitPath $FfprobePath -CommandName 'ffprobe'
$rekordboxProcesses = @(Get-RekordboxProcess)
$warnings = New-Object System.Collections.Generic.List[string]
if ($rekordboxProcesses.Count -gt 0) {
    $warnings.Add('Rekordbox-related processes are running. LiveApply is blocked until Rekordbox is closed.')
}

$reportRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'reports'))
$operationReport = Join-Path $reportRoot "chromaprint-duplicate-apply-$timestamp.json"
$reviewCsv = Join-Path $reportRoot "chromaprint-duplicate-review-$timestamp.csv"

if ($Mode -eq 'Plan' -or ($Mode -eq 'LiveApply' -and -not $Apply)) {
    $planRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "plan\$timestamp")
    $copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $planRoot)
    $helper = Invoke-ChromaprintHelper -PythonPath $pythonPath -DatabasePath (Join-Path $planRoot 'master.db') -DatabaseDir $planRoot -OperationReport $operationReport -ReviewCsv $reviewCsv
    if (-not $KeepRuntime) { Remove-Item -LiteralPath $planRoot -Recurse -Force }
    $result = [pscustomobject]@{
        mode = $Mode
        status = if ($Mode -eq 'LiveApply') { 'dry-run-live-apply' } else { 'plan' }
        success = ($helper.exitCode -eq 0 -and [bool] $helper.payload.success)
        generatedAt = (Get-Date).ToString('o')
        reportCsv = [System.IO.Path]::GetFullPath($ReportCsv)
        quarantineRoot = [System.IO.Path]::GetFullPath($QuarantineRoot)
        cdBackupRoot = [System.IO.Path]::GetFullPath($CdBackupRoot)
        copies = $copies
        warnings = @($warnings)
        operation = $helper.payload
    }
    Write-Result -Result $result
    if (-not $result.success) { exit 1 }
    exit 0
}

if ($Mode -eq 'CopyApply') {
    if (-not $Apply) { throw 'CopyApply requires -Apply. Use -Mode Plan for a read-only plan.' }
    $copyRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "copy-apply\$timestamp")
    $copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $copyRoot)
    $helper = Invoke-ChromaprintHelper -PythonPath $pythonPath -DatabasePath (Join-Path $copyRoot 'master.db') -DatabaseDir $copyRoot -OperationReport $operationReport -ReviewCsv $reviewCsv -HelperApplyDb -AllowRekordboxRunningCommit
    if (-not $KeepRuntime) { Remove-Item -LiteralPath $copyRoot -Recurse -Force }
    $result = [pscustomobject]@{
        mode = $Mode
        status = 'applied-to-copy-database-only'
        success = ($helper.exitCode -eq 0 -and [bool] $helper.payload.success)
        generatedAt = (Get-Date).ToString('o')
        reportCsv = [System.IO.Path]::GetFullPath($ReportCsv)
        quarantineRoot = [System.IO.Path]::GetFullPath($QuarantineRoot)
        cdBackupRoot = [System.IO.Path]::GetFullPath($CdBackupRoot)
        copies = $copies
        warnings = @($warnings)
        operation = $helper.payload
    }
    Write-Result -Result $result
    if (-not $result.success) { exit 1 }
    exit 0
}

if (-not $Apply) { throw 'LiveApply requires -Apply. Run without -Apply first to preview the live write.' }
if (-not $ConfirmLiveWrite) { throw 'LiveApply requires -ConfirmLiveWrite.' }
if ($rekordboxProcesses.Count -gt 0) {
    throw 'LiveApply is blocked while Rekordbox or rekordboxAgent processes are running. Close Rekordbox completely, then rerun.'
}
if (-not (Test-Path -LiteralPath $masterPath -PathType Leaf)) {
    throw "master.db not found: $masterPath"
}

$backupRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "live-backup\$timestamp")
$backup = @(Copy-RekordboxDatabaseSet -DestinationRoot $backupRoot)
$backupManifestPath = Join-Path $backupRoot 'manifest.json'
$backupManifest = [pscustomobject]@{
    createdAt = (Get-Date).ToString('o')
    purpose = 'pre-live-chromaprint-duplicate-cleanup-backup'
    rekordboxRoot = $RekordboxRoot
    reportCsv = [System.IO.Path]::GetFullPath($ReportCsv)
    quarantineRoot = [System.IO.Path]::GetFullPath($QuarantineRoot)
    cdBackupRoot = [System.IO.Path]::GetFullPath($CdBackupRoot)
    copied = $backup
}
$backupManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $backupManifestPath -Encoding UTF8

$helperLive = Invoke-ChromaprintHelper -PythonPath $pythonPath -DatabasePath $masterPath -DatabaseDir $RekordboxRoot -OperationReport $operationReport -ReviewCsv $reviewCsv -HelperApplyDb -MoveFiles -ConvertMasters:(!$SkipMasterConversion)
$resultLive = [pscustomObject]@{
    mode = $Mode
    status = 'applied-to-live-rekordbox-and-files'
    success = ($helperLive.exitCode -eq 0 -and [bool] $helperLive.payload.success)
    generatedAt = (Get-Date).ToString('o')
    backupRoot = $backupRoot
    backupManifestPath = $backupManifestPath
    reportCsv = [System.IO.Path]::GetFullPath($ReportCsv)
    quarantineRoot = [System.IO.Path]::GetFullPath($QuarantineRoot)
    cdBackupRoot = [System.IO.Path]::GetFullPath($CdBackupRoot)
    warnings = @($warnings)
    operation = $helperLive.payload
}
Write-Result -Result $resultLive
if (-not $resultLive.success) { exit 1 }
