param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,
    [switch]$NoBrowser
)

# Boots the full Sukabot Suite for a demo: the FastAPI backend (data/API +
# Telegram webhook) and the Next.js UI (suite + Supabase auth), each in its own
# PowerShell window. Close those windows (or Ctrl+C in them) to stop the servers.
#
#   .\start-suite.ps1                      # backend :8000, frontend :3000, opens browser
#   .\start-suite.ps1 -FrontendPort 3001   # override a busy port
#   .\start-suite.ps1 -NoBrowser           # do not auto-open the browser

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- Preflight ---------------------------------------------------------------
# venv lives inside backend/.
$venvPython = Join-Path $PSScriptRoot "backend\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "venv python not found at $venvPython. Create the venv and install deps first."
}

$backendDir = Join-Path $PSScriptRoot "backend"
if (-not (Test-Path (Join-Path $backendDir "main.py"))) {
    throw "backend not found at $backendDir (expected backend\main.py)."
}

$frontendDir = Join-Path $PSScriptRoot "frontend"
if (-not (Test-Path (Join-Path $frontendDir "package.json"))) {
    throw "frontend not found at $frontendDir. Run 'git submodule update --init' then 'npm install' in frontend."
}
if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    Write-Warning "frontend\node_modules is missing - run 'npm install' in $frontendDir first."
}
if (-not (Test-Path (Join-Path $frontendDir ".env.local"))) {
    Write-Warning "frontend\.env.local is missing - Supabase login will not work without it."
}

function Test-PortBusy([int]$port) {
    try { return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop) }
    catch { return $false }
}
foreach ($p in @($BackendPort, $FrontendPort)) {
    if (Test-PortBusy $p) {
        Write-Warning "Port $p is already in use. A stale server may be running (find it with: netstat -ano | findstr :$p)."
    }
}

# --- Launch backend ----------------------------------------------------------
Write-Host "Starting FastAPI backend on http://localhost:$BackendPort ..."
Start-Process -FilePath "powershell" -WorkingDirectory $backendDir -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle = 'Sukabot backend :$BackendPort'; & '$venvPython' -m uvicorn main:app --port $BackendPort"
) | Out-Null

# --- Launch frontend ---------------------------------------------------------
Write-Host "Starting Next.js frontend on http://localhost:$FrontendPort ..."
Start-Process -FilePath "powershell" -WorkingDirectory $frontendDir -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle = 'Sukabot frontend :$FrontendPort'; npm run dev -- -p $FrontendPort"
) | Out-Null

# --- Wait for the UI, then open the browser ----------------------------------
if (-not $NoBrowser) {
    Write-Host "Waiting for the UI to come up..."
    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Date) -lt $deadline -and -not (Test-PortBusy $FrontendPort)) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-PortBusy $FrontendPort) {
        Start-Process "http://localhost:$FrontendPort"
    } else {
        Write-Warning "Frontend did not open within 40s - check its window, then visit http://localhost:$FrontendPort manually."
    }
}

Write-Host ""
Write-Host "Sukabot Suite launching:"
Write-Host "  UI:      http://localhost:$FrontendPort   (login: demo@sukabot.app / Sukabot2026!)"
Write-Host "  Backend: http://localhost:$BackendPort/api/tickets"
Write-Host ""
Write-Host "Two PowerShell windows opened (backend + frontend). Close them to stop the servers."
