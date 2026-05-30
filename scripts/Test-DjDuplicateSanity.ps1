[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $CandidateRoot,
    [string[]] $CompareRoot = @(),
    [string] $ReportsRoot,
    [int] $Limit = 0,
    [switch] $AudioHash,
    [switch] $Fingerprint,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw 'python not found in PATH.'
}

$scriptPath = Join-Path $PSScriptRoot 'dj_duplicate_sanity.py'
$argsList = @($scriptPath, '--config', ([System.IO.Path]::GetFullPath($ConfigPath)))

if (-not [string]::IsNullOrWhiteSpace($CandidateRoot)) {
    $argsList += @('--candidate-root', $CandidateRoot)
}

foreach ($root in $CompareRoot) {
    if (-not [string]::IsNullOrWhiteSpace($root)) {
        $argsList += @('--compare-root', $root)
    }
}

if (-not [string]::IsNullOrWhiteSpace($ReportsRoot)) {
    $argsList += @('--reports-root', $ReportsRoot)
}

if ($Limit -gt 0) {
    $argsList += @('--limit', [string]$Limit)
}

if ($AudioHash) {
    $argsList += '--audio-hash'
}

if ($Fingerprint) {
    $argsList += '--fingerprint'
}

if ($Json) {
    $argsList += '--json'
}

& $python.Source @argsList
exit $LASTEXITCODE
