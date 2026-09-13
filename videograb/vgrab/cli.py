"""Command line interface."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

from . import __version__
from .auth import apply_auth, save_netscape
from .download import Downloader, filename_for, pick_best
from .http import Fetcher
from .models import EXTERNAL, Result, Video
from .pipeline import Pipeline
from .profiles import ProfileError, load as load_profiles
from .registry import build, load_plugins, all_extractor_classes

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


def make_logger(verbosity: int):
    threshold = 10 if verbosity >= 2 else (20 if verbosity == 1 else 30)

    def log(level: str, msg: str) -> None:
        if LEVELS.get(level, 20) >= threshold:
            prefix = {"debug": "  ", "info": "  ", "warn": "! ", "error": "E "}[level]
            print(f"{prefix}{msg}", file=sys.stderr)

    return log


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="videograb",
        description="Find and download the videos behind a web page.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # what is on this page?
  videograb extract https://example.com/lesson/1

  # a whole course you are logged into, as JSON
  videograb extract https://business.whizlabs.com/learn/course/slug/4418 \\
      --cookies cookies.txt --depth 3 --json

  # log in once by hand, reuse the session forever
  videograb login https://business.whizlabs.com/ --storage-state wl.json

  # JavaScript-rendered site: drive a real browser
  videograb extract <url> --browser --storage-state wl.json

  # download the best rendition of every lesson
  videograb download <url> --storage-state wl.json --browser -o ./course
""")
    p.add_argument("--version", action="version", version=f"videograb {__version__}")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        g = sp.add_argument_group("authentication")
        g.add_argument("--cookies", metavar="FILE",
                       help="Netscape cookies.txt exported from your browser")
        g.add_argument("--cookies-json", metavar="FILE",
                       help="cookies as JSON (DevTools / EditThisCookie / Playwright)")
        g.add_argument("--cookies-from-browser", metavar="BROWSER",
                       help="read cookies directly (chrome|firefox|edge; needs browser_cookie3)")
        g.add_argument("--header", action="append", default=[], metavar="'K: V'",
                       help="extra request header (repeatable)")
        g.add_argument("--bearer", metavar="TOKEN", help="Authorization: Bearer TOKEN")
        g.add_argument("--user-agent", metavar="UA")
        g.add_argument("--save-cookies", metavar="FILE",
                       help="write the resulting session to a cookies.txt")

        g = sp.add_argument_group("crawling")
        g.add_argument("--depth", type=int, default=2,
                       help="how many hops from the start URL (default: 2)")
        g.add_argument("--max-pages", type=int, default=200)
        g.add_argument("--max-videos", type=int, default=0, help="0 = no limit")
        g.add_argument("--max-lessons", type=int, default=0,
                       help="cap lessons taken from one curriculum page")
        g.add_argument("--scope", choices=["site", "host", "any"], default="site",
                       help="how far page links may wander (default: site)")
        g.add_argument("--allow-host", action="append", default=[], metavar="HOST")
        g.add_argument("--deny", action="append", default=[], metavar="REGEX",
                       help="never fetch URLs matching this (repeatable)")
        g.add_argument("--follow", action="append", default=[], metavar="REGEX",
                       help="also follow links matching this (repeatable)")
        g.add_argument("--follow-selector", action="append", default=[], metavar="CSS")
        g.add_argument("--no-course", action="store_true",
                       help="disable automatic course/curriculum crawling")
        g.add_argument("--all-iframes", action="store_true",
                       help="follow every iframe, not just player-looking ones")
        g.add_argument("--no-probe", action="store_true",
                       help="never guess API endpoints, only follow real links")

        g = sp.add_argument_group("http")
        g.add_argument("--timeout", type=float, default=30.0)
        g.add_argument("--retries", type=int, default=3)
        g.add_argument("--delay", type=float, default=0.0,
                       help="seconds between requests to the same host")
        g.add_argument("--proxy", metavar="URL")
        g.add_argument("--insecure", action="store_true", help="skip TLS verification")

        g = sp.add_argument_group("extractors")
        g.add_argument("--only", action="append", default=[], metavar="NAME")
        g.add_argument("--exclude", action="append", default=[], metavar="NAME")
        g.add_argument("--plugin", action="append", default=[], metavar="PATH",
                       help="extra Python file/dir defining @register extractors")
        g.add_argument("--profile", action="append", default=[], metavar="PATH",
                       help="extra YAML/JSON site profile file or directory")
        g.add_argument("--no-builtin-profiles", action="store_true")

        g = sp.add_argument_group("browser (needs: pip install playwright)")
        g.add_argument("--browser", action="store_true",
                       help="render pages in Chromium and sniff media requests")
        g.add_argument("--headful", action="store_true", help="show the browser window")
        g.add_argument("--storage-state", metavar="FILE",
                       help="Playwright storage state to load (see `login`)")
        g.add_argument("--settle", type=int, default=2500,
                       help="ms to wait after load for players to start (default: 2500)")
        g.add_argument("--no-scroll", action="store_true")
        g.add_argument("--chromium", metavar="PATH", help="Chromium executable override")
        g.add_argument("--dump-network", action="store_true",
                       help="print every media request the browser made")

        g = sp.add_argument_group("output")
        g.add_argument("-v", "--verbose", action="count", default=0)
        g.add_argument("-q", "--quiet", action="store_true")
        g.add_argument("--json", action="store_true", help="machine-readable output")
        g.add_argument("--urls", action="store_true", help="print media URLs only")
        g.add_argument("--save", metavar="FILE", help="write the JSON result here")

    sp = sub.add_parser("extract", help="discover videos (default command)")
    sp.add_argument("url")
    common(sp)

    sp = sub.add_parser("download", help="discover, then download")
    sp.add_argument("url")
    sp.add_argument("-o", "--out", default="downloads", metavar="DIR")
    sp.add_argument("--all", action="store_true",
                    help="download every rendition, not just the best per page")
    sp.add_argument("--backend", default="auto",
                    choices=["auto", "direct", "hls", "ffmpeg", "ytdlp"])
    sp.add_argument("--template", default="{index:03d} - {title} [{label}].{ext}",
                    help="filename template: {index} {title} {label} {height} "
                         "{kind} {id} {ext}")
    sp.add_argument("--overwrite", action="store_true")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be written, download nothing")
    sp.add_argument("--min-height", type=int, default=0)
    common(sp)

    sp = sub.add_parser("login", help="log in by hand once, save the session")
    sp.add_argument("url")
    sp.add_argument("--storage-state", default="storage-state.json", metavar="FILE")
    sp.add_argument("--save-cookies", metavar="FILE",
                    help="also write a cookies.txt (works with yt-dlp/curl)")
    sp.add_argument("--chromium", metavar="PATH")
    sp.add_argument("--user-agent", metavar="UA")
    sp.add_argument("-v", "--verbose", action="count", default=0)

    sp = sub.add_parser("extractors", help="list registered extractors")
    sp.add_argument("--profile", action="append", default=[], metavar="PATH")
    sp.add_argument("--plugin", action="append", default=[], metavar="PATH")

    sp = sub.add_parser("profiles", help="list loaded site profiles")
    sp.add_argument("--profile", action="append", default=[], metavar="PATH")
    sp.add_argument("url", nargs="?", help="show which profiles match this URL")
    return p


# ---------------------------------------------------------------------------

def make_fetcher(args, log) -> Fetcher:
    kw = dict(timeout=args.timeout, retries=args.retries, delay=args.delay,
              verify_tls=not args.insecure, proxy=args.proxy)
    if args.user_agent:
        kw["user_agent"] = args.user_agent
    if getattr(args, "browser", False):
        from .browser import BrowserFetcher

        fetcher = BrowserFetcher(
            headless=not args.headful, storage_state=args.storage_state,
            settle_ms=args.settle, scroll=not args.no_scroll,
            executable_path=args.chromium, **kw)
    else:
        fetcher = Fetcher(**kw)
        if args.storage_state:
            # storage_state is JSON cookies; usable without the browser too.
            args.cookies_json = args.cookies_json or args.storage_state
    for note in apply_auth(
            fetcher, cookies=args.cookies, cookies_json=args.cookies_json,
            cookies_from_browser=args.cookies_from_browser, headers=args.header,
            bearer=args.bearer, user_agent=args.user_agent):
        log("info", note)
    return fetcher


def make_pipeline(args, fetcher, log) -> Pipeline:
    if args.plugin:
        for f in load_plugins(args.plugin):
            log("info", f"loaded plugin {f}")
    profiles = []
    try:
        profiles = load_profiles(args.profile, not args.no_builtin_profiles)
    except ProfileError as e:
        log("warn", str(e))
    options = {
        "follow": args.follow, "follow_selector": args.follow_selector,
        "no_course": args.no_course, "all_iframes": args.all_iframes,
        "max_lessons": args.max_lessons,
        "probe_api": not args.no_probe,
    }
    extra = []
    depth = args.depth
    for prof in profiles:
        extra.append(prof.build())
        if prof.matches_url(args.url):
            log("info", f"profile {prof.name} matches ({prof.path})")
            opts = dict(prof.options)
            depth = max(depth, int(opts.pop("max_depth", depth) or depth))
            for k, v in opts.items():
                options.setdefault(k, v)
            fetcher.headers.update({k.title(): v for k, v in prof.headers.items()})
    # The network sniffer is opt-in; the browser backend is what makes it
    # useful, so turn it on there without narrowing anything else.
    enable = ["network"] if getattr(args, "browser", False) else []
    extractors = build(only=args.only or None, exclude=args.exclude,
                       extra=extra, enable=enable)
    log("info", f"extractors: {', '.join(e.name for e in extractors)}")
    return Pipeline(
        fetcher=fetcher, extractors=extractors, max_depth=depth,
        max_pages=args.max_pages, max_videos=args.max_videos, scope=args.scope,
        allow_hosts=args.allow_host, deny_patterns=args.deny,
        options=options, on_event=log,
    )


def print_result(result: Result, args) -> None:
    if args.json:
        print(result.to_json())
        return
    if args.urls:
        for v in result.videos:
            print(v.url)
        return
    if not result.videos:
        print("No videos found.", file=sys.stderr)
        _hints(result, args)
        return
    rows = []
    for i, v in enumerate(result.videos, 1):
        flags = []
        if v.meta.get("drm"):
            flags.append("DRM")
        if v.meta.get("encryption"):
            flags.append(v.meta["encryption"].get("method", "enc"))
        if v.kind == EXTERNAL:
            flags.append("yt-dlp")
        if v.meta.get("captured"):
            flags.append("sniffed")
        rows.append((str(i), v.kind, v.label, _dur(v.duration),
                     (v.title or "")[:44], ",".join(flags), v.url))
    widths = [max(len(r[c]) for r in rows) for c in range(6)]
    header = ("#", "kind", "quality", "length", "title", "flags")
    widths = [max(w, len(h)) for w, h in zip(widths, header)]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths)) + "  url"
    print(line)
    print("-" * min(len(line) + 20, 120))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r[:6], widths)) + "  " + r[6])
    print(f"\n{len(result.videos)} video(s) from {len(result.visited)} page(s).")
    _hints(result, args)


def _hints(result: Result, args) -> None:
    for note in result.notes:
        if "authentication" in note or "max_" in note:
            print(f"note: {note}", file=sys.stderr)
    if result.errors and args.verbose:
        for e in result.errors[:10]:
            print(f"error: {e['url']}: {e['error']}", file=sys.stderr)
    if not result.videos and not getattr(args, "browser", False):
        print("\nNothing found in the server-rendered HTML. If the site builds "
              "its player in JavaScript (most course platforms do), retry with "
              "--browser, and add --cookies/--storage-state if it needs a login.",
              file=sys.stderr)


def _dur(seconds) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60}:{s % 60:02d}"


def _progress_printer(quiet: bool):
    def progress(name: str, done: int, total: Optional[int]) -> None:
        if quiet:
            return
        if total:
            pct = min(100, int(done * 100 / total))
            bar = "#" * (pct // 4)
            sys.stderr.write(f"\r  {name[:48]:50s} [{bar:25s}] {pct:3d}%")
        else:
            sys.stderr.write(f"\r  {name[:48]:50s} working...")
        if total and done >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()

    return progress


# ---------------------------------------------------------------------------

def cmd_extract(args) -> int:
    log = make_logger(0 if args.quiet else args.verbose)
    fetcher = make_fetcher(args, log)
    try:
        pipeline = make_pipeline(args, fetcher, log)
        result = pipeline.run(args.url)
    finally:
        if hasattr(fetcher, "close"):
            if args.dump_network and getattr(fetcher, "captured", None):
                print("\n-- media requests seen by the browser --", file=sys.stderr)
                for page_url, hits in fetcher.captured.items():
                    for h in hits:
                        print(f"  [{h.get('status') or '-'}] {h['url']}", file=sys.stderr)
            fetcher.close()
    if args.save_cookies:
        n = save_netscape(fetcher.cookiejar, args.save_cookies)
        log("info", f"wrote {n} cookies to {args.save_cookies}")
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            fh.write(result.to_json())
        log("info", f"wrote {args.save}")
    print_result(result, args)
    return 0 if result.videos else 2


def cmd_download(args) -> int:
    log = make_logger(0 if args.quiet else args.verbose)
    fetcher = make_fetcher(args, log)
    try:
        pipeline = make_pipeline(args, fetcher, log)
        result = pipeline.run(args.url)
    finally:
        if hasattr(fetcher, "close"):
            fetcher.close()

    videos: List[Video] = result.videos
    if not args.all:
        videos = pick_best(videos)
    if args.min_height:
        videos = [v for v in videos if (v.height or 0) >= args.min_height] or videos
    if not videos:
        print_result(result, args)
        return 2
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            fh.write(result.to_json())

    cookies_path = args.save_cookies
    if not cookies_path and any(v.kind == EXTERNAL for v in videos):
        cookies_path = os.path.join(args.out, ".cookies.txt")
        os.makedirs(args.out, exist_ok=True)
        save_netscape(fetcher.cookiejar, cookies_path)

    dl = Downloader(fetcher, out_dir=args.out, backend=args.backend,
                    overwrite=args.overwrite, progress=_progress_printer(args.quiet),
                    cookies_file=cookies_path, dry_run=args.dry_run)
    ok = failed = 0
    for i, v in enumerate(videos, 1):
        target = filename_for(v, args.template, i, dl.choose(v))
        if not args.quiet:
            print(f"[{i}/{len(videos)}] {target}  <- {dl.choose(v)}", file=sys.stderr)
        try:
            path = dl.download(v, i, args.template)
            ok += 1
            if args.dry_run or args.verbose:
                print(path)
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            # Includes OSError, DownloadError, and anything a subprocess or an
            # optional C extension throws. One bad asset must not end the run.
            failed += 1
            print(f"  failed: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"\n{ok} downloaded, {failed} failed -> {args.out}", file=sys.stderr)
    return 0 if failed == 0 else 1


def cmd_login(args) -> int:
    from .browser import BrowserFetcher, PlaywrightUnavailable

    kw = {}
    if args.user_agent:
        kw["user_agent"] = args.user_agent
    fetcher = BrowserFetcher(headless=False, executable_path=args.chromium, **kw)
    try:
        path = fetcher.login(args.url, args.storage_state)
        print(f"saved browser session -> {path}")
        if args.save_cookies:
            n = save_netscape(fetcher.cookiejar, args.save_cookies)
            print(f"saved {n} cookies -> {args.save_cookies}")
        print("\nReuse it with:")
        print(f"  videograb extract <course-url> --storage-state {path} --browser")
    except PlaywrightUnavailable as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        fetcher.close()
    return 0


def cmd_extractors(args) -> int:
    for f in load_plugins(args.plugin or []):
        print(f"# plugin: {f}")
    extras = []
    try:
        extras = [p.build() for p in load_profiles(args.profile, True)]
    except ProfileError as e:
        print(f"! {e}", file=sys.stderr)
    print(f"{'priority':>8}  {'name':<22} class")
    for c in all_extractor_classes():
        mark = "" if c.enabled_by_default else "  (opt-in)"
        print(f"{c.priority:>8}  {c.name:<22} {c.__name__}{mark}")
    for e in extras:
        print(f"{e.priority:>8}  {e.name:<22} ProfileExtractor")
    return 0


def cmd_profiles(args) -> int:
    try:
        profiles = load_profiles(args.profile, True)
    except ProfileError as e:
        print(f"! {e}", file=sys.stderr)
        return 1
    if not profiles:
        print("no profiles loaded")
        return 0
    for p in profiles:
        match = ""
        if args.url:
            match = "  <-- matches" if p.matches_url(args.url) else ""
        print(f"{p.name:<20} prio={p.priority:<4} hosts={','.join(p.hosts) or '-'}"
              f"  follow={len(p.follow)} videos={len(p.videos)}{match}")
        print(f"{'':<20} {p.path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Allow `videograb <url>` as shorthand for `videograb extract <url>`.
    if argv and argv[0] not in ("extract", "download", "login", "extractors",
                                "profiles", "-h", "--help", "--version"):
        argv.insert(0, "extract")
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 1
    handler = {"extract": cmd_extract, "download": cmd_download,
               "login": cmd_login, "extractors": cmd_extractors,
               "profiles": cmd_profiles}[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:
        if getattr(args, "verbose", 0) >= 2:
            raise
        print(f"error: {e}", file=sys.stderr)
        return 1
