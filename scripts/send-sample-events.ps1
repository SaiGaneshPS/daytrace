# DT-15: post a few sample events to a running hub (Windows).
# Usage:
#   $env:DAYTRACE_DEVICE_ID = 'android-1'    # the device_id pairing returned with the token
#   .\scripts\send-sample-events.ps1 -HubUrl http://127.0.0.1:8765
# The script asks for the token without showing it (it is never saved to your PowerShell history). To set it
# yourself, do it the same way, never by typing it into a command:
#   $env:DAYTRACE_TOKEN = [Net.NetworkCredential]::new('', (Read-Host 'Device token' -AsSecureString)).Password
# The token never appears on a command line either: Invoke-RestMethod runs inside this PowerShell process.
param([string]$HubUrl = 'http://127.0.0.1:8765')
$ErrorActionPreference = 'Stop'
if (-not $env:DAYTRACE_DEVICE_ID) { throw 'Set $env:DAYTRACE_DEVICE_ID to the device_id pairing returned with the token.' }
if ($env:DAYTRACE_DEVICE_ID -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw 'DAYTRACE_DEVICE_ID does not look like a device_id.' }
$token = $env:DAYTRACE_TOKEN
if (-not $token) {
    $token = [Net.NetworkCredential]::new('', (Read-Host 'Device token' -AsSecureString)).Password
}

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
    -Headers @{ Authorization = "Bearer $token" } `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$response | ConvertTo-Json -Depth 5
if (@($response.rejected).Count -gt 0) {
    Write-Error 'The hub rejected some events (see "rejected" above; is DAYTRACE_DEVICE_ID the token''s device?).'
    exit 1
}
