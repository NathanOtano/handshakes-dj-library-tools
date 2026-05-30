[CmdletBinding()]
param(
    [string] $SourcePath,
    [string] $TargetDeviceName = 'PIONEER XDJ-AERO MIDI',
    [string] $TargetPath = (Join-Path $env:APPDATA "Pioneer\rekordbox6\MidiMappings\$TargetDeviceName.midi.csv"),
    [switch] $Apply,
    [switch] $CloseRekordbox,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-FactoryMapping {
    $installRoot = 'C:\Program Files\rekordbox'
    $relative = 'MidiMappings\PIONEER XDJ-AERO MIDI.midi.csv'

    if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
        return $null
    }

    $candidate = Get-ChildItem -LiteralPath $installRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object {
            $path = Join-Path $_.FullName $relative
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                Get-Item -LiteralPath $path
            }
        } |
        Select-Object -First 1

    if ($null -eq $candidate) {
        return $null
    }

    return $candidate.FullName
}

if ([string]::IsNullOrWhiteSpace($SourcePath)) {
    $SourcePath = Resolve-FactoryMapping
}

if ([string]::IsNullOrWhiteSpace($SourcePath)) {
    throw 'Factory XDJ-AERO MIDI mapping not found under C:\Program Files\rekordbox.'
}

$sourceFullPath = [System.IO.Path]::GetFullPath($SourcePath)
$targetFullPath = [System.IO.Path]::GetFullPath($TargetPath)

if (-not (Test-Path -LiteralPath $sourceFullPath -PathType Leaf)) {
    throw "Source mapping not found: $sourceFullPath"
}

$rekordboxProcesses = @(Get-Process rekordbox -ErrorAction SilentlyContinue)
if ($rekordboxProcesses.Count -gt 0 -and $CloseRekordbox) {
    foreach ($process in $rekordboxProcesses) {
        if ($process.MainWindowHandle -ne 0) {
            [void] $process.CloseMainWindow()
        }
    }
    Start-Sleep -Seconds 4
    $rekordboxProcesses = @(Get-Process rekordbox -ErrorAction SilentlyContinue)
}

if ($rekordboxProcesses.Count -gt 0 -and $Apply) {
    throw 'rekordbox is running. Close it first or rerun with -CloseRekordbox.'
}

$targetExists = Test-Path -LiteralPath $targetFullPath -PathType Leaf
$targetDir = Split-Path -Parent $targetFullPath
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = "$targetFullPath.backup-$stamp"
$sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFullPath).Hash
$targetHashBefore = if ($targetExists) { (Get-FileHash -Algorithm SHA256 -LiteralPath $targetFullPath).Hash } else { $null }
$sourceLines = @(Get-Content -LiteralPath $sourceFullPath)
$targetLines = @($sourceLines)
if ($targetLines.Count -gt 0 -and $targetLines[0] -like '@file,*') {
    $targetLines[0] = "@file,1,$TargetDeviceName"
}

$result = [ordered]@{
    source = $sourceFullPath
    target = $targetFullPath
    targetDeviceName = $TargetDeviceName
    backup = if ($targetExists) { $backupPath } else { $null }
    apply = [bool] $Apply
    closeRekordbox = [bool] $CloseRekordbox
    rekordboxRunning = ($rekordboxProcesses.Count -gt 0)
    sourceBytes = (Get-Item -LiteralPath $sourceFullPath).Length
    sourceHash = $sourceHash
    targetExists = $targetExists
    targetHashBefore = $targetHashBefore
    targetHashAfter = $targetHashBefore
}

if ($Apply) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    if ($targetExists) {
        Copy-Item -LiteralPath $targetFullPath -Destination $backupPath -Force
    }
    [System.IO.File]::WriteAllLines($targetFullPath, $targetLines, [System.Text.UTF8Encoding]::new($false))
    $result.targetHashAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $targetFullPath).Hash
    $result.targetBytes = (Get-Item -LiteralPath $targetFullPath).Length
    $result.targetLines = (Get-Content -LiteralPath $targetFullPath | Measure-Object -Line).Lines
}

$output = [pscustomobject] $result
if ($Json) {
    $output | ConvertTo-Json -Depth 6
    return
}

$output
