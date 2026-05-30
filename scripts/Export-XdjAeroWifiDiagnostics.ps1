[CmdletBinding()]
param(
    [string] $OutputPath,
    [string] $WifiInterfaceAlias = 'Wi-Fi',
    [string] $AeroSsidPattern = 'AERO',
    [string] $ExpectedGateway = '192.168.1.1',
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$reportsRoot = Join-Path $repoRoot 'reports'

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutputPath = Join-Path $reportsRoot "xdj-aero-wifi-diagnostics-$stamp.json"
}

$outputFullPath = [System.IO.Path]::GetFullPath($OutputPath)

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

function Convert-IpConfiguration {
    param([Parameter(Mandatory)] $Config)

    $netProfileName = $null
    if ($null -ne $Config.NetProfile) {
        $netProfileName = $Config.NetProfile.Name
    }

    $ipv4Addresses = @()
    if ($null -ne $Config.IPv4Address) {
        $ipv4Addresses = @($Config.IPv4Address | ForEach-Object { $_.IPAddress })
    }

    $ipv4Gateways = @()
    if ($null -ne $Config.IPv4DefaultGateway) {
        $ipv4Gateways = @($Config.IPv4DefaultGateway | ForEach-Object { $_.NextHop })
    }

    $dnsServers = @()
    if ($null -ne $Config.DNSServer) {
        $dnsServers = @($Config.DNSServer | ForEach-Object { $_.ServerAddresses } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }

    [pscustomobject]@{
        interfaceAlias = $Config.InterfaceAlias
        interfaceDescription = $Config.InterfaceDescription
        netProfileName = $netProfileName
        ipv4Addresses = $ipv4Addresses
        ipv4Gateways = $ipv4Gateways
        dnsServers = $dnsServers
    }
}

$wlanInterfacesRaw = Invoke-TextCommand -FilePath 'netsh.exe' -ArgumentList @('wlan', 'show', 'interfaces')
$wlanNetworksRaw = Invoke-TextCommand -FilePath 'netsh.exe' -ArgumentList @('wlan', 'show', 'networks', 'mode=bssid')

$wifiAdapter = Get-NetAdapter -Name $WifiInterfaceAlias -ErrorAction SilentlyContinue |
    Select-Object Name, InterfaceDescription, Status, MacAddress, LinkSpeed

$allActiveAdapters = @(Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object { $_.Status -ne 'Disabled' } |
    Sort-Object Name |
    Select-Object Name, InterfaceDescription, Status, MacAddress, LinkSpeed)

$vpnLikeAdapters = @($allActiveAdapters |
    Where-Object {
        ($_.Name -match 'vpn|tap|tailscale|wireguard|openvpn|zerotier') -or
        ($_.InterfaceDescription -match 'vpn|tap|tailscale|wireguard|openvpn|zerotier')
    })

$wifiIpConfiguration = $null
$allIpConfigurations = @()
$networkProfile = $null
try {
    $allIpConfigurations = @(Get-NetIPConfiguration -ErrorAction Stop |
        ForEach-Object { Convert-IpConfiguration -Config $_ })
}
catch {
    $allIpConfigurations = @()
}

try {
    $wifiIpConfiguration = Get-NetIPConfiguration -InterfaceAlias $WifiInterfaceAlias -ErrorAction Stop
    $wifiIpConfiguration = Convert-IpConfiguration -Config $wifiIpConfiguration
}
catch {
    $wifiIpConfiguration = [pscustomobject]@{
        error = $_.Exception.Message
    }
}

try {
    $networkProfile = Get-NetConnectionProfile -InterfaceAlias $WifiInterfaceAlias -ErrorAction Stop |
        Select-Object Name, InterfaceAlias, NetworkCategory, IPv4Connectivity, IPv6Connectivity
}
catch {
    $networkProfile = [pscustomobject]@{
        error = $_.Exception.Message
    }
}

$gatewayReachable = $false
try {
    $gatewayReachable = Test-Connection -ComputerName $ExpectedGateway -Count 2 -Quiet -ErrorAction Stop
}
catch {
    $gatewayReachable = $false
}

$rekordboxProcesses = @(Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -match 'rekordbox' } |
    Select-Object ProcessName, Id, Path)

$rekordboxProcessIds = @($rekordboxProcesses | ForEach-Object { $_.Id })
$rekordboxUdpEndpoints = @()
$rekordboxTcpConnections = @()
foreach ($processId in $rekordboxProcessIds) {
    $rekordboxUdpEndpoints += @(Get-NetUDPEndpoint -OwningProcess $processId -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, LocalPort, OwningProcess)
    $rekordboxTcpConnections += @(Get-NetTCPConnection -OwningProcess $processId -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, LocalPort, RemoteAddress, RemotePort, State, OwningProcess)
}

$rekordboxInstallations = @(Get-ChildItem -LiteralPath 'C:\Program Files\rekordbox' -Recurse -Filter 'rekordbox.exe' -ErrorAction SilentlyContinue |
    Sort-Object FullName |
    Select-Object FullName, LastWriteTime)

$firewallProfiles = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue |
    Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction)

$networksText = ($wlanNetworksRaw.output -join "`n")
$interfacesText = ($wlanInterfacesRaw.output -join "`n")
$aeroSsidVisible = $networksText -match $AeroSsidPattern
$connectedToAero = $interfacesText -match $AeroSsidPattern
$wifiIpv4 = @()
if ($wifiIpConfiguration.PSObject.Properties.Name -contains 'ipv4Addresses') {
    $wifiIpv4 = @($wifiIpConfiguration.ipv4Addresses)
}
$hasExpectedLocalIp = @($wifiIpv4 | Where-Object { $_ -match '^192\.168\.1\.' }).Count -gt 0
$sameSubnetAdapters = @($allIpConfigurations |
    Where-Object {
        ($_.interfaceAlias -ne $WifiInterfaceAlias) -and
        (@($_.ipv4Addresses | Where-Object { $_ -match '^192\.168\.1\.' }).Count -gt 0)
    })
$sameSubnetConflict = $hasExpectedLocalIp -and ($sameSubnetAdapters.Count -gt 0)
$networkCategory = $null
if ($networkProfile.PSObject.Properties.Name -contains 'NetworkCategory') {
    $networkCategory = $networkProfile.NetworkCategory
}

$report = [pscustomobject]@{
    schema = 'dj-library.xdj-aero.wifi-diagnostics.v1'
    generatedAt = (Get-Date).ToString('o')
    mutationPolicy = 'read-only; writes this diagnostics report only'
    repoRoot = $repoRoot
    input = [pscustomobject]@{
        wifiInterfaceAlias = $WifiInterfaceAlias
        aeroSsidPattern = $AeroSsidPattern
        expectedGateway = $ExpectedGateway
    }
    observed = [pscustomobject]@{
        aeroSsidVisible = $aeroSsidVisible
        connectedToAero = $connectedToAero
        wifiIpv4 = $wifiIpv4
        hasExpectedLocalIp = $hasExpectedLocalIp
        gatewayReachable = $gatewayReachable
        networkCategory = $networkCategory
        sameSubnetConflict = $sameSubnetConflict
        rekordboxRunning = ($rekordboxProcesses.Count -gt 0)
        rekordboxLinkUdpPortsOpen = @($rekordboxUdpEndpoints | Where-Object { $_.LocalPort -in @(50000, 50001, 50002, 50004, 50111) }).Count -gt 0
        vpnLikeAdapterCount = $vpnLikeAdapters.Count
    }
    wifiAdapter = $wifiAdapter
    allIpConfigurations = $allIpConfigurations
    wifiIpConfiguration = $wifiIpConfiguration
    sameSubnetAdapters = $sameSubnetAdapters
    networkProfile = $networkProfile
    activeAdapters = $allActiveAdapters
    vpnLikeAdapters = $vpnLikeAdapters
    firewallProfiles = $firewallProfiles
    rekordboxProcesses = $rekordboxProcesses
    rekordboxUdpEndpoints = $rekordboxUdpEndpoints
    rekordboxTcpConnections = $rekordboxTcpConnections
    rekordboxInstallations = $rekordboxInstallations
    wlanInterfacesRaw = $wlanInterfacesRaw
    wlanNetworksRaw = $wlanNetworksRaw
    interpretation = [pscustomobject]@{
        ifSsidInvisible = 'Put the XDJ-AERO in ACCESS POINT(AP) mode, keep wireless LAN enabled, then rescan.'
        ifNoExpectedIp = 'Enable the XDJ-AERO DHCP server or reconnect to the AERO SSID; expected client IP is usually 192.168.1.x.'
        ifGatewayUnreachable = 'Check that the AERO WLAN INFO IP address is 192.168.1.1 and that Windows is connected to the AERO SSID.'
        ifSameSubnetConflict = 'Another adapter is also on 192.168.1.x. Temporarily disconnect that adapter or move the AERO AP to another subnet before testing LINK.'
        ifLinkMissing = 'Use rekordbox EXPORT mode, press the blue rekordbox button on the AERO, then click LINK in rekordbox. Public firewall profile or unsupported rekordbox version can still block discovery.'
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
    ssidVisible = $aeroSsidVisible
    connected = $connectedToAero
    ipOk = $hasExpectedLocalIp
    pingOk = $gatewayReachable
    subnetConflict = $sameSubnetConflict
    rekordboxRunning = ($rekordboxProcesses.Count -gt 0)
    rbLinkPorts = @($rekordboxUdpEndpoints | Where-Object { $_.LocalPort -in @(50000, 50001, 50002, 50004, 50111) }).Count
}
