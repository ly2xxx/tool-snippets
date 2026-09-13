"""Generic HTML/JSON sweep - the extractor that handles most of the web.

Order of attack, cheapest and most reliable first:

  1. <video>/<source>/<track>   - explicit, always trustworthy
  2. og:video / twitter:player  - the site telling us on purpose
  3. JSON-LD VideoObject        - ditto, structured
  4. inline JSON/player configs - jwplayer setup, __NEXT_DATA__, __INITIAL_STATE__
  5. raw regex over the markup  - last resort, catches hand-rolled players
  6. iframes/embeds             - queued as leads, resolved on the next hop

Anything found gets a Referer header attached (see pipeline), because CDNs
that hand out signed URLs routinely 403 a request without one.
"""

from __future__ import annotations

import re
from typing import Iterator, Optional

from ..http import absolute
from ..models import API, EMBED, PAGE, Page, Video
from ..registry import Context, Extractor, register
from ..util import (DURATION_KEYS, TITLE_KEYS, as_int, classify, height_from_url,
                    iter_text_urls, json_blobs, json_media_urls, looks_like_media,
                    pick, unescape_url)

# Hosts whose iframes are players worth following.
EMBED_HINTS = (
    "player", "embed", "video", "stream", "media", "vimeo", "youtube", "youtu.be",
    "wistia", "jwplayer", "jwplat", "brightcove", "kaltura", "vidyard", "loom",
    "dailymotion", "vzaar", "sproutvideo", "panopto", "videodelivery",
    "cloudflarestream", "mediadelivery", "mux.com", "bunnycdn", "vdocipher",
    "spotlightr", "learnyst", "vidstack", "flowplayer", "hlsplayer",
)
# Never follow these as embeds.
EMBED_NOISE = re.compile(
    r"(googletagmanager|google-analytics|doubleclick|facebook\.com/(tr|plugins)|"
    r"recaptcha|hotjar|intercom|zendesk|disqus|twitter\.com/widgets|adservice)", re.I)


@register
class GenericHTML(Extractor):
    name = "generic"
    priority = 60
    kinds = (PAGE, EMBED)

    def matches(self, page: Page, ctx: Context) -> bool:
        if page.is_html:
            return True
        # Some players are served as text/plain or application/javascript.
        head = (page.text or "")[:2048].lstrip().lower()
        return head.startswith(("<!doctype html", "<html")) or "<video" in head

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        doc = page.dom
        page_title = _page_title(page)
        seen: set = set()

        def emit(url: str, **kw) -> Optional[Video]:
            url = absolute(page.url, unescape_url(url))
            if not url or url in seen:
                return None
            kind, container = classify(url)
            if kind is None:
                return None
            seen.add(url)
            kw.setdefault("title", page_title)
            kw.setdefault("height", height_from_url(url)
                          or _height_from_meta(kw.get("meta")))
            return Video(url=url, kind=kind, container=container,
                         page_url=page.url, **kw)

        # 1. explicit media elements ---------------------------------------
        for el in doc.select("video, source, audio"):
            for attr in ("src", "data-src", "data-setup-src"):
                v = el.get(attr)
                if v:
                    got = emit(v, meta=_el_meta(el))
                    if got:
                        yield got
            poster = el.get("poster")
            if poster:
                ctx.debug(f"poster: {absolute(page.url, poster)}")

        subs = [absolute(page.url, t.get("src") or "")
                for t in doc.select("track") if t.get("src")]

        # 2. social/meta tags ----------------------------------------------
        for prop in ("og:video:secure_url", "og:video:url", "og:video",
                     "twitter:player:stream", "video_src"):
            v = doc.meta(prop)
            if v:
                got = emit(v, meta={"via": prop})
                if got:
                    yield got

        # 3. JSON-LD --------------------------------------------------------
        for body in doc.script_texts("application/ld+json"):
            for blob in json_blobs(body):
                for obj in _iter_videoobjects(blob):
                    title = obj.get("name") or page_title
                    dur = _iso_duration(obj.get("duration"))
                    for key in ("contentUrl", "contentURL"):
                        if obj.get(key):
                            got = emit(obj[key], title=title, duration=dur,
                                       meta={"via": "json-ld"})
                            if got:
                                yield got
                    embed = obj.get("embedUrl") or obj.get("embedURL")
                    if embed and not looks_like_media(embed):
                        yield ctx.lead(page, embed, EMBED, title=title)

        # 4. inline JSON / player configs ------------------------------------
        markers = ctx.opt("json_markers") or ()
        for body in doc.script_texts():
            if len(body) > 4_000_000:
                continue
            for blob in json_blobs(body, markers):
                for url, path, sib in json_media_urls(blob):
                    got = emit(
                        url,
                        title=_str_or_none(pick(sib, TITLE_KEYS)) or page_title,
                        height=as_int(pick(sib, ("height", "res", "resolution")))
                        or height_from_url(url),
                        bitrate=_bitrate(sib),
                        duration=_seconds(pick(sib, DURATION_KEYS)),
                        language=_str_or_none(pick(sib, ("language", "lang", "locale"))),
                        meta={"json_path": path},
                    )
                    if got:
                        yield got

        # 5. raw sweep ------------------------------------------------------
        for raw in iter_text_urls(page.text):
            if looks_like_media(raw):
                got = emit(raw, meta={"via": "regex"})
                if got:
                    yield got

        # ...including attribute values that never made it into a <video>.
        for el in doc.walk():
            for v in el.urls():
                if looks_like_media(v):
                    got = emit(v, title=_nearby_title(el) or page_title,
                               meta=_el_meta(el))
                    if got:
                        yield got

        # 6. embeds ----------------------------------------------------------
        for el in doc.select("iframe, embed, object"):
            src = el.get("src") or el.get("data-src") or el.get("data")
            if not src:
                continue
            url = absolute(page.url, unescape_url(src))
            if not url.startswith("http") or EMBED_NOISE.search(url):
                continue
            low = url.lower()
            if any(h in low for h in EMBED_HINTS) or ctx.opt("all_iframes"):
                yield ctx.lead(page, url, EMBED,
                               title=el.get("title") or _nearby_title(el) or page_title)

        if subs:
            ctx.debug(f"subtitle tracks: {', '.join(subs)}")


@register
class JSONResponse(Extractor):
    """Scans JSON API responses (and .json pages) the same way."""

    name = "json"
    priority = 61
    kinds = (API, PAGE, EMBED)

    def matches(self, page: Page, ctx: Context) -> bool:
        ct = page.content_type
        if "json" in ct:
            return True
        head = (page.text or "").lstrip()[:1]
        return head in "{[" and not page.is_html

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        for blob in json_blobs(page.text):
            for url, path, sib in json_media_urls(blob):
                full = absolute(page.url, unescape_url(url))
                kind, container = classify(full)
                if kind is None:
                    continue
                yield Video(
                    url=full, kind=kind, container=container, page_url=page.url,
                    title=_str_or_none(pick(sib, TITLE_KEYS)),
                    height=as_int(pick(sib, ("height", "res"))) or height_from_url(full),
                    bitrate=_bitrate(sib),
                    duration=_seconds(pick(sib, DURATION_KEYS)),
                    meta={"json_path": path},
                )


# ---------------------------------------------------------------------------

def _iter_videoobjects(blob) -> Iterator[dict]:
    stack = [blob]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            t = node.get("@type") or node.get("type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() in ("videoobject", "video", "clip", "movie")
                   for x in types if x):
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _iso_duration(value) -> Optional[float]:
    """ISO-8601 PT1H2M3S -> seconds."""
    if not isinstance(value, str):
        return _seconds(value)
    m = re.match(r"^P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$", value.strip(), re.I)
    if not m or not any(m.groups()):
        return _seconds(value)
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def _seconds(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # Player configs are inconsistent about ms vs s.
        return v / 1000.0 if v > 86400 * 2 else v
    if isinstance(value, str):
        if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", value.strip()):
            parts = [float(p) for p in value.strip().split(":")]
            while len(parts) < 3:
                parts.insert(0, 0.0)
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        try:
            return _seconds(float(value))
        except ValueError:
            return None
    return None


def _bitrate(sib) -> Optional[int]:
    v = pick(sib, ("bitrate", "bandwidth", "bit_rate", "br", "averagebitrate"))
    n = as_int(v)
    if n is None:
        return None
    return n * 1000 if n < 10000 else n     # kbps -> bps


def _str_or_none(v) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()[:300]
    return None


QUALITY_RE = re.compile(r"(\d{3,4})\s*p?\b", re.I)


def _height_from_meta(meta) -> Optional[int]:
    """<source label="720p">, data-quality="1080", ... - the height the page
    states even when the URL keeps it to itself."""
    if not isinstance(meta, dict):
        return None
    for key in ("label", "data-label", "data-quality", "res", "size", "quality"):
        val = meta.get(key)
        if isinstance(val, str):
            m = QUALITY_RE.search(val)
            if m:
                n = int(m.group(1))
                if 100 <= n <= 4320:
                    return n
    return None


def _el_meta(el) -> dict:
    keep = {}
    for k, v in el.attrs.items():
        if k in ("label", "type", "size", "res", "data-quality", "data-label",
                 "data-title", "title", "data-lesson", "data-id", "id"):
            keep[k] = v
    return keep


def _nearby_title(el) -> Optional[str]:
    """Walk up a few levels looking for a heading or a title-ish attribute."""
    node = el
    for _ in range(4):
        if node is None:
            break
        for attr in ("title", "data-title", "aria-label", "alt"):
            v = node.get(attr) if hasattr(node, "get") else None
            if v and v.strip():
                return v.strip()[:300]
        node = node.parent
    node = el.parent
    for _ in range(3):
        if node is None:
            break
        for h in ("h1", "h2", "h3", "h4"):
            found = node.select_one(h)
            if found and found.text:
                return found.text[:300]
        node = node.parent
    return None


GENERIC_TITLES = {"player", "video", "embed", "video player", "untitled",
                  "loading", "media", "iframe"}


def _page_title(page: Page) -> Optional[str]:
    """Best human label for media found on this page.

    For an embed, the page that linked to it knows the lesson name; the embed
    itself is usually called "Player". So the inherited lead title wins there.
    """
    doc = page.dom
    lead_title = page.lead.title if page.lead else None
    own = doc.meta("og:title") or doc.title
    if page.lead and page.lead.kind == EMBED and lead_title:
        return lead_title.strip()[:300]
    if own and own.strip().lower() in GENERIC_TITLES and lead_title:
        return lead_title.strip()[:300]
    for cand in (own, lead_title):
        if cand and cand.strip():
            return cand.strip()[:300]
    return None
