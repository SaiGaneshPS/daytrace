# DT-7 / DT-10 / DT-48: start a hub profile on Windows.
# Usage: .\scripts\dev-hub.ps1 -Profile personal|shared-dev|demo
param(
    [Alias('Profile')]
    [ValidateSet('personal', 'shared-dev', 'demo')]
    [string]$HubProfile = 'personal'
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '..\hub\.venv\Scripts\python.exe'
& $python -m daytrace_hub run --profile $HubProfile
