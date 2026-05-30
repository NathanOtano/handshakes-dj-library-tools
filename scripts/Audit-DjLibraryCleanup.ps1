[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $RekordboxRoot = (Join-Path $env:APPDATA 'Pioneer\rekordbox'),
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\dj-cleanup-audit'),
    [string[]] $AudioRoot = @(),
    [string[]] $Playlist = @('playlist1', 'playlist2', 'playlist3'),

    [ValidateSet('none', 'candidate', 'all')]
    [string] $AudioHashMode = 'candidate',

    [int] $AudioHashFileLimit = 0,
    [switch] $PrepareRuntime,
    [switch] $KeepRuntime,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dj_cleanup_audit.py'))
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function Assert-UnderRepoRuntime {
    param([Parameter(Mandatory)] [string] $Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing runtime path outside repo: $resolved"
    }
    return $resolved
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

if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Helper not found: $helperPath"
}

$runtimeFullPath = Assert-UnderRepoRuntime -Path $runtimeFullPath
$auditRoot = Assert-UnderRepoRuntime -Path (Join-Path $runtimeFullPath "copy\$timestamp")
$pythonPath = Resolve-PythonRuntime
$copies = @(Copy-RekordboxDatabaseSet -DestinationRoot $auditRoot)
$masterCopy = Join-Path $auditRoot 'master.db'
if (-not (Test-Path -LiteralPath $masterCopy -PathType Leaf)) {
    throw "master.db copy missing: $masterCopy"
}

$env:PYTHONIOENCODING = 'utf-8'
$arguments = @(
    $helperPath,
    '--repo-root', $repoRoot,
    '--config', ([System.IO.Path]::GetFullPath($ConfigPath)),
    '--master', $masterCopy,
    '--db-dir', $auditRoot,
    '--audio-hash-mode', $AudioHashMode,
    '--audio-hash-file-limit', $AudioHashFileLimit.ToString([System.Globalization.CultureInfo]::InvariantCulture)
)
foreach ($root in $AudioRoot) {
    $arguments += @('--audio-root', $root)
}
foreach ($name in $Playlist) {
    $arguments += @('--playlist', $name)
}

$raw = & $pythonPath @arguments
$exit = $LASTEXITCODE
$payload = $raw | Out-String
$parsed = $payload | ConvertFrom-Json
$parsedErrorProperty = $parsed.PSObject.Properties['error']
$hasParsedError = ($null -ne $parsedErrorProperty -and -not [string]::IsNullOrWhiteSpace([string] $parsedErrorProperty.Value))
if (-not $KeepRuntime) {
    Remove-Item -LiteralPath $auditRoot -Recurse -Force
}

$result = [pscustomobject]@{
    status = 'read-only-audit'
    success = ($exit -eq 0 -and -not $hasParsedError)
    generatedAt = (Get-Date).ToString('o')
    rekordboxRoot = $RekordboxRoot
    runtimeCopyRoot = $auditRoot
    runtimeCopyRootExistsAfterCleanup = (Test-Path -LiteralPath $auditRoot)
    copies = $copies
    audit = $parsed
}

if ($Json) {
    $result | ConvertTo-Json -Depth 30
} else {
    $result
}

if (-not $result.success) {
    exit 1
}
