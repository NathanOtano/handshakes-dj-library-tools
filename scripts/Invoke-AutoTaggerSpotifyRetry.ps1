[CmdletBinding()]
param(
    [string] $TargetFolder = 'C:\DJ_Music\Processed_Library_Root\_ALL',
    [string] $RootFolder = 'C:\DJ_Music\Processed_Library_Root',
    [string] $ReportRoot = (Join-Path $PSScriptRoot '..\reports'),
    [string] $RuntimeDir = (Join-Path $PSScriptRoot '..\runtime\AutoTagger-spotify-retry'),
    [int] $CooldownHours = 24,
    [int] $Threads = 1,
    [switch] $Apply,
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

function Write-JsonFile {
    param([Parameter(Mandatory)][string] $Path, [Parameter(Mandatory)]$Value)
    $Value | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Append-Jsonl {
    param([Parameter(Mandatory)][string] $Path, [Parameter(Mandatory)]$Value)
    ($Value | ConvertTo-Json -Compress -Depth 12) | Add-Content -LiteralPath $Path -Encoding UTF8
}

function Get-RateLimitEvidence {
    param([Parameter(Mandatory)][string] $ReportRootPath)
    $logs = Get-ChildItem -LiteralPath $ReportRootPath -Recurse -Filter 'AutoTagger-server.stdout.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
    foreach ($log in $logs) {
        $matches = Select-String -LiteralPath $log.FullName -Pattern 'Spotify rate limit hit, sleeping for: (\d+)s' -AllMatches -ErrorAction SilentlyContinue
        if ($matches) {
            $last = $matches[-1]
            $seconds = [int]$last.Matches[-1].Groups[1].Value
            return [pscustomobject]@{
                logPath = $log.FullName
                logLastWriteTime = $log.LastWriteTime.ToString('o')
                seconds = $seconds
                estimatedReadyAt = $log.LastWriteTime.AddSeconds($seconds).ToString('o')
                line = $last.Line
            }
        }
    }
    return $null
}

function Read-M3uPaths {
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ,@()
    }
    $items = @(Get-Content -LiteralPath $Path -Encoding UTF8 | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and -not $_.TrimStart().StartsWith('#')
    })
    return ,$items
}

$reportRootFullPath = Resolve-FullPath $ReportRoot
$runtimeFullPath = Resolve-FullPath $RuntimeDir
$targetFullPath = Resolve-FullPath $TargetFolder
$rootFullPath = Resolve-FullPath $RootFolder
New-Item -ItemType Directory -Path $runtimeFullPath -Force | Out-Null

$statePath = Join-Path $runtimeFullPath 'state.json'
$eventsPath = Join-Path $runtimeFullPath 'events.jsonl'
$lockPath = Join-Path $runtimeFullPath 'retry.lock'

$now = Get-Date
$decision = [ordered]@{
    ts = $now.ToString('o')
    apply = [bool]$Apply
    action = 'undecided'
    reason = ''
    targetFolder = $targetFullPath
    rootFolder = $rootFullPath
    reportRoot = $reportRootFullPath
}

$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    $decision.action = 'skip'
    $decision.reason = 'retry-lock-active'
    Append-Jsonl -Path $eventsPath -Value $decision
    if ($Json) { $decision | ConvertTo-Json -Depth 12 } else { [pscustomobject]$decision }
    exit 0
}

try {
    $activeAutoTagger = @(Get-Process AutoTagger -ErrorAction SilentlyContinue)
    if ($activeAutoTagger.Count -gt 0) {
        $decision.action = 'skip'
        $decision.reason = 'AutoTagger-process-active'
        $decision.activePids = @($activeAutoTagger | Select-Object -ExpandProperty Id)
        Write-JsonFile -Path $statePath -Value $decision
        Append-Jsonl -Path $eventsPath -Value $decision
        if ($Json) { $decision | ConvertTo-Json -Depth 12 } else { [pscustomobject]$decision }
        exit 0
    }

    $coverage = & (Join-Path $PSScriptRoot 'Update-AutoTaggerPlatformCoverage.ps1') -ReportRoot $reportRootFullPath -Label 'retry-preflight' -Json | ConvertFrom-Json
    $decision.coverageSummary = $coverage.summaryJson
    $decision.spotify = $coverage.platforms.Spotify
    $decision.deezer = $coverage.platforms.Deezer

    if ([int]$coverage.platforms.Spotify.missingAttempt -le 0) {
        $decision.action = 'done'
        $decision.reason = 'spotify-covered-all-known-paths'
        Write-JsonFile -Path $statePath -Value $decision
        Append-Jsonl -Path $eventsPath -Value $decision
        if ($Json) { $decision | ConvertTo-Json -Depth 12 } else { [pscustomobject]$decision }
        exit 0
    }

    $rateLimit = Get-RateLimitEvidence -ReportRootPath $reportRootFullPath
    $decision.rateLimit = $rateLimit
    if ($rateLimit) {
        $readyAt = [datetime]::Parse($rateLimit.estimatedReadyAt)
        if ($now -lt $readyAt) {
            $decision.action = 'wait'
            $decision.reason = 'spotify-rate-limit-cooldown'
            $decision.nextEligibleAt = $readyAt.ToString('o')
            Write-JsonFile -Path $statePath -Value $decision
            Append-Jsonl -Path $eventsPath -Value $decision
            if ($Json) { $decision | ConvertTo-Json -Depth 12 } else { [pscustomobject]$decision }
            exit 0
        }
    }

    $missingPaths = Read-M3uPaths -Path ([string]$coverage.platforms.Spotify.missingAttemptM3u)
    $selectedTarget = $targetFullPath
    $targetPrefix = $targetFullPath.TrimEnd('\') + '\'
    $missingInTargetFolder = @($missingPaths | Where-Object { $_.StartsWith($targetPrefix, [System.StringComparison]::OrdinalIgnoreCase) })
    if ($missingInTargetFolder.Count -gt 0) {
        $selectedTarget = $targetFullPath
        $decision.selectedTargetReason = 'missing-paths-under-primary-target-folder'
    } elseif ($missingPaths.Count -gt 0) {
        $selectedTarget = $missingPaths[0]
        $decision.selectedTargetReason = 'single-remaining-path-outside-primary-target-folder'
    }
    $decision.selectedTarget = $selectedTarget

    if (-not (Test-Path -LiteralPath $selectedTarget)) {
        throw "Selected target not found: $selectedTarget"
    }

    if (-not $Apply) {
        $decision.action = 'dry-run'
        $decision.reason = 'apply-not-set'
        $decision.command = "pwsh -NoProfile -File .\scripts\Invoke-AutoTaggerSpotifyRetry.ps1 -Apply -Json"
        Write-JsonFile -Path $statePath -Value $decision
        Append-Jsonl -Path $eventsPath -Value $decision
        if ($Json) { $decision | ConvertTo-Json -Depth 12 } else { [pscustomobject]$decision }
        exit 0
    }

    $launcherOutput = & (Join-Path $PSScriptRoot 'Start-AutoTaggerCoverageRun.ps1') -TargetPath $selectedTarget -Platforms spotify -Threads $Threads -Json
    $launcher = $launcherOutput | ConvertFrom-Json
    $decision.action = 'started'
    $decision.reason = 'spotify-retry-launched'
    $decision.launcher = $launcher
    $decision.reportDir = $launcher.reportDir
    Write-JsonFile -Path $statePath -Value $decision
    Append-Jsonl -Path $eventsPath -Value $decision
    if ($Json) { $decision | ConvertTo-Json -Depth 12 } else { [pscustomobject]$decision }
} finally {
    if ($lockStream) {
        $lockStream.Dispose()
    }
}
