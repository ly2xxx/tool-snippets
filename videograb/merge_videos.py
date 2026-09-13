#!/usr/bin/env python3
"""CLI utility to find and combine separated video and audio tracks in a directory."""

import argparse
import sys
from pathlib import Path
from vgrab.merger import MediaMerger


def main():
    parser = argparse.ArgumentParser(
        description="Losslessly combine matching video and audio tracks in a directory."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default="./besa_videos",
        help="Folder containing separate video and audio tracks (default: ./besa_videos)",
    )
    parser.add_argument(
        "-c", "--cleanup",
        action="store_true",
        help="Delete the individual source files after a successful merge",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show matched files without merging",
    )
    args = parser.parse_args()

    target_dir = Path(args.directory)
    if not target_dir.is_dir():
        print(f"Error: Directory '{target_dir}' does not exist.", file=sys.stderr)
        return 1

    try:
        merger = MediaMerger()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    pairs = merger.find_pairs(target_dir)
    if not pairs:
        print(f"No separated audio/video pairs found in '{target_dir}'.")
        return 0

    print(f"Found {len(pairs)} video/audio pair(s) in '{target_dir}':\n")
    merged = merger.merge_directory(target_dir, cleanup_sources=args.cleanup, dry_run=args.dry_run)
    action = "previewed" if args.dry_run else "merged"
    print(f"\nSuccessfully {action} {len(merged)} video(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
