[CmdletBinding()]
param(
    [string] $InputPcap,

    [switch] $Live,

    [string] $UsbPcapDevice = '\\.\USBPcap3',

    [int] $UsbDeviceAddress = 16,

    [switch] $CaptureAllDevices,

    [int] $Bus = 3,

    [string] $MidiEndpoint = '0x85',

    [string] $OutputName = 'XDJ-AERO Bridge (B)',

    [int] $DurationSeconds = 0,

    [int] $MaxEvents = 0,

    [switch] $ReplayTiming,

    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$reportsRoot = Join-Path $repoRoot 'reports'
$bridgeScript = Join-Path $PSScriptRoot 'xdj_aero_usbpcap_midi_bridge.py'
$usbPcap = 'C:\Program Files\USBPcap\USBPcapCMD.exe'

if (-not (Test-Path -LiteralPath $bridgeScript -PathType Leaf)) {
    throw "Bridge script not found: $bridgeScript"
}

$python = Get-Command py -ErrorAction SilentlyContinue
$pythonArgs = @()
if ($null -ne $python) {
    $pythonExe = $python.Source
    $pythonArgs += '-3'
}
else {
    $python = Get-Command python -ErrorAction Stop
    $pythonExe = $python.Source
}

$follow = $false
$liveUsbPcap = $false

try {
    if ([string]::IsNullOrWhiteSpace($InputPcap)) {
        if (-not $Live) {
            throw "Pass -InputPcap for replay mode, or add -Live to capture USBPcap directly from stdout and forward it to the MIDI bridge."
        }
        if (-not (Test-Path -LiteralPath $usbPcap -PathType Leaf)) {
            throw "USBPcapCMD.exe not found at $usbPcap"
        }
        $liveUsbPcap = $true
    }
    elseif ($Live) {
        $follow = $true
    }

    $arguments = @(
        $bridgeScript,
        '--output-name', $OutputName,
        '--bus', [string] $Bus,
        '--device', [string] $UsbDeviceAddress,
        '--midi-endpoint', $MidiEndpoint
    )
    if ($liveUsbPcap) {
        $arguments += @(
            '--live-usbpcap',
            '--usbpcap-cmd', $usbPcap,
            '--capture-device', $UsbPcapDevice
        )
        if ($CaptureAllDevices) {
            $arguments += '--capture-from-all-devices'
        }
        else {
            $arguments += @('--capture-devices', [string] $UsbDeviceAddress)
        }
    }
    else {
        $inputFullPath = [System.IO.Path]::GetFullPath($InputPcap)
        $arguments += @('--input', $inputFullPath)
    }
    if ($follow) {
        $arguments += '--follow'
    }
    if ($DurationSeconds -gt 0) {
        $arguments += @('--duration-seconds', [string] $DurationSeconds)
    }
    if ($MaxEvents -gt 0) {
        $arguments += @('--max-events', [string] $MaxEvents)
    }
    if ($ReplayTiming) {
        $arguments += '--replay-timing'
    }

    $output = & $pythonExe @pythonArgs @arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $outputText = ($output | ForEach-Object { [string] $_ }) -join [Environment]::NewLine
        throw "XDJ-AERO USBPcap MIDI bridge failed with exit code $exitCode.$([Environment]::NewLine)$outputText"
    }

    if ($Json) {
        $output
        return
    }

    ($output | ConvertFrom-Json) | Format-List
}
finally {
}
