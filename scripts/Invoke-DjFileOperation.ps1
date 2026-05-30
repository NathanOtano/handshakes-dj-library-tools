[CmdletBinding()]
param(
    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $DatabasePath = (Join-Path $PSScriptRoot '..\runtime\dj-control\control.sqlite'),
    [Parameter(Mandatory)][string] $OperationId,
    [switch] $Apply,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonScript = Join-Path $PSScriptRoot 'dj_file_operation.py'

if (-not (Test-Path -LiteralPath $pythonScript -PathType Leaf)) {
    throw "Python helper not found: $pythonScript"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if ($null -eq $python) {
    throw "Python 3 is required for the file operation helper."
}

$arguments = @(
    $pythonScript,
    '--repo-root', $repoRoot,
    '--config', ([System.IO.Path]::GetFullPath($ConfigPath)),
    '--db', ([System.IO.Path]::GetFullPath($DatabasePath)),
    '--operation-id', $OperationId
)

if ($Apply) {
    $arguments += '--apply'
}
if ($Json) {
    $arguments += '--json'
}

& $python.Source @arguments
exit $LASTEXITCODE
