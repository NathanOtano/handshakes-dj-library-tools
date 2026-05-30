[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $RootPath,
    [int] $Limit = 0,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$configFullPath = [System.IO.Path]::GetFullPath($ConfigPath)

function Test-IsAbsolutePath {
    param([Parameter(Mandatory)][string] $Path)
    $root = [System.IO.Path]::GetPathRoot($Path)
    return -not [string]::IsNullOrWhiteSpace($root)
}

function Resolve-ConfiguredPath {
    param([AllowNull()][string] $Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    if (Test-IsAbsolutePath -Path $Path) {
        return [System.IO.Path]::GetFullPath($Path)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Path))
}

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}

$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if ($null -eq $ffprobe) {
    throw "ffprobe not found in PATH. Install ffmpeg/ffprobe or add it to PATH."
}

$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$root = Resolve-ConfiguredPath ($(if ([string]::IsNullOrWhiteSpace($RootPath)) { $config.libraryRoot } else { $RootPath }))
$reportsRoot = Resolve-ConfiguredPath $config.reportsRoot

if (-not (Test-Path -LiteralPath $root -PathType Container)) {
    throw "Audio root not found: $root"
}

New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null

$extensions = @($config.audioExtensions | ForEach-Object { $_.ToLowerInvariant() })
$files = Get-ChildItem -LiteralPath $root -File -Recurse -ErrorAction Stop |
    Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() }

if ($Limit -gt 0) {
    $files = $files | Select-Object -First $Limit
}

$rows = foreach ($file in $files) {
    $probeJson = & $ffprobe.Source -v error -select_streams a:0 -show_entries stream=codec_name,codec_type,sample_rate,bits_per_sample,bit_rate,channels,channel_layout -show_entries format=duration,bit_rate,format_name -of json -- $file.FullName
    $probe = $probeJson | ConvertFrom-Json
    $stream = @($probe.streams | Where-Object { $_.codec_type -eq 'audio' } | Select-Object -First 1)[0]

    $sampleRateRaw = $stream.PSObject.Properties['sample_rate']
    $bitsPerSampleRaw = $stream.PSObject.Properties['bits_per_sample']
    $streamBitRateRaw = $stream.PSObject.Properties['bit_rate']
    $channelLayoutRaw = $stream.PSObject.Properties['channel_layout']
    $formatBitRateRaw = $probe.format.PSObject.Properties['bit_rate']
    $durationRaw = $probe.format.PSObject.Properties['duration']

    $sampleRate = if ($null -ne $sampleRateRaw -and $sampleRateRaw.Value) { [int] $sampleRateRaw.Value } else { $null }
    $bitsPerSample = if ($null -ne $bitsPerSampleRaw -and $bitsPerSampleRaw.Value) { [int] $bitsPerSampleRaw.Value } else { $null }
    $streamBitRate = if ($null -ne $streamBitRateRaw -and $streamBitRateRaw.Value) { [int64] $streamBitRateRaw.Value } else { $null }
    $formatBitRate = if ($null -ne $formatBitRateRaw -and $formatBitRateRaw.Value) { [int64] $formatBitRateRaw.Value } else { $null }
    $duration = if ($null -ne $durationRaw -and $durationRaw.Value) { [double] $durationRaw.Value } else { $null }

    $cdQualityCandidate = (
        ($sampleRate -eq 44100) -and
        ($bitsPerSample -eq 16) -and
        ($stream.codec_name -in @('flac', 'alac', 'pcm_s16le', 'pcm_s16be'))
    )

    [pscustomobject]@{
        relativePath = [System.IO.Path]::GetRelativePath($root, $file.FullName)
        fullPath = $file.FullName
        extension = $file.Extension.ToLowerInvariant()
        codec = $stream.codec_name
        format = $probe.format.format_name
        sampleRate = $sampleRate
        bitsPerSample = $bitsPerSample
        channels = $stream.channels
        channelLayout = if ($null -ne $channelLayoutRaw) { $channelLayoutRaw.Value } else { $null }
        streamBitRate = $streamBitRate
        formatBitRate = $formatBitRate
        durationSeconds = $duration
        lengthBytes = $file.Length
        cdQualityCandidate = $cdQualityCandidate
    }
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$csvPath = Join-Path $reportsRoot "authorized-audio-quality-$timestamp.csv"
$rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8

$result = [pscustomobject]@{
    generatedAt = (Get-Date).ToString('o')
    rootPath = $root
    count = @($rows).Count
    csvPath = $csvPath
    rows = $rows
}

if ($Json) {
    $result | ConvertTo-Json -Depth 8
    return
}

$result
