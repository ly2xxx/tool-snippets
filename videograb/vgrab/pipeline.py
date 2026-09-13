"""The crawl/resolve loop.

Breadth-first over Leads. Each fetched page is shown to every matching
extractor; whatever they emit is either a Video (kept, deduped) or another
Lead (queued, if it's in scope and we haven't been there).

Scope rules matter more than they look. Page leads stay on the site you
pointed at - otherwise one "Related courses" sidebar turns a course rip into
a crawl of the whole internet. Embed/manifest leads are exempt, because the
actual media always lives on a different host (CDN, player provider).
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Callable, Dict, Iterable, Optional, Sequence, Set

from .http import Fetcher, FetchError, host_of
from .models import (API, DASH, EMBED, HLS, MANIFEST, PAGE, Lead, Page,
                     Result, Video)
from .registry import Context, Extractor, build

Event = Callable[[str, str], None]


def registrable(host: str) -> str:
    """Rough eTLD+1. Good enough to tell business.whizlabs.com from evil.com
    while still treating cdn.whizlabs.com as the same property."""
    host = (host or "").lower().lstrip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle the common multi-part public suffixes without shipping a PSL.
    two = ".".join(parts[-2:])
    if two in {"co.uk", "com.au", "co.in", "com.br", "co.jp", "co.nz", "com.sg",
               "co.za", "com.mx", "github.io"}:
        return ".".join(parts[-3:])
    return two


class Pipeline:
    def __init__(
        self,
        fetcher: Optional[Fetcher] = None,
        extractors: Optional[Sequence[Extractor]] = None,
        max_depth: int = 2,
        max_pages: int = 200,
        max_videos: int = 0,
        expand_manifests: bool = True,
        scope: str = "site",             # site | host | any
        allow_hosts: Sequence[str] = (),
        deny_patterns: Sequence[str] = (),
        options: Optional[Dict] = None,
        on_event: Optional[Event] = None,
    ):
        self.fetcher = fetcher or Fetcher()
        self.extractors = list(extractors) if extractors is not None else build()
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.max_videos = max_videos
        self.expand_manifests = expand_manifests
        self.scope = scope
        self.allow_hosts = {h.lower() for h in allow_hosts}
        self.deny = [re.compile(p, re.I) for p in deny_patterns]
        self.on_event = on_event or (lambda level, msg: None)
        self.ctx = Context(self.fetcher, options or {}, self.on_event)

    # -- scope -------------------------------------------------------------
    def in_scope(self, lead: Lead, root: str) -> bool:
        for pat in self.deny:
            if pat.search(lead.url):
                return False
        if lead.kind in (EMBED, MANIFEST, API):
            return True                      # media lives off-site by design
        host = host_of(lead.url)
        if host in self.allow_hosts or self.scope == "any":
            return True
        root_host = host_of(root)
        if self.scope == "host":
            return host == root_host
        return registrable(host) == registrable(root_host)

    # -- run ---------------------------------------------------------------
    def run(self, url: str, kind: str = PAGE) -> Result:
        result = Result(root=url)
        queue: deque = deque([Lead(url=url, kind=kind, depth=0)])
        seen_leads: Set[str] = {_key(url, kind)}
        seen_videos: Set[str] = set()
        by_id: Dict[str, Video] = {}
        pages_done = 0
        started = time.time()

        while queue:
            if pages_done >= self.max_pages:
                result.notes.append(
                    f"stopped: max_pages={self.max_pages} reached")
                break
            lead = queue.popleft()
            try:
                page = self.fetcher.fetch(lead.url, lead=lead)
            except FetchError as e:
                result.errors.append({"url": lead.url, "error": str(e)})
                self.on_event("warn", f"fetch failed: {e}")
                continue
            pages_done += 1
            result.visited.append(page.url)
            self.on_event("debug",
                          f"[{page.status}] d{lead.depth} {lead.kind} {page.url}")
            if page.status >= 400:
                result.errors.append(
                    {"url": page.url, "error": f"HTTP {page.status}"})
                if page.status in (401, 403):
                    result.notes.append(
                        f"HTTP {page.status} on {page.url} - authentication "
                        "may be required (see --cookies / --browser)")
                continue

            for item in self._scan(page, result):
                if isinstance(item, Video):
                    vid = item.id
                    if vid in seen_videos:
                        # Same media found twice by different routes. Keep the
                        # first entry but fold in whatever the second knew that
                        # the first did not (duration, AES-128 key, title...).
                        _merge_into(by_id[vid], item)
                        continue
                    seen_videos.add(vid)
                    by_id[vid] = item
                    item.page_url = item.page_url or page.url
                    result.videos.append(item)
                    self.on_event("info", f"found {item.kind}: {item.url}")
                    follow = self._manifest_lead(item, lead)
                    if follow is not None:
                        key = _key(follow.url, follow.kind)
                        if key not in seen_leads:
                            seen_leads.add(key)
                            queue.append(follow)
                    if self.max_videos and len(result.videos) >= self.max_videos:
                        result.notes.append(
                            f"stopped: max_videos={self.max_videos} reached")
                        queue.clear()
                        break
                elif isinstance(item, Lead):
                    key = _key(item.url, item.kind)
                    if key in seen_leads:
                        continue
                    if item.depth > self.max_depth:
                        continue
                    if not self.in_scope(item, url):
                        self.on_event("debug", f"out of scope: {item.url}")
                        continue
                    seen_leads.add(key)
                    queue.append(item)

        result.notes.append(
            f"{pages_done} request(s) in {time.time() - started:.1f}s")
        return result

    def _manifest_lead(self, video: Video, parent: Lead) -> Optional[Lead]:
        """A bare .m3u8/.mpd is a menu; queue it so we can read the menu.

        Already-expanded entries (a master we just parsed, one of its
        variants, a media playlist) carry a marker and are left alone -
        that is what stops the expansion from chasing its own tail.
        """
        if not self.expand_manifests or video.kind not in (HLS, DASH):
            return None
        if any(video.meta.get(k) for k in ("master", "variant", "media_playlist",
                                           "manifest", "representation")):
            return None
        if parent.depth + 1 > self.max_depth + 2:   # manifests get extra rope
            return None
        return Lead(url=video.url, kind=MANIFEST, title=video.title,
                    depth=parent.depth + 1, headers=dict(video.headers),
                    via="manifest-expand")

    def _scan(self, page: Page, result: Result) -> Iterable[object]:
        lead_kind = page.lead.kind if page.lead else PAGE
        for ex in self.extractors:
            if ex.kinds and lead_kind not in ex.kinds and "*" not in ex.kinds:
                continue
            try:
                if not ex.matches(page, self.ctx):
                    continue
            except Exception as e:
                result.errors.append(
                    {"url": page.url, "error": f"{ex.name}.matches: {e}"})
                continue
            try:
                produced = list(ex.extract(page, self.ctx) or [])
            except Exception as e:
                result.errors.append(
                    {"url": page.url, "error": f"{ex.name}.extract: {e}"})
                self.on_event("warn", f"{ex.name} failed on {page.url}: {e}")
                continue
            for item in produced:
                if isinstance(item, Video):
                    item.source = item.source or ex.name
                    if not item.headers.get("Referer"):
                        item.headers["Referer"] = page.url
                elif isinstance(item, Lead):
                    item.via = item.via or ex.name
                yield item


def _merge_into(keep: Video, extra: Video) -> None:
    """Fill blanks on `keep` from `extra`; never overwrite what we already had."""
    for field in ("title", "container", "width", "height", "bitrate", "duration",
                  "codecs", "language", "filesize"):
        if getattr(keep, field, None) in (None, "") and getattr(extra, field, None):
            setattr(keep, field, getattr(extra, field))
    for k, v in (extra.meta or {}).items():
        keep.meta.setdefault(k, v)
    for k, v in (extra.headers or {}).items():
        keep.headers.setdefault(k, v)
    if extra.source and extra.source != keep.source:
        also = keep.meta.setdefault("also_found_by", [])
        if extra.source not in also:
            also.append(extra.source)


def _key(url: str, kind: str) -> str:
    # Trailing-slash and fragment differences are not different pages.
    u = url.split("#", 1)[0]
    if u.endswith("/") and len(u) > 9:
        u = u[:-1]
    return f"{kind}|{u}"


def extract(url: str, fetcher: Optional[Fetcher] = None, **kw) -> Result:
    """One-call convenience wrapper around Pipeline."""
    return Pipeline(fetcher=fetcher, **kw).run(url)
