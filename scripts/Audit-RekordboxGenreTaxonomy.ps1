[CmdletBinding()]
param(
    [string] $RekordboxRoot = (Join-Path $env:APPDATA 'Pioneer\rekordbox'),
    [string] $TaxonomyPath = (Join-Path $PSScriptRoot '..\config\dj-genre-taxonomy.json'),
    [string] $ReportsRoot = (Join-Path $PSScriptRoot '..\reports'),
    [int] $SubgenreMinTracks = 0,
    [switch] $PrepareRuntime,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'runtime\rekordbox-controlled-write\pyrekordbox-venv'))
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'rekordbox_genre_taxonomy_audit.py'))
$masterPath = [System.IO.Path]::GetFullPath((Join-Path $RekordboxRoot 'master.db'))
$taxonomyFullPath = [System.IO.Path]::GetFullPath($TaxonomyPath)
$reportsFullPath = [System.IO.Path]::GetFullPath($ReportsRoot)

function Write-Result {
    param([Parameter(Mandatory)] [pscustomobject] $Result)
    if ($Json) {
        $Result | ConvertTo-Json -Depth 20
        return
    }
    $Result
}

function Assert-UnderRepo {
    param([Parameter(Mandatory)] [string] $Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside repo: $resolved"
    }
    return $resolved
}

function Resolve-PythonRuntime {
    $venvPython = Join-Path $runtimeRoot 'Scripts\python.exe'
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
        throw 'pyrekordbox + sqlcipher3 are required. Re-run with -PrepareRuntime to create or reuse the ignored repo-local venv.'
    }

    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    & $python.Source -m venv $runtimeRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create pyrekordbox virtual environment.'
    }
    & $venvPython -m pip install --upgrade pip pyrekordbox sqlcipher3-binary | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install pyrekordbox runtime.'
    }
    return $venvPython
}

Assert-UnderRepo -Path $taxonomyFullPath | Out-Null
Assert-UnderRepo -Path $reportsFullPath | Out-Null

if (-not (Test-Path -LiteralPath $masterPath -PathType Leaf)) {
    throw "Rekordbox master.db not found: $masterPath"
}
if (-not (Test-Path -LiteralPath $taxonomyFullPath -PathType Leaf)) {
    throw "Taxonomy config not found: $taxonomyFullPath"
}

$pythonPath = Resolve-PythonRuntime
$arguments = @(
    $helperPath,
    '--master', $masterPath,
    '--taxonomy', $taxonomyFullPath,
    '--reports-root', $reportsFullPath
)
if ($SubgenreMinTracks -gt 0) {
    $arguments += @('--subgenre-min-tracks', [string]$SubgenreMinTracks)
}

$output = & $pythonPath @arguments
if ($LASTEXITCODE -ne 0) {
    throw ($output -join [Environment]::NewLine)
}

$parsed = $output | ConvertFrom-Json
Write-Result -Result $parsed
