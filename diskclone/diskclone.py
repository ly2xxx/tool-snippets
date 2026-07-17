#!/usr/bin/env python3
"""
diskclone.py - Minimal, safety-first raw disk-to-disk cloner for Windows.

Performs a full sector-by-sector copy of one physical disk onto another
(\\.\PhysicalDriveN -> \\.\PhysicalDriveM). Everything on the target disk
is destroyed. Requires an elevated (Administrator) prompt.

Usage:
    python diskclone.py --list
    python diskclone.py --source 0 --target 1 [--verify] [--dry-run]

Safety features:
    - Refuses to clone a disk onto itself.
    - Refuses to write to the boot/system disk (the disk Windows runs from).
    - Refuses if the target is smaller than the source.
    - Takes the target disk OFFLINE before writing so Windows cannot
      interfere, and leaves it offline afterwards (bring it online in
      Disk Management when ready).
    - Requires you to type an explicit confirmation phrase.
    - Warns (and requires extra confirmation) if the source is the live
      Windows disk, because a live clone can be inconsistent. For cloning
      your Windows disk, prefer a bootable tool such as Rescuezilla.

Only Python 3.8+ standard library is used. No third-party packages.
"""

import argparse
import ctypes
import ctypes.wintypes
import hashlib
import json
import msvcrt
import os
import subprocess
import sys
import time

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB, sector-aligned for 512e and 4Kn disks
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C


def die(msg, code=1):
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_powershell(command):
    """Run a PowerShell command and return stdout text."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        die(f"PowerShell command failed:\n{result.stderr.strip()}")
    return result.stdout


def get_disks():
    """Return a list of dicts describing all physical disks."""
    ps = (
        "Get-Disk | ForEach-Object { [PSCustomObject]@{ "
        "Number = $_.Number; "
        "Name = $_.FriendlyName; "
        "Serial = ([string]$_.SerialNumber).Trim(); "
        "Size = $_.Size; "
        "IsBoot = $_.IsBoot; "
        "IsSystem = $_.IsSystem; "
        "Style = [string]$_.PartitionStyle; "
        "Status = [string]$_.OperationalStatus; "
        "Letters = (($_ | Get-Partition -ErrorAction SilentlyContinue | "
        "Where-Object DriveLetter | ForEach-Object { \"$($_.DriveLetter):\" }) -join ' ') "
        "} } | ConvertTo-Json"
    )
    out = run_powershell(ps).strip()
    if not out:
        die("No disks found (are you running as Administrator?)")
    data = json.loads(out)
    if isinstance(data, dict):  # ConvertTo-Json unwraps single-element arrays
        data = [data]
    return data


def format_size(num_bytes):
    gib = num_bytes / (1024 ** 3)
    if gib >= 1024:
        return f"{gib / 1024:.2f} TiB"
    return f"{gib:.1f} GiB"


def print_disk_table(disks):
    print(f"{'Disk':<5} {'Size':>10}  {'Boot/Sys':<8} {'Style':<5} {'Status':<8} "
          f"{'Letters':<10} Name (Serial)")
    print("-" * 90)
    for d in disks:
        flags = []
        if d.get("IsBoot"):
            flags.append("BOOT")
        if d.get("IsSystem"):
            flags.append("SYS")
        print(f"{d['Number']:<5} {format_size(d['Size']):>10}  "
              f"{'/'.join(flags) or '-':<8} {d.get('Style') or '?':<5} "
              f"{d.get('Status') or '?':<8} {d.get('Letters') or '-':<10} "
              f"{d.get('Name') or '?'} ({d.get('Serial') or 'no serial'})")


def get_exact_length(fileobj):
    """Exact byte length of an opened physical drive via DeviceIoControl."""
    handle = msvcrt.get_osfhandle(fileobj.fileno())
    length = ctypes.c_longlong(0)
    returned = ctypes.wintypes.DWORD(0)
    ok = ctypes.windll.kernel32.DeviceIoControl(
        ctypes.wintypes.HANDLE(handle), IOCTL_DISK_GET_LENGTH_INFO,
        None, 0, ctypes.byref(length), 8, ctypes.byref(returned), None)
    if not ok:
        die(f"DeviceIoControl(IOCTL_DISK_GET_LENGTH_INFO) failed "
            f"(WinError {ctypes.get_last_error()})")
    return length.value


def set_disk_offline(number, offline):
    state = "$true" if offline else "$false"
    run_powershell(f"Set-Disk -Number {number} -IsOffline {state}")
    if not offline:
        return
    run_powershell(f"Set-Disk -Number {number} -IsReadOnly $false")


class RawDevice:
    """Raw physical-drive handle that survives USB bridge resets.

    Cheap USB enclosures (especially SMR mechanical drives) can stall under
    sustained writes until Windows resets the device, which invalidates the
    open handle (surfaces as OSError errno 22). On any I/O error we wait,
    reopen the device, seek back to the failed offset, and retry the whole
    chunk. If the disk re-enumerated under a new number, we find it again
    by serial number before retrying.
    """

    RETRY_DELAYS = [3, 5, 10, 20, 30, 45, 60, 60]

    def __init__(self, disk_number, mode, expected_serial=None):
        self.disk_number = disk_number
        self.mode = mode
        self.expected_serial = (expected_serial or "").strip()
        self.offset = 0
        self.f = self._open()

    def path(self):
        return f"\\\\.\\PhysicalDrive{self.disk_number}"

    def _open(self):
        f = open(self.path(), self.mode, buffering=0)
        if self.offset:
            f.seek(self.offset)
        return f

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass

    def _relocate(self):
        """After a reset the disk may come back under a different number."""
        if not self.expected_serial:
            return
        try:
            disks = get_disks()
        except SystemExit:
            return
        for d in disks:
            if (d.get("Serial") or "").strip() == self.expected_serial:
                if d["Number"] != self.disk_number:
                    print(f"\nNote: disk re-enumerated as PhysicalDrive{d['Number']} "
                          f"(was {self.disk_number}); following it by serial.")
                    self.disk_number = d["Number"]
                return

    def _retry_loop(self, op_name, fn):
        last_err = None
        for attempt, delay in enumerate(self.RETRY_DELAYS, start=1):
            try:
                return fn()
            except OSError as err:
                last_err = err
                print(f"\n{op_name} error at offset {self.offset:,} ({err}). "
                      f"Device may have stalled/reset; waiting {delay}s, then "
                      f"retrying (attempt {attempt}/{len(self.RETRY_DELAYS)})...")
                self.close()
                time.sleep(delay)
                self._relocate()
                try:
                    self.f = self._open()
                except OSError as err2:
                    last_err = err2
        die(f"{op_name} failed permanently at offset {self.offset:,}: {last_err}\n"
            f"The data up to that offset is already cloned. You can continue with:\n"
            f"    --resume-from {self.offset}")

    def read(self, n):
        def do():
            buf = self.f.read(n)
            got = 0 if buf is None else len(buf)
            if got != n:
                raise OSError(f"short read ({got}/{n} bytes)")
            return buf
        buf = self._retry_loop("Read", do)
        self.offset += n
        return buf

    def write(self, buf):
        def do():
            written = self.f.write(buf)
            if written != len(buf):
                raise OSError(f"short write ({written}/{len(buf)} bytes)")
        self._retry_loop("Write", do)
        self.offset += len(buf)

    def seek(self, offset):
        self.offset = offset
        self.f.seek(offset)


def copy_raw(src, dst, total, label="Cloning", hash_src=False,
             chunk=CHUNK_SIZE, start_offset=0):
    """Copy bytes [start_offset, total) from src to dst.
    Returns SHA-256 of the copied region if hash_src."""
    hasher = hashlib.sha256() if hash_src else None
    copied = start_offset
    session = 0  # bytes moved in this run, for speed/ETA
    start = time.monotonic()
    last_print = 0.0
    while copied < total:
        want = min(chunk, total - copied)
        buf = src.read(want)
        if dst is not None:
            dst.write(buf)
        if hasher:
            hasher.update(buf)
        copied += len(buf)
        session += len(buf)
        now = time.monotonic()
        if now - last_print >= 0.5 or copied >= total:
            elapsed = now - start
            speed = session / elapsed if elapsed > 0 else 0
            eta = (total - copied) / speed if speed > 0 else 0
            print(f"\r{label}: {copied / (1024**3):7.2f} / {total / (1024**3):.2f} GiB "
                  f"({100 * copied / total:5.1f}%)  {speed / (1024**2):6.1f} MiB/s  "
                  f"ETA {int(eta // 60):3d}m{int(eta % 60):02d}s   ", end="", flush=True)
            last_print = now
    print()
    return hasher.hexdigest() if hasher else None


def hash_disk(disk_number, serial, total, label, chunk=CHUNK_SIZE, start=0):
    dev = RawDevice(disk_number, "rb", serial)
    try:
        if start:
            dev.seek(start)
        return copy_raw(dev, None, total, label=label, hash_src=True,
                        chunk=chunk, start_offset=start)
    finally:
        dev.close()


def main():
    parser = argparse.ArgumentParser(
        description="Minimal raw disk-to-disk cloner for Windows (run as Administrator).")
    parser.add_argument("--list", action="store_true", help="list physical disks and exit")
    parser.add_argument("--source", type=int, help="source disk number (from --list)")
    parser.add_argument("--target", type=int, help="target disk number (WILL BE ERASED)")
    parser.add_argument("--verify", action="store_true",
                        help="after cloning, re-read both disks and compare SHA-256")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen, copy nothing")
    parser.add_argument("--resume-from", type=int, default=0, metavar="BYTES",
                        help="continue an interrupted clone from this byte offset "
                             "(printed when a clone fails)")
    parser.add_argument("--chunk-mib", type=int, default=4,
                        help="copy chunk size in MiB (default 4; try 1 for "
                             "flaky USB enclosures)")
    args = parser.parse_args()

    if os.name != "nt":
        die("This tool only runs on Windows.")
    if not is_admin():
        die("Administrator privileges required. Re-run from an elevated prompt.")

    disks = get_disks()

    if args.list or args.source is None or args.target is None:
        print_disk_table(disks)
        if not args.list:
            print("\nSpecify --source N and --target M to clone.")
        return

    by_number = {d["Number"]: d for d in disks}
    src_info = by_number.get(args.source)
    dst_info = by_number.get(args.target)

    # ---- Safety checks -------------------------------------------------
    if src_info is None:
        die(f"Source disk {args.source} not found.")
    if dst_info is None:
        die(f"Target disk {args.target} not found.")
    if args.source == args.target:
        die("Source and target are the same disk.")
    if dst_info.get("IsBoot") or dst_info.get("IsSystem"):
        die(f"Target disk {args.target} is the boot/system disk. Refusing to erase it.")
    if dst_info["Size"] < src_info["Size"]:
        die(f"Target ({format_size(dst_info['Size'])}) is smaller than "
            f"source ({format_size(src_info['Size'])}). A raw clone cannot shrink.")

    print("About to clone:\n")
    print_disk_table([src_info, dst_info])
    print(f"\n  SOURCE: disk {args.source}  ({src_info['Name']})")
    print(f"  TARGET: disk {args.target}  ({dst_info['Name']})  "
          f"<-- ALL DATA ON THIS DISK WILL BE DESTROYED")

    if src_info.get("IsBoot") or src_info.get("IsSystem"):
        print("\nWARNING: the source is the LIVE Windows disk. Windows keeps writing to it,")
        print("so the copy may be inconsistent (files mid-write, dirty NTFS journal).")
        print("For a dependable clone of your Windows disk, boot Rescuezilla from USB instead.")
        ack = input("Type LIVE to accept this risk, anything else to abort: ")
        if ack.strip() != "LIVE":
            die("Aborted.", code=0)

    if args.dry_run:
        print("\n--dry-run: no data was written.")
        return

    phrase = f"DESTROY DISK {args.target}"
    answer = input(f"\nType exactly '{phrase}' to continue: ")
    if answer.strip() != phrase:
        die("Confirmation phrase did not match. Nothing was written.", code=0)

    chunk = args.chunk_mib * 1024 * 1024
    resume = args.resume_from
    if resume < 0 or resume % 4096:
        die("--resume-from must be a non-negative multiple of 4096.")

    print(f"\nTaking disk {args.target} offline...")
    set_disk_offline(args.target, True)

    src = dst = None
    try:
        try:
            src = RawDevice(args.source, "rb", src_info.get("Serial"))
            dst = RawDevice(args.target, "r+b", dst_info.get("Serial"))
        except PermissionError:
            die("Access denied opening the raw disks. Make sure no other tool "
                "(backup software, Disk Management) is holding them open.")
        total = get_exact_length(src.f)
        target_len = get_exact_length(dst.f)
        if target_len < total:
            die(f"Exact target length ({target_len:,}) is smaller than "
                f"source ({total:,}).")
        if resume:
            if resume >= total:
                die("--resume-from is beyond the end of the disk.")
            print(f"Resuming at offset {resume:,} "
                  f"({resume / (1024**3):.2f} GiB already done).")
            src.seek(resume)
            dst.seek(resume)
        src_hash = copy_raw(src, dst, total, hash_src=args.verify,
                            chunk=chunk, start_offset=resume)
        dst.f.flush()
        os.fsync(dst.f.fileno())
    finally:
        if src:
            src.close()
        if dst:
            dst.close()

    print("Clone complete.")

    if args.verify:
        scope = "" if not resume else f" (resumed region only, from {resume:,})"
        print(f"Verifying: re-reading target{scope}...")
        dst_hash = hash_disk(args.target, dst_info.get("Serial"), total,
                             "Verify", chunk=chunk, start=resume)
        if dst_hash == src_hash:
            print(f"Verify OK: SHA-256 {dst_hash}")
        else:
            die(f"VERIFY FAILED!\n  source: {src_hash}\n  target: {dst_hash}\n"
                "The clone does not match. Do not trust the target disk.")

    print(f"\nDisk {args.target} was left OFFLINE on purpose. Because it is now an exact")
    print("copy, Windows may see a disk-signature/GUID collision if both disks stay")
    print("connected. To use the clone: shut down, swap/disconnect the old disk, boot —")
    print("or bring it online in Disk Management if you know what you're doing.")


if __name__ == "__main__":
    main()
