"""Getting an authenticated session, in the four ways that actually work.

Paid course platforms gate everything behind a login. Rather than handle
passwords (which breaks on SSO, MFA, and captchas anyway), we reuse a session
you already established in your own browser:

  --cookies FILE            Netscape cookies.txt (Get cookies.txt LOCALLY etc.)
  --cookies-json FILE       DevTools / Playwright / EditThisCookie JSON export
  --cookies-from-browser B  read chrome/firefox/edge directly (browser_cookie3)
  --header 'K: V'           raw header, e.g. Authorization: Bearer ...

All of them land in the same Fetcher, so every request in the crawl -
including CDN segment fetches - carries the session.
"""

from __future__ import annotations

import json
from http.cookiejar import Cookie, CookieJar
from typing import Iterable, List, Optional, Tuple

from .http import Fetcher


def make_cookie(name: str, value: str, domain: str, path: str = "/",
                secure: bool = False, expires: Optional[int] = None) -> Cookie:
    domain = domain or ""
    return Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path or "/", path_specified=True, secure=bool(secure),
        expires=expires, discard=False, comment=None, comment_url=None,
        rest={"HttpOnly": None}, rfc2109=False,
    )


def load_netscape(path: str, jar: CookieJar) -> int:
    """Parse a cookies.txt by hand.

    `MozillaCookieJar.load` asserts that the domain-specified column agrees
    with a leading dot on the domain, and browser exporters routinely write
    files that violate that (`example.com` + `TRUE`). Real files also carry
    the `#HttpOnly_` prefix. Rather than reject the exports people actually
    have, we read the seven columns ourselves and normalise.
    """
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                parts = line.split()
                if len(parts) < 7:
                    continue
            domain, _flag, cpath, secure, expires, name, value = parts[:7]
            try:
                exp = int(float(expires)) if expires not in ("", "0") else None
            except ValueError:
                exp = None
            jar.set_cookie(make_cookie(
                name, value, domain, cpath or "/",
                str(secure).upper() == "TRUE", exp))
            n += 1
    return n


def load_json_cookies(path: str, jar: CookieJar) -> int:
    """DevTools "Copy all cookies", EditThisCookie, or Playwright storage_state."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("cookies", data.get("Cookies", []))
    n = 0
    for c in data or []:
        name = c.get("name") or c.get("Name")
        value = c.get("value") or c.get("Value")
        if not name:
            continue
        domain = c.get("domain") or c.get("Domain") or ""
        expires = c.get("expirationDate") or c.get("expires")
        try:
            expires = int(float(expires)) if expires and float(expires) > 0 else None
        except (TypeError, ValueError):
            expires = None
        jar.set_cookie(make_cookie(
            name, str(value), domain, c.get("path") or "/",
            bool(c.get("secure")), expires))
        n += 1
    return n


def load_browser_cookies(spec: str, jar: CookieJar) -> int:
    """`chrome`, `firefox:profile`, `edge`, ... via the optional
    browser_cookie3 package. Optional on purpose - it reads your keyring."""
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        raise RuntimeError(
            "--cookies-from-browser needs browser_cookie3: pip install browser-cookie3")
    browser, _, profile = spec.partition(":")
    fn = getattr(browser_cookie3, browser.strip().lower(), None)
    if fn is None:
        raise RuntimeError(f"unknown browser {browser!r} for browser_cookie3")
    kwargs = {}
    if profile:
        kwargs["domain_name"] = profile if "." in profile else ""
    src = fn(**kwargs)
    n = 0
    for c in src:
        jar.set_cookie(c)
        n += 1
    return n


def parse_header(spec: str) -> Tuple[str, str]:
    if ":" not in spec:
        raise ValueError(f"header must look like 'Name: value', got {spec!r}")
    k, _, v = spec.partition(":")
    return k.strip(), v.strip()


def save_netscape(jar: CookieJar, path: str) -> int:
    """Write cookies.txt ourselves, in the form yt-dlp and curl accept."""
    lines = ["# Netscape HTTP Cookie File",
             "# Written by videograb - reusable with curl/yt-dlp/ffmpeg.", ""]
    n = 0
    for c in jar:
        domain = c.domain or ""
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.path or "/",
            "TRUE" if c.secure else "FALSE",
            str(int(c.expires)) if c.expires else "0",
            c.name, c.value or "",
        ]))
        n += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return n


def apply_auth(fetcher: Fetcher, cookies: Optional[str] = None,
               cookies_json: Optional[str] = None,
               cookies_from_browser: Optional[str] = None,
               headers: Iterable[str] = (), bearer: Optional[str] = None,
               user_agent: Optional[str] = None) -> List[str]:
    """Wire every supplied credential into the session. Returns a log."""
    notes: List[str] = []
    if cookies:
        notes.append(f"loaded {load_netscape(cookies, fetcher.cookiejar)} "
                     f"cookies from {cookies}")
    if cookies_json:
        notes.append(f"loaded {load_json_cookies(cookies_json, fetcher.cookiejar)} "
                     f"cookies from {cookies_json}")
    if cookies_from_browser:
        notes.append(f"loaded {load_browser_cookies(cookies_from_browser, fetcher.cookiejar)} "
                     f"cookies from browser {cookies_from_browser}")
    for h in headers or ():
        k, v = parse_header(h)
        fetcher.headers[k.title()] = v
        notes.append(f"header {k}")
    if bearer:
        fetcher.headers["Authorization"] = f"Bearer {bearer}"
        notes.append("header Authorization (bearer)")
    if user_agent:
        fetcher.user_agent = user_agent
        notes.append("custom user-agent")
    return notes
