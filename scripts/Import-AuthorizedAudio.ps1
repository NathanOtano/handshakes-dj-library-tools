[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $SourcePath,
    [string] $DestinationRoot,
    [ValidateSet('Copy', 'Move')]
    [string] $Mode = 'Copy',
    [ValidateSet('Skip', 'Rename', 'Fail')]
    [string] $Conflict = 'Skip',
    [int] $Limit = 0,
    [switch] $Apply,
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

function Normalize-ExistingDirectory {
    param([Parameter(Mandatory)][string] $Path)
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    return $resolved.Path.TrimEnd('\', '/')
}

function Test-PathContains {
    param(
        [Parameter(Mandatory)][string] $Parent,
        [Parameter(Mandatory)][string] $Child
    )

    $parentWithSlash = $Parent.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    return $Child.StartsWith($parentWithSlash, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-AvailableDestinationPath {
    param([Parameter(Mandatory)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $Path
    }

    $directory = Split-Path -Parent $Path
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($Path)
    $extension = [System.IO.Path]::GetExtension($Path)

    for ($i = 1; $i -lt 10000; $i++) {
        $candidate = Join-Path $directory ('{0}_{1:D3}{2}' -f $baseName, $i, $extension)
        if (-not (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    throw "Unable to find available destination for $Path"
}

if (-not (Test-Path -LiteralPath $configFullPath -PathType Leaf)) {
    throw "Config not found: $configFullPath"
}

$config = Get-Content -LiteralPath $configFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$source = Resolve-ConfiguredPath ($(if ([string]::IsNullOrWhiteSpace($SourcePath)) { $config.intakeRoot } else { $SourcePath }))
$destination = Resolve-ConfiguredPath ($(if ([string]::IsNullOrWhiteSpace($DestinationRoot)) { $config.libraryRoot } else { $DestinationRoot }))
$reportsRoot = Resolve-ConfiguredPath $config.reportsRoot

if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "Source folder not found: $source"
}

if (-not (Test-Path -LiteralPath $destination -PathType Container)) {
    throw "Destination folder not found: $destination"
}

$sourceResolved = Normalize-ExistingDirectory -Path $source
$destinationResolved = Normalize-ExistingDirectory -Path $destination

if ($sourceResolved.Equals($destinationResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Source and destination are the same folder: $sourceResolved"
}

if ((Test-PathContains -Parent $sourceResolved -Child $destinationResolved) -or (Test-PathContains -Parent $destinationResolved -Child $sourceResolved)) {
    throw "Source and destination must not contain each other. Source: $sourceResolved Destination: $destinationResolved"
}

$extensions = @($config.audioExtensions | ForEach-Object { $_.ToLowerInvariant() })
$files = Get-ChildItem -LiteralPath $sourceResolved -File -Recurse -ErrorAction Stop |
    Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() }

if ($Limit -gt 0) {
    $files = $files | Select-Object -First $Limit
}

$plan = foreach ($file in $files) {
    $relativePath = [System.IO.Path]::GetRelativePath($sourceResolved, $file.FullName)
    $targetPath = [System.IO.Path]::GetFullPath((Join-Path $destinationResolved $relativePath))
    $action = $Mode.ToLowerInvariant()
    $reason = 'ready'

    if (Test-Path -LiteralPath $targetPath) {
        switch ($Conflict) {
            'Skip' {
                $action = 'skip'
                $reason = 'destination-exists'
            }
            'Rename' {
                $targetPath = Get-AvailableDestinationPath -Path $targetPath
                $reason = 'renamed-conflict'
            }
            'Fail' {
                throw "Destination already exists: $targetPath"
            }
        }
    }

    [pscustomobject]@{
        sourcePath = $file.FullName
        relativePath = $relativePath
        destinationPath = $targetPath
        action = $action
        reason = $reason
        lengthBytes = $file.Length
    }
}

$applied = @()
if ($Apply) {
    New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null

    foreach ($item in $plan) {
        if ($item.action -eq 'skip') {
            $applied += $item
            continue
        }

        $destinationDirectory = Split-Path -Parent $item.destinationPath
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null

        if ($item.action -eq 'copy') {
            Copy-Item -LiteralPath $item.sourcePath -Destination $item.destinationPath
        }
        elseif ($item.action -eq 'move') {
            Move-Item -LiteralPath $item.sourcePath -Destination $item.destinationPath
        }

        $applied += $item
    }

    $manifestPath = Join-Path $reportsRoot ("import-manifest-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    [pscustomobject]@{
        generatedAt = (Get-Date).ToString('o')
        source = $sourceResolved
        destination = $destinationResolved
        mode = $Mode
        conflict = $Conflict
        items = $applied
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
}
else {
    $manifestPath = $null
}

$result = [pscustomobject]@{
    generatedAt = (Get-Date).ToString('o')
    dryRun = -not [bool] $Apply
    source = $sourceResolved
    destination = $destinationResolved
    mode = $Mode
    conflict = $Conflict
    totalItems = @($plan).Count
    actionableItems = @($plan | Where-Object { $_.action -ne 'skip' }).Count
    skippedItems = @($plan | Where-Object { $_.action -eq 'skip' }).Count
    manifestPath = $manifestPath
    plan = $plan
}

if ($Json) {
    $result | ConvertTo-Json -Depth 8
    return
}

$result

