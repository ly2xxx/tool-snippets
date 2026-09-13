"""HLS (.m3u8) and DASH (.mpd) manifest expansion.

A master playlist is a menu, not a video. Expanding it is what lets you say
"give me 1080p" instead of downloading whatever the player felt like. We keep
the master too - ffmpeg and yt-dlp both prefer it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, List, Optional

from ..http import absolute
from ..models import DASH, EMBED, HLS, MANIFEST, PAGE, PROGRESSIVE, Page, Video
from ..registry import Context, Extractor, register
from ..util import as_int, height_from_url

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(line: str) -> Dict[str, str]:
    out = {}
    for k, v in ATTR_RE.findall(line):
        out[k] = v.strip().strip('"')
    return out


@register
class HLSManifest(Extractor):
    name = "hls"
    priority = 20
    kinds = (MANIFEST, PAGE, EMBED)

    def matches(self, page: Page, ctx: Context) -> bool:
        text = (page.text or "").lstrip()
        if text.startswith("#EXTM3U"):
            return True
        ct = page.content_type
        return ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl",
                      "audio/mpegurl", "audio/x-mpegurl") and "#EXT" in text

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        text = page.text
        base_title = (page.lead.title if page.lead else None)
        lines = [ln.strip() for ln in text.splitlines()]
        is_master = "#EXT-X-STREAM-INF" in text
        encryption = _encryption(lines)

        if is_master:
            # The master itself: what you hand to ffmpeg/yt-dlp for ABR.
            yield Video(url=page.url, kind=HLS, container="m3u8", title=base_title,
                        page_url=page.url, meta={"master": True,
                                                 "encryption": encryption} if encryption
                        else {"master": True})
            pending: Optional[Dict[str, str]] = None
            for ln in lines:
                if ln.startswith("#EXT-X-STREAM-INF:"):
                    pending = parse_attrs(ln.split(":", 1)[1])
                    continue
                if not ln or ln.startswith("#"):
                    if ln.startswith("#EXT-X-MEDIA:"):
                        a = parse_attrs(ln.split(":", 1)[1])
                        uri = a.get("URI")
                        if uri and a.get("TYPE") in ("AUDIO", "SUBTITLES"):
                            yield Video(
                                url=absolute(page.url, uri), kind=HLS,
                                container="m3u8", page_url=page.url,
                                title=base_title,
                                language=a.get("LANGUAGE"),
                                meta={"track_type": (a.get("TYPE") or "").lower(),
                                      "name": a.get("NAME")})
                    continue
                if pending is None:
                    continue
                w = h = None
                res = pending.get("RESOLUTION")
                if res and "x" in res.lower():
                    try:
                        w, h = (int(x) for x in res.lower().split("x", 1))
                    except ValueError:
                        w = h = None
                url = absolute(page.url, ln)
                yield Video(
                    url=url, kind=HLS, container="m3u8", page_url=page.url,
                    title=base_title, width=w, height=h or height_from_url(url),
                    bitrate=as_int(pending.get("AVERAGE-BANDWIDTH")
                                   or pending.get("BANDWIDTH")),
                    codecs=pending.get("CODECS"),
                    meta={"variant": True, "frame_rate": pending.get("FRAME-RATE"),
                          "encryption": encryption} if encryption else
                    {"variant": True, "frame_rate": pending.get("FRAME-RATE")},
                )
                pending = None
            return

        # Media playlist: one rendition, already concrete.
        durations = [float(m.group(1)) for m in
                     re.finditer(r"#EXTINF:\s*([\d.]+)", text)]
        segments = [ln for ln in lines if ln and not ln.startswith("#")]
        meta = {"segments": len(segments), "media_playlist": True}
        if encryption:
            meta["encryption"] = encryption
        yield Video(
            url=page.url, kind=HLS, container="m3u8", page_url=page.url,
            title=base_title, height=height_from_url(page.url),
            duration=round(sum(durations), 2) if durations else None, meta=meta,
        )


def _encryption(lines: List[str]) -> Optional[Dict[str, str]]:
    for ln in lines:
        if ln.startswith("#EXT-X-KEY:") or ln.startswith("#EXT-X-SESSION-KEY:"):
            a = parse_attrs(ln.split(":", 1)[1])
            method = a.get("METHOD", "")
            if method and method != "NONE":
                return {"method": method, "key_uri": a.get("URI", ""),
                        "keyformat": a.get("KEYFORMAT", "identity")}
    return None


@register
class DASHManifest(Extractor):
    name = "dash"
    priority = 21
    kinds = (MANIFEST, PAGE, EMBED)

    def matches(self, page: Page, ctx: Context) -> bool:
        head = (page.text or "").lstrip()[:512]
        return "<MPD" in head or page.content_type == "application/dash+xml"

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        try:
            root = ET.fromstring(page.text)
        except ET.ParseError as e:
            ctx.warn(f"bad MPD at {page.url}: {e}")
            return
        ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}

        def find(el, tag):
            return el.findall(f"m:{tag}", ns) if ns else el.findall(tag)

        title = page.lead.title if page.lead else None
        duration = _mpd_duration(root.get("mediaPresentationDuration"))
        protected = bool(_deep_find(root, "ContentProtection", ns))

        yield Video(url=page.url, kind=DASH, container="mpd", title=title,
                    page_url=page.url, duration=duration,
                    meta={"manifest": True, "drm": protected} if protected
                    else {"manifest": True})

        for period in find(root, "Period") or [root]:
            for aset in find(period, "AdaptationSet"):
                mime = aset.get("mimeType", "")
                for rep in find(aset, "Representation"):
                    rmime = rep.get("mimeType") or mime
                    if rmime and not rmime.startswith(("video", "audio")):
                        continue
                    base = find(rep, "BaseURL")
                    url = absolute(page.url, base[0].text.strip()) if base and base[0].text \
                        else page.url
                    yield Video(
                        url=url,
                        kind=PROGRESSIVE if base else DASH,
                        container=(rmime.split("/")[-1] if rmime else None),
                        title=title, page_url=page.url, duration=duration,
                        width=as_int(rep.get("width")), height=as_int(rep.get("height")),
                        bitrate=as_int(rep.get("bandwidth")), codecs=rep.get("codecs"),
                        language=aset.get("lang"),
                        meta={"representation": rep.get("id"), "mime": rmime,
                              "drm": protected, "mpd": page.url},
                    )


def _deep_find(root, tag: str, ns) -> List:
    want = f"{{{ns['m']}}}{tag}" if ns else tag
    return [e for e in root.iter() if e.tag == want or e.tag.endswith("}" + tag)]


def _mpd_duration(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    m = re.match(r"^P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$", value, re.I)
    if not m:
        return None
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    total = h * 3600 + mi * 60 + s
    return total or None
