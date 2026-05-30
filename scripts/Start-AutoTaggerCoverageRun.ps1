[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $TargetPath,
    [string[]] $Platforms = @('spotify'),
    [string[]] $Tags = @('genre', 'style'),
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $ReportRoot = (Join-Path $PSScriptRoot '..\reports'),
    [int] $Threads = 2,
    [switch] $Overwrite,
    [switch] $SkipTagged,
    [switch] $RunInForeground,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-NodeRuntime {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($null -eq $node) {
        throw 'node is required for the AutoTagger coverage runner.'
    }
    return $node.Source
}

function Resolve-ConfiguredPath {
    param([AllowNull()][string] $Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
}

function Wait-AutoTaggerWsUrl {
    param(
        [Parameter(Mandatory)][string] $StdoutPath,
        [int] $TimeoutSeconds = 45
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $StdoutPath -PathType Leaf) {
            $text = Get-Content -LiteralPath $StdoutPath -Raw -ErrorAction SilentlyContinue
            if ($text -match 'ws://127\.0\.0\.1:\d+') {
                return $Matches[0]
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "AutoTagger server did not expose a WebSocket URL within $TimeoutSeconds seconds. See $StdoutPath"
}

function New-StopScript {
    param(
        [Parameter(Mandatory)][string] $Path,
        [int[]] $ProcessIds
    )
    $ids = ($ProcessIds | Where-Object { $_ -gt 0 }) -join ', '
    @"
`$ErrorActionPreference = 'Continue'
`$ProcessIds = $ids
foreach (`$processId in `$ProcessIds) {
  `$p = Get-Process -Id `$processId -ErrorAction SilentlyContinue
  if (`$p) {
    Stop-Process -Id `$processId -Force
    Write-Output "Stopped PID `$processId (`$(`$p.ProcessName))"
  } else {
    Write-Output "PID `$processId not running"
  }
}
"@ | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Join-ProcessArguments {
    param([Parameter(Mandatory)][string[]] $Arguments)
    return ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    }) -join ' '
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configFullPath = Resolve-ConfiguredPath $ConfigPath
$targetFullPath = Resolve-ConfiguredPath $TargetPath
$reportRootFullPath = Resolve-ConfiguredPath $ReportRoot
$runnerPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'AutoTagger_coverage_runner.js'))

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}
if (-not (Test-Path -LiteralPath $targetFullPath)) {
    throw "Target not found: $targetFullPath"
}
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Runner not found: $runnerPath"
}

$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$AutoTaggerPath = Resolve-ConfiguredPath $config.toolHints.AutoTagger.configuredPath
if (-not (Test-Path -LiteralPath $AutoTaggerPath -PathType Leaf)) {
    throw "AutoTagger not found: $AutoTaggerPath"
}

$nodePath = Resolve-NodeRuntime
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$safePlatforms = ($Platforms | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ }) -join '-'
if ([string]::IsNullOrWhiteSpace($safePlatforms)) {
    throw 'At least one platform is required.'
}
$reportDir = Join-Path $reportRootFullPath "AutoTagger-coverage-$timestamp-$safePlatforms"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null

$serverStdout = Join-Path $reportDir 'AutoTagger-server.stdout.log'
$serverStderr = Join-Path $reportDir 'AutoTagger-server.stderr.log'
$serverArgs = Join-ProcessArguments @('--server', '--path', $targetFullPath)
$server = Start-Process -FilePath $AutoTaggerPath -ArgumentList $serverArgs -WindowStyle Hidden -RedirectStandardOutput $serverStdout -RedirectStandardError $serverStderr -PassThru
$wsUrl = Wait-AutoTaggerWsUrl -StdoutPath $serverStdout

$runnerStdout = Join-Path $reportDir 'runner.stdout.log'
$runnerStderr = Join-Path $reportDir 'runner.stderr.log'
$argsList = @(
    $runnerPath,
    '--ws-url', $wsUrl,
    '--report-dir', $reportDir,
    '--target-path', $targetFullPath,
    '--server-pid', $server.Id.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--platforms', (($Platforms | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ }) -join ','),
    '--tags', (($Tags | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join ','),
    '--threads', $Threads.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    '--overwrite', ([bool]$Overwrite).ToString().ToLowerInvariant(),
    '--skip-tagged', ([bool]$SkipTagged).ToString().ToLowerInvariant()
)

$statusScript = Join-Path $reportDir 'status.ps1'
@"
`$ErrorActionPreference = 'SilentlyContinue'
`$ReportDir = Split-Path -Parent `$MyInvocation.MyCommand.Path
`$statusPath = Join-Path `$ReportDir 'live-status.json'
if (Test-Path -LiteralPath `$statusPath) {
  Get-Content -LiteralPath `$statusPath -Raw
} else {
  Write-Output '{"state":"missing live-status.json"}'
}
"@ | Set-Content -LiteralPath $statusScript -Encoding UTF8

if ($RunInForeground) {
    New-StopScript -Path (Join-Path $reportDir 'stop.ps1') -ProcessIds @($server.Id)
    $preflight = [pscustomobject]@{
        runId = Split-Path -Leaf $reportDir
        reportDir = $reportDir
        serverPid = $server.Id
        runnerPid = $PID
        wsUrl = $wsUrl
        targetPath = $targetFullPath
        platforms = $Platforms
        tags = $Tags
        overwrite = [bool]$Overwrite
        skipTagged = [bool]$SkipTagged
        threads = $Threads
    }
    $preflight | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $reportDir 'launcher-preflight.json') -Encoding UTF8
    try {
        & $nodePath @argsList 1> $runnerStdout 2> $runnerStderr
        $exit = $LASTEXITCODE
    } finally {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
    $result = [pscustomobject]@{
        success = ($exit -eq 0)
        reportDir = $reportDir
        serverPid = $server.Id
        runnerPid = $PID
        wsUrl = $wsUrl
        finalSummary = Join-Path $reportDir 'final-summary.json'
        statusScript = $statusScript
        stopScript = Join-Path $reportDir 'stop.ps1'
    }
    if ($Json) { $result | ConvertTo-Json -Depth 8 } else { $result }
    exit $exit
}

$runnerArgs = Join-ProcessArguments $argsList
$runner = Start-Process -FilePath $nodePath -ArgumentList $runnerArgs -WindowStyle Hidden -RedirectStandardOutput $runnerStdout -RedirectStandardError $runnerStderr -PassThru
New-StopScript -Path (Join-Path $reportDir 'stop.ps1') -ProcessIds @($runner.Id, $server.Id)

$result = [pscustomobject]@{
    success = $true
    reportDir = $reportDir
    serverPid = $server.Id
    runnerPid = $runner.Id
    wsUrl = $wsUrl
    statusScript = $statusScript
    stopScript = Join-Path $reportDir 'stop.ps1'
    runnerStdout = $runnerStdout
    runnerStderr = $runnerStderr
    serverStdout = $serverStdout
    serverStderr = $serverStderr
    targetPath = $targetFullPath
    platforms = $Platforms
    tags = $Tags
    overwrite = [bool]$Overwrite
    skipTagged = [bool]$SkipTagged
    threads = $Threads
}

if ($Json) {
    $result | ConvertTo-Json -Depth 8
} else {
    $result
}
