[CmdletBinding()]
param(
    [string] $TaskName = 'DJ Library OneTagger Spotify Retry',
    [int] $IntervalMinutes = 120,
    [switch] $Apply,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$retryScript = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'Invoke-OneTaggerSpotifyRetry.ps1'))
$runtimeDir = Join-Path $repoRoot 'runtime\onetagger-spotify-retry'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

if (-not (Test-Path -LiteralPath $retryScript -PathType Leaf)) {
    throw "Retry script not found: $retryScript"
}

$ps = (Get-Command pwsh -ErrorAction SilentlyContinue)
if ($null -eq $ps) {
    throw 'pwsh is required to register the scheduled retry task.'
}

$stdout = Join-Path $runtimeDir 'scheduled-task.stdout.log'
$stderr = Join-Path $runtimeDir 'scheduled-task.stderr.log'
$applyArg = if ($Apply) { ' -Apply' } else { '' }
$retryCommand = "& '$retryScript'$applyArg -Json 1>> '$stdout' 2>> '$stderr'"
$argument = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command `"$retryCommand`""

if (-not $Apply) {
    $result = [pscustomobject]@{
        taskName = $TaskName
        intervalMinutes = $IntervalMinutes
        action = 'dry-run'
        executable = $ps.Source
        argument = $argument
        note = 'Add -Apply to register or update the scheduled task.'
    }
    if ($Json) { $result | ConvertTo-Json -Depth 8 } else { $result }
    exit 0
}

$action = New-ScheduledTaskAction -Execute $ps.Source -Argument $argument -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8) -StartWhenAvailable -AllowStartIfOnBatteries:$false
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Relance OneTagger Spotify pour compléter la couverture genres DJ Library après cooldown Spotify.' -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$result = [pscustomobject]@{
    taskName = $TaskName
    state = $task.State.ToString()
    intervalMinutes = $IntervalMinutes
    nextRunTime = $info.NextRunTime.ToString('o')
    executable = $ps.Source
    argument = $argument
    runtimeDir = $runtimeDir
    stdout = $stdout
    stderr = $stderr
}

if ($Json) {
    $result | ConvertTo-Json -Depth 8
} else {
    $result
}
