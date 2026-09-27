# DT-7 / DT-10 / DT-48: start a hub profile on Windows.
# Usage: .\scripts\dev-hub.ps1 -Profile personal|shared-dev|demo
#        .\scripts\dev-hub.ps1 -Profile demo [-Days 14] [-NoSeed] [-NoWarmUp] [-NoOpen]
# With the execution policy locked (Windows' default), run it without changing any setting:
#        powershell -ExecutionPolicy Bypass -File .\scripts\dev-hub.ps1 -Profile demo
# The demo profile is made ready for a demo (docs/demo-script.md): its days are seeded again (always safe: only demo
# data is replaced), the hub starts, the local model is woken up by writing yesterday's story and last week's Wrapped
# ahead (so they show at once on stage), and the dashboard opens. Ctrl+C stops the hub. Other profiles just start.
# Profile names are validated by the hub itself (daytrace_hub.config.PROFILES), so they live in one place.
param(
    [Alias('Profile')]
    [string]$HubProfile = 'personal',
    [int]$Days = 14,
    [switch]$NoSeed,
    [switch]$NoWarmUp,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '..\hub\.venv\Scripts\python.exe'
if ($HubProfile -ne 'demo') {
    & $python -m daytrace_hub run --profile $HubProfile
    exit $LASTEXITCODE
}

$port = & $python -c "from daytrace_hub.config import get_profile; print(get_profile('demo').port)"
$hub = "http://localhost:$port"

function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }

function Ask([string]$Path, [int]$Seconds = 15) {
    # The hub trusts this computer when it is asked at localhost: no token needed.
    Invoke-RestMethod -Uri "$hub$Path" -TimeoutSec $Seconds
}

if (-not $NoSeed) {
    Step "Seeding $Days days of demo data"
    & $python -m daytrace_hub seed --profile demo --days $Days
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Something already answering here is not this run's hub: say so instead of taking it for ours.
try {
    $already = Ask '/api/v1/health' 2
    throw "A hub already answers at $hub (the $($already.profile) profile): stop it (Ctrl+C in its window), then run this again."
} catch [System.Net.WebException] { }

Step "Starting the demo hub at $hub"
$server = Start-Process -FilePath $python -ArgumentList @('-m', 'daytrace_hub', 'run', '--profile', 'demo') -NoNewWindow -PassThru
$null = $server.Handle  # kept, so the exit code can be read if it stops
try {
    $deadline = (Get-Date).AddSeconds(60)
    while ($true) {
        if ($server.HasExited) { throw "The hub stopped (exit code $($server.ExitCode)): see its messages above." }
        try {
            $health = Ask '/api/v1/health' 3
            if ($health.profile -ne 'demo') { throw "Something else answers at $hub (the $($health.profile) profile)." }
            break
        } catch [System.Net.WebException] { }
        if ((Get-Date) -gt $deadline) { throw "The hub didn't answer at $hub within a minute." }
        Start-Sleep -Milliseconds 500
    }

    if (-not $NoWarmUp) {
        Step 'Waking up the local model'
        # Nothing here may stop the demo: at worst the AI steps show their plain fallbacks.
        try {
            $status = Ask '/api/v1/ai/status' 180  # loading a model the first time can take a while
            if ($status.reachable) {
                Write-Host "The model $($status.model) answers. Writing ahead what the demo shows (the first time can take a minute)..."
                $yesterday = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
                $took = Measure-Command { $story = Ask "/api/v1/story?date=$yesterday" 600 }
                Write-Host ("  Yesterday's story: {0:n0} s, {1}" -f $took.TotalSeconds, $(if ($story.fallback) { "plain (the model's didn't pass the number check)" } else { "by $($story.model)" }))
                $took = Measure-Command { $week = Ask '/api/v1/wrapped' 600 }
                Write-Host ("  Last week's Wrapped: {0:n0} s, {1}" -f $took.TotalSeconds, $(if ($week.fallback) { 'plain lines' } else { "lines by $($week.model)" }))
            } else {
                Write-Warning "The local model isn't answering ($($status.error)). Start LM Studio and load a model, then run this again with -NoSeed: until then the AI steps show their plain fallbacks."
            }
        } catch {
            Write-Warning "Waking the model up didn't finish ($($_.Exception.Message)). The hub keeps running; the AI steps are written when first opened, or show their plain fallbacks."
        }
    }

    if (-not $NoOpen) { Start-Process $hub }
    Step "Ready: $hub (Ctrl+C stops the hub)"
    Write-Host 'Live events or a nudge without the phone: .\hub\.venv\Scripts\python.exe -m daytrace_hub demo live (or nudge)'
    Wait-Process -Id $server.Id
} finally {
    if (-not $server.HasExited) { Stop-Process -Id $server.Id }
}
