# reclaim-disk

A single PowerShell script that reclaims space on the `C:` drive. Self-elevates, idempotent, safe defaults. Built from the three-stage cleanup we ran together — consolidated so you can run it yourself.

## Quick start

From any PowerShell prompt:

```powershell
cd C:\Temp\reclaim-disk
.\reclaim-disk.ps1
```

A UAC prompt appears (the script re-launches itself as Administrator). Approve it. The elevated window stays open after the run so you can read the summary.

If Windows blocks the script with "running scripts is disabled":

```powershell
Unblock-File .\reclaim-disk.ps1
```

## What it does by default

These run every time, in order. All paths are derived from `%LOCALAPPDATA%` etc., so the script works on any user account.

| Step | Action                                                                                                                                                                                                                                                            | Safe? |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| A    | Clear`SquirrelTemp`, `npm-cache`, `uv`, `pip\Cache` under `%LOCALAPPDATA%`. Tools re-download on next use.                                                                                                                                              | yes   |
| B    | Delete`C:\$GetCurrent` (Windows upgrade staging) if present.                                                                                                                                                                                                    | yes   |
| F    | Shut down WSL + Docker Desktop and**compact** every `.vhdx` it finds under `%LOCALAPPDATA%\Docker` and `%LOCALAPPDATA%\wsl`. Compaction only returns *empty/slack* space — your Docker images, containers, volumes, and WSL distros remain intact. | yes   |
| G    | Empty`%LOCALAPPDATA%\Temp`, preserving the `claude` subfolder.                                                                                                                                                                                                | yes   |

## Optional flags

These are off by default. Add them when you want more aggressive reclaim.

| Flag                    | What it adds                                                                                                                   | Notes                                                                                                                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `-PruneDocker`        | Starts Docker Desktop, runs`docker system prune -af --volumes` and `docker builder prune -af` **before** compacting. | **Destructive.** Deletes every Docker image, container, network, anonymous volume, and build cache entry that isn't tied to a running container. Use when you want a big reclaim and accept you'll re-pull images later. |
| `-DismCleanup`        | Runs`DISM /Online /Cleanup-Image /StartComponentCleanup /ResetBase`.                                                         | Cleans the WinSxS component store. Takes 5–15 minutes. After`/ResetBase`, you can no longer uninstall already-installed Windows updates.                                                                                    |
| `-DisableHibernation` | Runs`powercfg /h off`.                                                                                                       | Deletes`C:\hiberfil.sys` (~RAM-sized: 8–64 GB). Only use if you don't rely on Hibernate or Fast Startup. Reversible with `powercfg /h on`.                                                                                |
| `-SkipDocker`         | Skips both prune and compact entirely.                                                                                         | Use when Docker is busy or you've just used the script and only want the other cleanups.                                                                                                                                       |
| `-DryRun`             | Reports what would be freed; touches nothing.                                                                                  | Recommended before the first run on a new machine. Compaction and DISM amounts are unknown until done — the rest is precise.                                                                                                  |
| `-LogDir <path>`      | Where to write the timestamped log.                                                                                            | Default: alongside the script.                                                                                                                                                                                                 |

## Examples

```powershell
# See what would happen — touch nothing
.\reclaim-disk.ps1 -DryRun

# Default safe cleanup
.\reclaim-disk.ps1

# Big reclaim after Docker has accumulated junk
.\reclaim-disk.ps1 -PruneDocker

# Everything: Docker prune + DISM + hibernation off
.\reclaim-disk.ps1 -PruneDocker -DismCleanup -DisableHibernation
```

## What's in each stage (and why)

**[A] User caches** — These four folders are documented caches for npm, uv (Python), pip, and Squirrel (auto-updaters). Each grows over time and never shrinks on its own. Cleared safely; the tools repopulate them as you use them.

**[B] `C:\$GetCurrent`** — Leftover from a Windows feature update. Windows itself does not remove this. Often 3–5 GB.

**[C] Hibernation** *(optional)* — `hiberfil.sys` is sized at ~75% of installed RAM. Disabling hibernation deletes it. Skip this if you use Hibernate or Fast Startup.

**[D] DISM `/ResetBase`** *(optional)* — Removes superseded Windows component-store files. After this you cannot uninstall already-installed Windows updates, which is usually fine.

**[E] Docker prune** *(optional)* — `docker system prune -af --volumes` plus `docker builder prune -af` deletes every Docker artifact that isn't currently in use. The 32 GB of internal Docker space that gets freed only returns to Windows after [F] compaction runs.

**[F] Compact `.vhdx`** — Docker Desktop and each WSL distro store their filesystem in a dynamic VHDX. The VHDX grows but never shrinks on its own. We shut down WSL + Docker, then call `diskpart compact vdisk` on each VHDX found. Empty/slack space is returned to the host drive; live data is untouched.

**[G] Temp** — `%LOCALAPPDATA%\Temp` regularly holds gigabytes of installer/runtime junk. The `claude` subfolder is preserved so Claude Code's task output isn't disrupted if you run this mid-session.

## Logs

Every run writes a timestamped log next to the script (or under `-LogDir`):

```
reclaim_20260623_143055.log
```

Each line is also printed to the elevated console. Look for the **Breakdown** table at the end for per-step reclaim.

## Re-runnability notes

- The script is idempotent. Re-running with the same flags is safe and just frees whatever has re-accumulated.
- Paths are detected dynamically (`%LOCALAPPDATA%`, both `Program Files` locations, recursive `.vhdx` discovery). It survives Docker Desktop version upgrades that move the VHDX path.
- If Docker isn't installed at all, `-PruneDocker` is silently skipped and the compact step finds zero `.vhdx` files. No errors.
- The script writes nothing to the registry and does not change system settings — except when you explicitly pass `-DisableHibernation` or `-DismCleanup`.

## Things this script deliberately does NOT do

- It will not touch `C:\Users\<you>\Downloads`, `Documents`, or anything in `Roaming`. Those are your files.
- It will not uninstall programs.
- It will not delete WSL distros or your data inside them.
- It will not modify `pagefile.sys` (Windows-managed).
- It will not run any web request, telemetry, or external command beyond `powercfg`, `dism`, `wsl`, `diskpart`, `docker`, `takeown`, and `icacls`.

## When to run it

- Whenever C: free space drops below ~15%.
- After a big Docker session that pulled lots of images.
- After a Windows feature update (often leaves `$GetCurrent` behind).
- Before a long task that will write a lot (CI build, large download, etc.).

  ---
  "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
