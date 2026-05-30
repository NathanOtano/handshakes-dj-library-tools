[CmdletBinding()]
param(
    [ValidateSet('Plan', 'CopyApply', 'LiveApply')]
    [string] $Mode = 'Plan',

    [string] $RekordboxRoot = (Join-Path $env:APPDATA 'Pioneer\rekordbox'),
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\rekordbox-smart-playlist-sync'),
    [string[]] $PlaylistName = @(),
    [string] $SourceSuffix = '_',

    [switch] $RemoveStale,
    [switch] $Apply,
    [switch] $ConfirmLiveWrite,
    [switch] $PrepareRuntime,
    [switch] $KeepRuntime,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'rekordbox_smart_playlist_sync.py'))
$masterPath = [System.IO.Path]::GetFullPath((Join-Path $RekordboxRoot 'master.db'))
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function Write-Result {
    param([Parameter(Mandatory)] [pscustomobject] $Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 20
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

function Invoke-SyncHelper {
    param(
        [Parameter(Mandatory)] [string] $PythonPath,
        [Parameter(Mandatory)] [string] $DatabasePath,
        [Parameter(Mandatory)] [string] $DatabaseDir,
        [switch] $HelperApply,
        [switch] $AllowRunningCommit
    )

    $env:PYTHONIOENCODING = 'utf-8'
    $arguments = @(
        $helperPath,
        '--master', $DatabasePath,
        '--db-dir', $DatabaseDir,
        '--source-suffix', $SourceSuffix
    )
    foreach ($name in $PlaylistName) {
        $arguments += @('--playlist', $name)
    }
    if ($RemoveStale) {
        $arguments += '--remove-stale'
    }
    if ($HelperApply) {
        $arguments += '--apply'
    }
    if ($AllowRunningCommit) {
        $arguments += '--allow-rekordbox-running-commit'
    }

    $raw = & $PythonPath @arguments
    $exit = $LASTEXITCODE
    $payload = $raw | Out-String
    $parsed = $payload | ConvertFrom-Json
    return [pscustomobject]@{ exitCode = $exit; payload = $parsed }
}

if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Helper not found: $helperPath"
}

$runtimeFullPath = Assert-UnderRepoRuntime -Path $runtimeFullPath
$pythonPath = Resolve-PythonRuntime
$rekordboxProcesses = @(Get-RekordboxProcess)
$warnings = New-Object System.Collections.Generic.List[string]
if ($rekordboxProcesses.Count -gt 0) {
    $warnings.Add('Rekordbox-related processes are running. LiveApply is blocked until Rekordbox is closed.')
}

if ($Mode -eq 'Plan' -or ($Mode -eq 'LiveApply' -and -not $Apply)) {
    $planRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "plan\$timestamp")
    $copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $planRoot)
    $masterCopy = Join-Path $planRoot 'master.db'
    if (-not (Test-Path -LiteralPath $masterCopy -PathType Leaf)) {
        throw "master.db copy missing: $masterCopy"
    }

    $helper = Invoke-SyncHelper -PythonPath $pythonPath -DatabasePath $masterCopy -DatabaseDir $planRoot
    if (-not $KeepRuntime) {
        Remove-Item -LiteralPath $planRoot -Recurse -Force
    }

    $result = [pscustomobject]@{
        mode = $Mode
        status = if ($Mode -eq 'LiveApply') { 'dry-run-live-apply' } else { 'plan' }
        success = ($helper.exitCode -eq 0 -and [bool] $helper.payload.success)
        generatedAt = (Get-Date).ToString('o')
        rekordboxRoot = $RekordboxRoot
        runtimeRoot = $planRoot
        runtimeRootExistsAfterCleanup = (Test-Path -LiteralPath $planRoot)
        copies = $copies
        warnings = @($warnings)
        sync = $helper.payload
    }
    Write-Result -Result $result
    if (-not $result.success) { exit 1 }
    exit 0
}

if ($Mode -eq 'CopyApply') {
    if (-not $Apply) {
        throw 'CopyApply requires -Apply. Use -Mode Plan for a read-only plan.'
    }

    $copyRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "copy-apply\$timestamp")
    $copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $copyRoot)
    $masterCopy = Join-Path $copyRoot 'master.db'
    if (-not (Test-Path -LiteralPath $masterCopy -PathType Leaf)) {
        throw "master.db copy missing: $masterCopy"
    }

    $helper = Invoke-SyncHelper -PythonPath $pythonPath -DatabasePath $masterCopy -DatabaseDir $copyRoot -HelperApply -AllowRunningCommit
    if (-not $KeepRuntime) {
        Remove-Item -LiteralPath $copyRoot -Recurse -Force
    }

    $result = [pscustomobject]@{
        mode = $Mode
        status = 'applied-to-copy'
        success = ($helper.exitCode -eq 0 -and [bool] $helper.payload.success)
        generatedAt = (Get-Date).ToString('o')
        rekordboxRoot = $RekordboxRoot
        copyRoot = $copyRoot
        copyRootExistsAfterCleanup = (Test-Path -LiteralPath $copyRoot)
        copies = $copies
        warnings = @($warnings)
        sync = $helper.payload
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
if (-not (Test-Path -LiteralPath $masterPath -PathType Leaf)) {
    throw "master.db not found: $masterPath"
}

$backupRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "live-backup\$timestamp")
$backup = @(Copy-RekordboxDatabaseSet -DestinationRoot $backupRoot)
$backupManifestPath = Join-Path $backupRoot 'manifest.json'
$backupManifest = [pscustomobject]@{
    createdAt = (Get-Date).ToString('o')
    purpose = 'pre-live-smart-playlist-sync-backup'
    rekordboxRoot = $RekordboxRoot
    sourceSuffix = $SourceSuffix
    playlistName = $PlaylistName
    removeStale = [bool] $RemoveStale
    copied = $backup
}
$backupManifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $backupManifestPath -Encoding UTF8

$helperLive = Invoke-SyncHelper -PythonPath $pythonPath -DatabasePath $masterPath -DatabaseDir $RekordboxRoot -HelperApply
$resultLive = [pscustomobject]@{
    mode = $Mode
    status = 'applied-to-live-rekordbox'
    success = ($helperLive.exitCode -eq 0 -and [bool] $helperLive.payload.success)
    generatedAt = (Get-Date).ToString('o')
    rekordboxRoot = $RekordboxRoot
    backupRoot = $backupRoot
    backupManifestPath = $backupManifestPath
    warnings = @($warnings)
    sync = $helperLive.payload
}
Write-Result -Result $resultLive
if (-not $resultLive.success) { exit 1 }
