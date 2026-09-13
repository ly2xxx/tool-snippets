"""A miniature course site used to test the extractors offline.

It deliberately reproduces the awkward shapes seen in the wild: a curriculum
page, a plain <video>, a jwplayer JS config with escaped URLs, a same-origin
iframe embed, JSON-LD, an HLS master with variants, and a DASH manifest.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple

HTML = "text/html; charset=utf-8"
JSON = "application/json"
M3U8 = "application/vnd.apple.mpegurl"
MPD = "application/dash+xml"

COURSE = """<!doctype html><html><head>
<title>BESA Generative AI Labs Subscription</title>
<meta property="og:title" content="Generative AI Labs">
</head><body>
<h1>Course curriculum</h1>
<ul class="curriculum">
  <li><a href="/learn/lesson/intro/1">1. Introduction</a></li>
  <li><a href="/learn/lesson/hls/2">2. Prompt engineering lab</a></li>
  <li><a href="/learn/lesson/embed/3">3. RAG lab</a></li>
  <li><a href="/learn/lesson/jsonld/4">4. Fine-tuning lab</a></li>
  <li><a href="/learn/lesson/spa/5">5. Agents lab</a></li>
</ul>
<a href="/pricing">Pricing</a><a href="/login">Login</a>
<script>window.__INITIAL_STATE__ = {"course":{"id":4418,"lessons":[
  {"id":1,"title":"Introduction","url":"/learn/lesson/intro/1"},
  {"id":9,"title":"Bonus","url":"/learn/lesson/bonus/9"}]}};</script>
</body></html>"""

LESSON_VIDEO = """<!doctype html><html><head><title>1. Introduction</title></head>
<body><h2>Introduction</h2>
<video id="p" poster="/media/poster.jpg" controls>
  <source src="/media/lesson1.mp4" type="video/mp4" label="720p">
  <source src="/media/lesson1-480.mp4" type="video/mp4" label="480p">
  <track src="/media/lesson1.vtt" kind="subtitles" srclang="en">
</video></body></html>"""

LESSON_HLS = """<!doctype html><html><head><title>2. Prompt engineering lab</title></head>
<body><div id="player"></div>
<script>
jwplayer("player").setup({
  playlist: [{ title: 'Prompt engineering lab', duration: 754,
    sources: [{ file: "\\/\\/HOST\\/media\\/master.m3u8", type: "hls" }] }],
  width: "100%"
});
</script></body></html>"""

LESSON_EMBED = """<!doctype html><html><head><title>3. RAG lab</title></head>
<body><h2>RAG lab</h2>
<iframe src="/embed/player?v=rag01" title="RAG lab" allowfullscreen></iframe>
</body></html>"""

LESSON_JSONLD = """<!doctype html><html><head><title>4. Fine-tuning lab</title>
<meta property="og:video" content="/media/finetune.mp4">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"VideoObject","name":"Fine-tuning lab",
 "duration":"PT21M9S","contentUrl":"/media/finetune-hd.mp4",
 "embedUrl":"/embed/player?v=ft01"}
</script></head><body><h2>Fine-tuning lab</h2></body></html>"""

# A lesson whose media only exists after JavaScript runs - the exact case
# that defeats static scraping and justifies the browser backend.
LESSON_SPA = """<!doctype html><html><head><title>5. Agents lab</title></head>
<body><div id="mount">loading...</div>
<script>
fetch('/api/lesson/5').then(r => r.json()).then(function (d) {
  var v = document.createElement('video');
  v.setAttribute('src', d.sources[0].src);
  v.setAttribute('data-title', d.title);
  document.getElementById('mount').replaceChildren(v);
  return fetch(d.sources[1].src);   // the player pulling its HLS manifest
});
</script></body></html>"""

LESSON_SPA_API = """{"title":"Agents lab","duration":900,
 "sources":[{"src":"/media/agents-720.mp4","height":720},
            {"src":"/media/master.m3u8","type":"hls"}]}"""

EMBED_PLAYER = """<!doctype html><html><head><title>Player</title></head><body>
<script type="application/json" id="cfg">
{"media":{"title":"RAG lab","duration":1330,
 "sources":[{"src":"/media/master.m3u8","type":"application/x-mpegURL"},
            {"src":"/media/rag-1080.mp4","type":"video/mp4","height":1080,"bitrate":4200}]}}
</script></body></html>"""

MASTER_M3U8 = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",URI="/media/audio_en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,AVERAGE-BANDWIDTH=750000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
/media/360p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",FRAME-RATE=30.000
/media/720p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5200000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
/media/1080p/index.m3u8
"""

MEDIA_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXTINF:4.5,
seg3.ts
#EXT-X-ENDLIST
"""

# A real AES-128 stream: segments below are genuinely encrypted with the key
# served at /media/enc/key.bin, so the native HLS downloader has to decrypt
# them correctly to reproduce SEG_PLAIN.
ENC_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI="/media/enc/key.bin"
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXTINF:6.0,
seg3.ts
#EXT-X-ENDLIST
"""

AES_KEY = bytes(range(16))
SEG_PLAIN = [f"SEGMENT-{i}-".encode() + bytes([i]) * 200 for i in (1, 2, 3)]


def aes_encrypt(data: bytes, key: bytes, seq: int) -> bytes:
    """AES-128-CBC + PKCS7, IV derived from the media sequence number - the
    default when a playlist's EXT-X-KEY carries no IV attribute."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(seq.to_bytes(16, "big"))).encryptor()
    return enc.update(data) + enc.finalize()


MPD_XML = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT12M34S" type="static">
  <Period>
    <AdaptationSet mimeType="video/mp4" lang="en">
      <Representation id="v0" width="854" height="480" bandwidth="1200000" codecs="avc1.4d401e">
        <BaseURL>/media/dash-480.mp4</BaseURL>
      </Representation>
      <Representation id="v1" width="1920" height="1080" bandwidth="5000000" codecs="avc1.640028">
        <BaseURL>/media/dash-1080.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

MP4_BYTES = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
             + b"\x00\x00\x00\x08free" + b"\xde\xad\xbe\xef" * 512)


def routes(host: str) -> Dict[str, Tuple[str, object]]:
    return {
        "/learn/course/besa-generative-ai-labs-subscription/4418": (HTML, COURSE),
        "/learn/lesson/intro/1": (HTML, LESSON_VIDEO),
        "/learn/lesson/hls/2": (HTML, LESSON_HLS.replace("HOST", host)),
        "/learn/lesson/embed/3": (HTML, LESSON_EMBED),
        "/learn/lesson/jsonld/4": (HTML, LESSON_JSONLD),
        "/learn/lesson/bonus/9": (HTML, LESSON_VIDEO),
        "/learn/lesson/spa/5": (HTML, LESSON_SPA),
        "/api/lesson/5": (JSON, LESSON_SPA_API),
        "/media/agents-720.mp4": ("video/mp4", MP4_BYTES),
        "/embed/player": (HTML, EMBED_PLAYER),
        "/media/master.m3u8": (M3U8, MASTER_M3U8),
        "/media/360p/index.m3u8": (M3U8, MEDIA_M3U8),
        "/media/720p/index.m3u8": (M3U8, MEDIA_M3U8),
        "/media/1080p/index.m3u8": (M3U8, MEDIA_M3U8),
        "/media/audio_en.m3u8": (M3U8, MEDIA_M3U8),
        "/media/manifest.mpd": (MPD, MPD_XML),
        "/media/enc/index.m3u8": (M3U8, ENC_M3U8),
        "/media/enc/key.bin": ("application/octet-stream", AES_KEY),
        "/media/lesson1.mp4": ("video/mp4", MP4_BYTES),
        "/media/lesson1-480.mp4": ("video/mp4", MP4_BYTES),
        "/media/finetune.mp4": ("video/mp4", MP4_BYTES),
        "/media/finetune-hd.mp4": ("video/mp4", MP4_BYTES),
        "/media/rag-1080.mp4": ("video/mp4", MP4_BYTES),
        "/pricing": (HTML, "<html><title>Pricing</title><body>no video</body></html>"),
        "/login": (HTML, "<html><title>Login</title><body>no video</body></html>"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FixtureLMS/1.0"
    require_cookie = None          # set on the server instance
    hits = None

    def log_message(self, *a):     # keep test output clean
        pass

    SEG_RE = __import__("re").compile(r"^/media/(?P<dir>[\w/]+)/seg(?P<n>\d+)\.ts$")

    def _route(self):
        path = self.path.split("?")[0]
        hit = self.server.routes.get(path)
        if hit is not None:
            return hit
        m = self.SEG_RE.match(path)
        if m:
            n = int(m.group("n"))
            if m.group("dir") == "enc":
                try:
                    plain = SEG_PLAIN[n - 1]
                except IndexError:
                    return None
                return ("video/mp2t", aes_encrypt(plain, AES_KEY, n - 1))
            return ("video/mp2t", f"TS-SEGMENT-{n}".encode() + b"\x47" * 188)
        return None

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False):
        self.server.hits.append(self.path)
        if self.server.require_cookie:
            cookies = self.headers.get("Cookie", "")
            if self.server.require_cookie not in cookies:
                self.send_response(403)
                self.send_header("Content-Type", HTML)
                self.end_headers()
                if not head_only:
                    self.wfile.write(b"<html><body>Please log in</body></html>")
                return
        route = self._route()
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", HTML)
            self.end_headers()
            if not head_only:
                self.wfile.write(b"not found")
            return
        ctype, body = route
        data = body.encode("utf-8") if isinstance(body, str) else body
        start = 0
        end = len(data) - 1
        status = 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng.split("=", 1)[1]
            lo, _, hi = spec.partition("-")
            start = int(lo or 0)
            end = int(hi) if hi else len(data) - 1
            end = min(end, len(data) - 1)
            status = 206
        chunk = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        if not head_only:
            self.wfile.write(chunk)


class FixtureSite:
    """Context manager returning the base URL of a running fixture site."""

    def __init__(self, require_cookie: str = None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.httpd.routes = routes(f"127.0.0.1:{self.port}")
        self.httpd.require_cookie = require_cookie
        self.httpd.hits = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def hits(self):
        return self.httpd.hits

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return self.base + path

    @property
    def course_url(self) -> str:
        return self.url("/learn/course/besa-generative-ai-labs-subscription/4418")


if __name__ == "__main__":  # manual poking: python tests/fixture_server.py
    import time

    site = FixtureSite()
    site.__enter__()
    print("fixture site:", site.course_url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        site.__exit__()
