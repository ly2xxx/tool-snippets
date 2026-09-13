"""Browser-backed fetching, for sites that build their pages in JavaScript.

Design note: the browser is a *fetch strategy*, not an extractor. `BrowserFetcher`
subclasses `Fetcher` and, for HTML navigations, drives a real Chromium and
returns the rendered DOM. Everything downstream - every extractor, the crawl
logic, the downloader - is unchanged and gets the post-JavaScript page for
free. While the page runs, we also record every media request it makes, which
is the single most reliable extraction method there is: whatever the player
actually played, we saw it, signed URLs and all.

Requires the optional dependency:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional

from .http import Fetcher
from .models import EMBED, PAGE, Page, Video
from .registry import Context, Extractor, register
from .util import classify, height_from_url

MEDIA_CT = re.compile(
    r"(video/|audio/|application/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml|"
    r"octet-stream|mp4))", re.I)
# Requests worth recording even before the response arrives.
MEDIA_URL = re.compile(
    r"\.(m3u8|mpd|mp4|m4s|webm|ts|mov|mkv|m4v|flv)(?:$|[?#])|/manifest/|/hls/|/dash/",
    re.I)


class PlaywrightUnavailable(RuntimeError):
    pass


class BrowserFetcher(Fetcher):
    """A Fetcher that renders HTML pages in Chromium.

    Non-HTML requests (manifests, JSON APIs, media probes) keep using plain
    urllib with the browser's cookies copied in - much faster, and it keeps
    the download path identical to a non-browser run.
    """

    def __init__(self, headless: bool = True, storage_state: Optional[str] = None,
                 wait_until: str = "networkidle", settle_ms: int = 2500,
                 scroll: bool = True, executable_path: Optional[str] = None,
                 block_media: bool = False, **kw):
        super().__init__(**kw)
        self.headless = headless
        self.storage_state = storage_state
        self.wait_until = wait_until
        self.settle_ms = settle_ms
        self.scroll = scroll
        self.executable_path = executable_path or os.environ.get("VGRAB_CHROMIUM")
        self.block_media = block_media
        # page url -> list of {url, content_type, status, method}
        self.captured: Dict[str, List[Dict[str, Any]]] = {}
        self._pw = None
        self._browser = None
        self._context = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as e:
            raise PlaywrightUnavailable(
                "the browser backend needs Playwright:\n"
                "    pip install playwright && playwright install chromium") from e
        self._pw = sync_playwright().start()
        launch: Dict[str, Any] = {"headless": self.headless}
        if self.executable_path:
            launch["executable_path"] = self.executable_path
        try:
            self._browser = self._pw.chromium.launch(**launch)
        except Exception as first:
            # Playwright pins an exact browser build; a machine that has some
            # other Chromium installed (managed images, `apt install chromium`,
            # a stale PLAYWRIGHT_BROWSERS_PATH) hits this constantly. Look for
            # one before giving up.
            found = find_chromium()
            if not found or self.executable_path:
                raise PlaywrightUnavailable(
                    f"could not launch Chromium: {first}\n"
                    "Install the matching build with:  playwright install chromium\n"
                    "or point at an existing one with --chromium /path/to/chrome"
                ) from first
            launch["executable_path"] = found
            self._browser = self._pw.chromium.launch(**launch)
        ctx_args: Dict[str, Any] = {"user_agent": self.user_agent,
                                    "ignore_https_errors": True}
        if self.storage_state and os.path.exists(self.storage_state):
            ctx_args["storage_state"] = self.storage_state
        self._context = self._browser.new_context(**ctx_args)
        # Carry any cookies supplied via --cookies into the browser session.
        cookies = _jar_to_playwright(self.cookiejar)
        if cookies:
            try:
                self._context.add_cookies(cookies)
            except Exception:
                pass
        return self._context

    def close(self):
        for obj, meth in ((self._context, "close"), (self._browser, "close"),
                          (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass
        self._context = self._browser = self._pw = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- fetching ----------------------------------------------------------
    def fetch(self, url: str, lead=None, headers=None, method: str = "GET",
              data: Optional[bytes] = None, retries: Optional[int] = None,
              timeout: Optional[float] = None) -> Page:
        kind = lead.kind if lead else PAGE
        if method != "GET" or kind not in (PAGE, EMBED):
            return super().fetch(url, lead, headers, method, data, retries, timeout)
        try:
            return self._render(url, lead)
        except PlaywrightUnavailable:
            raise
        except Exception:
            # A browser failure should degrade to a plain fetch, not kill the run.
            return super().fetch(url, lead, headers, method, data, retries, timeout)

    def _render(self, url: str, lead) -> Page:
        context = self.start()
        page = context.new_page()
        hits: List[Dict[str, Any]] = []

        def on_request(req):
            try:
                if MEDIA_URL.search(req.url):
                    hits.append({"url": req.url, "method": req.method,
                                 "content_type": None, "status": None,
                                 "via": "request"})
            except Exception:
                pass

        def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if MEDIA_CT.search(ct or "") or MEDIA_URL.search(resp.url):
                    hits.append({"url": resp.url, "method": "GET",
                                 "content_type": ct, "status": resp.status,
                                 "via": "response"})
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        if self.block_media:
            # Saves bandwidth: we want the URLs, not the bytes.
            page.route(re.compile(r"\.(ts|m4s|mp4|webm)(\?|$)"),
                       lambda route: route.abort())
        try:
            resp = page.goto(url, wait_until=self.wait_until,
                             timeout=int(self.timeout * 1000))
            if self.scroll:
                try:
                    page.mouse.wheel(0, 4000)
                except Exception:
                    pass
            if self.settle_ms:
                page.wait_for_timeout(self.settle_ms)
            html = page.content()
            final_url = page.url
            status = resp.status if resp else 200
        finally:
            try:
                page.close()
            except Exception:
                pass
        self._sync_cookies()
        # Deduplicate while preserving first-seen order.
        seen = set()
        uniq = []
        for h in hits:
            if h["url"] in seen:
                continue
            seen.add(h["url"])
            uniq.append(h)
        self.captured.setdefault(final_url, []).extend(uniq)
        if final_url != url:
            self.captured.setdefault(url, []).extend(uniq)
        return Page(url=final_url, requested_url=url, status=int(status),
                    headers={"content-type": "text/html; charset=utf-8"},
                    text=html, lead=lead, body=html.encode("utf-8", "replace"))

    def _sync_cookies(self) -> None:
        """Copy browser cookies back into the urllib jar so direct fetches and
        downloads are authenticated too."""
        if self._context is None:
            return
        from .auth import make_cookie

        try:
            for c in self._context.cookies():
                exp = c.get("expires")
                exp = int(exp) if exp and exp > 0 else None
                self.cookiejar.set_cookie(make_cookie(
                    c.get("name", ""), c.get("value", ""), c.get("domain", ""),
                    c.get("path", "/"), bool(c.get("secure")), exp))
        except Exception:
            pass

    def save_state(self, path: str) -> None:
        if self._context is not None:
            self._context.storage_state(path=path)

    # -- interactive login -------------------------------------------------
    def login(self, url: str, state_path: str, timeout: float = 300.0) -> str:
        """Open a real window, let a human sign in, then persist the session.

        This is why the tool never asks for your password: you log in the same
        way you always do (SSO, MFA, whatever), and we keep only the cookies.
        """
        self.headless = False
        context = self.start()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded",
                  timeout=int(self.timeout * 1000))
        print("\nA browser window is open. Log in, navigate to the course page,")
        print("then press Enter here to save the session.")
        try:
            input()
        except EOFError:
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(2)
        self._sync_cookies()
        context.storage_state(path=state_path)
        try:
            page.close()
        except Exception:
            pass
        return state_path


def find_chromium() -> Optional[str]:
    """Any usable Chromium/Chrome on this machine, newest build first."""
    import glob
    import shutil

    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "",
             os.path.expanduser("~/.cache/ms-playwright"),
             os.path.expanduser("~/AppData/Local/ms-playwright"),
             "/opt/pw-browsers"]
    patterns = ["chromium-*/chrome-linux/chrome",
                "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
                "chromium-*/chrome-win/chrome.exe",
                "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"]
    candidates = []
    for root in filter(None, roots):
        for pat in patterns:
            candidates.extend(glob.glob(os.path.join(root, pat)))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0]
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome",
                 "msedge"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _jar_to_playwright(jar) -> List[Dict[str, Any]]:
    out = []
    for c in jar or []:
        if not c.domain:
            continue
        out.append({
            "name": c.name, "value": c.value or "", "domain": c.domain,
            "path": c.path or "/", "secure": bool(c.secure),
            "expires": float(c.expires) if c.expires else -1,
        })
    return out


@register
class NetworkCapture(Extractor):
    """Turns what the player actually requested into Videos.

    Only active with the browser backend - hence enabled_by_default=False.
    """

    name = "network"
    priority = 10
    kinds = (PAGE, EMBED)
    enabled_by_default = False

    def matches(self, page: Page, ctx: Context) -> bool:
        fetcher = ctx.fetcher
        return isinstance(getattr(fetcher, "captured", None), dict)

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        captured = getattr(ctx.fetcher, "captured", {})
        hits = list(captured.get(page.url, [])) + list(captured.get(page.requested_url, []))
        title = page.dom.title or (page.lead.title if page.lead else None)
        seen = set()
        for hit in hits:
            url = hit.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            kind, container = classify(url)
            if kind is None:
                ct = hit.get("content_type") or ""
                if "mpegurl" in ct.lower():
                    kind, container = "hls", "m3u8"
                elif "dash+xml" in ct.lower():
                    kind, container = "dash", "mpd"
                elif ct.lower().startswith("video/"):
                    kind, container = "progressive", ct.split("/")[-1]
                else:
                    continue
            # Segments are noise; the playlist that lists them is the asset.
            if re.search(r"\.(ts|m4s)(?:$|[?#])", url, re.I) and not ctx.opt("keep_segments"):
                continue
            yield Video(url=url, kind=kind, container=container, title=title,
                        page_url=page.url, height=height_from_url(url),
                        headers={"Referer": page.url},
                        meta={"captured": True, "http_status": hit.get("status"),
                              "content_type": hit.get("content_type")})
