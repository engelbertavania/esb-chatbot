# Dot-source this from PowerShell to make `gcloud` work in THIS session only:
#   . .\gcloud-env.ps1
# (the leading dot + space means "load into the current shell")

$gcloudCmd = Join-Path $env:LOCALAPPDATA 'Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd'

if (-not (Test-Path $gcloudCmd)) {
    Write-Error "gcloud not found at $gcloudCmd. Is Google Cloud SDK installed?"
    return
}

Set-Alias -Name gcloud -Value $gcloudCmd -Scope Global -Force
$gcloudBin = Split-Path $gcloudCmd -Parent
if (($env:PATH -split ';') -notcontains $gcloudBin) {
    $env:PATH = "$env:PATH;$gcloudBin"
}

Write-Host "gcloud ready in this session." -ForegroundColor Green
& $gcloudCmd --version | Select-Object -First 1
