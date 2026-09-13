"""Shared sniffing helpers used by most extractors."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .models import DASH, EXTERNAL, HLS, PROGRESSIVE

# Containers we treat as directly downloadable.
PROGRESSIVE_EXTS = (
    "mp4", "m4v", "webm", "mov", "mkv", "flv", "avi", "ogv", "ogg", "3gp",
    "mpg", "mpeg", "wmv", "m4a", "mp3", "aac", "wav", "opus", "ts",
)
ALL_EXTS = PROGRESSIVE_EXTS + ("m3u8", "m3u", "mpd", "ism", "f4m")

_EXT_RE = re.compile(
    r"\.(" + "|".join(ALL_EXTS) + r")(?=$|[?#/])", re.I)

# Quoted URL-ish strings inside scripts/JSON/attributes.
_URL_IN_TEXT = re.compile(
    r"""["'(]\s*((?:https?:)?//[^"'()\s\\]{4,600}|/[A-Za-z0-9_\-./%]{4,600})\s*["')]""")
# Escaped JSON (\/ separators) - very common in embedded player configs.
_ESCAPED = re.compile(r"https?:\\/\\/[^\"'\\\s]{4,600}")

# JSON keys that conventionally hold a media URL.
MEDIA_KEYS = {
    "file", "src", "url", "source", "video", "video_url", "videourl",
    "videosrc", "hls", "hls_url", "hlsurl", "m3u8", "mpd", "dash", "dash_url",
    "playbackurl", "playback_url", "stream", "stream_url", "streamurl",
    "manifest", "manifesturl", "manifest_url", "contenturl", "content_url",
    "mediaurl", "media_url", "embedurl", "embed_url", "signedurl",
    "signed_url", "downloadurl", "download_url", "progressive", "mp4",
    "master", "masterplaylist", "player_url", "asseturl", "asset_url",
}
TITLE_KEYS = ("title", "name", "lesson_title", "displayname", "display_name",
              "heading", "label", "chapter_title")
DURATION_KEYS = ("duration", "duration_seconds", "durationinseconds",
                 "length", "video_duration", "total_duration")


def has_media_ext(url: str) -> bool:
    return bool(_EXT_RE.search(url.split("#")[0]))


def media_ext(url: str) -> Optional[str]:
    m = _EXT_RE.search(url.split("#")[0])
    return m.group(1).lower() if m else None


def classify(url: str) -> Tuple[Optional[str], Optional[str]]:
    """(kind, container) for a media URL, or (None, None) if it isn't one."""
    ext = media_ext(url)
    if ext in ("m3u8", "m3u"):
        return HLS, "m3u8"
    if ext == "mpd":
        return DASH, "mpd"
    if ext in ("ism", "f4m"):
        return EXTERNAL, ext
    if ext:
        return PROGRESSIVE, ext
    # Extension-less CDN URLs still identify themselves often enough, but the
    # signal has to be specific: a lesson page at /learn/lesson/hls/2 is not
    # an HLS manifest, and treating it as one poisons the whole crawl.
    low = url.lower()
    if "m3u8" in low or re.search(r"/hls/[^?#]*(master|index|playlist|chunklist|manifest)", low):
        return HLS, "m3u8"
    if re.search(r"\.mpd(?=$|[?#])|/dash/[^?#]*manifest|manifest[^?#]*\.mpd", low):
        return DASH, "mpd"
    if re.search(r"\.ism/manifest", low):
        return EXTERNAL, "ism"
    return None, None


def looks_like_media(url: str) -> bool:
    return classify(url)[0] is not None


def iter_text_urls(text: str) -> Iterator[str]:
    """Every URL-shaped token in a blob of script/markup."""
    if not text:
        return
    for m in _URL_IN_TEXT.finditer(text):
        yield m.group(1)
    for m in _ESCAPED.finditer(text):
        yield m.group(0).replace("\\/", "/")
    # Bare (unquoted) absolute media URLs, e.g. inside comments or HLS bodies.
    for m in re.finditer(r"https?://[^\s\"'<>\\)]{6,600}", text):
        yield m.group(0)


def unescape_url(u: str) -> str:
    return (u or "").replace("\\/", "/").replace("&amp;", "&").strip()


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def json_blobs(text: str, markers: Iterable[str] = ()) -> Iterator[Any]:
    """Yield parsed JSON objects found in a script body.

    Tries the whole body first (handles <script type="application/json">),
    then any `marker = {...}` assignments (window.__INITIAL_STATE__,
    __NEXT_DATA__, jwplayer().setup({...}), ...).
    """
    text = (text or "").strip()
    if not text:
        return
    for candidate in (text, text.rstrip(";")):
        try:
            yield json.loads(candidate)
            return
        except Exception:
            pass
    pats: List[str] = []
    for mk in markers:
        pats.append(re.escape(mk) + r"\s*[:=]\s*")
    if not pats:
        pats = [r"window\.[A-Za-z_$][\w$.]*\s*=\s*",
                r"var\s+[A-Za-z_$][\w$]*\s*=\s*",
                r"setup\s*\(\s*", r"\bconfig\s*[:=]\s*"]
    for pat in pats:
        for m in re.finditer(pat, text):
            obj = _balanced(text, m.end())
            if obj is None:
                continue
            try:
                yield json.loads(obj)
            except Exception:
                try:
                    yield json.loads(_loosen(obj))
                except Exception:
                    continue


def _balanced(text: str, start: int) -> Optional[str]:
    """Slice the balanced {...} / [...] beginning at or after `start`."""
    n = len(text)
    while start < n and text[start] in " \t\r\n":
        start += 1
    if start >= n or text[start] not in "{[":
        return None
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str: Optional[str] = None
    esc = False
    for i in range(start, min(n, start + 2_000_000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _loosen(js: str) -> str:
    """Make a JS object literal JSON-ish: quote bare keys, drop trailing commas,
    convert single-quoted strings. Best effort - failures are caught upstream."""
    out = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', js)
    out = re.sub(r",\s*([}\]])", r"\1", out)
    out = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", lambda m: json.dumps(m.group(1)), out)
    return out


def walk_json(obj: Any, path: str = "") -> Iterator[Tuple[str, str, Any]]:
    """Yield (path, key, value) for every scalar in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, (dict, list)):
                yield from walk_json(v, p)
            else:
                yield p, str(k), v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{path}[{i}]"
            if isinstance(v, (dict, list)):
                yield from walk_json(v, p)
            else:
                yield p, "", v


def json_media_urls(obj: Any) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Find media URLs anywhere in a decoded JSON structure.

    Yields (url, json_path, sibling_context) so callers can pick up the title
    and quality that sit next to the URL.
    """
    for path, key, value in walk_json(obj):
        if not isinstance(value, str) or len(value) < 5:
            continue
        v = unescape_url(value)
        if not (v.startswith(("http://", "https://", "//")) or v.startswith("/")):
            continue
        if looks_like_media(v) or key.lower() in MEDIA_KEYS:
            if not looks_like_media(v) and not v.lower().startswith(("http", "//", "/")):
                continue
            yield v, path, _context_for(obj, path)


def _context_for(root: Any, path: str) -> Dict[str, Any]:
    """The dict that directly contains `path`, for sibling metadata."""
    node = root
    parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    parents: List[Any] = []
    for part in parts:
        parents.append(node)
        try:
            if part.startswith("["):
                node = node[int(part[1:-1])]
            else:
                node = node[part]
        except Exception:
            break
    # Merge outermost -> innermost so the nearest dict wins, but a title or
    # duration sitting on the parent playlist entry is still visible.
    merged: Dict[str, Any] = {}
    for cand in parents:
        if isinstance(cand, dict):
            merged.update({k: v for k, v in cand.items()
                           if not isinstance(v, (dict, list))})
    return merged


def pick(ctx: Dict[str, Any], keys: Iterable[str]) -> Optional[Any]:
    low = {str(k).lower(): v for k, v in ctx.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def as_int(value: Any) -> Optional[int]:
    try:
        if isinstance(value, str):
            value = re.sub(r"[^\d.]", "", value) or None
        return int(float(value)) if value is not None else None
    except Exception:
        return None


def height_from_url(url: str) -> Optional[int]:
    """CDN paths usually leak the rendition: .../720p/index.m3u8, _1080.mp4"""
    m = re.search(r"(?<![\d])(\d{3,4})p(?![a-z\d])", url, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"[_\-/](240|360|480|540|576|720|1080|1440|2160)(?=[._\-/]|$)", url)
    if m:
        return int(m.group(1))
    return None
