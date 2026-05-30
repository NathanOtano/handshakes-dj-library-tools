[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $RootPath,
    [int] $Limit = 0,
    [switch] $Hash,
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

$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$libraryRoot = Resolve-ConfiguredPath ($(if ([string]::IsNullOrWhiteSpace($RootPath)) { $config.libraryRoot } else { $RootPath }))
$reportsRoot = Resolve-ConfiguredPath $config.reportsRoot

if (-not (Test-Path -LiteralPath $libraryRoot -PathType Container)) {
    throw "Library root not found: $libraryRoot"
}

New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null

$extensions = @($config.audioExtensions | ForEach-Object { $_.ToLowerInvariant() })
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$csvPath = Join-Path $reportsRoot "library-inventory-$timestamp.csv"
$jsonlPath = Join-Path $reportsRoot "library-inventory-$timestamp.jsonl"

$files = Get-ChildItem -LiteralPath $libraryRoot -File -Recurse -ErrorAction Stop |
    Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() }

if ($Limit -gt 0) {
    $files = $files | Select-Object -First $Limit
}

$rows = foreach ($file in $files) {
    $hashValue = $null
    if ($Hash) {
        $hashValue = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }

    [pscustomobject]@{
        relativePath = [System.IO.Path]::GetRelativePath($libraryRoot, $file.FullName)
        fullPath = $file.FullName
        fileName = $file.Name
        extension = $file.Extension.ToLowerInvariant()
        directory = $file.DirectoryName
        lengthBytes = $file.Length
        createdTimeUtc = $file.CreationTimeUtc.ToString('o')
        lastWriteTimeUtc = $file.LastWriteTimeUtc.ToString('o')
        sha256 = $hashValue
    }
}

$rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$writer = [System.IO.StreamWriter]::new($jsonlPath, $false, $utf8NoBom)
try {
    foreach ($row in $rows) {
        $writer.WriteLine(($row | ConvertTo-Json -Compress))
    }
}
finally {
    $writer.Dispose()
}

$result = [pscustomobject]@{
    generatedAt = (Get-Date).ToString('o')
    libraryRoot = $libraryRoot
    count = @($rows).Count
    hashIncluded = [bool] $Hash
    csvPath = $csvPath
    jsonlPath = $jsonlPath
}

if ($Json) {
    $result | ConvertTo-Json -Depth 4
    return
}

$result

