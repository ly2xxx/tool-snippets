#requires -Version 5.1
<#
.SYNOPSIS
  Reclaim space on the C: drive. Self-elevates. Safe defaults.

.DESCRIPTION
  Default actions (non-destructive to your data):
    A. Clear user-level regenerable caches (SquirrelTemp, npm, uv, pip).
    B. Delete C:\$GetCurrent Windows-upgrade staging if present.
    C. Shut down WSL + Docker and compact every discovered .vhdx
       (under %LOCALAPPDATA%\Docker and %LOCALAPPDATA%\wsl).
    D. Empty %LOCALAPPDATA%\Temp (preserving the 'claude' subdir).

  Optional, controlled by switches:
    -PruneDocker         Start Docker, run 'docker system prune -af --volumes'
                         and 'docker builder prune -af' BEFORE compacting.
                         Aggressive: removes any image/container/volume not
                         currently in use. Skip unless that's what you want.
    -DismCleanup         Run 'DISM /Online /Cleanup-Image /StartComponentCleanup
                         /ResetBase' (5-15 min; reclaims WinSxS bloat).
    -DisableHibernation  Run 'powercfg /h off' (deletes hiberfil.sys, ~RAM size).
    -SkipDocker          Don't touch Docker or WSL at all.
    -DryRun              Report only. Nothing is deleted, compacted, or shut down.
    -LogDir <path>       Where to write the timestamped log. Default: script dir.

  The script self-elevates via UAC. Run it from any PowerShell prompt.

.EXAMPLE
  .\reclaim-disk.ps1
  Safe default cleanup.

.EXAMPLE
  .\reclaim-disk.ps1 -DryRun
  Show what would be freed without changing anything.

.EXAMPLE
  .\reclaim-disk.ps1 -PruneDocker -DismCleanup
  Full cleanup: prune unused Docker content + DISM component cleanup.
#>
[CmdletBinding()]
param(
  [switch]$DryRun,
  [switch]$PruneDocker,
  [switch]$SkipDocker,
  [switch]$DisableHibernation,
  [switch]$DismCleanup,
  [string]$LogDir
)

$ErrorActionPreference = 'Continue'

# ----- Self-elevate -----
function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  ([Security.Principal.WindowsPrincipal]::new($id)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
  Write-Host "Not Administrator. Re-launching elevated (approve UAC)..." -ForegroundColor Yellow
  $reArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File',$PSCommandPath)
  foreach ($k in $PSBoundParameters.Keys) {
    $v = $PSBoundParameters[$k]
    if ($v -is [switch]) { if ($v) { $reArgs += "-$k" } }
    else { $reArgs += @("-$k", $v) }
  }
  Start-Process powershell -Verb RunAs -ArgumentList $reArgs -WorkingDirectory $PWD
  exit
}

# ----- Logging -----
if (-not $LogDir) { $LogDir = if ($PSScriptRoot) { $PSScriptRoot } else { 'C:\Temp' } }
if (-not (Test-Path $LogDir)) { New-Item -Path $LogDir -ItemType Directory -Force | Out-Null }
$LogFile = Join-Path $LogDir ("reclaim_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

function Log {
  param([string]$Msg, [ConsoleColor]$Color = 'Gray')
  $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $Msg
  $line | Out-File -FilePath $LogFile -Append -Encoding utf8
  Write-Host $line -ForegroundColor $Color
}

# ----- Helpers -----
function Get-FreeGB { [math]::Round((Get-PSDrive C).Free/1GB, 2) }
function Get-FolderSizeGB {
  param([string]$Path)
  if (-not (Test-Path $Path)) { return 0 }
  $sum = (Get-ChildItem $Path -Recurse -Force -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum).Sum
  if ($null -eq $sum) { return 0 }
  [math]::Round($sum/1GB, 2)
}
function Remove-Contents {
  param([string]$Path, [string[]]$Exclude = @())
  if (-not (Test-Path $Path)) { return }
  Get-ChildItem $Path -Force -ErrorAction SilentlyContinue |
    Where-Object { $Exclude -notcontains $_.Name } |
    ForEach-Object {
      if ($DryRun) { Log ("  [DRY] would remove {0}" -f $_.FullName) DarkGray }
      else { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# ----- Banner -----
$initialFree = Get-FreeGB
Log "==========================================================" Cyan
Log ("Reclaim-Disk start. Free: {0} GB. DryRun={1}" -f $initialFree, $DryRun) Cyan
Log ("Flags: PruneDocker={0} SkipDocker={1} DisableHibernation={2} DismCleanup={3}" `
     -f $PruneDocker, $SkipDocker, $DisableHibernation, $DismCleanup) Cyan
Log "Log: $LogFile" Cyan
Log "==========================================================" Cyan

$summary = [ordered]@{}

# ----- A: User caches -----
Log ""; Log "[A] Clearing user-level regenerable caches" Cyan
$caches = @(
  @{Name='SquirrelTemp'; Path="$env:LOCALAPPDATA\SquirrelTemp"},
  @{Name='npm-cache';    Path="$env:LOCALAPPDATA\npm-cache"},
  @{Name='uv';           Path="$env:LOCALAPPDATA\uv"},
  @{Name='pip Cache';    Path="$env:LOCALAPPDATA\pip\Cache"}
)
$cacheTotal = 0
foreach ($c in $caches) {
  $before = Get-FolderSizeGB $c.Path
  if ($before -eq 0) { Log ("  {0}: empty or missing" -f $c.Name); continue }
  Remove-Contents -Path $c.Path
  $after = if ($DryRun) { $before } else { Get-FolderSizeGB $c.Path }
  $freed = if ($DryRun) { $before } else { [math]::Round($before - $after, 2) }
  $cacheTotal += $freed
  $verb = if ($DryRun) { "would free" } else { "freed" }
  Log ("  {0}: {1} GB -> {2} GB ({3} {4} GB)" -f $c.Name, $before, $after, $verb, $freed)
}
$summary['User caches'] = $cacheTotal

# ----- B: $GetCurrent -----
Log ""; Log "[B] Windows upgrade staging (C:\`$GetCurrent)" Cyan
$gc = 'C:\$GetCurrent'
$gcSize = Get-FolderSizeGB $gc
if ($gcSize -eq 0) {
  Log "  Not present, skipping."
} else {
  Log "  Size: $gcSize GB"
  if ($DryRun) {
    Log ("  [DRY] would remove {0}" -f $gc) DarkGray
  } else {
    Remove-Item -LiteralPath $gc -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $gc) {
      Log "  Retrying with takeown/icacls..." Yellow
      takeown /f $gc /r /d Y 2>&1 | Out-Null
      icacls $gc /grant administrators:F /t /q 2>&1 | Out-Null
      Remove-Item -LiteralPath $gc -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $gc) { Log "  FAILED to remove." Red } else { Log "  Removed." }
  }
}
$summary['$GetCurrent'] = $gcSize

# ----- C: Hibernation (optional) -----
if ($DisableHibernation) {
  Log ""; Log "[C] Disabling hibernation (powercfg /h off)" Cyan
  $hiberSize = if (Test-Path 'C:\hiberfil.sys') {
    [math]::Round((Get-Item 'C:\hiberfil.sys' -Force).Length/1GB, 2)
  } else { 0 }
  Log "  hiberfil.sys: $hiberSize GB"
  if ($hiberSize -gt 0 -and -not $DryRun) {
    powercfg /h off 2>&1 | Out-Null
    Log "  Disabled."
  } elseif ($DryRun -and $hiberSize -gt 0) {
    Log "  [DRY] would run 'powercfg /h off'" DarkGray
  }
  $summary['Hibernation'] = $hiberSize
}

# ----- D: DISM (optional) -----
if ($DismCleanup) {
  Log ""; Log "[D] DISM component cleanup (5-15 min)" Cyan
  if ($DryRun) {
    Log "  [DRY] would run DISM /Online /Cleanup-Image /StartComponentCleanup /ResetBase" DarkGray
    $summary['DISM cleanup'] = 0
  } else {
    $dismBefore = Get-FreeGB
    Dism /Online /Cleanup-Image /StartComponentCleanup /ResetBase 2>&1 |
      Out-File -FilePath $LogFile -Append -Encoding utf8
    $dismFreed = [math]::Round((Get-FreeGB) - $dismBefore, 2)
    Log ("  Reclaimed: {0} GB" -f $dismFreed)
    $summary['DISM cleanup'] = $dismFreed
  }
}

# ----- E + F: Docker prune + compact .vhdx -----
$dockerExe = @(
  "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
  "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $SkipDocker) {

  if ($PruneDocker) {
    Log ""; Log "[E] Docker prune (-af --volumes + builder)" Cyan
    if (-not $dockerExe) {
      Log "  Docker Desktop exe not found. Skipping prune." Yellow
    } elseif ($DryRun) {
      Log "  [DRY] would start Docker, prune unused images/containers/volumes/build cache" DarkGray
    } else {
      if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        Log "  Starting Docker Desktop..."
        Start-Process $dockerExe
      } else { Log "  Docker Desktop already running." }
      Log "  Waiting for daemon (up to 240s)..."
      $ready = $false
      for ($i = 0; $i -lt 48; $i++) {
        Start-Sleep 5
        docker info 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; Log ("  Ready after {0}s" -f (($i+1)*5)); break }
      }
      if ($ready) {
        "--- Before prune ---" | Out-File $LogFile -Append -Encoding utf8
        docker system df 2>&1 | Out-File $LogFile -Append -Encoding utf8
        Log "  Pruning system (images/containers/networks/volumes)..."
        docker system prune -af --volumes 2>&1 | Out-File $LogFile -Append -Encoding utf8
        Log "  Pruning builder cache..."
        docker builder prune -af 2>&1 | Out-File $LogFile -Append -Encoding utf8
        "--- After prune ---" | Out-File $LogFile -Append -Encoding utf8
        docker system df 2>&1 | Out-File $LogFile -Append -Encoding utf8
        Log "  Prune complete."
      } else {
        Log "  Docker daemon never came up; skipping prune." Yellow
      }
    }
  }

  Log ""; Log "[F] Shut down WSL + Docker and compact all .vhdx" Cyan
  $vhdxFiles = @()
  foreach ($r in @("$env:LOCALAPPDATA\Docker", "$env:LOCALAPPDATA\wsl")) {
    if (Test-Path $r) {
      $vhdxFiles += Get-ChildItem $r -Recurse -File -Filter '*.vhdx' -Force -ErrorAction SilentlyContinue
    }
  }
  if ($vhdxFiles.Count -eq 0) {
    Log "  No .vhdx files found."
  } else {
    $totalVhdxGB = [math]::Round(($vhdxFiles | Measure-Object Length -Sum).Sum/1GB, 2)
    Log ("  Found {0} .vhdx file(s) totaling {1} GB" -f $vhdxFiles.Count, $totalVhdxGB)

    if (-not $DryRun) {
      if ($dockerExe) {
        Log "  Quitting Docker Desktop..."
        & $dockerExe '--quit' 2>&1 | Out-Null
        Start-Sleep 8
        Get-Process -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -match 'Docker Desktop|com\.docker|vpnkit|wsl-vpnkit' } |
          ForEach-Object {
            Log ("    killing {0} (pid {1})" -f $_.Name, $_.Id)
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
          }
        Start-Sleep 3
      }
      Log "  wsl --shutdown"
      wsl --shutdown 2>&1 | Out-File $LogFile -Append -Encoding utf8
      Start-Sleep 5
    }

    $compactTotal = 0
    foreach ($v in $vhdxFiles) {
      $before = [math]::Round($v.Length/1GB, 2)
      if ($DryRun) {
        Log ("  [DRY] would compact {0} ({1} GB)" -f $v.FullName, $before) DarkGray
        continue
      }
      Log ("  Compacting {0} (currently {1} GB)..." -f $v.FullName, $before)
      $dp = "select vdisk file=`"$($v.FullName)`"`r`nattach vdisk readonly`r`ncompact vdisk`r`ndetach vdisk`r`nexit"
      $tmp = [System.IO.Path]::GetTempFileName()
      Set-Content -Path $tmp -Value $dp -Encoding ASCII
      diskpart /s $tmp 2>&1 | Out-File $LogFile -Append -Encoding utf8
      Remove-Item $tmp -ErrorAction SilentlyContinue
      $after = [math]::Round((Get-Item $v.FullName -Force).Length/1GB, 2)
      $delta = [math]::Round($before - $after, 2)
      $compactTotal += $delta
      Log ("    {0}: {1} GB -> {2} GB (freed {3} GB)" -f $v.Name, $before, $after, $delta)
    }
    $summary['Compact vhdx'] = $compactTotal
  }
}

# ----- G: Temp -----
Log ""; Log "[G] Clearing %LOCALAPPDATA%\Temp (preserving 'claude' subdir)" Cyan
$tempPath = "$env:LOCALAPPDATA\Temp"
$tempBefore = Get-FolderSizeGB $tempPath
Remove-Contents -Path $tempPath -Exclude @('claude')
$tempAfter = if ($DryRun) { $tempBefore } else { Get-FolderSizeGB $tempPath }
$tempFreed = if ($DryRun) { $tempBefore } else { [math]::Round($tempBefore - $tempAfter, 2) }
$verb = if ($DryRun) { "would free" } else { "freed" }
Log ("  Temp: {0} GB -> {1} GB ({2} {3} GB)" -f $tempBefore, $tempAfter, $verb, $tempFreed)
$summary['Temp'] = $tempFreed

# ----- Summary -----
$finalFree = Get-FreeGB
$drive = Get-PSDrive C
$total = [math]::Round(($drive.Used + $drive.Free)/1GB, 2)
$totalReclaimed = [math]::Round($finalFree - $initialFree, 2)
$freePct = [math]::Round($drive.Free / ($drive.Used + $drive.Free) * 100, 1)

Log ""
Log "==========================================================" Cyan
Log ("Done. Drive total: {0} GB. Free: {1} GB -> {2} GB" -f $total, $initialFree, $finalFree) Green
if (-not $DryRun) {
  Log ("Reclaimed this run: {0} GB. Drive now {1}% free." -f $totalReclaimed, $freePct) Green
} else {
  $est = ($summary.Values | Measure-Object -Sum).Sum
  Log ("DryRun estimate: would free ~{0} GB (plus DISM/compact, which are unknown)." -f $est) Yellow
}
Log "Breakdown:" Cyan
foreach ($k in $summary.Keys) {
  Log ("  {0,-16} {1,8} GB" -f $k, $summary[$k])
}
Log "==========================================================" Cyan
Log ("Log: {0}" -f $LogFile)
