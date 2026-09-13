"""Utility to combine separated video and audio streams into single MP4 files.

Handles adaptive streams (like Vimeo HLS or DASH) downloaded when ffmpeg
was not initially present on system PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def find_ffmpeg(custom_path: Optional[str] = None) -> Optional[str]:
    """Find a usable ffmpeg executable (system PATH or imageio_ffmpeg)."""
    if custom_path and os.path.isfile(custom_path):
        return custom_path
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


class MediaMerger:
    """Combines matching video-only and audio-only tracks into a single container."""

    def __init__(self, ffmpeg_path: Optional[str] = None):
        self.ffmpeg = find_ffmpeg(ffmpeg_path)
        if not self.ffmpeg:
            raise RuntimeError(
                "ffmpeg not found. Install ffmpeg on PATH or run `uv add imageio-ffmpeg`"
            )

    def merge_pair(self, video_path: str | Path, audio_path: str | Path,
                   output_path: str | Path, overwrite: bool = True) -> str:
        """Losslessly mux video and audio without re-encoding (-c copy)."""
        video_path = str(video_path)
        audio_path = str(audio_path)
        output_path = str(output_path)

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video track missing: {video_path}")
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio track missing: {audio_path}")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        cmd = [self.ffmpeg]
        if overwrite:
            cmd.append("-y")
        cmd += ["-i", video_path, "-i", audio_path, "-c", "copy", output_path]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg muxing failed: {proc.stderr[-400:]}")
        return output_path

    def find_pairs(self, directory: str | Path) -> List[Tuple[Path, Path, Path]]:
        """Scans a directory for matching (video, audio) tracks.

        Returns list of tuples: (video_path, audio_path, target_output_path)
        """
        directory = Path(directory)
        if not directory.is_dir():
            return []

        # Find all audio files
        audio_files = list(directory.glob("*audio*.mp4"))
        pairs = []

        for a_file in audio_files:
            # Suffix pattern like: .fhls-fastly_skyfire-audio-high-English.mp4
            # Strip the audio qualifier to find the matching video file
            base_pattern = re.sub(r"\.f[a-zA-Z0-9_-]*audio[a-zA-Z0-9_-]*\.mp4$", "", a_file.name)
            if base_pattern == a_file.name:
                # Fallback: simple split on audio
                base_pattern = a_file.name.split("audio")[0].rstrip(".-_")

            # Search for candidate video file with same base prefix
            v_candidates = [
                f for f in directory.glob(f"{glob_escape(base_pattern)}*.mp4")
                if f != a_file and "audio" not in f.name.lower()
            ]

            if v_candidates:
                # Pick best/largest video file if multiple
                video_file = max(v_candidates, key=lambda f: f.stat().st_size)
                # Clean up target filename (strip intermediate track labels)
                clean_name = re.sub(r"\s*\[external\]\.f[a-zA-Z0-9_.-]+$", "", base_pattern)
                if not clean_name.endswith(".mp4"):
                    clean_name += ".mp4"
                target = directory / clean_name
                pairs.append((video_file, a_file, target))

        pairs.sort(key=lambda x: x[2].name)
        return pairs

    def merge_directory(self, directory: str | Path, cleanup_sources: bool = False,
                        dry_run: bool = False) -> List[Path]:
        """Discovers and merges all paired files in a folder."""
        pairs = self.find_pairs(directory)
        merged = []
        for v, a, out in pairs:
            if dry_run:
                print(f"[dry-run] Merge:\n  Video: {v.name}\n  Audio: {a.name}\n  -> {out.name}\n")
                merged.append(out)
                continue

            print(f"Merging: {out.name}...")
            self.merge_pair(v, a, out, overwrite=True)
            merged.append(out)

            if cleanup_sources:
                v.unlink(missing_ok=True)
                a.unlink(missing_ok=True)

        return merged


def glob_escape(pathname: str) -> str:
    """Escape special glob characters [ ] ? *."""
    drive, pathname = os.path.splitdrive(pathname)
    pathname = re.sub(r"([*?\[\]])", r"[\1]", pathname)
    return drive + pathname
