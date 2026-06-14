param(
    [int]$LocalPort = 8000,
    [int]$TimeoutSeconds = 30
)

# Boots an ngrok tunnel to the local backend and registers the resulting public
# URL as the Telegram webhook. ngrok's *.ngrok-free.app hostnames are long-lived
# and resolve reliably for Telegram (unlike fresh trycloudflare hosts, which
# Telegram's resolver negative-caches for minutes). The URL still changes each
# restart, so re-run this whenever ngrok is restarted.
#
# Prereqs (already satisfied on this machine): ngrok installed + authtoken set
#   (ngrok config add-authtoken <TOKEN>).
#
# The ngrok agent opens in its own window; close it (or Ctrl+C) to stop the tunnel.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $ngrok) {
    throw "ngrok not found. Install it (winget install ngrok) and run 'ngrok config add-authtoken <TOKEN>'."
}

# venv lives at the repo root (one level up from backend/).
$venvPython = Join-Path $PSScriptRoot "..\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "venv python not found at $venvPython" }

$api = "http://127.0.0.1:4040/api/tunnels"

# Warn if the backend isn't up — ngrok will tunnel to nothing otherwise.
try {
    $null = Invoke-WebRequest -Uri "http://localhost:$LocalPort/health" -TimeoutSec 4 -UseBasicParsing
} catch {
    Write-Warning "Backend not reachable on http://localhost:$LocalPort - start it first (uvicorn main:app --port $LocalPort)."
}

# Reuse an existing ngrok agent if one is already running, else start one.
$existing = $null
try { $existing = Invoke-RestMethod -Uri $api -TimeoutSec 3 -ErrorAction Stop } catch { $existing = $null }
if ($existing) {
    Write-Host "An ngrok agent is already running on :4040 - reusing it."
} else {
    Write-Host "Starting ngrok http $LocalPort ..."
    Start-Process -FilePath $ngrok -ArgumentList @("http", "$LocalPort") -WindowStyle Normal | Out-Null
}

# Poll the ngrok local API for the public https URL.
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$url = $null
while ((Get-Date) -lt $deadline -and -not $url) {
    Start-Sleep -Milliseconds 600
    try {
        $resp = Invoke-RestMethod -Uri $api -TimeoutSec 4 -ErrorAction Stop
        $t = $resp.tunnels | Where-Object { $_.public_url -like "https://*" } | Select-Object -First 1
        if ($t) { $url = $t.public_url }
    } catch { }
}

if (-not $url) {
    throw "Timed out after $TimeoutSeconds s waiting for the ngrok URL. Open the ngrok window - if it says 'authentication failed', run 'ngrok config add-authtoken <TOKEN>'."
}

Write-Host ""
Write-Host "Tunnel up: $url"
Write-Host "Registering Telegram webhook..."
& $venvPython (Join-Path $PSScriptRoot "_register_webhook.py") $url
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Webhook registration failed (exit $LASTEXITCODE). Tunnel is still running; re-run this script to retry."
} else {
    Write-Host ""
    Write-Host "Done. Public URL: $url"
    Write-Host "Send the bot '/start' to confirm it replies. Close the ngrok window to stop the tunnel."
}
