[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $SourceConfigPath = (Join-Path $PSScriptRoot '..\config\dj-genre-resolver.sources.json'),
    [string] $OnetaggerRunPath,
    [string] $InputM3uPath,
    [ValidateSet('AllErrors', 'MissingExistingGenre', 'TrueUnresolvedMissingExistingGenre')]
    [string] $WorklistScope = 'AllErrors',
    [string] $RuntimeRoot = (Join-Path $PSScriptRoot '..\runtime\dj-genre-resolver'),
    [ValidateSet('Worklist', 'Resolve', 'Verify', 'AudioReadiness')]
    [string] $Mode = 'Worklist',
    [string[]] $Sources = @(),
    [int] $Limit = 0,
    [int] $MaxCandidatesPerSource = 5,
    [switch] $VerifyAudioTags,
    [string] $FfprobePath,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configFullPath = [System.IO.Path]::GetFullPath($ConfigPath)
$sourceConfigFullPath = [System.IO.Path]::GetFullPath($SourceConfigPath)
$runtimeFullPath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$helperPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dj_genre_resolver.py'))

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

    throw "$CommandName not found. Configure it in config/dj-library.paths.json or pass the explicit path."
}

function Resolve-LatestOneTaggerRun {
    param([Parameter(Mandatory)] [string] $ReportsRoot)
    $runs = Get-ChildItem -LiteralPath $ReportsRoot -Directory -Filter 'onetagger-run-*' -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'derived-latest-state-by-path.csv') -PathType Leaf } |
        Sort-Object LastWriteTime -Descending

    if (@($runs).Count -eq 0) {
        throw "No OneTagger derived run found under $ReportsRoot"
    }

    return $runs[0].FullName
}

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}
if (-not (Test-Path -LiteralPath $sourceConfigFullPath -PathType Leaf)) {
    throw "Source config not found: $sourceConfigFullPath"
}
if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw "Helper not found: $helperPath"
}

$runtimeFullPath = Assert-UnderRepoRuntime -Path $runtimeFullPath
$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$reportsRoot = Resolve-ConfiguredPath $config.reportsRoot
if ([string]::IsNullOrWhiteSpace($reportsRoot)) {
    $reportsRoot = Join-Path $repoRoot 'reports'
}

if ([string]::IsNullOrWhiteSpace($OnetaggerRunPath)) {
    $OnetaggerRunPath = Resolve-LatestOneTaggerRun -ReportsRoot $reportsRoot
}
$onetaggerRunFullPath = Resolve-ConfiguredPath $OnetaggerRunPath
$inputM3uFullPath = Resolve-ConfiguredPath $InputM3uPath

if (-not (Test-Path -LiteralPath $onetaggerRunFullPath -PathType Container)) {
    throw "OneTagger run not found: $onetaggerRunFullPath"
}
if (-not [string]::IsNullOrWhiteSpace($inputM3uFullPath) -and -not (Test-Path -LiteralPath $inputM3uFullPath -PathType Leaf)) {
    throw "Input M3U not found: $inputM3uFullPath"
}
if ($Mode -eq 'Resolve' -and [string]::IsNullOrWhiteSpace($inputM3uFullPath) -and $Limit -le 0 -and $WorklistScope -eq 'AllErrors') {
    throw 'Unbounded Resolve refused. Pass -InputM3uPath, -Limit, or -WorklistScope MissingExistingGenre / TrueUnresolvedMissingExistingGenre.'
}

$ffprobe = Resolve-Tool -ExplicitPath $FfprobePath -ConfiguredPath $null -CommandName 'ffprobe' -Optional
if ($VerifyAudioTags -and [string]::IsNullOrWhiteSpace($ffprobe)) {
    throw 'ffprobe is required for -VerifyAudioTags.'
}

New-Item -ItemType Directory -Path $runtimeFullPath -Force | Out-Null
New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null

$pythonPath = Resolve-PythonRuntime
$env:PYTHONIOENCODING = 'utf-8'
$arguments = @(
    $helperPath,
    '--repo-root', $repoRoot,
    '--config', $configFullPath,
    '--source-config', $sourceConfigFullPath,
    '--onetagger-run', $onetaggerRunFullPath,
    '--reports-root', $reportsRoot,
    '--runtime-root', $runtimeFullPath,
    '--mode', $Mode.ToLowerInvariant(),
    '--worklist-scope', (($WorklistScope -creplace '([a-z0-9])([A-Z])', '$1-$2').ToLowerInvariant()),
    '--max-candidates-per-source', $MaxCandidatesPerSource.ToString([System.Globalization.CultureInfo]::InvariantCulture)
)

if (-not [string]::IsNullOrWhiteSpace($inputM3uFullPath)) {
    $arguments += @('--input-m3u', $inputM3uFullPath)
}
if ($Limit -gt 0) {
    $arguments += @('--limit', $Limit.ToString([System.Globalization.CultureInfo]::InvariantCulture))
}
if (@($Sources).Count -gt 0) {
    foreach ($source in $Sources) {
        foreach ($sourcePart in ($source -split ',')) {
            $sourceName = $sourcePart.Trim()
            if (-not [string]::IsNullOrWhiteSpace($sourceName)) {
                $arguments += @('--source', $sourceName)
            }
        }
    }
}
if ($VerifyAudioTags) {
    $arguments += '--verify-audio-tags'
}
if (-not [string]::IsNullOrWhiteSpace($ffprobe)) {
    $arguments += @('--ffprobe', $ffprobe)
}

$raw = & $pythonPath @arguments
$exit = $LASTEXITCODE
$payload = $raw | Out-String
$parsed = $payload | ConvertFrom-Json

$result = [pscustomobject]@{
    status = 'dj-genre-resolver'
    success = ($exit -eq 0 -and [bool] $parsed.success)
    generatedAt = (Get-Date).ToString('o')
    mode = $Mode
    onetaggerRunPath = $onetaggerRunFullPath
    reportsRoot = $reportsRoot
    runtimeRoot = $runtimeFullPath
    resolver = $parsed
}

if ($Json) {
    $result | ConvertTo-Json -Depth 40
} else {
    $result
}

if (-not $result.success) {
    exit 1
}
