"""Core data types shared by every extractor.

The whole tool is a breadth-first search over two node types:

    Lead   - something worth fetching/looking at next (a page, an embed,
             an API endpoint, a manifest).
    Video  - a concrete, playable media URL we managed to pin down.

Extractors turn a fetched page into zero or more of each. That single
convention is what makes the tool extensible: a new site only has to know
how to emit Leads and Videos, never how to crawl, dedupe or download.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# Lead kinds. Kept as plain strings so out-of-tree extractors can invent
# their own without touching this file; the pipeline only special-cases
# the ones it knows and treats anything else as "fetch and re-scan".
PAGE = "page"          # an HTML page to scan with every matching extractor
EMBED = "embed"        # a player iframe / embed URL
MANIFEST = "manifest"  # an HLS .m3u8 or DASH .mpd to expand into renditions
API = "api"            # a JSON endpoint an extractor asked us to fetch

# Video kinds.
PROGRESSIVE = "progressive"  # a plain .mp4/.webm/... byte range downloadable file
HLS = "hls"                  # an HLS media or master playlist
DASH = "dash"                # a DASH MPD
EXTERNAL = "external"        # needs a specialist tool (yt-dlp) to resolve


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", {}, [])}


@dataclass
class Lead:
    """A URL the pipeline should visit next."""

    url: str
    kind: str = PAGE
    title: Optional[str] = None
    via: Optional[str] = None          # name of the extractor that produced it
    depth: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

    def child(self, url: str, kind: str = PAGE, **kw) -> "Lead":
        """Derive a deeper lead, inheriting headers by default."""
        headers = dict(self.headers)
        headers.update(kw.pop("headers", {}) or {})
        return Lead(url=url, kind=kind, depth=self.depth + 1, headers=headers, **kw)


@dataclass
class Video:
    """A concrete media URL plus everything needed to fetch it."""

    url: str
    kind: str = PROGRESSIVE
    title: Optional[str] = None
    source: Optional[str] = None       # extractor that found it
    page_url: Optional[str] = None     # where it was found
    container: Optional[str] = None    # mp4, webm, ts, m4s...
    width: Optional[int] = None
    height: Optional[int] = None
    bitrate: Optional[int] = None      # bits per second
    duration: Optional[float] = None   # seconds
    codecs: Optional[str] = None
    language: Optional[str] = None
    filesize: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)   # required on download
    # Free-form: DRM hints, lesson ids, poster art, subtitle tracks, ...
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Stable short id, used for dedupe and default filenames."""
        basis = f"{self.url}|{self.kind}|{self.height or ''}|{self.bitrate or ''}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    @property
    def label(self) -> str:
        bits = []
        if self.height:
            bits.append(f"{self.height}p")
        if self.bitrate:
            bits.append(f"{round(self.bitrate / 1000)}kbps")
        if self.container:
            bits.append(self.container)
        return " ".join(bits) or self.kind

    @property
    def needs_ytdlp(self) -> bool:
        return self.kind == EXTERNAL

    def to_dict(self) -> Dict[str, Any]:
        d = _clean(asdict(self))
        d["id"] = self.id
        d["label"] = self.label
        return d


@dataclass
class Page:
    """A fetched resource handed to extractors.

    `text` is decoded lazily-ish by the fetcher; binary bodies get an empty
    string so extractors can stay naive about content types.
    """

    url: str                      # final URL after redirects
    requested_url: str
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    text: str = ""
    lead: Optional[Lead] = None
    body: bytes = b""

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        ct = self.content_type
        return ct.startswith("text/html") or ct.startswith("application/xhtml")

    @property
    def depth(self) -> int:
        return self.lead.depth if self.lead else 0

    _dom: Any = field(default=None, repr=False, compare=False)

    @property
    def dom(self):
        """Parsed mini-DOM (cached). Only meaningful for markup responses."""
        if self._dom is None:
            from .htmlmini import parse

            self._dom = parse(self.text)
        return self._dom


@dataclass
class Result:
    """Everything one run produced."""

    root: str
    videos: List[Video] = field(default_factory=list)
    visited: List[str] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "count": len(self.videos),
            "videos": [v.to_dict() for v in self.videos],
            "visited": self.visited,
            "errors": self.errors,
            "notes": self.notes,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
