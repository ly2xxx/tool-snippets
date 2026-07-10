<#
.SYNOPSIS
Installs Node.js, Git, and OpenClaw on Windows.
#>

$ErrorActionPreference = 'Stop'

Write-Host "Starting installation process for dependencies and OpenClaw..." -ForegroundColor Cyan

# Check and Install Git via winget
Write-Host "`n[1/3] Installing Git..." -ForegroundColor Yellow
winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements

# Check and Install Node.js via winget
Write-Host "`n[2/3] Installing Node.js..." -ForegroundColor Yellow
winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements

# Install OpenClaw
Write-Host "`n[3/3] Installing OpenClaw..." -ForegroundColor Yellow
iwr -useb https://openclaw.ai/install.ps1 | iex

Write-Host "`nInstallation complete!" -ForegroundColor Green
Write-Host "Note: You may need to restart your terminal/PowerShell for environment variables (like 'git' and 'npm') to take effect." -ForegroundColor Cyan
