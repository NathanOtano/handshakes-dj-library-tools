[CmdletBinding()]
param(
    [ValidateSet('init', 'snapshot', 'status', 'plan-operation', 'list-operations')]
    [string] $Mode = 'status',

    [string] $ConfigPath = (Join-Path $PSScriptRoot '..\config\dj-library.paths.json'),
    [string] $SchemaPath = (Join-Path $PSScriptRoot '..\schemas\dj-control-db.schema.sql'),
    [string] $DatabasePath = (Join-Path $PSScriptRoot '..\runtime\dj-control\control.sqlite'),
    [string] $RootPath,
    [int] $Limit = 0,
    [string] $OperationId,
    [string] $OperationType,
    [string] $OperationStatus,
    [string] $TargetKind,
    [string] $TargetRef,
    [string] $PayloadJson = '{}',
    [string] $VerifierJson = '{}',
    [string] $FileActionStatus = 'pending',
    [string] $RekordboxActionStatus = 'pending',
    [string] $EventMessage,
    [switch] $Hash,
    [switch] $Apply,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonScript = Join-Path $PSScriptRoot 'dj_control_db.py'

if (-not (Test-Path -LiteralPath $pythonScript -PathType Leaf)) {
    throw "Python helper not found: $pythonScript"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if ($null -eq $python) {
    throw "Python 3 is required for the SQLite control database helper."
}

$arguments = @(
    $pythonScript,
    $Mode,
    '--repo-root', $repoRoot,
    '--config', ([System.IO.Path]::GetFullPath($ConfigPath)),
    '--schema', ([System.IO.Path]::GetFullPath($SchemaPath)),
    '--db', ([System.IO.Path]::GetFullPath($DatabasePath)),
    '--limit', $Limit.ToString([System.Globalization.CultureInfo]::InvariantCulture)
)

if (-not [string]::IsNullOrWhiteSpace($RootPath)) {
    $arguments += @('--root-path', $RootPath)
}
if ([string]::IsNullOrWhiteSpace($OperationStatus) -and $Mode -eq 'plan-operation') {
    $OperationStatus = 'draft'
}
if (-not [string]::IsNullOrWhiteSpace($OperationId)) {
    $arguments += @('--operation-id', $OperationId)
}
if (-not [string]::IsNullOrWhiteSpace($OperationType)) {
    $arguments += @('--operation-type', $OperationType)
}
if (-not [string]::IsNullOrWhiteSpace($OperationStatus)) {
    $arguments += @('--status', $OperationStatus)
}
if (-not [string]::IsNullOrWhiteSpace($TargetKind)) {
    $arguments += @('--target-kind', $TargetKind)
}
if (-not [string]::IsNullOrWhiteSpace($TargetRef)) {
    $arguments += @('--target-ref', $TargetRef)
}
if (-not [string]::IsNullOrWhiteSpace($PayloadJson)) {
    $arguments += @('--payload-json', $PayloadJson)
}
if (-not [string]::IsNullOrWhiteSpace($VerifierJson)) {
    $arguments += @('--verifier-json', $VerifierJson)
}
if (-not [string]::IsNullOrWhiteSpace($FileActionStatus)) {
    $arguments += @('--file-action-status', $FileActionStatus)
}
if (-not [string]::IsNullOrWhiteSpace($RekordboxActionStatus)) {
    $arguments += @('--rekordbox-action-status', $RekordboxActionStatus)
}
if (-not [string]::IsNullOrWhiteSpace($EventMessage)) {
    $arguments += @('--event-message', $EventMessage)
}
if ($Hash) {
    $arguments += '--hash'
}
if ($Apply) {
    $arguments += '--apply'
}
if ($Json) {
    $arguments += '--json'
}

& $python.Source @arguments
exit $LASTEXITCODE
