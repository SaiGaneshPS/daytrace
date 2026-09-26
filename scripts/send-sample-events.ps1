# DT-15: post a few sample events to a running hub (Windows).
# Usage:
#   $env:DAYTRACE_TOKEN = '<device token>'   # from pairing; never a command-line argument
#   $env:DAYTRACE_DEVICE_ID = 'android-1'    # the device_id pairing returned with that token
#   .\scripts\send-sample-events.ps1 -HubUrl http://127.0.0.1:8765
# The token comes from the DAYTRACE_TOKEN environment variable, never a command-line argument,
# so it stays out of your shell history and the process list (Invoke-RestMethod runs in this process).
param([string]$HubUrl = 'http://127.0.0.1:8765')
$ErrorActionPreference = 'Stop'
if (-not $env:DAYTRACE_TOKEN) { throw 'Set $env:DAYTRACE_TOKEN to a paired device token first.' }
if (-not $env:DAYTRACE_DEVICE_ID) { throw 'Set $env:DAYTRACE_DEVICE_ID to the device_id pairing returned with the token.' }

$now = [DateTimeOffset]::Now
function Stamp([DateTimeOffset]$Moment) {
    $Moment.ToString("yyyy-MM-dd'T'HH:mm:sszzz", [Globalization.CultureInfo]::InvariantCulture)
}
$device = $env:DAYTRACE_DEVICE_ID
$events = @(
    @{ device_id = $device; kind = 'app_session'; source = 'manual'; app = 'Instagram'
       start = (Stamp $now.AddMinutes(-6)); end = (Stamp $now.AddMinutes(-2)) },
    @{ device_id = $device; kind = 'app_session'; source = 'manual'; app = 'YouTube'
       start = (Stamp $now.AddMinutes(-2)); end = (Stamp $now) },
    @{ device_id = $device; kind = 'meal'; source = 'manual'; start = (Stamp $now)
       data = @{ text = 'two rotis and dal'; meal_type = 'dinner' } }
)
$body = @{ events = $events } | ConvertTo-Json -Depth 5
$response = Invoke-RestMethod -Method Post -Uri "$HubUrl/api/v1/events" `
    -Headers @{ Authorization = "Bearer $env:DAYTRACE_TOKEN" } `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$response | ConvertTo-Json -Depth 5
