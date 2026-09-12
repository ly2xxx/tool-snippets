# reclaim-disk

A single PowerShell script that **analyses** and **reclaims** space on the `C:` drive. Self-elevates, idempotent, safe defaults.

Two modes:

| Mode | Command | Effect |
| --- | --- | --- |
| **Analyse** | `.\reclaim-disk.ps1 -Analyse` | Read-only. Works out where C: actually went and writes a `.md` + `.json` report. Changes nothing. |
| **Clean** | `.\reclaim-disk.ps1` | Reclaims space: age-based Docker prune, `fstrim` + VHDX compaction, temp/cache clearing. |

## Quick start

```powershell
cd H:\code\yl\tool-snippets

# First: find out where the space went
.\reclaim-disk.ps1 -Analyse

# Then: reclaim it
.\reclaim-disk.ps1
```

A UAC prompt appears (the script re-launches itself as Administrator). Approve it. The elevated window stays open after the run so you can read the summary.

If Windows blocks the script with "running scripts is disabled":

```powershell
Unblock-File .\reclaim-disk.ps1
```

---

## Analyse mode

`-Analyse` never writes to anything except its own report files. It produces:

```
reclaim-analysis_<timestamp>.md      human-readable tables
reclaim-analysis_<timestamp>.json    same data, machine-readable
reclaim_<timestamp>.log              full console transcript
```

### What it inventories

| Section | Covers |
| --- | --- |
| Volumes | Every drive: total, free, free %. Includes H:, so you can see the headroom for moves. |
| Windows consumers | `hiberfil.sys`, `pagefile.sys`, `Windows.old`, `$GetCurrent`, `$WinREAgent`, Recycle Bin, WinSxS, Windows Installer cache, Windows Update download cache, Delivery Optimization, CBS logs, memory dumps, Package Cache — each with the specific remedy. |
| Virtual disks | Every `.vhdx` / `.vhd` / `.qcow2` / `.vmdk`, discovered from the **Lxss registry** (real WSL distro base paths) plus Docker, CRC, minikube and Hyper-V locations — not guessed from two hardcoded folders. |
| Docker | `docker system df` inline; full `docker system df -v` (per-image, per-volume, per-build-cache-record) written to the log. |
| Developer caches | ~50 known caches: npm, pnpm, yarn, pip, uv, poetry, conda, HuggingFace, torch, Ollama, Maven, Gradle, NuGet, Cargo, rustup, Go mod + build, Android SDK/AVD, Playwright, Puppeteer, Electron, Chocolatey, minikube, CRC, VS Code, Cursor, JetBrains, and more. Each tagged **regen** (safe to delete, tool re-downloads) or **data**. |
| Largest directories | Top-N children of `C:\`, your profile, `LOCALAPPDATA`, `APPDATA`, `ProgramData`, both `Program Files`. |
| Largest files | Every file over `-MinFileMB` (default 500 MB), sorted descending. |
| Move to H: | Ranked list of relocatable caches with a suggested destination and **the env var that relocates each one**. |

### Analyse flags

| Flag | Effect |
| --- | --- |
| `-DeepScan` | Also size `C:\Windows` and scan the whole of `C:\` for large files. Adds several minutes. |
| `-MinFileMB <n>` | Threshold for "largest files". Default 500. |
| `-Top <n>` | Rows per table. Default 30. |

Directory sizing uses `robocopy /L /S /XJ` rather than `Get-ChildItem -Recurse`. It is an order of magnitude faster, skips junctions (no infinite loops), and doesn't choke on permission-denied or long paths.

> **WinSxS caveat:** the reported size of `C:\Windows\WinSxS` overstates real usage, because most of it is hardlinks into `C:\Windows\System32`. Treat it as an upper bound; only `-DismCleanup` tells you what's actually reclaimable.

---

## Clean mode

### Runs every time

| Step | Action | Safe? |
| --- | --- | --- |
| A | Clear `SquirrelTemp`, `npm-cache`, `uv`, `pip\Cache`, `Yarn\Cache`, `go-build`, `CrashDumps`, `electron-builder\Cache` under `%LOCALAPPDATA%`. Tools re-download on next use. | yes |
| B | Delete `C:\$GetCurrent` and `C:\$WinREAgent` (Windows upgrade staging) if present. | yes |
| G | **Docker prune, by age.** Removes build cache, images, stopped containers and unused networks that have been unused for more than `-PruneAgeDays` (default 14). Volumes are left alone. | yes |
| H | **`fstrim` inside every WSL distro, then compact every `.vhdx`.** | yes |
| I | Empty `%LOCALAPPDATA%\Temp` (preserving `claude`) and `C:\Windows\Temp`. | yes |

### The fstrim step is the important one

`diskpart compact vdisk` and `Optimize-VHD` can only reclaim blocks the **guest** filesystem has marked as discarded. Deleting a 40 GB image inside Docker frees those blocks in ext4 but the host has no idea — the `.vhdx` stays 40 GB larger.

Real numbers from this machine, without fstrim:

```
docker_data.vhdx: 152.65 GB -> 150.88 GB (freed 1.77 GB)
```

That is 1.2%. The script now runs `fstrim -av` as root inside each distro **before** shutting WSL down and compacting, so the guest actually tells the host which blocks are free.

If a large VHDX still barely shrinks after fstrim, the script flags it inline:

```
<-- barely shrank: the guest FS is genuinely full, prune inside it
```

That means the space really is in use by images/layers/volumes, and you need a harder prune (`-PruneAgeDays 3`, or `-FullPrune`), not more compaction.

### Docker prune: age-based by default

| Flag | Effect |
| --- | --- |
| *(default)* | `builder prune`, `image prune`, `container prune` filtered to `until=<PruneAgeDays*24>h`, plus `network prune`. Anything you've touched in the last 14 days survives. |
| `-PruneAgeDays <n>` | Change the horizon. `7` is a reasonable weekly cadence; `3` before a big build. |
| `-PruneVolumes` | Also `docker volume prune -af`. **Destroys volume data** — databases, named caches. Off by default. |
| `-FullPrune` | Legacy behaviour: `docker system prune -af --volumes`. Ignores the age filter. Removes everything not attached to a running container. |
| `-NoPrune` | Skip pruning entirely; still fstrim + compact. |
| `-SkipDocker` | Don't touch Docker or WSL at all. |
| `-SkipTrim` | Skip fstrim. Not recommended — compaction will reclaim almost nothing. |

### Other optional flags

| Flag | Effect | Notes |
| --- | --- | --- |
| `-DismCleanup` | `DISM /Online /Cleanup-Image /StartComponentCleanup /ResetBase` | 5–15 min. Afterwards you can no longer uninstall already-installed Windows updates. |
| `-WindowsUpdateCache` | Stops `wuauserv`/`bits`/`dosvc`, clears `SoftwareDistribution\Download`, restarts them | Windows re-downloads anything it still needs. |
| `-EmptyRecycleBin` | `Clear-RecycleBin -DriveLetter C` | Permanent. |
| `-DisableHibernation` | `powercfg /h off` | Deletes `hiberfil.sys` (~75% of RAM). Reversible with `powercfg /h on`. |
| `-DryRun` | Reports what clean mode would do; touches nothing | Docker prune amounts aren't included in the estimate. |
| `-LogDir <path>` | Where to write log + reports | Default: alongside the script. |

---

## Examples

```powershell
# Where did my C: go?
.\reclaim-disk.ps1 -Analyse

# Same, but include C:\Windows and every file over 200 MB
.\reclaim-disk.ps1 -Analyse -DeepScan -MinFileMB 200

# Safe cleanup (age-based prune + fstrim + compact)
.\reclaim-disk.ps1

# Weekly cadence
.\reclaim-disk.ps1 -PruneAgeDays 7

# Emergency: drive nearly full
.\reclaim-disk.ps1 -FullPrune -DismCleanup -WindowsUpdateCache -EmptyRecycleBin

# Preview only
.\reclaim-disk.ps1 -DryRun
```

---

## Moving caches to H:

Analyse mode reports each relocatable cache with the env var that moves it. The pattern is always the same: move the folder, then set a **user** environment variable so the tool looks in the new place.

```powershell
# Example: Ollama models
robocopy "$env:USERPROFILE\.ollama\models" "H:\devcache\ollama-models" /E /MOVE /R:1 /W:1
[Environment]::SetEnvironmentVariable('OLLAMA_MODELS','H:\devcache\ollama-models','User')
```

Common ones:

| Cache | Env var |
| --- | --- |
| Ollama models | `OLLAMA_MODELS` |
| HuggingFace | `HF_HOME` |
| Maven repo | `-Dmaven.repo.local` in `MAVEN_OPTS`, or `<localRepository>` in `settings.xml` |
| Gradle | `GRADLE_USER_HOME` |
| Go modules | `GOMODCACHE` |
| Go build cache | `GOCACHE` |
| Cargo | `CARGO_HOME` |
| rustup | `RUSTUP_HOME` |
| NuGet | `NUGET_PACKAGES` |
| pip | `PIP_CACHE_DIR` |
| uv | `UV_CACHE_DIR` |
| Playwright browsers | `PLAYWRIGHT_BROWSERS_PATH` |
| Puppeteer | `PUPPETEER_CACHE_DIR` |
| Android SDK | `ANDROID_SDK_ROOT` |
| minikube | `MINIKUBE_HOME` |

Restart your shell (and any IDE) after setting a user env var.

### Moving Docker's disk image

The biggest single win, and the one the script deliberately does **not** automate:

1. Docker Desktop → Settings → Resources → Advanced → **Disk image location** → change to `H:\docker`.
2. Docker restarts and migrates. Budget the current VHDX size in free space on H: during the move.

For a WSL distro, `wsl --export <name> H:\wsl\<name>.tar` then `wsl --unregister <name>` then `wsl --import <name> H:\wsl\<name> H:\wsl\<name>.tar --version 2`. Verify the export before unregistering.

---

## Logs

Every run writes a timestamped log next to the script (or under `-LogDir`). Analyse mode adds the `.md` and `.json` reports.

diskpart/DISM progress spam ("`n percent completed`" x 3000) is filtered out of the log — the previous version's logs were 90% noise.

Suggested `.gitignore`:

```
*.log
reclaim-analysis_*.md
reclaim-analysis_*.json
```

---

## Re-runnability notes

- Idempotent. Re-running with the same flags just frees whatever has re-accumulated.
- Paths are detected dynamically. WSL distros come from `HKCU:\...\Lxss`, so it survives distro installs and Docker Desktop upgrades that move the VHDX.
- If Docker isn't installed, the prune is skipped and the compact step finds zero `.vhdx`. No errors.
- Compaction prefers `Optimize-VHD` (Hyper-V module) and falls back to `diskpart` if the module is missing or Hyper-V isn't enabled.
- Writes nothing to the registry and changes no system settings — except with `-DisableHibernation`, `-DismCleanup`, or `-WindowsUpdateCache`.

## Things this script deliberately does NOT do

- Does not touch `Downloads`, `Documents`, or anything in `Roaming`. Those are your files.
- Does not move anything to H: — analyse mode identifies candidates, you decide.
- Does not uninstall programs.
- Does not delete WSL distros or your data inside them.
- Does not prune Docker volumes unless you pass `-PruneVolumes`.
- Does not modify `pagefile.sys`.
- Runs no web request or telemetry. External commands: `robocopy`, `powercfg`, `dism`, `wsl`, `diskpart`, `docker`, `takeown`, `icacls`.

## When to run it

- `-Analyse` whenever you're surprised by how full C: is.
- Clean mode whenever C: free space drops below ~15%.
- After a big Docker session that pulled lots of images.
- After a Windows feature update (often leaves `$GetCurrent` behind).

---

## Appendix: Chrome remote debugging

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
```
