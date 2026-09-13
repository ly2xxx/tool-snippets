"""Downloading, with four backends and an honest auto-picker.

    direct   plain HTTP with Range resume         progressive files
    hls      native segment fetch (+ AES-128)     m3u8, no ffmpeg needed
    ffmpeg   `ffmpeg -c copy`                     m3u8/mpd, best quality-of-life
    ytdlp    `yt-dlp`                             YouTube & friends

Auto picks ffmpeg for streams when it is on PATH, falls back to the native
HLS downloader when it is not, and refuses to pretend it can do DRM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.parse
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .http import Fetcher, absolute
from .models import DASH, EXTERNAL, HLS, PROGRESSIVE, Video
from .merger import find_ffmpeg

Progress = Callable[[str, int, Optional[int]], None]

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10))}


def safe_name(text: str, max_len: int = 120) -> str:
    """Filesystem-safe on Windows too (this repo's other tools are Windows-first)."""
    text = INVALID.sub("_", (text or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    if text.split(".")[0].lower() in RESERVED:
        text = "_" + text
    return text[:max_len] or "video"


def extension_for(video: Video, backend: Optional[str] = None) -> str:
    if backend == "hls":
        # The native downloader concatenates segments; it does not remux, so
        # calling the result .mp4 would be a lie. It plays fine in VLC/mpv,
        # and `--backend ffmpeg` gives you a real .mp4.
        return "ts"
    if video.kind in (HLS, DASH):
        return "mp4"                       # what a remux produces
    if video.container and video.container.lower() not in ("m3u8", "mpd"):
        return video.container.lower()
    guess = os.path.splitext(urllib.parse.urlsplit(video.url).path)[1].lstrip(".")
    return (guess or "mp4").lower()


def filename_for(video: Video, template: str = "{index:03d} - {title} [{label}].{ext}",
                 index: int = 1, backend: Optional[str] = None) -> str:
    fields = {
        "index": index,
        "title": safe_name(video.title or "video"),
        "label": safe_name(video.label.replace(" ", "_")),
        "id": video.id,
        "kind": video.kind,
        "height": video.height or "",
        "ext": extension_for(video, backend),
        "source": video.source or "",
    }
    try:
        name = template.format(**fields)
    except (KeyError, ValueError, IndexError):
        name = f"{index:03d} - {fields['title']}.{fields['ext']}"
    return safe_name(name, 200)


def have(tool: str) -> bool:
    if tool == "ffmpeg":
        return find_ffmpeg("ffmpeg") is not None
    return shutil.which(tool) is not None


class DownloadError(Exception):
    pass


class Downloader:
    def __init__(self, fetcher: Fetcher, out_dir: str = ".",
                 backend: str = "auto", overwrite: bool = False,
                 progress: Optional[Progress] = None,
                 ffmpeg: str = "ffmpeg", ytdlp: str = "yt-dlp",
                 cookies_file: Optional[str] = None, dry_run: bool = False):
        self.fetcher = fetcher
        self.out_dir = out_dir
        self.backend = backend
        self.overwrite = overwrite
        self.progress = progress or (lambda name, done, total: None)
        self.ffmpeg = ffmpeg
        self.ytdlp = ytdlp
        self.cookies_file = cookies_file
        self.dry_run = dry_run

    # -- selection ---------------------------------------------------------
    def choose(self, video: Video) -> str:
        if self.backend != "auto":
            return self.backend
        if video.kind == EXTERNAL:
            return "ytdlp"
        if video.kind in (HLS, DASH):
            if have(self.ffmpeg):
                return "ffmpeg"
            if video.kind == HLS:
                return "hls"
            return "ytdlp"
        return "direct"

    def download(self, video: Video, index: int = 1,
                 template: str = "{index:03d} - {title} [{label}].{ext}") -> str:
        if video.meta.get("drm"):
            raise DownloadError(
                f"{video.url}: DRM-protected ({video.meta.get('provider', 'unknown')}); "
                "no extraction path exists")
        os.makedirs(self.out_dir, exist_ok=True)
        backend = self.choose(video)
        name = filename_for(video, template, index, backend)
        path = os.path.join(self.out_dir, name)
        if os.path.exists(path) and not self.overwrite:
            existing = os.path.getsize(path)
            if existing > 0:
                self.progress(name, existing, existing)
                return path
        if self.dry_run:
            return f"[dry-run {backend}] {path}"
        fn = {"direct": self._direct, "hls": self._hls, "ffmpeg": self._ffmpeg,
              "ytdlp": self._ytdlp}.get(backend)
        if fn is None:
            raise DownloadError(f"unknown backend {backend!r}")
        return fn(video, path, name)

    # -- backends ----------------------------------------------------------
    def _direct(self, video: Video, path: str, name: str) -> str:
        part = path + ".part"
        done = os.path.getsize(part) if os.path.exists(part) else 0
        headers = dict(video.headers)
        total: Optional[int] = video.filesize
        for attempt in range(4):
            h = dict(headers)
            if done:
                h["Range"] = f"bytes={done}-"
            resp = self.fetcher.raw(video.url, h)
            status = getattr(resp, "status", 0) or 0
            if status in (200, 206):
                length = resp.headers.get("content-length")
                if length and status == 200:
                    total = int(length)
                elif length:
                    total = done + int(length)
                mode = "ab" if (done and status == 206) else "wb"
                if mode == "wb":
                    done = 0
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        self.progress(name, done, total)
                resp.close()
                if total is None or done >= total:
                    os.replace(part, path)
                    return path
                # Short read: loop and resume from where we stopped.
                continue
            if status == 416 and done:      # already complete
                os.replace(part, path)
                return path
            raise DownloadError(f"{video.url}: HTTP {status}")
        raise DownloadError(f"{video.url}: incomplete after retries")

    def _hls(self, video: Video, path: str, name: str) -> str:
        """Native HLS: resolve to a media playlist, fetch segments in order.

        Produces a .ts (or raw fMP4) concatenation. That plays in VLC/mpv as
        is; pass --backend ffmpeg for a clean MP4 if you have ffmpeg.
        """
        playlist_url, text = self._media_playlist(video)
        lines = [ln.strip() for ln in text.splitlines()]
        segments: List[str] = []
        init_seg: Optional[str] = None
        key_info: Optional[Dict[str, str]] = None
        for ln in lines:
            if ln.startswith("#EXT-X-MAP:"):
                m = re.search(r'URI="([^"]+)"', ln)
                if m:
                    init_seg = absolute(playlist_url, m.group(1))
            elif ln.startswith("#EXT-X-KEY:"):
                from .extractors.manifests import parse_attrs

                attrs = parse_attrs(ln.split(":", 1)[1])
                if attrs.get("METHOD", "NONE") != "NONE":
                    key_info = attrs
            elif ln and not ln.startswith("#"):
                segments.append(absolute(playlist_url, ln))
        if not segments:
            raise DownloadError(f"{playlist_url}: no segments in playlist")

        decryptor = None
        if key_info:
            decryptor = self._aes_decryptor(key_info, playlist_url, video)

        part = path + ".part"
        total = len(segments)
        with open(part, "wb") as fh:
            if init_seg:
                fh.write(self._get_bytes(init_seg, video.headers))
            for i, seg in enumerate(segments, 1):
                data = self._get_bytes(seg, video.headers)
                if decryptor:
                    data = decryptor(data, i - 1)
                fh.write(data)
                self.progress(name, i, total)
        os.replace(part, path)
        return path

    def _media_playlist(self, video: Video) -> Tuple[str, str]:
        """Follow a master playlist down to the rendition we actually want."""
        page = self.fetcher.fetch(video.url, headers=video.headers)
        if page.status >= 400:
            raise DownloadError(f"{video.url}: HTTP {page.status}")
        text = page.text
        if "#EXT-X-STREAM-INF" not in text:
            return page.url, text
        from .extractors.manifests import parse_attrs

        best: Optional[Tuple[int, str]] = None
        pending = None
        for ln in (l.strip() for l in text.splitlines()):
            if ln.startswith("#EXT-X-STREAM-INF:"):
                pending = parse_attrs(ln.split(":", 1)[1])
            elif ln and not ln.startswith("#") and pending is not None:
                bw = int(pending.get("BANDWIDTH", "0") or 0)
                if best is None or bw > best[0]:
                    best = (bw, absolute(page.url, ln))
                pending = None
        if best is None:
            raise DownloadError(f"{video.url}: master playlist with no variants")
        sub = self.fetcher.fetch(best[1], headers=video.headers)
        return sub.url, sub.text

    def _aes_decryptor(self, key_info: Dict[str, str], playlist_url: str,
                       video: Video):
        method = key_info.get("METHOD", "")
        fmt = key_info.get("KEYFORMAT", "identity")
        if method != "AES-128" or fmt not in ("identity", ""):
            raise DownloadError(
                f"HLS encrypted with {method}/{fmt} - that is DRM, not obfuscation; "
                "no extraction path")
        try:
            from cryptography.hazmat.primitives.ciphers import (
                Cipher, algorithms, modes)
        except BaseException as e:
            # Not just ImportError: a mismatched cryptography build raises a
            # Rust panic on import, which would otherwise abort the whole run.
            raise DownloadError(
                f"AES-128 HLS needs a working `cryptography` package ({e}); "
                "pip install -U cryptography, or use --backend ffmpeg")
        key_url = absolute(playlist_url, key_info.get("URI", ""))
        key = self._get_bytes(key_url, video.headers)
        iv_hex = key_info.get("IV", "")

        def decrypt(data: bytes, seq: int) -> bytes:
            if iv_hex:
                iv = bytes.fromhex(iv_hex[2:] if iv_hex.lower().startswith("0x") else iv_hex)
            else:
                iv = seq.to_bytes(16, "big")
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            dec = cipher.decryptor()
            out = dec.update(data) + dec.finalize()
            if out:                                   # strip PKCS7 padding
                pad = out[-1]
                if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
                    out = out[:-pad]
            return out

        return decrypt

    def _get_bytes(self, url: str, headers: Dict[str, str]) -> bytes:
        resp = self.fetcher.raw(url, headers)
        status = getattr(resp, "status", 0) or 0
        if status >= 400:
            raise DownloadError(f"{url}: HTTP {status}")
        data = resp.read()
        resp.close()
        return data

    def _ffmpeg(self, video: Video, path: str, name: str) -> str:
        if not have(self.ffmpeg):
            raise DownloadError(f"{self.ffmpeg} not found on PATH")
        headers = dict(video.headers)
        cookie = self.fetcher.cookie_header_for(video.url)
        if cookie:
            headers["Cookie"] = cookie
        header_arg = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
               "-user_agent", self.fetcher.user_agent]
        if header_arg:
            cmd += ["-headers", header_arg]
        cmd += ["-i", video.url, "-c", "copy", "-bsf:a", "aac_adtstoasc", path]
        self.progress(name, 0, None)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise DownloadError(f"ffmpeg failed: {proc.stderr.strip()[-500:]}")
        self.progress(name, 1, 1)
        return path

    def _ytdlp(self, video: Video, path: str, name: str) -> str:
        if not have(self.ytdlp):
            raise DownloadError(
                f"{self.ytdlp} not found on PATH (pip install yt-dlp) - this "
                f"source ({video.meta.get('provider', 'external')}) needs it")
        cmd = [self.ytdlp, "--no-playlist", "-o", path]
        if self.cookies_file:
            cmd += ["--cookies", self.cookies_file]
        ua = self.fetcher.user_agent
        if ua:
            cmd += ["--user-agent", ua]
        ffmpeg_bin = find_ffmpeg(self.ffmpeg)
        if ffmpeg_bin:
            cmd += ["--ffmpeg-location", ffmpeg_bin]
        ref = video.headers.get("Referer")
        if ref:
            cmd += ["--referer", ref]
        cmd.append(video.url)
        self.progress(name, 0, None)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise DownloadError(f"yt-dlp failed: {proc.stderr.strip()[-500:]}")
        self.progress(name, 1, 1)
        return path


def _root_page(video: Video, by_url: Dict[str, Video], hops: int = 4) -> str:
    """Walk from a rendition back to the lesson page it came from.

    HLS variants record the master playlist as their page_url, and the master
    records the lesson. Without this, one lesson looks like three separate
    videos and you download the same thing three times.
    """
    page = video.page_url or video.url
    for _ in range(hops):
        parent = by_url.get(page)
        if parent is None or not parent.page_url or parent.page_url == page:
            break
        page = parent.page_url
    return page


def pick_best(videos: Sequence[Video], per: str = "page") -> List[Video]:
    """One video per source page (or per title): the highest resolution that
    is not an audio-only or subtitle rendition."""
    by_url: Dict[str, Video] = {v.url: v for v in videos}
    roots: Dict[str, str] = {v.id: _root_page(v, by_url) for v in videos}
    has_variant = {roots[v.id] for v in videos if v.meta.get("variant")}
    groups: Dict[str, List[Video]] = {}
    for v in videos:
        if v.meta.get("track_type") in ("audio", "subtitles"):
            continue
        if v.meta.get("master") and roots[v.id] in has_variant:
            continue                      # prefer concrete variants over the menu
        key = roots[v.id] if per == "page" else (v.title or v.id)
        groups.setdefault(key or v.id, []).append(v)

    def score(v: Video) -> tuple:
        return (v.height or 0, v.bitrate or 0,
                1 if v.kind == PROGRESSIVE else 0, -len(v.url))

    return [max(g, key=score) for g in groups.values()]
