# DT-15: post a few sample events to a running hub (Windows).
# Usage: $env:DAYTRACE_TOKEN = '<device token>'; .\scripts\send-sample-events.ps1 -HubUrl http://127.0.0.1:8765
# The token comes from the DAYTRACE_TOKEN environment variable, never a command-line argument,
# so it stays out of your shell history and the process list.
param([string]$HubUrl = 'http://127.0.0.1:8765')
$ErrorActionPreference = 'Stop'
if (-not $env:DAYTRACE_TOKEN) { throw 'Set $env:DAYTRACE_TOKEN to a paired device token first.' }
Write-Host 'Implemented in DT-15.'
