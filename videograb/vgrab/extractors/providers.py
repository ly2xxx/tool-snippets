"""Known video hosts.

Most embeds need no special code: the pipeline fetches the player page and
the generic extractor finds the HLS URL in its config. This module exists for
the cases where that is not true:

  * the media URL is derivable from the embed URL (Cloudflare, Bunny, Mux)
  * there is a documented JSON endpoint worth calling (Vimeo, Wistia, JW)
  * the platform is hostile to scraping and yt-dlp already solved it (YouTube)

Each provider is a small class, so adding one is ~15 lines.
"""

from __future__ import annotations

import re
from typing import Iterator, Pattern, Tuple

from ..http import host_of
from ..models import API, EMBED, EXTERNAL, HLS, PAGE, PROGRESSIVE, Page, Video
from ..registry import Context, Extractor, register


class ProviderExtractor(Extractor):
    """Base: match on the URL, emit videos/leads. No page parsing needed."""

    patterns: Tuple[Pattern, ...] = ()
    kinds = (EMBED, PAGE)
    priority = 30

    def match_url(self, url: str):
        for p in self.patterns:
            m = p.search(url)
            if m:
                return m
        return None

    def matches(self, page: Page, ctx: Context) -> bool:
        return self.match_url(page.url) is not None or \
            self.match_url(page.requested_url) is not None

    def _m(self, page: Page):
        return self.match_url(page.url) or self.match_url(page.requested_url)


@register
class YouTube(ProviderExtractor):
    """YouTube signatures change constantly; delegate to yt-dlp rather than
    pretend. We still record the canonical watch URL and title."""

    name = "youtube"
    patterns = (
        re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/(?:embed|v|shorts)/([\w-]{6,})"),
        re.compile(r"youtube\.com/watch\?(?:[^&]*&)*v=([\w-]{6,})"),
        re.compile(r"youtu\.be/([\w-]{6,})"),
    )

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        vid = self._m(page).group(1)
        title = (page.lead.title if page.lead else None) or page.dom.title
        yield Video(
            url=f"https://www.youtube.com/watch?v={vid}", kind=EXTERNAL,
            title=title, page_url=page.url,
            meta={"provider": "youtube", "video_id": vid, "resolver": "yt-dlp"},
        )


@register
class GenericExternal(ProviderExtractor):
    """Platforms with heavy client-side signing - hand them to yt-dlp too."""

    name = "external-platforms"
    patterns = (
        re.compile(r"(?:www\.)?dailymotion\.com/(?:embed/)?video/(\w+)"),
        re.compile(r"player\.twitch\.tv/\?video=v?(\d+)"),
        re.compile(r"(?:www\.)?facebook\.com/.+/videos/(\d+)"),
        re.compile(r"(?:www\.)?loom\.com/(?:share|embed)/(\w{16,})"),
        re.compile(r"(?:www\.)?tiktok\.com/@[^/]+/video/(\d+)"),
        re.compile(r"(?:www\.)?bilibili\.com/video/(\w+)"),
    )

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        yield Video(url=page.url, kind=EXTERNAL, page_url=page.url,
                    title=(page.lead.title if page.lead else None) or page.dom.title,
                    meta={"provider": host_of(page.url), "resolver": "yt-dlp"})


@register
class Vimeo(ProviderExtractor):
    name = "vimeo"
    patterns = (
        re.compile(r"player\.vimeo\.com/video/(\d+)"),
        re.compile(r"(?:www\.)?vimeo\.com/(?:channels/[\w]+/)?(\d+)"),
    )

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        vid = self._m(page).group(1)
        # The player config carries both progressive MP4s and the HLS master.
        cfg = f"https://player.vimeo.com/video/{vid}/config"
        referer = page.lead.headers.get("Referer") if page.lead else None
        try:
            data = ctx.fetcher.fetch_json(
                cfg, headers={"Referer": referer or page.url})
        except Exception as e:
            ctx.debug(f"vimeo config unavailable ({e}); falling back to yt-dlp")
            yield Video(url=f"https://vimeo.com/{vid}", kind=EXTERNAL,
                        page_url=page.url,
                        meta={"provider": "vimeo", "resolver": "yt-dlp"})
            return
        title = (((data.get("video") or {}).get("title"))
                 or (page.lead.title if page.lead else None))
        duration = (data.get("video") or {}).get("duration")
        files = ((data.get("request") or {}).get("files") or {})
        for f in files.get("progressive") or []:
            yield Video(url=f.get("url", ""), kind=PROGRESSIVE, container="mp4",
                        title=title, page_url=page.url, duration=duration,
                        width=f.get("width"), height=f.get("height"),
                        meta={"provider": "vimeo", "quality": f.get("quality")})
        for key in ("hls", "dash"):
            cdns = ((files.get(key) or {}).get("cdns") or {})
            for cdn, info in cdns.items():
                url = info.get("url") or info.get("avc_url")
                if url:
                    yield Video(url=url, kind=HLS if key == "hls" else "dash",
                                title=title, page_url=page.url, duration=duration,
                                meta={"provider": "vimeo", "cdn": cdn})


@register
class Wistia(ProviderExtractor):
    name = "wistia"
    patterns = (
        re.compile(r"(?:fast\.)?wistia\.(?:net|com)/(?:embed|medias)/(?:medias/|iframe/)?(\w{8,})"),
        re.compile(r"wistia_async_(\w{8,})"),
    )

    def matches(self, page: Page, ctx: Context) -> bool:
        return super().matches(page, ctx) or bool(
            re.search(r"wistia_async_\w{8,}", page.text or ""))

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        m = self._m(page) or re.search(r"wistia_async_(\w{8,})", page.text or "")
        if not m:
            return
        mid = m.group(1)
        try:
            data = ctx.fetcher.fetch_json(
                f"https://fast.wistia.net/embed/medias/{mid}.json")
        except Exception as e:
            ctx.debug(f"wistia json failed: {e}")
            return
        media = (data.get("media") or {})
        title = media.get("name") or (page.lead.title if page.lead else None)
        for a in media.get("assets") or []:
            url = a.get("url") or ""
            if not url:
                continue
            url = url.replace(".bin", f"/{mid}.mp4") if url.endswith(".bin") else url
            kind = HLS if url.endswith(".m3u8") else PROGRESSIVE
            yield Video(url=url, kind=kind, title=title, page_url=page.url,
                        width=a.get("width"), height=a.get("height"),
                        bitrate=(a.get("bitrate") or 0) * 1000 or None,
                        filesize=a.get("size"), duration=media.get("duration"),
                        container=a.get("container"),
                        meta={"provider": "wistia", "asset": a.get("type")})


@register
class JWPlatform(ProviderExtractor):
    name = "jwplayer"
    patterns = (
        re.compile(r"(?:cdn|content)\.jwplayer\.com/(?:players|videos|v2/media)/(\w{8,})"),
        re.compile(r"jwpsrv\.com/.*?/(\w{8,})"),
    )

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        mid = self._m(page).group(1).split("-")[0]
        # The v2 media endpoint returns a clean JSON playlist; let the JSON
        # extractor read it by emitting an API lead.
        yield ctx.lead(page, f"https://cdn.jwplayer.com/v2/media/{mid}", API,
                       title=page.lead.title if page.lead else None)


@register
class CloudflareStream(ProviderExtractor):
    """Cloudflare Stream manifests are pure convention - no API call needed."""

    name = "cloudflare-stream"
    patterns = (
        re.compile(r"(?:iframe\.)?(?:videodelivery\.net|cloudflarestream\.com|"
                   r"customer-\w+\.cloudflarestream\.com)/([\w]{16,})"),
    )

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        uid = self._m(page).group(1)
        host = host_of(page.url) or "videodelivery.net"
        title = page.lead.title if page.lead else None
        yield Video(url=f"https://{host}/{uid}/manifest/video.m3u8", kind=HLS,
                    container="m3u8", title=title, page_url=page.url,
                    meta={"provider": "cloudflare-stream", "uid": uid})
        yield Video(url=f"https://{host}/{uid}/manifest/video.mpd", kind="dash",
                    container="mpd", title=title, page_url=page.url,
                    meta={"provider": "cloudflare-stream", "uid": uid})


@register
class BunnyStream(ProviderExtractor):
    name = "bunny-stream"
    patterns = (
        re.compile(r"(?:iframe|video)\.mediadelivery\.net/(?:embed|play)/(\d+)/([\w-]{8,})"),
    )

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        m = self._m(page)
        lib, vid = m.group(1), m.group(2)
        yield Video(url=f"https://iframe.mediadelivery.net/{lib}/{vid}/playlist.m3u8",
                    kind=HLS, container="m3u8", page_url=page.url,
                    title=page.lead.title if page.lead else None,
                    headers={"Referer": page.url},
                    meta={"provider": "bunny-stream", "library": lib, "video_id": vid})


@register
class MuxStream(ProviderExtractor):
    name = "mux"
    patterns = (re.compile(r"stream\.mux\.com/([\w]{10,})"),)

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        pid = self._m(page).group(1).split(".")[0]
        yield Video(url=f"https://stream.mux.com/{pid}.m3u8", kind=HLS,
                    container="m3u8", page_url=page.url,
                    title=page.lead.title if page.lead else None,
                    meta={"provider": "mux", "playback_id": pid})


@register
class Brightcove(ProviderExtractor):
    name = "brightcove"
    patterns = (
        re.compile(r"players\.brightcove\.net/(\d+)/([\w-]+)(?:_default)?/index\.html"),
        re.compile(r"link\.brightcove\.com/services/player/bcpid(\d+).*?bctid(\d+)"),
    )

    def matches(self, page: Page, ctx: Context) -> bool:
        return super().matches(page, ctx) or "brightcove" in (page.text or "")[:200000]

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        m = self._m(page)
        video_id = None
        account = None
        if m:
            account = m.group(1)
            qs = re.search(r"videoId=(\d+)", page.requested_url + page.url)
            video_id = qs.group(1) if qs else None
        if not (account and video_id):
            return
        # The policy key lives in the player bundle; without it the Edge API
        # rejects us, so pull it out first.
        key = None
        try:
            pj = ctx.fetcher.fetch(
                f"https://players.brightcove.net/{account}/{m.group(2)}/index.min.js")
            km = re.search(r"policyKey:\s*[\"']([\w-]+)[\"']", pj.text)
            key = km.group(1) if km else None
        except Exception as e:
            ctx.debug(f"brightcove player js failed: {e}")
        if not key:
            ctx.warn("brightcove: no policy key found; try --browser")
            return
        yield ctx.lead(
            page,
            f"https://edge.api.brightcove.com/playback/v1/accounts/{account}"
            f"/videos/{video_id}", API,
            headers={"Accept": f"application/json;pk={key}"},
        )


@register
class Kaltura(ProviderExtractor):
    name = "kaltura"
    patterns = (
        re.compile(r"kaltura\.com/(?:p|partner_id)/(\d+)/.*?entry_id[/=]([\w_]+)"),
        re.compile(r"kaltura\.com/p/(\d+)/sp/\d+/embedIframeJs/uiconf_id/(\d+)"),
    )

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        m = self._m(page)
        partner = m.group(1)
        entry = None
        em = re.search(r"entry_id[/=]([\w_]+)", page.requested_url + " " + page.url)
        if em:
            entry = em.group(1)
        if not entry:
            return
        base = (f"https://cdnapisec.kaltura.com/p/{partner}/sp/{partner}00"
                f"/playManifest/entryId/{entry}/format")
        title = page.lead.title if page.lead else None
        yield Video(url=f"{base}/applehttp/protocol/https/a.m3u8", kind=HLS,
                    container="m3u8", title=title, page_url=page.url,
                    meta={"provider": "kaltura", "entry_id": entry})
        yield Video(url=f"{base}/url/protocol/https/a.mp4", kind=PROGRESSIVE,
                    container="mp4", title=title, page_url=page.url,
                    meta={"provider": "kaltura", "entry_id": entry})


@register
class VdoCipher(ProviderExtractor):
    """DRM by design - report it clearly instead of failing mysteriously."""

    name = "vdocipher"
    patterns = (re.compile(r"(?:player\.)?vdocipher\.com/(?:v2/)?\?otp=|vdocipher\.com/api"),)

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        ctx.warn("VdoCipher embed detected: Widevine/FairPlay DRM, not extractable")
        yield Video(url=page.url, kind=EXTERNAL, page_url=page.url,
                    title=page.lead.title if page.lead else None,
                    meta={"provider": "vdocipher", "drm": True,
                          "note": "DRM-protected; no extraction path"})
