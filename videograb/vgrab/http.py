"""HTTP plumbing: one cookie-aware session with retries and polite pacing.

Standard library only, so `videograb` runs anywhere Python 3.8+ does with
nothing to install. urllib gives us proxy support from the environment,
a real cookie jar, and Range requests for resumable downloads.
"""

from __future__ import annotations

import gzip
import io
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.cookiejar import CookieJar, MozillaCookieJar
from typing import Any, Dict, Optional, Tuple

from .models import Lead, Page

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Bodies bigger than this are almost certainly media, not something to parse.
MAX_PARSE_BYTES = 8 * 1024 * 1024


class FetchError(Exception):
    def __init__(self, url: str, reason: str, status: Optional[int] = None):
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Fetcher:
    """A configured HTTP session.

    Every extractor receives one of these, so auth (cookies, headers) set up
    once at the CLI level automatically applies to the whole crawl - including
    CDN requests that would otherwise 403.
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        headers: Optional[Dict[str, str]] = None,
        cookiejar: Optional[CookieJar] = None,
        timeout: float = 30.0,
        retries: int = 3,
        delay: float = 0.0,
        verify_tls: bool = True,
        follow_redirects: bool = True,
        proxy: Optional[str] = None,
    ):
        self.user_agent = user_agent
        self.headers = {k.title(): v for k, v in (headers or {}).items()}
        self.cookiejar = cookiejar if cookiejar is not None else CookieJar()
        self.timeout = timeout
        self.retries = max(0, retries)
        self.delay = delay
        self._last_request_at: Dict[str, float] = {}

        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers: list = [
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self.cookiejar),
        ]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if not follow_redirects:
            handlers.append(_NoRedirect())
        self.opener = urllib.request.build_opener(*handlers)

    # -- cookies -----------------------------------------------------------
    def load_cookie_file(self, path: str) -> int:
        """Load a Netscape-format cookies.txt (what browser extensions export)."""
        jar = MozillaCookieJar()
        jar.load(path, ignore_discard=True, ignore_expires=True)
        n = 0
        for c in jar:
            self.cookiejar.set_cookie(c)
            n += 1
        return n

    def cookie_header_for(self, url: str) -> str:
        req = urllib.request.Request(url)
        self.cookiejar.add_cookie_header(req)
        return req.get_header("Cookie", "")

    # -- requests ----------------------------------------------------------
    def _pace(self, url: str) -> None:
        if self.delay <= 0:
            return
        host = urllib.parse.urlsplit(url).netloc
        last = self._last_request_at.get(host)
        now = time.monotonic()
        if last is not None:
            wait = self.delay - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    def _build(self, url: str, headers: Optional[Dict[str, str]], method: str,
               data: Optional[bytes]) -> urllib.request.Request:
        h = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        h.update(self.headers)
        for k, v in (headers or {}).items():
            h[k.title()] = v
        return urllib.request.Request(url, data=data, headers=h, method=method)

    def raw(self, url: str, headers: Optional[Dict[str, str]] = None,
            method: str = "GET", data: Optional[bytes] = None,
            retries: Optional[int] = None, timeout: Optional[float] = None):
        """Perform a request with retries; returns the open response object.

        `retries`/`timeout` override the session defaults for one call - used
        for speculative probes, which must not cost three backed-off attempts
        against a host that is simply not there.
        """
        attempts = self.retries if retries is None else max(0, retries)
        deadline = self.timeout if timeout is None else timeout
        last_exc: Optional[Exception] = None
        for attempt in range(attempts + 1):
            self._pace(url)
            try:
                return self.opener.open(self._build(url, headers, method, data),
                                        timeout=deadline)
            except urllib.error.HTTPError as e:
                # 4xx are answers, not failures - hand them back for inspection.
                if e.code < 500 and e.code != 429:
                    return e
                last_exc = e
            except Exception as e:  # URLError, socket timeouts, TLS errors
                last_exc = e
            if attempt < attempts:
                time.sleep((2 ** attempt) * 0.5 + random.random() * 0.3)
        raise FetchError(url, str(last_exc))

    def fetch(self, url: str, lead: Optional[Lead] = None,
              headers: Optional[Dict[str, str]] = None, method: str = "GET",
              data: Optional[bytes] = None, retries: Optional[int] = None,
              timeout: Optional[float] = None) -> Page:
        merged = dict(lead.headers) if lead else {}
        merged.update(headers or {})
        resp = self.raw(url, merged, method, data, retries, timeout)
        final_url = resp.geturl()
        raw_headers = {k.lower(): v for k, v in resp.headers.items()}
        body = _read_capped(resp, MAX_PARSE_BYTES)
        body = _decompress(body, raw_headers.get("content-encoding", ""))
        text = _decode(body, raw_headers.get("content-type", ""))
        status = getattr(resp, "status", None) or getattr(resp, "code", 0) or 0
        try:
            resp.close()
        except Exception:
            pass
        return Page(url=final_url, requested_url=url, status=int(status),
                    headers=raw_headers, text=text, lead=lead, body=body)

    def fetch_json(self, url: str, **kw) -> Any:
        import json

        page = self.fetch(url, **kw)
        try:
            return json.loads(page.text)
        except Exception as e:
            raise FetchError(url, f"not JSON ({e})", page.status)

    def head_info(self, url: str, headers: Optional[Dict[str, str]] = None
                  ) -> Tuple[int, Dict[str, str]]:
        """Cheap probe: size/type without pulling the body. Falls back to a
        ranged GET for servers that dislike HEAD (many CDNs)."""
        try:
            resp = self.raw(url, headers, method="HEAD")
            status = getattr(resp, "status", 0) or 0
            if status and status < 400:
                return status, {k.lower(): v for k, v in resp.headers.items()}
        except Exception:
            pass
        h = dict(headers or {})
        h["Range"] = "bytes=0-0"
        resp = self.raw(url, h)
        info = {k.lower(): v for k, v in resp.headers.items()}
        status = getattr(resp, "status", 0) or 0
        try:
            resp.close()
        except Exception:
            pass
        return int(status), info


def _read_capped(resp, cap: int) -> bytes:
    buf = io.BytesIO()
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        buf.write(chunk)
        if buf.tell() > cap:
            break
    return buf.getvalue()


def _decompress(body: bytes, encoding: str) -> bytes:
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        return body
    return body


_META_CHARSET = re.compile(rb'charset=["\']?([\w-]+)', re.I)


def _decode(body: bytes, content_type: str) -> str:
    if not body:
        return ""
    ct = (content_type or "").lower()
    # Don't try to decode obvious media payloads as text.
    if ct.startswith(("video/", "audio/", "image/", "font/")):
        return ""
    charset = None
    m = re.search(r"charset=([\w-]+)", ct)
    if m:
        charset = m.group(1)
    if not charset:
        m2 = _META_CHARSET.search(body[:4096])
        if m2:
            charset = m2.group(1).decode("ascii", "ignore")
    for enc in filter(None, (charset, "utf-8", "latin-1")):
        try:
            return body.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def absolute(base: str, url: str) -> str:
    """urljoin that tolerates protocol-relative and whitespace-padded URLs."""
    url = (url or "").strip().replace("&amp;", "&")
    if not url:
        return ""
    if url.startswith("//"):
        scheme = urllib.parse.urlsplit(base).scheme or "https"
        return f"{scheme}:{url}"
    return urllib.parse.urljoin(base, url)


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


def strip_query(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def path_of(url: str) -> str:
    return urllib.parse.urlsplit(url).path
