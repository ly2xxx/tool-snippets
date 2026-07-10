<#
.SYNOPSIS
Complete uninstallation script for OpenClaw on Windows.

.DESCRIPTION
This script performs a thorough cleanup of OpenClaw, including:
- npm global package removal
- Scheduled Task cleanup
- Configuration and data directory removal
- Optional removal of Node.js and Git

.PARAMETER KeepData
If specified, preserves user data (sessions, credentials, config).

.PARAMETER Force
If specified, skips confirmation prompts.

.EXAMPLE
.\uninstall-openclaw.ps1
# Interactive uninstall

.EXAMPLE
.\uninstall-openclaw.ps1 -Force
# Uninstall without confirmations

.EXAMPLE
.\uninstall-openclaw.ps1 -KeepData
# Uninstall but keep user data
#>

param(
    [switch]$KeepData,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

Write-Host "╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║         OpenClaw Complete Uninstallation Script          ║" -ForegroundColor Cyan
Write-Host "╚═══════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Check if running as administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (!$isAdmin) {
    Write-Host "⚠️  Not running as Administrator - some cleanup may fail" -ForegroundColor Yellow
    Write-Host "   (Scheduled Tasks require admin privileges)" -ForegroundColor DarkGray
    Write-Host ""
}

# Confirmation unless -Force
if (!$Force) {
    Write-Host "This will remove:" -ForegroundColor Yellow
    Write-Host "  • OpenClaw npm package" -ForegroundColor White
    Write-Host "  • Scheduled Tasks (gateway, node services)" -ForegroundColor White
    if (!$KeepData) {
        Write-Host "  • Configuration (~\.config\openclaw\)" -ForegroundColor White
        Write-Host "  • User data (~\.openclaw\)" -ForegroundColor White
        Write-Host "  • Sessions, logs, credentials" -ForegroundColor White
    }
    Write-Host ""
    $confirm = Read-Host "Continue? (y/N)"
    if ($confirm -notmatch '^[Yy]') {
        Write-Host "Cancelled." -ForegroundColor DarkGray
        exit 0
    }
    Write-Host ""
}

# Step 1: Stop gateway if running
Write-Host "[1/7] Stopping OpenClaw gateway..." -ForegroundColor Cyan
try {
    if (Get-Command openclaw -ErrorAction SilentlyContinue) {
        & openclaw gateway stop 2>$null
        Write-Host "  ✓ Gateway stopped" -ForegroundColor Green
    } else {
        Write-Host "  ⊘ openclaw command not found (already removed?)" -ForegroundColor DarkGray
    }
} catch {
    Write-Host "  ⚠ Failed to stop gateway: $($_.Exception.Message)" -ForegroundColor Yellow
}
Write-Host ""

# Step 2: Remove Scheduled Tasks
Write-Host "[2/7] Removing Scheduled Tasks..." -ForegroundColor Cyan
$tasks = @(
    "OpenClaw Gateway",
    "OpenClaw Node"
)

foreach ($taskName in $tasks) {
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Host "  ✓ Removed: $taskName" -ForegroundColor Green
        } else {
            Write-Host "  ⊘ Not found: $taskName" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  ✗ Failed to remove $taskName : $($_.Exception.Message)" -ForegroundColor Red
    }
}
Write-Host ""

# Step 3: Uninstall npm package
Write-Host "[3/7] Uninstalling OpenClaw npm package..." -ForegroundColor Cyan
try {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        $npmList = npm list -g openclaw --depth=0 2>&1
        if ($npmList -match 'openclaw@') {
            npm uninstall -g openclaw
            Write-Host "  ✓ npm package removed" -ForegroundColor Green
        } else {
            Write-Host "  ⊘ OpenClaw not installed globally via npm" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "  ⊘ npm not found - skipping npm uninstall" -ForegroundColor DarkGray
    }
} catch {
    Write-Host "  ✗ npm uninstall failed: $($_.Exception.Message)" -ForegroundColor Red
}
Write-Host ""

# Step 4: Clean npm cache (openclaw remnants)
Write-Host "[4/7] Cleaning npm global node_modules..." -ForegroundColor Cyan
try {
    $globalNodeModules = "$env:APPDATA\npm\node_modules\openclaw"
    if (Test-Path $globalNodeModules) {
        Remove-Item -Path $globalNodeModules -Recurse -Force
        Write-Host "  ✓ Removed: $globalNodeModules" -ForegroundColor Green
    } else {
        Write-Host "  ⊘ Already clean" -ForegroundColor DarkGray
    }
} catch {
    Write-Host "  ✗ Failed: $($_.Exception.Message)" -ForegroundColor Red
}
Write-Host ""

# Step 5: Remove configuration directory
if (!$KeepData) {
    Write-Host "[5/7] Removing configuration directory..." -ForegroundColor Cyan
    $configDir = Join-Path $env:USERPROFILE ".config\openclaw"
    try {
        if (Test-Path $configDir) {
            Write-Host "  📁 $configDir" -ForegroundColor DarkGray
            Remove-Item -Path $configDir -Recurse -Force
            Write-Host "  ✓ Configuration removed" -ForegroundColor Green
        } else {
            Write-Host "  ⊘ No configuration found" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  ✗ Failed: $($_.Exception.Message)" -ForegroundColor Red
    }
    Write-Host ""
} else {
    Write-Host "[5/7] Keeping configuration (--KeepData specified)..." -ForegroundColor Cyan
    Write-Host "  📁 Preserved: ~\.config\openclaw\" -ForegroundColor Yellow
    Write-Host ""
}

# Step 6: Remove data directory
if (!$KeepData) {
    Write-Host "[6/7] Removing data directory..." -ForegroundColor Cyan
    $dataDir = Join-Path $env:USERPROFILE ".openclaw"
    try {
        if (Test-Path $dataDir) {
            # Show what's being removed
            $subdirs = Get-ChildItem -Path $dataDir -Directory -ErrorAction SilentlyContinue | Select-Object -First 10
            if ($subdirs) {
                Write-Host "  📁 Contents:" -ForegroundColor DarkGray
                foreach ($dir in $subdirs) {
                    Write-Host "     • $($dir.Name)" -ForegroundColor DarkGray
                }
            }
            
            Write-Host "  📁 $dataDir" -ForegroundColor DarkGray
            Remove-Item -Path $dataDir -Recurse -Force
            Write-Host "  ✓ Data removed (sessions, logs, credentials, etc.)" -ForegroundColor Green
        } else {
            Write-Host "  ⊘ No data directory found" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  ✗ Failed: $($_.Exception.Message)" -ForegroundColor Red
    }
    Write-Host ""
} else {
    Write-Host "[6/7] Keeping data (--KeepData specified)..." -ForegroundColor Cyan
    Write-Host "  📁 Preserved: ~\.openclaw\" -ForegroundColor Yellow
    Write-Host ""
}

# Step 7: Optional removal of Node.js and Git
Write-Host "[7/7] Optional dependency removal..." -ForegroundColor Cyan

if (!$Force) {
    $uninstallNode = Read-Host "  Remove Node.js? (y/N)"
    if ($uninstallNode -match '^[Yy]') {
        try {
            winget uninstall -e --id OpenJS.NodeJS.LTS --accept-source-agreements
            Write-Host "  ✓ Node.js removed" -ForegroundColor Green
        } catch {
            Write-Host "  ✗ Node.js removal failed" -ForegroundColor Red
        }
    } else {
        Write-Host "  ⊘ Keeping Node.js" -ForegroundColor DarkGray
    }

    $uninstallGit = Read-Host "  Remove Git? (y/N)"
    if ($uninstallGit -match '^[Yy]') {
        try {
            winget uninstall -e --id Git.Git --accept-source-agreements
            Write-Host "  ✓ Git removed" -ForegroundColor Green
        } catch {
            Write-Host "  ✗ Git removal failed" -ForegroundColor Red
        }
    } else {
        Write-Host "  ⊘ Keeping Git" -ForegroundColor DarkGray
    }
} else {
    Write-Host "  ⊘ Skipping (use interactive mode to remove Node.js/Git)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║              Uninstallation Complete! ✓                  ║" -ForegroundColor Green
Write-Host "╚═══════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

if ($KeepData) {
    Write-Host "📦 Your data was preserved:" -ForegroundColor Yellow
    Write-Host "   • Configuration: ~\.config\openclaw\" -ForegroundColor White
    Write-Host "   • Data: ~\.openclaw\" -ForegroundColor White
    Write-Host ""
}

Write-Host "💡 Next steps:" -ForegroundColor Cyan
Write-Host "   • Restart your terminal/PowerShell" -ForegroundColor White
Write-Host "   • Verify removal: openclaw --version (should fail)" -ForegroundColor White
if (!$KeepData) {
    Write-Host "   • All data removed - fresh install possible" -ForegroundColor White
} else {
    Write-Host "   • Your data is safe for reinstall" -ForegroundColor White
}
Write-Host ""
