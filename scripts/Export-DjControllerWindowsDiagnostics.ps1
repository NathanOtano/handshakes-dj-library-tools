[CmdletBinding()]
param(
    [string] $OutputPath,
    [switch] $DetailedPnpProperties,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$reportsRoot = Join-Path $repoRoot 'reports'

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutputPath = Join-Path $reportsRoot "DJ-Controller-windows11-diagnostics-$stamp.json"
}

$outputFullPath = [System.IO.Path]::GetFullPath($OutputPath)
$expectedHardwareId = 'USB\VID_08E4&PID_0172'
$targetPattern = 'VID_08E4&PID_0172|XDJ|AERO|PIONEER|AlphaTheta'

function Invoke-TextCommand {
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [Parameter()][string[]] $ArgumentList = @()
    )

    try {
        $output = & $FilePath @ArgumentList 2>&1
        return [pscustomobject]@{
            file = $FilePath
            arguments = $ArgumentList
            exitCode = $LASTEXITCODE
            output = @($output | ForEach-Object { [string] $_ })
            error = $null
        }
    }
    catch {
        return [pscustomobject]@{
            file = $FilePath
            arguments = $ArgumentList
            exitCode = $null
            output = @()
            error = $_.Exception.Message
        }
    }
}

function Get-PnpPropertyMap {
    param([Parameter(Mandatory)][string] $InstanceId)

    $wanted = @(
        'DEVPKEY_Device_HardwareIds',
        'DEVPKEY_Device_CompatibleIds',
        'DEVPKEY_Device_Service',
        'DEVPKEY_Device_Driver',
        'DEVPKEY_Device_DriverInfPath',
        'DEVPKEY_Device_ProblemCode',
        'DEVPKEY_Device_Class',
        'DEVPKEY_Device_ClassGuid',
        'DEVPKEY_NAME'
    )

    $map = [ordered]@{}
    foreach ($keyName in $wanted) {
        try {
            $property = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName $keyName -ErrorAction Stop
            $map[$keyName] = $property.Data
        }
        catch {
            $map[$keyName] = $null
        }
    }

    return [pscustomobject]$map
}

function Convert-PnpEntity {
    param(
        [Parameter(Mandatory)] $Device,
        [switch] $IncludeProperties
    )

    $result = [ordered]@{
        name = $Device.Name
        manufacturer = $Device.Manufacturer
        pnpClass = $Device.PNPClass
        status = $Device.Status
        configManagerErrorCode = $Device.ConfigManagerErrorCode
        deviceId = $Device.PNPDeviceID
    }

    if ($IncludeProperties) {
        $properties = Get-PnpPropertyMap -InstanceId $Device.PNPDeviceID
        $result['service'] = $properties.DEVPKEY_Device_Service
        $result['driverInfPath'] = $properties.DEVPKEY_Device_DriverInfPath
        $result['hardwareIds'] = @($properties.DEVPKEY_Device_HardwareIds)
        $result['compatibleIds'] = @($properties.DEVPKEY_Device_CompatibleIds)
        $result['problemCode'] = $properties.DEVPKEY_Device_ProblemCode
        $result['classGuid'] = $properties.DEVPKEY_Device_ClassGuid
    }

    return [pscustomobject]$result
}

$os = Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption, Version, BuildNumber, OSArchitecture, LastBootUpTime

$allPnpDevices = @(Get-CimInstance Win32_PnPEntity)

$matchingDevices = @($allPnpDevices |
    Where-Object {
        ($_.Name -match $targetPattern) -or
        ($_.Manufacturer -match $targetPattern) -or
        ($_.PNPDeviceID -match $targetPattern)
    } |
    Sort-Object PNPDeviceID |
    ForEach-Object {
        $includeProperties = $DetailedPnpProperties -and ($_.PNPDeviceID -match '^(USB|HID)\\.*VID_08E4&PID_0172')
        Convert-PnpEntity -Device $_ -IncludeProperties:$includeProperties
    })

$usbAudioDevices = @($allPnpDevices |
    Where-Object {
        ($_.PNPClass -eq 'MEDIA') -and
        (($_.Name -match 'USB|Audio|XDJ|PIONEER') -or ($_.PNPDeviceID -match 'USB\\'))
    } |
    Sort-Object Name |
    ForEach-Object { Convert-PnpEntity -Device $_ })

$midiSoftwareDevices = @($allPnpDevices |
    Where-Object {
        ($_.PNPClass -eq 'SoftwareDevice') -and
        (($_.Name -match 'MIDI') -or ($_.PNPDeviceID -match 'MIDISRV'))
    } |
    Sort-Object Name |
    ForEach-Object { Convert-PnpEntity -Device $_ })

$driverStoreMatches = @(Get-ChildItem -LiteralPath 'C:\Windows\System32\DriverStore\FileRepository' -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'pioneer|xdj|aero|alphatheta|asio' } |
    Sort-Object Name |
    Select-Object Name, FullName, LastWriteTime)

$publishedInfMatches = @(Get-ChildItem -LiteralPath 'C:\Windows\INF' -Filter 'oem*.inf' -File -ErrorAction SilentlyContinue |
    ForEach-Object {
        $matches = @(Select-String -LiteralPath $_.FullName -Pattern 'VID_08E4&PID_0172|PIONEER DJ-Controller|DJ-ControllerAudio64|DriverVer=07/09/2018,1.300.0.0' -ErrorAction SilentlyContinue)
        if ($matches.Count -gt 0) {
            [pscustomobject]@{
                name = $_.Name
                fullName = $_.FullName
                lastWriteTime = $_.LastWriteTime
                matchingLines = @($matches | ForEach-Object { $_.Line.Trim() })
            }
        }
    } |
    Sort-Object Name)

$driverService = Invoke-TextCommand -FilePath 'sc.exe' -ArgumentList @('query', 'DJ-ControllerAudio')
$midiServices = @(Get-Service -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -match 'midi') -or ($_.DisplayName -match 'midi') } |
    Sort-Object Name |
    Select-Object Name, DisplayName, Status, StartType)

$observedXdjPresent = @($matchingDevices | Where-Object { $_.deviceId -match 'VID_08E4&PID_0172' }).Count -gt 0
$observedXdjWithProblem = @($matchingDevices | Where-Object {
    ($_.deviceId -match 'VID_08E4&PID_0172') -and
    ($_.configManagerErrorCode -ne 0)
}).Count -gt 0

$report = [pscustomobject]@{
    schema = 'dj-library.DJ-Controller.windows-diagnostics.v1'
    generatedAt = (Get-Date).ToString('o')
    mutationPolicy = 'read-only; writes this diagnostics report only'
    repoRoot = $repoRoot
    expectedHardwareIdPrefix = $expectedHardwareId
    detailedPnpProperties = [bool] $DetailedPnpProperties
    observed = [pscustomobject]@{
        xdjAeroPresent = $observedXdjPresent
        xdjAeroProblemReported = $observedXdjWithProblem
    }
    os = $os
    matchingDevices = $matchingDevices
    usbAudioDevices = $usbAudioDevices
    midiSoftwareDevices = $midiSoftwareDevices
    midiServices = $midiServices
    driverStoreMatches = $driverStoreMatches
    driverService = $driverService
    publishedInfMatches = $publishedInfMatches
    interpretation = [pscustomobject]@{
        ifXdjAbsent = 'Connect the DJ-Controller by USB, put it in PC/MIDI control mode, then rerun this script.'
        ifCode43OrUnsigned = 'Treat as driver binding/signature problem before writing any custom driver.'
        ifMidiSeenButDead = 'Check Windows MIDI Services and the target DJ app mapping before kernel-driver work.'
        ifOnlyAudioInterfaceSeen = 'Capture the other USB interfaces with USBPcap/Wireshark before deciding on HID or UMDF.'
    }
}

$outputDir = Split-Path -Parent $outputFullPath
if (-not [string]::IsNullOrWhiteSpace($outputDir)) {
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}

$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $outputFullPath -Encoding UTF8

if ($Json) {
    $report | ConvertTo-Json -Depth 12
    return
}

[pscustomobject]@{
    report = $outputFullPath
    present = $observedXdjPresent
    problem = $observedXdjWithProblem
}
