# diskclone

Minimal, safety-first raw disk-to-disk cloner for Windows. Single Python file,
standard library only. Does a full sector-by-sector copy of one physical disk
onto another (like `dd`), with guardrails.

## When to use this vs. a real tool

| Scenario | Recommendation |
|---|---|
| Cloning the disk Windows is running from | **Rescuezilla bootable USB** (this tool warns you: a live clone can be inconsistent) |
| Cloning a data disk / second disk | This tool, or Hasleo Backup Suite Free |
| Clone to a *smaller* SSD | Hasleo / Clonezilla (raw clones can't shrink) |
| Scheduled image backups | Hasleo Backup Suite Free |

## Usage

Open an **elevated** (Run as Administrator) terminal:

```powershell
# See your disks first
python diskclone.py --list

# Rehearse without writing anything
python diskclone.py --source 0 --target 1 --dry-run

# Clone disk 0 onto disk 1, then verify with SHA-256
python diskclone.py --source 0 --target 1 --verify

# Flaky USB enclosure (stalls/resets): smaller chunks are gentler
python diskclone.py --source 0 --target 1 --chunk-mib 1

# Continue an interrupted clone (offset is printed on failure)
python diskclone.py --source 0 --target 1 --resume-from 17179869184
```

I/O errors (common with cheap USB enclosures and SMR drives that stall and
get reset by Windows) are retried automatically: the tool waits, reopens the
device — following it by serial number if it re-enumerated — seeks back, and
rewrites the chunk. Only after 8 failed attempts does it give up, printing
the exact `--resume-from` offset so no progress is lost.

You must type `DESTROY DISK <n>` to confirm before anything is written.

## Safety guarantees

- Never writes to the boot/system disk.
- Refuses source == target and target smaller than source.
- Takes the target offline first (so Windows can't corrupt the write) and
  leaves it offline afterwards to avoid disk-signature collisions.
- `--verify` re-reads the target and compares SHA-256 against the source.
- `--dry-run` performs all checks but writes nothing.

## Notes

- A raw clone copies free space too, so it is slower than smart tools that
  copy only used blocks — but it is also the most faithful copy possible
  (works for any filesystem, including Linux/BitLocker partitions).
- After cloning, keep only one of the two disks connected on next boot, or
  bring the clone online manually in Disk Management.
