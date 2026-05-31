param(
    [int]$LocalPort = 8000,
    [int]$TimeoutSeconds = 30
)

# Boots a Cloudflare quick tunnel and registers the resulting *.trycloudflare.com
# URL as the Telegram webhook. Re-run any time the URL needs to be refreshed
# (it changes every time cloudflared restarts). Press Ctrl+C to stop the tunnel.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $cloudflared)) {
    $cloudflared = (Get-Command cloudflared -ErrorAction Stop).Source
}

$venvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython"
}

$logPath = Join-Path $PSScriptRoot ".cloudflared.log"
if (Test-Path $logPath) { Remove-Item $logPath -Force }

Write-Host "Starting cloudflared -> http://localhost:$LocalPort ..."
$proc = Start-Process -FilePath $cloudflared `
    -ArgumentList @("tunnel","--url","http://localhost:$LocalPort") `
    -RedirectStandardOutput $logPath `
    -RedirectStandardError "$logPath.err" `
    -NoNewWindow -PassThru

# cloudflared writes to stderr; merge after start so both streams feed parsing.
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$url = $null
while ((Get-Date) -lt $deadline -and -not $url) {
    Start-Sleep -Milliseconds 500
    foreach ($f in @($logPath, "$logPath.err")) {
        if (Test-Path $f) {
            $match = Select-String -Path $f -Pattern "https://[a-zA-Z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($match) { $url = $match.Matches[0].Value; break }
        }
    }
}

if (-not $url) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    throw "Timed out after $TimeoutSeconds s waiting for trycloudflare URL. Check $logPath"
}

Write-Host ""
Write-Host "Tunnel up: $url"
Write-Host "Registering Telegram webhook..."
& $venvPython (Join-Path $PSScriptRoot "_register_webhook.py") $url
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Webhook registration failed (exit $LASTEXITCODE). Tunnel is still running."
} else {
    Write-Host ""
    Write-Host "Done. Dashboard: $url"
    Write-Host "Press Ctrl+C to stop the tunnel."
}

try {
    Wait-Process -Id $proc.Id
} finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
}
