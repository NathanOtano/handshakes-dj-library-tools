[CmdletBinding()]
param(
    [ValidateSet('CopySmoke', 'AddContent')]
    [string] $Mode = 'CopySmoke',

    [string] $RekordboxRoot = (Join-Path $env:APPDATA 'Pioneer\rekordbox'),
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\rekordbox-controlled-write'),
    [string] $TrackPath,
    [string] $Title,

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
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'rekordbox_controlled_write.py'))
$masterPath = [System.IO.Path]::GetFullPath((Join-Path $RekordboxRoot 'master.db'))
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function Write-Result {
    param([Parameter(Mandatory)] [pscustomobject] $Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 12
        return
    }

    $Result
}

function Get-RekordboxProcess {
    $names = @('rekordbox', 'rekordboxAgent', 'rbinit', 'rbcloudagent')
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $names -contains $_.ProcessName } |
        Select-Object ProcessName, Id, Path
}

function Assert-UnderRepoRuntime {
    param([Parameter(Mandatory)] [string] $Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing runtime path outside repo: $resolved"
    }
    return $resolved
}

function Resolve-PythonRuntime {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        $python = Get-Command py -ErrorAction SilentlyContinue
    }
    if ($null -eq $python) {
        throw 'Python 3 is required.'
    }

    $venvRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath 'pyrekordbox-venv')
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }

    $probe = & $python.Source -c "import importlib.util; print('yes' if importlib.util.find_spec('pyrekordbox') and importlib.util.find_spec('sqlcipher3') else 'no')"
    if ($LASTEXITCODE -eq 0 -and ($probe | Select-Object -Last 1) -eq 'yes') {
        return $python.Source
    }

    if (-not $PrepareRuntime) {
        throw 'pyrekordbox + sqlcipher3 are required. Re-run with -PrepareRuntime to create an ignored repo-local venv.'
    }

    New-Item -ItemType Directory -Path $runtimeFullPath -Force | Out-Null
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

        $in = $null
        $out = $null
        try {
            $in = [System.IO.File]::Open($src, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $out = [System.IO.File]::Open($dst, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $in.CopyTo($out)
            $out.Dispose()
            $out = $null
            $hash = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash
            [pscustomobject]@{ name = $name; copied = $true; reason = ''; source = $src; dest = $dst; bytes = (Get-Item -LiteralPath $dst).Length; sha256 = $hash }
        }
        catch {
            [pscustomobject]@{ name = $name; copied = $false; reason = $_.Exception.Message; source = $src; dest = $dst; bytes = 0; sha256 = $null }
        }
        finally {
            if ($null -ne $out) { $out.Dispose() }
            if ($null -ne $in) { $in.Dispose() }
        }
    }
}

if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Helper not found: $helperPath"
}

$runtimeFullPath = Assert-UnderRepoRuntime -Path $runtimeFullPath
$rekordboxProcesses = @(Get-RekordboxProcess)
$warnings = New-Object System.Collections.Generic.List[string]
if ($rekordboxProcesses.Count -gt 0) {
    $warnings.Add('Rekordbox-related processes are running. CopySmoke is allowed, live AddContent is blocked.')
}

if ($Mode -eq 'CopySmoke') {
    $pythonPath = Resolve-PythonRuntime
    $copyRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "copy-smoke\$timestamp")
    $copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $copyRoot)
    $masterCopy = Join-Path $copyRoot 'master.db'
    if (-not (Test-Path -LiteralPath $masterCopy -PathType Leaf)) {
        throw "master.db copy missing: $masterCopy"
    }

    $raw = & $pythonPath $helperPath copy-smoke --master $masterCopy --work-root $copyRoot
    $exit = $LASTEXITCODE
    $payload = $raw | Out-String
    $pyResult = $payload | ConvertFrom-Json

    if (-not $KeepRuntime) {
        Remove-Item -LiteralPath $copyRoot -Recurse -Force
        $copyRootExistsAfterCleanup = Test-Path -LiteralPath $copyRoot
    }
    else {
        $copyRootExistsAfterCleanup = Test-Path -LiteralPath $copyRoot
    }

    $result = [pscustomobject]@{
        mode = $Mode
        success = ($exit -eq 0 -and [bool] $pyResult.success)
        generatedAt = (Get-Date).ToString('o')
        rekordboxRoot = $RekordboxRoot
        copyRoot = $copyRoot
        copyRootExistsAfterCleanup = $copyRootExistsAfterCleanup
        copies = $copies
        warnings = @($warnings)
        pyrekordbox = $pyResult
    }
    Write-Result -Result $result
    if (-not $result.success) { exit 1 }
    exit 0
}

if ([string]::IsNullOrWhiteSpace($TrackPath)) {
    throw '-TrackPath is required for AddContent.'
}
if ([string]::IsNullOrWhiteSpace($Title)) {
    $Title = [System.IO.Path]::GetFileNameWithoutExtension($TrackPath)
}

$trackFullPath = [System.IO.Path]::GetFullPath($TrackPath)
$plan = [pscustomobject]@{
    mode = $Mode
    generatedAt = (Get-Date).ToString('o')
    rekordboxRoot = $RekordboxRoot
    masterPath = $masterPath
    trackPath = $trackFullPath
    title = $Title
    apply = [bool] $Apply
    confirmLiveWrite = [bool] $ConfirmLiveWrite
    rekordboxProcesses = $rekordboxProcesses
    warnings = @($warnings)
}

if (-not $Apply) {
    $plan | Add-Member -NotePropertyName status -NotePropertyValue 'dry-run'
    $plan | Add-Member -NotePropertyName wouldWriteLiveMasterDb -NotePropertyValue $true
    Write-Result -Result $plan
    exit 0
}

if (-not $ConfirmLiveWrite) {
    throw 'Live AddContent requires -ConfirmLiveWrite.'
}
if ($rekordboxProcesses.Count -gt 0) {
    throw 'Live AddContent is blocked while Rekordbox or rekordboxAgent processes are running.'
}
if (-not (Test-Path -LiteralPath $masterPath -PathType Leaf)) {
    throw "master.db not found: $masterPath"
}
if (-not (Test-Path -LiteralPath $trackFullPath -PathType Leaf)) {
    throw "TrackPath not found: $trackFullPath"
}

$pythonPath = Resolve-PythonRuntime
$backupRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "live-backup\$timestamp")
$backup = @(Copy-RekordboxDatabaseSet -DestinationRoot $backupRoot)
$backupManifestPath = Join-Path $backupRoot 'manifest.json'
$backupManifest = [pscustomobject]@{
    createdAt = (Get-Date).ToString('o')
    purpose = 'pre-live-rekordbox-add-content-backup'
    rekordboxRoot = $RekordboxRoot
    trackPath = $trackFullPath
    title = $Title
    copied = $backup
}
$backupManifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $backupManifestPath -Encoding UTF8

$rawApply = & $pythonPath $helperPath add-content --master $masterPath --track-path $trackFullPath --title $Title
$exitApply = $LASTEXITCODE
$applyPayload = $rawApply | Out-String
$applyResult = $applyPayload | ConvertFrom-Json

$resultApply = [pscustomobject]@{
    mode = $Mode
    success = ($exitApply -eq 0 -and [bool] $applyResult.success)
    generatedAt = (Get-Date).ToString('o')
    backupRoot = $backupRoot
    backupManifestPath = $backupManifestPath
    warnings = @($warnings)
    pyrekordbox = $applyResult
}
Write-Result -Result $resultApply
if (-not $resultApply.success) { exit 1 }
