[CmdletBinding()]
param(
    [string] $BaselineCsvPath = (Join-Path $PSScriptRoot '..\reports\onetagger-run-20260524-004620\derived-latest-state-by-path.csv'),
    [string] $ReportRoot = (Join-Path $PSScriptRoot '..\reports'),
    [string] $Label = 'latest',
    [string[]] $Platforms = @('Spotify', 'Deezer'),
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-FullPath {
    param([Parameter(Mandatory)][string] $Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
}

function New-OrdinalSet {
    $set = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    return ,$set
}

function New-OrdinalMap {
    $map = [System.Collections.Generic.Dictionary[string,object]]::new([System.StringComparer]::OrdinalIgnoreCase)
    return ,$map
}

function Add-M3u {
    param(
        [Parameter(Mandatory)][string] $Path,
        [AllowEmptyCollection()][string[]] $Items
    )
    $lines = @('#EXTM3U') + ($Items | Sort-Object)
    Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
}

$baselineFullPath = Resolve-FullPath $BaselineCsvPath
$reportRootFullPath = Resolve-FullPath $ReportRoot
if (-not (Test-Path -LiteralPath $baselineFullPath -PathType Leaf)) {
    throw "Baseline CSV not found: $baselineFullPath"
}
if (-not (Test-Path -LiteralPath $reportRootFullPath -PathType Container)) {
    throw "Report root not found: $reportRootFullPath"
}

$allPaths = New-OrdinalSet
$canonicalPath = [System.Collections.Generic.Dictionary[string,string]]::new([System.StringComparer]::OrdinalIgnoreCase)

foreach ($row in (Import-Csv -LiteralPath $baselineFullPath)) {
    if ($row.path) {
        [void]$allPaths.Add($row.path)
        if (-not $canonicalPath.ContainsKey($row.path)) {
            $canonicalPath[$row.path] = $row.path
        }
    }
}

$platformStates = @{}
foreach ($platform in $Platforms) {
    $platformStates[$platform] = [System.Collections.Generic.Dictionary[string,object]]::new([System.StringComparer]::OrdinalIgnoreCase)
}

$eventLogs = Get-ChildItem -LiteralPath $reportRootFullPath -Directory |
    Where-Object { $_.Name -like 'onetagger-run-*' -or $_.Name -like 'onetagger-coverage-*' } |
    ForEach-Object { Join-Path $_.FullName 'onetagger-events.jsonl' } |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Sort-Object

$eventCounts = @{}
foreach ($platform in $Platforms) {
    $eventCounts[$platform] = @{ ok = 0; error = 0; skipped = 0; other = 0 }
}

foreach ($eventLog in $eventLogs) {
    Get-Content -LiteralPath $eventLog -Encoding UTF8 | ForEach-Object {
        if ([string]::IsNullOrWhiteSpace($_)) { return }
        try {
            $event = $_ | ConvertFrom-Json
        } catch {
            return
        }
        if ($event.action -ne 'taggingProgress' -or $null -eq $event.status -or $null -eq $event.status.status) {
            return
        }
        if (-not $event.status.PSObject.Properties['platform']) {
            return
        }
        $platform = [string]$event.status.platform
        if (-not $platformStates.ContainsKey($platform)) {
            return
        }
        $inner = $event.status.status
        $trackPath = [string]$inner.path
        if ([string]::IsNullOrWhiteSpace($trackPath)) {
            return
        }
        $status = [string]$inner.status
        if ([string]::IsNullOrWhiteSpace($status)) {
            $status = 'other'
        }
        [void]$allPaths.Add($trackPath)
        if (-not $canonicalPath.ContainsKey($trackPath)) {
            $canonicalPath[$trackPath] = $trackPath
        }
        if (-not $platformStates[$platform].ContainsKey($trackPath)) {
            $platformStates[$platform][$trackPath] = [pscustomobject]@{
                ok = $false
                error = $false
                skipped = $false
                other = $false
                latest = $null
            }
        }
        $pathState = $platformStates[$platform][$trackPath]
        switch ($status) {
            'ok' { $pathState.ok = $true; break }
            'error' { $pathState.error = $true; break }
            'skipped' { $pathState.skipped = $true; break }
            default { $pathState.other = $true; break }
        }
        $pathState.latest = $status
        if ($eventCounts[$platform].ContainsKey($status)) {
            $eventCounts[$platform][$status] += 1
        } else {
            $eventCounts[$platform].other += 1
        }
    }
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$safeLabel = ($Label -replace '[^A-Za-z0-9_.-]', '-')
$prefix = "onetagger-platform-coverage-$timestamp-$safeLabel"
$summaryPath = Join-Path $reportRootFullPath "$prefix-summary.json"

$platformSummary = @{}
foreach ($platform in $Platforms) {
    $state = $platformStates[$platform]
    $attempted = $state.Count
    $ok = 0
    $errorCount = 0
    $skipped = 0
    $other = 0
    foreach ($value in $state.Values) {
        if ($value.ok) { $ok += 1 }
        if ($value.error) { $errorCount += 1 }
        if ($value.skipped) { $skipped += 1 }
        if ($value.other) { $other += 1 }
    }
    $missing = foreach ($p in $allPaths) {
        if (-not $state.ContainsKey($p)) { $canonicalPath[$p] }
    }
    $errorNoOk = foreach ($entry in $state.GetEnumerator()) {
        if ($entry.Value.error -and -not $entry.Value.ok) { $canonicalPath[$entry.Key] }
    }
    $missingM3u = Join-Path $reportRootFullPath "$prefix-missing-$($platform.ToLowerInvariant())-attempt.m3u"
    $errorM3u = Join-Path $reportRootFullPath "$prefix-$($platform.ToLowerInvariant())-error-no-ok.m3u"
    Add-M3u -Path $missingM3u -Items @($missing)
    Add-M3u -Path $errorM3u -Items @($errorNoOk)
    $platformSummary[$platform] = [pscustomobject]@{
        attempted = $attempted
        ok = $ok
        error = $errorCount
        skipped = $skipped
        other = $other
        missingAttempt = @($missing).Count
        errorWithoutOk = @($errorNoOk).Count
        eventCounts = $eventCounts[$platform]
        missingAttemptM3u = $missingM3u
        errorNoOkM3u = $errorM3u
    }
}

$spotifyMissingByFolder = @()
if ($platformSummary.ContainsKey('Spotify')) {
    $spotifyState = $platformStates['Spotify']
    $spotifyMissing = foreach ($p in $allPaths) {
        if (-not $spotifyState.ContainsKey($p)) { $canonicalPath[$p] }
    }
    $spotifyMissingByFolder = @($spotifyMissing | Group-Object {
        $parent = Split-Path -Parent $_
        if ([string]::IsNullOrWhiteSpace($parent)) { return $_ }
        $name = Split-Path -Leaf $parent
        if ([string]::IsNullOrWhiteSpace($name)) { return $_ }
        return $name
    } | Sort-Object Count -Descending | Select-Object Count,Name)
}

$summary = [pscustomobject]@{
    generatedAt = (Get-Date).ToString('o')
    baselineCsv = $baselineFullPath
    totalPaths = $allPaths.Count
    eventLogs = @($eventLogs)
    platforms = $platformSummary
    spotifyMissingByFolder = $spotifyMissingByFolder
    summaryJson = $summaryPath
    note = 'Coverage is derived from OneTagger JSONL taggingProgress events. M3U files are worklists, not direct OneTagger targets in the current server path mode.'
}

$summary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $summaryPath -Encoding UTF8

if ($Json) {
    $summary | ConvertTo-Json -Depth 12
} else {
    $summary
}
