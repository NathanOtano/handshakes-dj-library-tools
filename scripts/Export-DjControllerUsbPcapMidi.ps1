[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $InputPath,

    [string] $OutputDirectory,

    [string] $Bus,

    [string] $DeviceAddress,

    [string] $MidiEndpoint = '0x85',

    [string] $HidEndpoint = '0x87',

    [switch] $IncludeHid,

    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot 'reports'
}

$inputFullPath = [System.IO.Path]::GetFullPath($InputPath)
if (-not (Test-Path -LiteralPath $inputFullPath -PathType Leaf)) {
    throw "USBPcap input file not found: $inputFullPath"
}

$scriptPath = Join-Path $PSScriptRoot 'dj_controller_usbpcap_extract.py'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Extractor script not found: $scriptPath"
}

$python = Get-Command py -ErrorAction SilentlyContinue
$arguments = @()
if ($null -ne $python) {
    $pythonExe = $python.Source
    $arguments += '-3'
}
else {
    $python = Get-Command python -ErrorAction Stop
    $pythonExe = $python.Source
}

$arguments += @(
    $scriptPath,
    '--input', $inputFullPath,
    '--output-dir', ([System.IO.Path]::GetFullPath($OutputDirectory)),
    '--midi-endpoint', $MidiEndpoint,
    '--hid-endpoint', $HidEndpoint
)

if (-not [string]::IsNullOrWhiteSpace($Bus)) {
    $arguments += @('--bus', $Bus)
}
if (-not [string]::IsNullOrWhiteSpace($DeviceAddress)) {
    $arguments += @('--device', $DeviceAddress)
}
if ($IncludeHid) {
    $arguments += '--include-hid'
}

$output = & $pythonExe @arguments 2>&1
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    $outputText = ($output | ForEach-Object { [string] $_ }) -join [Environment]::NewLine
    throw "DJ-Controller USBPcap extraction failed with exit code $exitCode.$([Environment]::NewLine)$outputText"
}

if ($Json) {
    $output
    return
}

($output | ConvertFrom-Json) | Format-List
