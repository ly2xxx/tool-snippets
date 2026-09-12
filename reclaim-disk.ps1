#requires -Version 5.1
<#
.SYNOPSIS
  Analyse and reclaim space on the C: drive. Self-elevates. Safe defaults.

.DESCRIPTION
  TWO MODES.

  1. ANALYSE (-Analyse) - read only, changes nothing.
     Inventories where C: has actually gone and writes two reports next to
     the log: reclaim-analysis_<ts>.md (human) and .json (machine).
     Covers: all volumes, known Windows hogs, every .vhdx (Docker + WSL,
     discovered from the Lxss registry, not guessed), Docker's own disk
     breakdown, ~40 developer caches, the largest directories under C:\,
     the largest individual files, and a ranked list of candidates to move
     to H: with the env var that relocates each one.

  2. CLEAN (default) - reclaims space.
     A. User-level regenerable caches (npm/pnpm/yarn/pip/uv/Squirrel/...).
     B. C:\$GetCurrent Windows-upgrade staging.
     C. Docker prune, BY AGE (default: anything unused > -PruneAgeDays,
        default 14). Build cache, images, stopped containers, networks.
        Volumes are NOT touched unless you pass -PruneVolumes.
     D. fstrim inside every WSL distro, THEN compact the .vhdx.
        This is the important one. diskpart 'compact vdisk' can only
        reclaim blocks the guest filesystem has discarded. Without fstrim
        first, a 150 GB docker_data.vhdx that is 90% empty inside still
        compacts to ~150 GB. With fstrim it collapses to its real size.
     E. %LOCALAPPDATA%\Temp and C:\Windows\Temp.

  Optional switches:
    -Analyse             Report only. No changes. Writes .md + .json.
    -DeepScan            With -Analyse: also walk C:\Windows and size
                         everything. Slower (several minutes), more complete.
    -MinFileMB <n>       With -Analyse: threshold for "largest files".
                         Default 500.
    -Top <n>             With -Analyse: rows per table. Default 30.
    -PruneAgeDays <n>    Docker prune horizon in days. Default 14.
    -NoPrune             Skip the Docker prune entirely (still compacts).
    -PruneVolumes        Also 'docker volume prune'. Destroys volume DATA.
    -FullPrune           Legacy aggressive mode: 'system prune -af --volumes'.
                         Ignores -PruneAgeDays. Removes everything not running.
    -SkipDocker          Don't touch Docker or WSL at all.
    -SkipTrim            Don't fstrim before compacting (not recommended).
    -DismCleanup         DISM /StartComponentCleanup /ResetBase (5-15 min).
    -WindowsUpdateCache  Stop wuauserv/BITS, clear SoftwareDistribution\Download.
    -EmptyRecycleBin     Empty the Recycle Bin on C:.
    -DisableHibernation  powercfg /h off (deletes hiberfil.sys, ~RAM size).
    -DryRun              Report what CLEAN would do. Nothing is changed.
    -LogDir <path>       Where to write log + reports. Default: script dir.

.EXAMPLE
  .\reclaim-disk.ps1 -Analyse
  Find out where the space went. Changes nothing.

.EXAMPLE
  .\reclaim-disk.ps1
  Safe cleanup: age-based Docker prune, fstrim + compact, temp files.

.EXAMPLE
  .\reclaim-disk.ps1 -PruneAgeDays 7 -DismCleanup -WindowsUpdateCache
  Harder cleanup.

.EXAMPLE
  .\reclaim-disk.ps1 -DryRun
  Show what the cleanup would do without doing it.
#>
[CmdletBinding()]
param(
  [switch]$Analyse,
  [switch]$DeepScan,
  [int]$MinFileMB = 500,
  [int]$Top = 30,

  [switch]$DryRun,
  [switch]$SkipDocker,
  [switch]$NoPrune,
  [int]$PruneAgeDays = 14,
  [switch]$PruneVolumes,
  [switch]$FullPrune,
  [switch]$SkipTrim,

  [switch]$DismCleanup,
  [switch]$WindowsUpdateCache,
  [switch]$EmptyRecycleBin,
  [switch]$DisableHibernation,

  [string]$LogDir
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'
$script:StartTime      = Get-Date

# ---------------------------------------------------------------- elevate ---
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
    else { $reArgs += @("-$k", "$v") }
  }
  Start-Process powershell -Verb RunAs -ArgumentList $reArgs -WorkingDirectory $PWD
  exit
}

# ---------------------------------------------------------------- logging ---
if (-not $LogDir) { $LogDir = if ($PSScriptRoot) { $PSScriptRoot } else { 'C:\Temp' } }
if (-not (Test-Path $LogDir)) { New-Item -Path $LogDir -ItemType Directory -Force | Out-Null }
$stamp   = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile = Join-Path $LogDir ("reclaim_{0}.log" -f $stamp)
$MdFile  = Join-Path $LogDir ("reclaim-analysis_{0}.md"   -f $stamp)
$JsonFile= Join-Path $LogDir ("reclaim-analysis_{0}.json" -f $stamp)

function Log {
  param([string]$Msg = '', [ConsoleColor]$Color = 'Gray')
  $line = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $Msg
  $line | Out-File -FilePath $LogFile -Append -Encoding utf8
  Write-Host $line -ForegroundColor $Color
}
function LogRaw { param([string]$Text) $Text | Out-File -FilePath $LogFile -Append -Encoding utf8 }

# ---------------------------------------------------------------- helpers ---
function To-GB { param($Bytes) if ($null -eq $Bytes -or $Bytes -lt 0) { 0 } else { [math]::Round($Bytes/1GB, 2) } }
function Get-FreeGB { To-GB (Get-PSDrive C).Free }

# Fast directory sizing via robocopy list-only. Falls back to .NET walk.
function Get-SizeBytes {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return -1 }
  if (-not (Test-Path -LiteralPath $Path -ErrorAction SilentlyContinue)) { return -1 }
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
  if ($null -eq $item) { return -1 }
  if (-not $item.PSIsContainer) { return [int64]$item.Length }

  try {
    $out = & robocopy.exe $Path NULL /L /S /XJ /NJH /NC /NFL /NDL /BYTES /R:0 /W:0 2>$null
    $line = $out | Where-Object { $_ -match '^\s*Bytes\s*:' } | Select-Object -First 1
    if ($line -and $line -match '^\s*Bytes\s*:\s+(\d+)') { return [int64]$Matches[1] }
  } catch { }

  try {
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { return 0 }
    return [int64]$sum
  } catch { return -1 }
}

# Fast large-file listing via robocopy. Returns [pscustomobject]@{Path;Bytes}
function Get-BigFiles {
  param([string]$Root, [int64]$MinBytes)
  $res = @()
  if (-not (Test-Path -LiteralPath $Root -ErrorAction SilentlyContinue)) { return $res }
  try {
    $out = & robocopy.exe $Root NULL /L /S /XJ /NJH /NJS /NDL /NC /NS /BYTES /FP `
             /MIN:$MinBytes /R:0 /W:0 2>$null
    foreach ($l in $out) {
      $t = $l.Trim()
      if ($t.Length -eq 0) { continue }
      if ($t -match '^([A-Za-z]:\\.+)$') {
        $p = $Matches[1]
        $fi = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
        if ($fi -and -not $fi.PSIsContainer) {
          $res += [pscustomobject]@{ Path = $fi.FullName; Bytes = [int64]$fi.Length }
        }
      }
    }
  } catch { }
  if ($res.Count -eq 0) {
    try {
      $res = Get-ChildItem -LiteralPath $Root -Recurse -Force -File -ErrorAction SilentlyContinue |
             Where-Object { $_.Length -ge $MinBytes } |
             ForEach-Object { [pscustomobject]@{ Path = $_.FullName; Bytes = [int64]$_.Length } }
    } catch { }
  }
  return $res
}

function Remove-Contents {
  param([string]$Path, [string[]]$Exclude = @())
  if (-not (Test-Path -LiteralPath $Path)) { return }
  Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue |
    Where-Object { $Exclude -notcontains $_.Name } |
    ForEach-Object {
      if ($DryRun) { Log ("  [DRY] would remove {0}" -f $_.FullName) DarkGray }
      else { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# diskpart / DISM emit thousands of "n percent completed" lines. Keep the signal.
function Write-FilteredOutput {
  param([string[]]$Lines, [string]$Prefix = '    ')
  $seen = @{}
  foreach ($l in $Lines) {
    if ($null -eq $l) { continue }
    $t = ($l -replace '\s+',' ').Trim()
    if ($t.Length -eq 0) { continue }
    if ($t -match '^\d+ percent completed$') {
      if ($seen.ContainsKey('pct')) { continue }
      $seen['pct'] = $true
      LogRaw ("{0}(progress output suppressed)" -f $Prefix)
      continue
    }
    if ($t -match '^(Copyright|Microsoft DiskPart version|On computer:|Leaving DiskPart)') { continue }
    LogRaw ("{0}{1}" -f $Prefix, $t)
  }
}

function Get-WslDistros {
  $out = @()
  foreach ($hive in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss')) {
    if (-not (Test-Path $hive)) { continue }
    Get-ChildItem $hive -ErrorAction SilentlyContinue | ForEach-Object {
      $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
      if ($p -and $p.BasePath) {
        $bp = $p.BasePath -replace '^\\\\\?\\',''
        $out += [pscustomobject]@{
          Name     = $p.DistributionName
          BasePath = $bp
          Version  = $p.Version
        }
      }
    }
  }
  return $out
}

function Find-Vhdx {
  $roots = @(
    "$env:LOCALAPPDATA\Docker",
    "$env:LOCALAPPDATA\wsl",
    "$env:LOCALAPPDATA\Packages",
    "$env:USERPROFILE\.crc",
    "$env:USERPROFILE\.minikube",
    "$env:PUBLIC\Documents\Hyper-V",
    "C:\ProgramData\Microsoft\Windows\Virtual Hard Disks"
  )
  foreach ($d in (Get-WslDistros)) { $roots += $d.BasePath }
  $files = @{}
  foreach ($r in ($roots | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $r -ErrorAction SilentlyContinue)) { continue }
    Get-ChildItem -LiteralPath $r -Recurse -File -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.Extension -in @('.vhdx','.vhd','.qcow2','.vmdk') -or
                     ($_.Extension -eq '.raw' -and $_.Length -gt 1GB) } |
      ForEach-Object { $files[$_.FullName] = $_ }
  }
  return $files.Values | Sort-Object Length -Descending
}

$script:DockerExe = @(
  "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
  "${env:ProgramFiles(x86)}\Docker\Docker\Docker Desktop.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

# ================================================================ ANALYSE ===
function Invoke-DiskAnalysis {

  $report = [ordered]@{
    generated   = (Get-Date).ToString('s')
    computer    = $env:COMPUTERNAME
    deepScan    = [bool]$DeepScan
    volumes     = @()
    windowsHogs = @()
    virtualDisks= @()
    wslDistros  = @()
    docker      = [ordered]@{ available = $false; df = @(); note = '' }
    devCaches   = @()
    bigDirs     = @()
    bigFiles    = @()
    moveToH     = @()
  }

  Log "==========================================================" Cyan
  Log "ANALYSE MODE - read only, nothing will be changed" Cyan
  Log "==========================================================" Cyan

  # --- volumes -------------------------------------------------------------
  Log ""; Log "[1/7] Volumes" Cyan
  foreach ($d in (Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
                  Where-Object { $_.Size -gt 0 } | Sort-Object DeviceID)) {
    $tot = To-GB $d.Size; $free = To-GB $d.FreeSpace
    $pct = if ($d.Size) { [math]::Round($d.FreeSpace/$d.Size*100,1) } else { 0 }
    $report.volumes += [pscustomobject]@{
      drive=$d.DeviceID; label=$d.VolumeName; totalGB=$tot; freeGB=$free; freePct=$pct
    }
    Log ("  {0} {1,-14} {2,8} GB total  {3,8} GB free  ({4}%)" -f $d.DeviceID,$d.VolumeName,$tot,$free,$pct)
  }

  # --- known Windows hogs --------------------------------------------------
  Log ""; Log "[2/7] Known Windows space consumers" Cyan
  $hogs = @(
    @{n='hiberfil.sys';           p='C:\hiberfil.sys';                                     fix='-DisableHibernation'}
    @{n='pagefile.sys';           p='C:\pagefile.sys';                                     fix='System > Virtual memory (can move to H:)'}
    @{n='swapfile.sys';           p='C:\swapfile.sys';                                     fix='managed by Windows'}
    @{n='Windows.old';            p='C:\Windows.old';                                      fix='Disk Cleanup > Previous Windows installations'}
    @{n='$GetCurrent';            p='C:\$GetCurrent';                                      fix='cleaned by default'}
    @{n='$WinREAgent';            p='C:\$WinREAgent';                                      fix='safe to delete after a successful update'}
    @{n='Recycle Bin';            p='C:\$Recycle.Bin';                                     fix='-EmptyRecycleBin'}
    @{n='WinSxS (component store)';p='C:\Windows\WinSxS';                                  fix='-DismCleanup (reported size overstates real usage: hardlinks)'}
    @{n='Windows Installer cache';p='C:\Windows\Installer';                                fix='do NOT delete manually; use PatchCleaner'}
    @{n='Windows Update download';p='C:\Windows\SoftwareDistribution\Download';             fix='-WindowsUpdateCache'}
    @{n='Delivery Optimization';  p='C:\Windows\SoftwareDistribution\DeliveryOptimization'; fix='Disk Cleanup'}
    @{n='CBS logs';               p='C:\Windows\Logs\CBS';                                  fix='safe to delete'}
    @{n='Windows Temp';           p='C:\Windows\Temp';                                      fix='cleaned by default'}
    @{n='Memory dump';            p='C:\Windows\MEMORY.DMP';                                fix='safe to delete'}
    @{n='Crash dumps (user)';     p="$env:LOCALAPPDATA\CrashDumps";                         fix='safe to delete'}
    @{n='Package Cache';          p='C:\ProgramData\Package Cache';                         fix='VS/redist installers; safe-ish to prune'}
  )
  foreach ($h in $hogs) {
    $b = Get-SizeBytes $h.p
    if ($b -lt 0) { continue }
    $g = To-GB $b
    if ($g -lt 0.1) { continue }
    $report.windowsHogs += [pscustomobject]@{ name=$h.n; path=$h.p; gb=$g; remedy=$h.fix }
    Log ("  {0,-28} {1,8} GB   {2}" -f $h.n, $g, $h.fix)
  }

  # --- virtual disks -------------------------------------------------------
  Log ""; Log "[3/7] Virtual disks (Docker / WSL / VMs)" Cyan
  foreach ($d in (Get-WslDistros)) {
    $report.wslDistros += [pscustomobject]@{ name=$d.Name; basePath=$d.BasePath; wslVersion=$d.Version }
    Log ("  distro: {0,-24} {1}" -f $d.Name, $d.BasePath)
  }
  $vhdxTotal = 0
  foreach ($v in (Find-Vhdx)) {
    $g = To-GB $v.Length
    $vhdxTotal += $v.Length
    $report.virtualDisks += [pscustomobject]@{ path=$v.FullName; gb=$g; modified=$v.LastWriteTime.ToString('s') }
    Log ("  {0,8} GB  {1}" -f $g, $v.FullName)
  }
  Log ("  ---> virtual disks total: {0} GB" -f (To-GB $vhdxTotal)) Yellow

  # --- docker --------------------------------------------------------------
  Log ""; Log "[4/7] Docker disk usage" Cyan
  $dockerOk = $false
  if (Get-Command docker -ErrorAction SilentlyContinue) {
    & docker info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $dockerOk = $true }
  }
  if ($dockerOk) {
    $report.docker.available = $true
    $df = @(& docker system df 2>&1 | ForEach-Object { "$_" })
    $df | ForEach-Object { Log ("  {0}" -f $_) }
    $report.docker.df = $df
    LogRaw "--- docker system df -v ---"
    LogRaw ((& docker system df -v 2>&1 | ForEach-Object { "$_" }) -join "`r`n")
    Log "  (full 'docker system df -v' written to the log)"
  } else {
    $report.docker.note = 'Docker daemon not running - start Docker Desktop and re-run -Analyse for the image/cache breakdown.'
    Log "  Docker daemon not running. Start Docker Desktop and re-run for the breakdown." Yellow
  }

  # --- developer caches ----------------------------------------------------
  Log ""; Log "[5/7] Developer caches" Cyan
  $up = $env:USERPROFILE; $la = $env:LOCALAPPDATA; $ra = $env:APPDATA
  # movable = can be relocated to H: by setting the given env var
  $caches = @(
    @{n='npm cache';         p="$la\npm-cache";                      e=$null;              regen=$true}
    @{n='pnpm store';        p="$la\pnpm-store";                     e='PNPM_HOME';        regen=$true}
    @{n='yarn cache';        p="$la\Yarn\Cache";                     e=$null;              regen=$true}
    @{n='pip cache';         p="$la\pip\Cache";                      e='PIP_CACHE_DIR';    regen=$true}
    @{n='uv cache';          p="$la\uv";                             e='UV_CACHE_DIR';     regen=$true}
    @{n='poetry cache';      p="$la\pypoetry\Cache";                 e='POETRY_CACHE_DIR'; regen=$true}
    @{n='conda pkgs';        p="$up\.conda\pkgs";                    e='CONDA_PKGS_DIRS';  regen=$true}
    @{n='miniconda pkgs';    p="$up\miniconda3\pkgs";                e='CONDA_PKGS_DIRS';  regen=$true}
    @{n='anaconda pkgs';     p="$up\anaconda3\pkgs";                 e='CONDA_PKGS_DIRS';  regen=$true}
    @{n='HuggingFace cache'; p="$up\.cache\huggingface";             e='HF_HOME';          regen=$true}
    @{n='torch cache';       p="$up\.cache\torch";                   e='TORCH_HOME';       regen=$true}
    @{n='Ollama models';     p="$up\.ollama\models";                 e='OLLAMA_MODELS';    regen=$true}
    @{n='Maven repo';        p="$up\.m2\repository";                 e='MAVEN_OPTS -Dmaven.repo.local'; regen=$true}
    @{n='Gradle home';       p="$up\.gradle";                        e='GRADLE_USER_HOME'; regen=$true}
    @{n='NuGet packages';    p="$up\.nuget\packages";                e='NUGET_PACKAGES';   regen=$true}
    @{n='Cargo home';        p="$up\.cargo";                         e='CARGO_HOME';       regen=$true}
    @{n='Rustup toolchains'; p="$up\.rustup";                        e='RUSTUP_HOME';      regen=$true}
    @{n='Go module cache';   p="$up\go\pkg\mod";                     e='GOMODCACHE';       regen=$true}
    @{n='Go build cache';    p="$la\go-build";                       e='GOCACHE';          regen=$true}
    @{n='Android SDK';       p="$la\Android\Sdk";                    e='ANDROID_SDK_ROOT'; regen=$false}
    @{n='Android AVDs';      p="$up\.android\avd";                   e='ANDROID_AVD_HOME'; regen=$false}
    @{n='Playwright browsers';p="$la\ms-playwright";                 e='PLAYWRIGHT_BROWSERS_PATH'; regen=$true}
    @{n='Puppeteer cache';   p="$up\.cache\puppeteer";               e='PUPPETEER_CACHE_DIR'; regen=$true}
    @{n='Electron cache';    p="$la\electron";                       e='electron_config_cache'; regen=$true}
    @{n='electron-builder';  p="$la\electron-builder";               e=$null;              regen=$true}
    @{n='Chocolatey lib';    p='C:\ProgramData\chocolatey\lib';      e=$null;              regen=$false}
    @{n='minikube';          p="$up\.minikube";                      e='MINIKUBE_HOME';    regen=$true}
    @{n='CRC (OpenShift)';   p="$up\.crc";                           e='CRC_MACHINE_IMAGE_DIR'; regen=$true}
    @{n='VS Code extensions';p="$up\.vscode\extensions";             e=$null;              regen=$true}
    @{n='Cursor (roaming)';  p="$ra\Cursor";                         e=$null;              regen=$true}
    @{n='Cursor (home)';     p="$up\.cursor";                        e=$null;              regen=$true}
    @{n='Antigravity';       p="$up\.antigravity";                   e=$null;              regen=$true}
    @{n='Antigravity IDE';   p="$up\.antigravity-ide";               e=$null;              regen=$true}
    @{n='JetBrains';         p="$la\JetBrains";                      e=$null;              regen=$true}
    @{n='SquirrelTemp';      p="$la\SquirrelTemp";                   e=$null;              regen=$true}
    @{n='LOCALAPPDATA Temp'; p="$la\Temp";                           e=$null;              regen=$true}
    @{n='Docker Desktop data';p="$la\Docker";                        e='disk image location in Docker Desktop settings'; regen=$false}
    @{n='Docker roaming';    p="$ra\Docker";                         e=$null;              regen=$false}
    @{n='WSL distros';       p="$la\wsl";                            e='wsl --export/--import to H:'; regen=$false}
    @{n='gitlab-runner';     p="$up\gitlab-runner";                  e=$null;              regen=$false}
    @{n='Postman';           p="$up\Postman";                        e=$null;              regen=$true}
    @{n='dspy cache';        p="$up\.dspy_cache";                    e=$null;              regen=$true}
    @{n='mem0';              p="$up\.mem0";                          e=$null;              regen=$true}
    @{n='embedchain';        p="$up\.embedchain";                    e=$null;              regen=$true}
    @{n='phoenix';           p="$up\.phoenix";                       e=$null;              regen=$true}
    @{n='Downloads';         p="$up\Downloads";                      e='move folder + retarget in Explorer'; regen=$false}
    @{n='WeChat files';      p="$up\xwechat_files";                  e='WeChat > Settings > File storage'; regen=$false}
    @{n='Sync folder';       p="$up\Sync";                           e=$null;              regen=$false}
    @{n='CrossDevice';       p="$up\CrossDevice";                    e=$null;              regen=$true}
  )
  $i = 0
  foreach ($c in $caches) {
    $i++
    Write-Host ("`r    scanning {0}/{1} {2,-28}" -f $i,$caches.Count,$c.n) -NoNewline
    $b = Get-SizeBytes $c.p
    if ($b -le 0) { continue }
    $g = To-GB $b
    if ($g -lt 0.1) { continue }
    $report.devCaches += [pscustomobject]@{
      name=$c.n; path=$c.p; gb=$g; regenerable=$c.regen; relocateVia=$c.e
    }
  }
  Write-Host ("`r" + (' ' * 70) + "`r") -NoNewline
  foreach ($c in ($report.devCaches | Sort-Object { -$_.gb })) {
    $tag = if ($c.regenerable) { 'regen' } else { 'data ' }
    Log ("  {0,8} GB  [{1}]  {2,-24} {3}" -f $c.gb, $tag, $c.name, $c.path)
  }
  Log ("  ---> dev caches total: {0} GB" -f (To-GB (($report.devCaches | Measure-Object gb -Sum).Sum * 1GB))) Yellow

  # --- largest directories -------------------------------------------------
  Log ""; Log "[6/7] Largest directories" Cyan
  $dirRoots = @('C:\')
  $dirRoots += $env:USERPROFILE
  $dirRoots += $env:LOCALAPPDATA
  $dirRoots += $env:APPDATA
  $dirRoots += 'C:\ProgramData'
  $dirRoots += 'C:\Program Files'
  $dirRoots += 'C:\Program Files (x86)'
  foreach ($root in ($dirRoots | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    Log ("  under {0}" -f $root)
    $kids = Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.LinkType -ne 'Junction' -and $_.LinkType -ne 'SymbolicLink' }
    if (-not $DeepScan) { $kids = $kids | Where-Object { $_.FullName -ne 'C:\Windows' } }
    $rows = @()
    $n = 0
    foreach ($k in $kids) {
      $n++
      Write-Host ("`r    {0}/{1} {2,-40}" -f $n,$kids.Count,$k.Name.Substring(0,[Math]::Min(40,$k.Name.Length))) -NoNewline
      $b = Get-SizeBytes $k.FullName
      if ($b -le 0) { continue }
      $rows += [pscustomobject]@{ Path=$k.FullName; GB=(To-GB $b) }
    }
    Write-Host ("`r" + (' ' * 70) + "`r") -NoNewline
    foreach ($r in ($rows | Sort-Object GB -Descending | Select-Object -First $Top)) {
      if ($r.GB -lt 0.5) { continue }
      $report.bigDirs += [pscustomobject]@{ root=$root; path=$r.Path; gb=$r.GB }
      Log ("    {0,8} GB  {1}" -f $r.GB, $r.Path)
    }
  }
  if (-not $DeepScan) { Log "  (C:\Windows not sized - pass -DeepScan to include it)" DarkGray }

  # --- largest files -------------------------------------------------------
  Log ""; Log ("[7/7] Largest individual files (>= {0} MB)" -f $MinFileMB) Cyan
  $minBytes = [int64]$MinFileMB * 1MB
  $fileRoots = @($env:USERPROFILE, 'C:\ProgramData', 'C:\Program Files', 'C:\Program Files (x86)')
  if ($DeepScan) { $fileRoots = @('C:\') }
  $all = @()
  foreach ($fr in ($fileRoots | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $fr)) { continue }
    Log ("  scanning {0} ..." -f $fr) DarkGray
    $all += Get-BigFiles -Root $fr -MinBytes $minBytes
  }
  # C:\ root-level files (hiberfil / pagefile live here)
  Get-ChildItem -LiteralPath 'C:\' -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Length -ge $minBytes } |
    ForEach-Object { $all += [pscustomobject]@{ Path=$_.FullName; Bytes=[int64]$_.Length } }

  $uniq = $all | Sort-Object Bytes -Descending | Group-Object Path | ForEach-Object { $_.Group[0] }
  foreach ($f in ($uniq | Sort-Object Bytes -Descending | Select-Object -First $Top)) {
    $g = To-GB $f.Bytes
    $report.bigFiles += [pscustomobject]@{ path=$f.Path; gb=$g }
    Log ("  {0,8} GB  {1}" -f $g, $f.Path)
  }

  # --- move-to-H candidates ------------------------------------------------
  Log ""; Log "Candidates to move to H:" Cyan
  $hFree = ($report.volumes | Where-Object { $_.drive -eq 'H:' } | Select-Object -First 1).freeGB
  foreach ($c in ($report.devCaches | Where-Object { $_.relocateVia -and $_.gb -ge 1 } | Sort-Object { -$_.gb })) {
    $dest = 'H:\devcache\' + ($c.name -replace '[^A-Za-z0-9]','-').ToLower()
    $report.moveToH += [pscustomobject]@{
      name=$c.name; source=$c.path; gb=$c.gb; suggestedDest=$dest; relocateVia=$c.relocateVia
    }
    Log ("  {0,8} GB  {1,-24} -> {2}   (via {3})" -f $c.gb, $c.name, $dest, $c.relocateVia)
  }
  if ($hFree) { Log ("  H: currently has {0} GB free." -f $hFree) }

  # --- write reports -------------------------------------------------------
  $report | ConvertTo-Json -Depth 6 | Out-File -FilePath $JsonFile -Encoding utf8

  $md = New-Object System.Text.StringBuilder
  [void]$md.AppendLine("# C: drive analysis - $($report.computer) - $($report.generated)")
  [void]$md.AppendLine()
  [void]$md.AppendLine("## Volumes")
  [void]$md.AppendLine()
  [void]$md.AppendLine("| Drive | Label | Total GB | Free GB | Free % |")
  [void]$md.AppendLine("|---|---|---:|---:|---:|")
  foreach ($v in $report.volumes) { [void]$md.AppendLine("| $($v.drive) | $($v.label) | $($v.totalGB) | $($v.freeGB) | $($v.freePct) |") }

  [void]$md.AppendLine()
  [void]$md.AppendLine("## Virtual disks")
  [void]$md.AppendLine()
  [void]$md.AppendLine("| GB | Path |")
  [void]$md.AppendLine("|---:|---|")
  foreach ($v in $report.virtualDisks) { [void]$md.AppendLine("| $($v.gb) | $($v.path) |") }

  [void]$md.AppendLine()
  [void]$md.AppendLine("## Windows consumers")
  [void]$md.AppendLine()
  [void]$md.AppendLine("| GB | Item | Remedy |")
  [void]$md.AppendLine("|---:|---|---|")
  foreach ($h in ($report.windowsHogs | Sort-Object { -$_.gb })) { [void]$md.AppendLine("| $($h.gb) | $($h.name) | $($h.remedy) |") }

  [void]$md.AppendLine()
  [void]$md.AppendLine("## Developer caches")
  [void]$md.AppendLine()
  [void]$md.AppendLine("| GB | Regenerable | Name | Path | Relocate via |")
  [void]$md.AppendLine("|---:|---|---|---|---|")
  foreach ($c in ($report.devCaches | Sort-Object { -$_.gb })) { [void]$md.AppendLine("| $($c.gb) | $($c.regenerable) | $($c.name) | ``$($c.path)`` | $($c.relocateVia) |") }

  [void]$md.AppendLine()
  [void]$md.AppendLine("## Largest directories")
  [void]$md.AppendLine()
  [void]$md.AppendLine("| GB | Path |")
  [void]$md.AppendLine("|---:|---|")
  foreach ($d in ($report.bigDirs | Sort-Object { -$_.gb } | Select-Object -First 60)) { [void]$md.AppendLine("| $($d.gb) | ``$($d.path)`` |") }

  [void]$md.AppendLine()
  [void]$md.AppendLine("## Largest files")
  [void]$md.AppendLine()
  [void]$md.AppendLine("| GB | Path |")
  [void]$md.AppendLine("|---:|---|")
  foreach ($f in $report.bigFiles) { [void]$md.AppendLine("| $($f.gb) | ``$($f.path)`` |") }

  [void]$md.AppendLine()
  [void]$md.AppendLine("## Move to H:")
  [void]$md.AppendLine()
  [void]$md.AppendLine("| GB | What | From | To | Relocate via |")
  [void]$md.AppendLine("|---:|---|---|---|---|")
  foreach ($m in $report.moveToH) { [void]$md.AppendLine("| $($m.gb) | $($m.name) | ``$($m.source)`` | ``$($m.suggestedDest)`` | $($m.relocateVia) |") }

  $md.ToString() | Out-File -FilePath $MdFile -Encoding utf8

  $elapsed = [math]::Round(((Get-Date) - $script:StartTime).TotalMinutes,1)
  Log ""
  Log "==========================================================" Cyan
  Log ("Analysis complete in {0} min. Nothing was changed." -f $elapsed) Green
  Log ("  Markdown: {0}" -f $MdFile) Green
  Log ("  JSON:     {0}" -f $JsonFile) Green
  Log ("  Log:      {0}" -f $LogFile)
  Log "==========================================================" Cyan
}

if ($Analyse) { Invoke-DiskAnalysis; return }

# ================================================================== CLEAN ===
$initialFree = Get-FreeGB
Log "==========================================================" Cyan
Log ("Reclaim-Disk start. Free: {0} GB. DryRun={1}" -f $initialFree, $DryRun) Cyan
Log ("Flags: PruneAgeDays={0} NoPrune={1} FullPrune={2} PruneVolumes={3} SkipDocker={4} SkipTrim={5}" `
     -f $PruneAgeDays,$NoPrune,$FullPrune,$PruneVolumes,$SkipDocker,$SkipTrim) Cyan
Log ("       DismCleanup={0} WindowsUpdateCache={1} EmptyRecycleBin={2} DisableHibernation={3}" `
     -f $DismCleanup,$WindowsUpdateCache,$EmptyRecycleBin,$DisableHibernation) Cyan
Log "Log: $LogFile" Cyan
Log "==========================================================" Cyan

$summary = [ordered]@{}

# ----- A: user caches -------------------------------------------------------
Log ""; Log "[A] Clearing user-level regenerable caches" Cyan
$cacheTargets = @(
  @{Name='SquirrelTemp';   Path="$env:LOCALAPPDATA\SquirrelTemp"}
  @{Name='npm-cache';      Path="$env:LOCALAPPDATA\npm-cache"}
  @{Name='uv';             Path="$env:LOCALAPPDATA\uv"}
  @{Name='pip Cache';      Path="$env:LOCALAPPDATA\pip\Cache"}
  @{Name='yarn Cache';     Path="$env:LOCALAPPDATA\Yarn\Cache"}
  @{Name='go-build';       Path="$env:LOCALAPPDATA\go-build"}
  @{Name='CrashDumps';     Path="$env:LOCALAPPDATA\CrashDumps"}
  @{Name='electron-builder';Path="$env:LOCALAPPDATA\electron-builder\Cache"}
)
$cacheTotal = 0
foreach ($c in $cacheTargets) {
  $beforeB = Get-SizeBytes $c.Path
  if ($beforeB -le 0) { Log ("  {0}: empty or missing" -f $c.Name); continue }
  $before = To-GB $beforeB
  Remove-Contents -Path $c.Path
  $after = if ($DryRun) { $before } else { To-GB (Get-SizeBytes $c.Path) }
  $freed = if ($DryRun) { $before } else { [math]::Round($before - $after, 2) }
  $cacheTotal += $freed
  $verb = if ($DryRun) { "would free" } else { "freed" }
  Log ("  {0}: {1} GB -> {2} GB ({3} {4} GB)" -f $c.Name, $before, $after, $verb, $freed)
}
$summary['User caches'] = [math]::Round($cacheTotal,2)

# ----- B: $GetCurrent / $WinREAgent ----------------------------------------
Log ""; Log "[B] Windows upgrade staging" Cyan
$stagingTotal = 0
foreach ($gc in @('C:\$GetCurrent','C:\$WinREAgent')) {
  $gcB = Get-SizeBytes $gc
  if ($gcB -le 0) { Log ("  {0}: not present" -f $gc); continue }
  $gcSize = To-GB $gcB
  Log ("  {0}: {1} GB" -f $gc, $gcSize)
  if ($DryRun) { Log ("  [DRY] would remove {0}" -f $gc) DarkGray; $stagingTotal += $gcSize; continue }
  Remove-Item -LiteralPath $gc -Recurse -Force -ErrorAction SilentlyContinue
  if (Test-Path $gc) {
    Log "  Retrying with takeown/icacls..." Yellow
    takeown /f $gc /r /d Y 2>&1 | Out-Null
    icacls $gc /grant administrators:F /t /q 2>&1 | Out-Null
    Remove-Item -LiteralPath $gc -Recurse -Force -ErrorAction SilentlyContinue
  }
  if (Test-Path $gc) { Log "  FAILED to remove." Red } else { Log "  Removed."; $stagingTotal += $gcSize }
}
$summary['Upgrade staging'] = [math]::Round($stagingTotal,2)

# ----- C: hibernation -------------------------------------------------------
if ($DisableHibernation) {
  Log ""; Log "[C] Disabling hibernation (powercfg /h off)" Cyan
  $hiberSize = if (Test-Path 'C:\hiberfil.sys') { To-GB (Get-Item 'C:\hiberfil.sys' -Force).Length } else { 0 }
  Log ("  hiberfil.sys: {0} GB" -f $hiberSize)
  if ($hiberSize -gt 0 -and -not $DryRun) { powercfg /h off 2>&1 | Out-Null; Log "  Disabled." }
  elseif ($DryRun -and $hiberSize -gt 0) { Log "  [DRY] would run 'powercfg /h off'" DarkGray }
  $summary['Hibernation'] = $hiberSize
}

# ----- D: Windows Update cache ---------------------------------------------
if ($WindowsUpdateCache) {
  Log ""; Log "[D] Clearing Windows Update download cache" Cyan
  $wu = 'C:\Windows\SoftwareDistribution\Download'
  $wuBefore = To-GB (Get-SizeBytes $wu)
  Log ("  SoftwareDistribution\Download: {0} GB" -f $wuBefore)
  if ($DryRun) { Log "  [DRY] would stop wuauserv/bits, clear the folder, restart them" DarkGray }
  else {
    foreach ($svc in @('wuauserv','bits','dosvc')) { Stop-Service $svc -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
    Remove-Contents -Path $wu
    foreach ($svc in @('wuauserv','bits','dosvc')) { Start-Service $svc -ErrorAction SilentlyContinue }
    $wuAfter = To-GB (Get-SizeBytes $wu)
    Log ("  {0} GB -> {1} GB" -f $wuBefore, $wuAfter)
  }
  $summary['Windows Update'] = $wuBefore
}

# ----- E: Recycle Bin -------------------------------------------------------
if ($EmptyRecycleBin) {
  Log ""; Log "[E] Emptying Recycle Bin" Cyan
  $rbBefore = To-GB (Get-SizeBytes 'C:\$Recycle.Bin')
  Log ("  Recycle Bin: {0} GB" -f $rbBefore)
  if ($DryRun) { Log "  [DRY] would empty it" DarkGray }
  else { Clear-RecycleBin -DriveLetter C -Force -ErrorAction SilentlyContinue; Log "  Emptied." }
  $summary['Recycle Bin'] = $rbBefore
}

# ----- F: DISM --------------------------------------------------------------
if ($DismCleanup) {
  Log ""; Log "[F] DISM component cleanup (5-15 min)" Cyan
  if ($DryRun) {
    Log "  [DRY] would run DISM /Online /Cleanup-Image /StartComponentCleanup /ResetBase" DarkGray
    $summary['DISM cleanup'] = 0
  } else {
    $dismBefore = Get-FreeGB
    $out = & Dism /Online /Cleanup-Image /StartComponentCleanup /ResetBase 2>&1
    Write-FilteredOutput -Lines $out
    $dismFreed = [math]::Round((Get-FreeGB) - $dismBefore, 2)
    Log ("  Reclaimed: {0} GB" -f $dismFreed)
    $summary['DISM cleanup'] = $dismFreed
  }
}

# ----- G: Docker prune (age-based) + fstrim + compact -----------------------
if (-not $SkipDocker) {

  $doPrune = (-not $NoPrune)
  if ($doPrune) {
    Log ""
    if ($FullPrune) { Log "[G] Docker prune - FULL (removes everything not running)" Cyan }
    else            { Log ("[G] Docker prune - age based (unused > {0} days)" -f $PruneAgeDays) Cyan }

    if (-not $script:DockerExe) {
      Log "  Docker Desktop exe not found. Skipping prune." Yellow
    } elseif ($DryRun) {
      Log "  [DRY] would start Docker and prune:" DarkGray
      if ($FullPrune) { Log "  [DRY]   docker system prune -af --volumes" DarkGray }
      else {
        $h = $PruneAgeDays * 24
        Log ("  [DRY]   docker builder prune -af --filter until={0}h" -f $h) DarkGray
        Log ("  [DRY]   docker image   prune -af --filter until={0}h" -f $h) DarkGray
        Log ("  [DRY]   docker container prune -f --filter until={0}h" -f $h) DarkGray
        Log  "  [DRY]   docker network prune -f" DarkGray
        if ($PruneVolumes) { Log "  [DRY]   docker volume prune -af" DarkGray }
      }
    } else {
      if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        Log "  Starting Docker Desktop..."
        Start-Process $script:DockerExe
      } else { Log "  Docker Desktop already running." }

      Log "  Waiting for daemon (up to 240s)..."
      $ready = $false
      for ($i = 0; $i -lt 48; $i++) {
        Start-Sleep 5
        & docker info 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; Log ("  Ready after {0}s" -f (($i+1)*5)); break }
      }

      if ($ready) {
        LogRaw "--- docker system df BEFORE ---"
        LogRaw ((& docker system df 2>&1) -join "`r`n")

        if ($FullPrune) {
          Log "  docker system prune -af --volumes"
          LogRaw ((& docker system prune -af --volumes 2>&1) -join "`r`n")
          Log "  docker builder prune -af"
          LogRaw ((& docker builder prune -af 2>&1) -join "`r`n")
        } else {
          $filter = "until={0}h" -f ($PruneAgeDays * 24)
          Log ("  docker builder prune -af --filter {0}" -f $filter)
          LogRaw ((& docker builder prune -af --filter $filter 2>&1) -join "`r`n")
          Log ("  docker image prune -af --filter {0}" -f $filter)
          LogRaw ((& docker image prune -af --filter $filter 2>&1) -join "`r`n")
          Log ("  docker container prune -f --filter {0}" -f $filter)
          LogRaw ((& docker container prune -f --filter $filter 2>&1) -join "`r`n")
          Log  "  docker network prune -f"
          LogRaw ((& docker network prune -f 2>&1) -join "`r`n")
          if ($PruneVolumes) {
            Log "  docker volume prune -af   (DESTROYS unused volume data)" Yellow
            LogRaw ((& docker volume prune -af 2>&1) -join "`r`n")
          } else {
            Log "  volumes left alone (pass -PruneVolumes to prune them)"
          }
        }

        LogRaw "--- docker system df AFTER ---"
        $dfAfter = & docker system df 2>&1
        LogRaw ($dfAfter -join "`r`n")
        $dfAfter | ForEach-Object { Log ("    {0}" -f $_) }
        Log "  Prune complete."
      } else {
        Log "  Docker daemon never came up; skipping prune." Yellow
      }
    }
  }

  # --- fstrim + compact ------------------------------------------------------
  Log ""; Log "[H] fstrim inside WSL, then compact all .vhdx" Cyan
  $vhdxFiles = Find-Vhdx | Where-Object { $_.Extension -eq '.vhdx' }

  if (-not $vhdxFiles -or $vhdxFiles.Count -eq 0) {
    Log "  No .vhdx files found."
  } else {
    $totalVhdxGB = To-GB (($vhdxFiles | Measure-Object Length -Sum).Sum)
    Log ("  Found {0} .vhdx file(s) totaling {1} GB" -f $vhdxFiles.Count, $totalVhdxGB)

    if (-not $DryRun) {

      # 1. TRIM from inside the guests. This is what makes compaction work.
      if (-not $SkipTrim) {
        Log "  Trimming guest filesystems (fstrim) before compaction..."
        $distros = @(& wsl.exe --list --quiet 2>$null) |
                   ForEach-Object { ($_ -replace "`0",'').Trim() } |
                   Where-Object { $_ }
        if (-not $distros) { Log "    No WSL distros reported by 'wsl --list'." Yellow }
        foreach ($d in $distros) {
          Log ("    fstrim in '{0}'..." -f $d)
          $r = & wsl.exe -d $d -u root -e sh -c "fstrim -av 2>&1 || true" 2>&1
          Write-FilteredOutput -Lines @($r) -Prefix '      '
          ($r | Select-Object -First 6) | ForEach-Object { if ("$_".Trim()) { Log ("      {0}" -f "$_".Trim()) DarkGray } }
        }
      } else {
        Log "  -SkipTrim set: compaction will reclaim little or nothing." Yellow
      }

      # 2. Shut everything down so the .vhdx files are unlocked.
      if ($script:DockerExe) {
        Log "  Quitting Docker Desktop..."
        & $script:DockerExe '--quit' 2>&1 | Out-Null
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
      & wsl.exe --shutdown 2>&1 | Out-Null
      Start-Sleep 5
    }

    # 3. Compact. Try Optimize-VHD (Hyper-V module) first, fall back to diskpart.
    $haveOptimize = $null -ne (Get-Command Optimize-VHD -ErrorAction SilentlyContinue)
    if ($haveOptimize) { Log "  Optimize-VHD available; will use it with a diskpart fallback." }
    else               { Log "  Optimize-VHD unavailable; using diskpart." }

    function Compact-WithDiskpart {
      param([string]$Path)
      $dp  = "select vdisk file=`"$Path`"`r`nattach vdisk readonly`r`ncompact vdisk`r`ndetach vdisk`r`nexit"
      $tmp = [System.IO.Path]::GetTempFileName()
      Set-Content -Path $tmp -Value $dp -Encoding ASCII
      $out = & diskpart /s $tmp 2>&1
      Write-FilteredOutput -Lines @($out | ForEach-Object { "$_" })
      Remove-Item $tmp -ErrorAction SilentlyContinue
    }

    $compactTotal = 0
    foreach ($v in $vhdxFiles) {
      $before = To-GB $v.Length
      if ($DryRun) { Log ("  [DRY] would fstrim + compact {0} ({1} GB)" -f $v.FullName, $before) DarkGray; continue }
      Log ("  Compacting {0} ({1} GB)..." -f $v.FullName, $before)
      $done = $false
      if ($haveOptimize) {
        try {
          Mount-VHD -Path $v.FullName -ReadOnly -NoDriveLetter -ErrorAction Stop | Out-Null
          Optimize-VHD -Path $v.FullName -Mode Full -ErrorAction Stop
          Dismount-VHD -Path $v.FullName -ErrorAction SilentlyContinue
          $done = $true
        } catch {
          Log ("    Optimize-VHD failed ({0}); falling back to diskpart." -f $_.Exception.Message) Yellow
          Dismount-VHD -Path $v.FullName -ErrorAction SilentlyContinue
        }
      }
      if (-not $done) {
        try { Compact-WithDiskpart -Path $v.FullName }
        catch { Log ("    diskpart compaction error: {0}" -f $_.Exception.Message) Red }
      }
      $afterItem = Get-Item $v.FullName -Force -ErrorAction SilentlyContinue
      $after = if ($afterItem) { To-GB $afterItem.Length } else { $before }
      $delta = [math]::Round($before - $after, 2)
      $compactTotal += $delta
      $flag = if ($delta -lt 1 -and $before -gt 20) { ' <-- barely shrank: the guest FS is genuinely full, prune inside it' } else { '' }
      Log ("    {0}: {1} GB -> {2} GB (freed {3} GB){4}" -f $v.Name, $before, $after, $delta, $flag)
    }
    $summary['Compact vhdx'] = [math]::Round($compactTotal,2)
  }
}

# ----- I: Temp --------------------------------------------------------------
Log ""; Log "[I] Clearing temp folders" Cyan
$tempTotal = 0
foreach ($t in @(
    @{n='%LOCALAPPDATA%\Temp'; p="$env:LOCALAPPDATA\Temp"; ex=@('claude')}
    @{n='C:\Windows\Temp';     p='C:\Windows\Temp';        ex=@()}
  )) {
  $beforeB = Get-SizeBytes $t.p
  if ($beforeB -lt 0) { continue }
  $before = To-GB $beforeB
  Remove-Contents -Path $t.p -Exclude $t.ex
  $after = if ($DryRun) { $before } else { To-GB (Get-SizeBytes $t.p) }
  $freed = if ($DryRun) { $before } else { [math]::Round($before - $after, 2) }
  $tempTotal += $freed
  $verb = if ($DryRun) { "would free" } else { "freed" }
  Log ("  {0}: {1} GB -> {2} GB ({3} {4} GB)" -f $t.n, $before, $after, $verb, $freed)
}
$summary['Temp'] = [math]::Round($tempTotal,2)

# ----- summary --------------------------------------------------------------
$finalFree = Get-FreeGB
$drive = Get-PSDrive C
$total = To-GB ($drive.Used + $drive.Free)
$totalReclaimed = [math]::Round($finalFree - $initialFree, 2)
$freePct = [math]::Round($drive.Free / ($drive.Used + $drive.Free) * 100, 1)
$elapsed = [math]::Round(((Get-Date) - $script:StartTime).TotalMinutes,1)

Log ""
Log "==========================================================" Cyan
Log ("Done in {0} min. Drive total: {1} GB. Free: {2} GB -> {3} GB" -f $elapsed, $total, $initialFree, $finalFree) Green
if (-not $DryRun) {
  Log ("Reclaimed this run: {0} GB. Drive now {1}% free." -f $totalReclaimed, $freePct) Green
} else {
  $est = ($summary.Values | Measure-Object -Sum).Sum
  Log ("DryRun estimate: would free ~{0} GB (Docker prune not included)." -f [math]::Round($est,2)) Yellow
}
Log "Breakdown:" Cyan
foreach ($k in $summary.Keys) { Log ("  {0,-18} {1,8} GB" -f $k, $summary[$k]) }
if ($freePct -lt 10) {
  Log ""
  Log ("WARNING: only {0}% free. Run '.\reclaim-disk.ps1 -Analyse' to see where it went." -f $freePct) Red
}
Log "==========================================================" Cyan
Log ("Log: {0}" -f $LogFile)
