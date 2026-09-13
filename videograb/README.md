# videograb

Find and download the videos behind a web page. Built for the case where a
page plays video but gives you no download link: course platforms, LMS
players, embedded players, HLS/DASH streams.

Single-purpose tools already exist (yt-dlp is excellent and this one shells
out to it where it is the right answer). What this adds is **extensibility**:
a crawl/extract pipeline you can teach a new site in a 15-line YAML file, or a
20-line Python class, without touching the core.

- **No required dependencies.** Python 3.8+ standard library only. Everything
  optional (browser automation, AES, yt-dlp, ffmpeg) degrades with a clear
  message rather than a traceback.
- **Works on logged-in sites.** Reuses a session from your own browser; never
  asks for a password.
- **Tested offline.** 54 tests run against a local fixture site - no network,
  no third-party service, no flakiness.

```
python videograb.py extract  <url>            # what video is on this page?
python videograb.py download <url> -o ./out   # get it
python videograb.py login    <url>            # log in once, reuse the session
```

---

## Quick start

```bash
# No install step. Just run it.
python videograb.py https://example.com/some/lesson

# A whole course you are logged into
python videograb.py extract https://business.whizlabs.com/learn/course/besa-generative-ai-labs-subscription/4418 \
    --cookies cookies.txt --depth 3

# Pages built by JavaScript (most course platforms): drive a real browser
python videograb.py login https://business.whizlabs.com/ --storage-state wl.json
python videograb.py extract <course-url> --browser --storage-state wl.json
python videograb.py download <course-url> --browser --storage-state wl.json -o ./course
```

Typical output:

```
#   kind         quality              length  title                     flags    url
-------------------------------------------------------------------------------------
1   progressive  720p mp4             8:32    1. Introduction                    https://cdn/.../lesson1.mp4
2   hls          1080p 5200kbps m3u8  12:34   2. Prompt engineering lab          https://cdn/.../1080p/index.m3u8
3   hls          m3u8                 24:10   3. RAG lab                AES-128  https://cdn/.../master.m3u8
4   external     external                     4. Guest talk             yt-dlp   https://www.youtube.com/watch?v=...

4 video(s) from 11 page(s).
```

`--json` for machine-readable output, `--urls` to pipe into something else.

---

## How it works

Two node types and a breadth-first walk between them:

```
       ┌──────────────────────────────────────────────┐
       │                                              │
    ┌──▼───┐   extractors    ┌───────┐             ┌──┴───┐
    │ Lead │ ──────────────► │ Video │             │ Lead │
    └──────┘                 └───────┘             └──────┘
   page / embed             concrete media       (queued, deduped,
   manifest / api            + metadata           scope-checked)
```

A **Lead** is something worth fetching. A **Video** is a concrete playable
URL. Extractors turn a fetched page into any mix of the two, and the pipeline
handles fetching, deduplication, scope and depth. So a course page emits
lesson Leads → a lesson page emits an embed Lead → the embed emits an HLS
manifest → the manifest expands into per-rendition Videos. Nothing in that
chain knows about the others.

Practical consequences:

- **Duplicates merge instead of colliding.** The same file found three ways
  becomes one entry, keeping the union of what each route knew (title from
  the lesson page, duration from JSON-LD, AES key from the playlist).
- **Scope is enforced.** Page links stay on the site you pointed at;
  embeds and manifests are exempt, because media always lives on a CDN.
  `--scope host|site|any` to change that.
- **One failure is one failure.** A bad extractor, a 403, a dead CDN - the
  run continues and reports it.

### What the built-in extractors cover

| Extractor | Handles |
|---|---|
| `generic` | `<video>`/`<source>`, `og:video`, JSON-LD `VideoObject`, inline player configs (jwplayer `setup({...})`, `__NEXT_DATA__`, `__INITIAL_STATE__`), raw regex sweep, iframes |
| `hls` / `dash` | Expands `.m3u8` masters into renditions (resolution, bitrate, codecs) and `.mpd` into representations; flags AES-128 and DRM |
| `course` | Curriculum → lesson crawling, by shape rather than by platform (Learnyst, Teachable, Thinkific, Kajabi, Moodle, LearnDash, …) |
| `learnyst` | Whizlabs Business and other Learnyst schools |
| `vimeo` `wistia` `jwplayer` `brightcove` `kaltura` `cloudflare-stream` `bunny-stream` `mux` | Provider APIs / derivable manifest URLs |
| `youtube` `external-platforms` | Recognised and handed to yt-dlp, which solves them properly |
| `vdocipher` | Detected and reported as DRM - see [limits](#what-it-will-not-do) |
| `network` | Everything the page actually requested (browser mode) |

`python videograb.py extractors` lists them with priorities.

---

## Authenticated sites

The tool never handles your password. You log in the way you normally do and
it borrows the resulting session.

| Option | Use when |
|---|---|
| `--cookies FILE` | You exported `cookies.txt` (e.g. the "Get cookies.txt LOCALLY" extension) |
| `--cookies-json FILE` | You have a DevTools / EditThisCookie / Playwright JSON export |
| `--cookies-from-browser chrome` | You want it read straight from your browser (needs `pip install browser-cookie3`) |
| `--header 'Authorization: Bearer …'` | The site uses a token, not cookies |
| `login` + `--storage-state` | Easiest: a real browser window, you log in, press Enter |

```bash
python videograb.py login https://business.whizlabs.com/ \
    --storage-state wl.json --save-cookies wl-cookies.txt
```

That writes a reusable session. `--save-cookies` also produces a
`cookies.txt` that works with `yt-dlp`, `curl` and `ffmpeg`.

Cookie files from real browser extensions are frequently malformed in ways
Python's own `MozillaCookieJar` rejects outright (`#HttpOnly_` prefixes, a
domain-specified flag that disagrees with the leading dot). This parses them
anyway.

---

## Browser mode

Most course platforms render the player client-side after an authenticated
XHR, so there is nothing in the HTML to scrape. `--browser` renders the page
in Chromium and records every media request it makes - whatever the player
played, we saw, signed URLs included.

```bash
pip install playwright && playwright install chromium

python videograb.py extract <url> --browser --storage-state wl.json
python videograb.py extract <url> --browser --headful --dump-network   # watch it work
```

The browser is a *fetch strategy*, not a separate mode: pages come back
rendered and every ordinary extractor runs on them unchanged. Manifests, APIs
and the downloads themselves still go over plain HTTP with the browser's
cookies copied across, so it stays fast.

Useful flags: `--settle 5000` (wait longer for slow players), `--headful`
(watch it), `--no-scroll`, `--chromium /path/to/chrome`.

---

## Downloading

```bash
python videograb.py download <url> -o ./course            # best rendition per lesson
python videograb.py download <url> -o ./course --all      # every rendition
python videograb.py download <url> --min-height 720
python videograb.py download <url> --dry-run              # show filenames, fetch nothing
```

| Backend | Chosen for | Notes |
|---|---|---|
| `direct` | `.mp4` and friends | Range-resumes a partial file |
| `hls` | `.m3u8` | Native segment fetch, AES-128 decryption. Writes `.ts` - it concatenates, it does not remux, and the filename says so |
| `ffmpeg` | `.m3u8` / `.mpd` when ffmpeg is on PATH | Produces a real `.mp4`; preferred automatically |
| `ytdlp` | YouTube, Vimeo fallback, Loom, … | Passes your cookies and referer through |

Force one with `--backend`. Filenames come from `--template`, default
`{index:03d} - {title} [{label}].{ext}`; available fields are `{index}`
`{title}` `{label}` `{height}` `{kind}` `{id}` `{source}` `{ext}`. Names are
sanitised for Windows (reserved names included). Re-running skips files that
already exist unless you pass `--overwrite`.

---

## Adding a site

### 1. A YAML profile (no code)

Drop a file in `profiles/` or `~/.config/videograb/profiles/`:

```yaml
name: acme-academy
match:
  hosts: [learn.acme.com]
  url_patterns: ['/course/(\d+)']
follow:
  - selector: "a.lesson-link"                      # -> pages to visit
  - json: {markers: ['__NEXT_DATA__'],
           path: 'props.pageProps.lessons[*].url', title: name}
  - template: 'https://{host}/api/courses/{group1}/items'
    kind: api
videos:
  - selector: "video source"
    attr: src
  - regex: '"hlsUrl":"([^"]+)"'
    kind: hls
  - json: {path: '**.playback_url'}
    kind: hls
```

Rule types: `selector` (+`attr`), `regex` (capture group 1), `json`
(`markers` + a `a.b[*].c` / `**.key` path), `template` (fills in captures from
`url_patterns`). Rules are additive and a rule that matches nothing costs
nothing, so start broad and tighten. `profiles/_example.yaml` is a fully
annotated template; `python videograb.py profiles <url>` shows what matches.

### 2. A Python extractor (anything else)

```python
# mysite.py
from vgrab.registry import Extractor, register
from vgrab.models import Video, PAGE, HLS

@register
class MySite(Extractor):
    name = "mysite"
    priority = 20                      # lower runs first; default 50
    kinds = (PAGE,)

    def matches(self, page, ctx):
        return "mysite.com" in page.url

    def extract(self, page, ctx):
        for a in page.dom.select("a.lesson"):
            yield ctx.lead(page, a.get("href"), PAGE, title=a.text)
        for m in ctx.fetcher.fetch_json(f"{page.url}/api")["items"]:
            yield Video(url=m["hls"], kind=HLS, title=m["name"])
```

```bash
python videograb.py extract <url> --plugin mysite.py
```

`ctx` gives you the authenticated `fetcher`, `ctx.lead(...)` for correctly
resolved child leads, `ctx.opt(...)` for options, and `ctx.debug/warn`.
`page.dom` is a parsed DOM with a CSS-subset `select()`.

### 3. Or just point it at the links

```bash
python videograb.py extract <url> --follow '/lesson/\d+' --depth 3
python videograb.py extract <url> --follow-selector '.curriculum a'
```

---

## About the Whizlabs example

`profiles/whizlabs.yaml` targets
`business.whizlabs.com/learn/course/<slug>/<id>`, a Learnyst-hosted school.

**It was written without live access to that host** - the machine it was
developed on could not reach it - so the profile deliberately does not
hardcode an API contract that was never verified. It matches on the URL shape
and platform markers, harvests lesson links from the markup and from whatever
state blob the page ships, and never fabricates endpoints. The `learnyst`
extractor probes a short list of candidate curriculum endpoints (one cheap
request each, all failures silent; `--no-probe` disables this).

If a plain run finds nothing, the curriculum is being loaded client-side after
login, which is the common case. Browser mode does not care:

```bash
python videograb.py login https://business.whizlabs.com/ --storage-state wl.json
python videograb.py extract <course-url> --browser --storage-state wl.json --dump-network
```

`--dump-network` prints every media URL the page requested. Feeding those
patterns back into `profiles/whizlabs.yaml` turns the slow browser path into
a fast static one - that is exactly the workflow the profile format is for.

---

## What it will not do

- **Break DRM.** Widevine/FairPlay/PlayReady (VdoCipher, most paid streaming)
  are reported as DRM with no extraction path, not silently skipped. HLS
  AES-128 is *not* DRM and is handled.
- **Log in for you**, solve captchas, or store credentials.
- **Hammer a site.** `--delay` paces requests per host; `--max-pages` and
  `--depth` bound the crawl. Defaults are conservative.

Use it on content you are entitled to access. A login you hold does not
automatically grant redistribution rights, and many platforms' terms restrict
offline copies - that is between you and the site, and worth a look before
pointing this at a subscription service.

---

## Options

`python videograb.py <command> --help` for the full list. The ones that matter:

| | |
|---|---|
| `--depth N` | Hops from the start URL (default 2; a course usually wants 3) |
| `--max-pages` `--max-videos` `--max-lessons` | Bound the crawl |
| `--scope site\|host\|any` | How far page links may wander (default `site`) |
| `--follow REGEX` `--follow-selector CSS` | Follow links nothing recognised |
| `--deny REGEX` | Never fetch matching URLs |
| `--only` `--exclude` | Restrict which extractors run |
| `--delay SECONDS` | Pace requests per host |
| `-v` / `-vv` | Progress / full trace, on stderr (stdout stays pipeable) |
| `--save FILE` | Write the JSON result |

Exit codes: `0` found something, `2` found nothing, `1` error.

---

## Use as a library

```python
from vgrab import extract, Fetcher
from vgrab.auth import apply_auth
from vgrab.download import Downloader, pick_best

fetcher = Fetcher(delay=0.5)
apply_auth(fetcher, cookies="cookies.txt")

result = extract("https://example.com/course/1", fetcher=fetcher, max_depth=3)
for v in pick_best(result.videos):
    print(v.title, v.label, v.url)

Downloader(fetcher, out_dir="out").download(pick_best(result.videos)[0])
```

---

## Layout

```
videograb.py            entry point
vgrab/
  models.py             Lead / Video / Page / Result
  http.py               cookie-aware session, retries, Range support
  htmlmini.py           dependency-free DOM + CSS-subset selectors
  registry.py           the Extractor contract, @register, plugin loading
  pipeline.py           the crawl: BFS, dedupe, scope, manifest expansion
  profiles.py           YAML site profiles + JSON path evaluator
  auth.py               cookies from files, JSON, or your browser
  browser.py            Playwright fetch strategy + network sniffing
  download.py           direct / hls / ffmpeg / yt-dlp backends
  cli.py                argument parsing and output
  extractors/           generic, manifests, providers, lms, learnyst
profiles/               whizlabs.yaml, _example.yaml
tests/                  fixture site + 54 offline tests
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

No network needed - `tests/fixture_server.py` serves a miniature course site
(curriculum page, `<video>` lesson, jwplayer config with escaped URLs, an
iframe embed, JSON-LD, a JavaScript-only lesson, an HLS master with three
renditions, a genuinely AES-128-encrypted stream, and a DASH manifest). Run it
standalone to poke at it:

```bash
python tests/fixture_server.py
```

Browser tests skip themselves if Playwright is not installed.

## Optional extras

```bash
pip install playwright && playwright install chromium   # --browser, login
pip install yt-dlp                                      # YouTube et al.
pip install cryptography                                # AES-128 HLS without ffmpeg
pip install browser-cookie3                             # --cookies-from-browser
pip install pyyaml                                      # YAML profiles (JSON works without)
apt/brew install ffmpeg                                 # best HLS/DASH downloads
```
