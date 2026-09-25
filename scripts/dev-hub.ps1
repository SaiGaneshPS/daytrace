# DT-7 / DT-10 / DT-48: start a hub profile on Windows.
# Usage: .\scripts\dev-hub.ps1 -Profile personal|shared-dev|demo
# Profile names are validated by the hub itself (daytrace_hub.config.PROFILES), so they live in one place.
param(
    [Alias('Profile')]
    [string]$HubProfile = 'personal'
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '..\hub\.venv\Scripts\python.exe'
& $python -m daytrace_hub run --profile $HubProfile
exit $LASTEXITCODE
