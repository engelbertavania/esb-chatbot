<#
.SYNOPSIS
    Deploy the FastAPI backend to Google Cloud Run (free tier).

.DESCRIPTION
    Reads your local .env, converts it to a temporary env-vars file that gcloud
    understands, deploys from source (uses the Dockerfile), then deletes the
    temp file so no secrets are left on disk.

.NOTES
    One-time setup -- run these YOURSELF in the Claude Code prompt with the `!`
    prefix so the interactive browser login works:

        ! gcloud auth login
        ! gcloud config set project YOUR_PROJECT_ID

    Then deploy:
        .\deploy.ps1
        .\deploy.ps1 -Project my-proj -Region us-central1 -Service esb-chatbot

    After it prints the service URL, register the Telegram webhook:
        .\venv\Scripts\python.exe _register_webhook.py https://<service-url>

    NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads a BOM-less
    .ps1 as cp1252, so any multibyte char (em-dash, smart quote) corrupts the
    parse. Use plain '-' and straight quotes only.
#>
param(
    [string]$Service = "esb-chatbot",
    [string]$Region  = "us-central1",
    [string]$Project = "",
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

# 1. gcloud must be installed.
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud CLI not found. Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
}

# 2. Build a YAML env-vars file from .env (avoids comma-escaping issues that
#    --set-env-vars has with values like DATABASE_URL).
if (-not (Test-Path $EnvFile)) {
    throw "$EnvFile not found. Copy .env.example to .env and fill in real values first."
}

# PORT is injected by Cloud Run; the credentials FILE path won't exist in the
# container (the service uses its own service-account identity instead).
$skip = @("PORT", "GOOGLE_APPLICATION_CREDENTIALS")

$envYaml = Join-Path $PSScriptRoot ".env.yaml"
$sb = New-Object System.Text.StringBuilder
$count = 0
foreach ($line in (Get-Content $EnvFile)) {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $idx = $t.IndexOf("=")
    if ($idx -lt 1) { continue }
    $key = $t.Substring(0, $idx).Trim()
    $val = $t.Substring($idx + 1).Trim()
    if ($skip -contains $key) { continue }
    if ($val -eq "") { continue }
    # Strip surrounding quotes if the .env wrapped the value.
    if (($val.StartsWith('"') -and $val.EndsWith('"')) -or `
        ($val.StartsWith("'") -and $val.EndsWith("'"))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    $esc = $val.Replace("\", "\\").Replace('"', '\"')
    [void]$sb.AppendLine("${key}: `"$esc`"")
    $count++
}

# Write UTF-8 WITHOUT BOM. gcloud's YAML parser chokes on a BOM.
[System.IO.File]::WriteAllText($envYaml, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Wrote $envYaml with $count env vars." -ForegroundColor Cyan

# 3. Deploy. --max-instances 1 keeps in-memory SESSION_STATE in a single
#    process; scale-to-zero (default min-instances 0) keeps you on the free tier.
$deployArgs = @(
    "run", "deploy", $Service,
    "--source", ".",
    "--quiet",
    "--region", $Region,
    "--platform", "managed",
    "--allow-unauthenticated",
    "--max-instances", "1",
    "--memory", "1Gi",
    "--cpu", "1",
    "--timeout", "300",
    "--env-vars-file", $envYaml
)
if ($Project -ne "") { $deployArgs += @("--project", $Project) }

try {
    Write-Host "Deploying '$Service' to Cloud Run ($Region)..." -ForegroundColor Green
    & gcloud @deployArgs
    if ($LASTEXITCODE -ne 0) { throw "gcloud run deploy failed (exit $LASTEXITCODE)." }
} finally {
    # 4. Never leave secrets on disk, even if the deploy failed.
    Remove-Item $envYaml -Force -ErrorAction SilentlyContinue
}

# 5. Show the URL + next step.
$url = (& gcloud run services describe $Service --region $Region --format "value(status.url)" 2>$null)
Write-Host "`nDeployed. Service URL:" -ForegroundColor Green
Write-Host "  $url"
Write-Host "`nNext - register the Telegram webhook:" -ForegroundColor Green
Write-Host "  .\venv\Scripts\python.exe _register_webhook.py $url"
Write-Host "`nThen set CORS_ALLOW_ORIGINS to your Vercel URL, e.g.:" -ForegroundColor Green
Write-Host "  gcloud run services update $Service --region $Region --set-env-vars CORS_ALLOW_ORIGINS=https://your-app.vercel.app"
