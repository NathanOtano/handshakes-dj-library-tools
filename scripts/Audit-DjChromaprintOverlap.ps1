[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $RootPath,
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\dj-chromaprint-overlap'),
    [string] $FpcalcPath,
    [string] $FfprobePath,
    [int] $Workers = 4,
    [int] $Limit = 0,
    [int] $FingerprintLengthSeconds = 180,
    [double] $MinSimilarity = 0.88,
    [int] $MinOverlapFrames = 80,
    [ValidateSet('Filename', 'All')]
    [string] $CandidateMode = 'Filename',
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configFullPath = [System.IO.Path]::GetFullPath($ConfigPath)
$runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dj_chromaprint_overlap.py'))

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

function Resolve-Tool {
    param(
        [string] $ExplicitPath,
        [string] $ConfiguredPath,
        [Parameter(Mandatory)] [string] $CommandName
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

    throw "$CommandName not found. Configure it in config/dj-library.paths.json or pass the explicit path."
}

function Resolve-PythonRuntime {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        $python = Get-Command py -ErrorAction SilentlyContinue
    }
    if ($null -eq $python) {
        throw 'Python 3 is required.'
    }
    return $python.Source
}

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}
if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Helper not found: $helperPath"
}

$runtimeFullPath = Assert-UnderRepoRuntime -Path $runtimeFullPath
$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$root = Resolve-ConfiguredPath ($(if ([string]::IsNullOrWhiteSpace($RootPath)) { $config.postProcessed_Library_RootRoot } else { $RootPath }))
$reportsRoot = Resolve-ConfiguredPath $config.reportsRoot
$fpcalc = Resolve-Tool -ExplicitPath $FpcalcPath -ConfiguredPath $config.toolHints.chromaprint.configuredPath -CommandName 'fpcalc'
$ffprobe = Resolve-Tool -ExplicitPath $FfprobePath -ConfiguredPath $null -CommandName 'ffprobe'
$pythonPath = Resolve-PythonRuntime

if (-not (Test-Path -LiteralPath $root -PathType Container)) {
    throw "Audio root not found: $root"
}
New-Item -ItemType Directory -Path $runtimeFullPath -Force | Out-Null
New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null

$env:PYTHONIOENCODING = 'utf-8'
$arguments = @(
    $helperPath,
    '--repo-root', $repoRoot,
    '--config', $configFullPath,
    '--root-path', $root,
    '--reports-root', $reportsRoot,
    '--runtime-root', $runtimeFullPath,
    '--fpcalc', $fpcalc,
    '--ffprobe', $ffprobe,
    '--workers', $Workers.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--fingerprint-length-seconds', $FingerprintLengthSeconds.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--min-similarity', $MinSimilarity.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--min-overlap-frames', $MinOverlapFrames.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--candidate-mode', $CandidateMode.ToLowerInvariant()
)
if ($Limit -gt 0) {
    $arguments += @('--limit', $Limit.ToString([System.Globalization.CultureInfo]::InvariantCulture))
}

$raw = & $pythonPath @arguments
$exit = $LASTEXITCODE
$payload = $raw | Out-String
$parsed = $payload | ConvertFrom-Json

$result = [pscustomobject]@{
    status = 'read-only-chromaprint-overlap-audit'
    success = ($exit -eq 0 -and [bool] $parsed.success)
    generatedAt = (Get-Date).ToString('o')
    rootPath = $root
    fpcalcPath = $fpcalc
    ffprobePath = $ffprobe
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
