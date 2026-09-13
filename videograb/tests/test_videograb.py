"""Offline test suite. No network required - everything runs against the
fixture site in fixture_server.py.

    python -m unittest discover -s tests -v
    python tests/test_videograb.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from fixture_server import FixtureSite, SEG_PLAIN                  # noqa: E402
from vgrab import Fetcher, extract                                 # noqa: E402
from vgrab.download import (Downloader, filename_for, pick_best,    # noqa: E402
                            safe_name)
from vgrab.htmlmini import parse                                   # noqa: E402
from vgrab.models import HLS, PROGRESSIVE, Video                   # noqa: E402
from vgrab.pipeline import Pipeline, registrable                   # noqa: E402
from vgrab.profiles import Profile, json_path, load as load_profiles  # noqa: E402
from vgrab.registry import build                                   # noqa: E402
from vgrab.util import classify, height_from_url, json_blobs, json_media_urls  # noqa: E402


def has_cryptography() -> bool:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401
        return True
    except BaseException:
        return False


class TestHtmlMini(unittest.TestCase):
    def test_selectors(self):
        doc = parse('<div class="a b"><a id="x" href="/1" data-q="720p">L1</a>'
                    '<a href="/2">L2</a></div><iframe src="//p.vimeo/1"></iframe>')
        self.assertEqual(len(doc.select("a[href]")), 2)
        self.assertEqual(doc.select_one("#x").get("data-q"), "720p")
        self.assertEqual(len(doc.select(".a a")), 2)
        self.assertEqual(len(doc.select("div.a > a, iframe")), 1)   # '>' unsupported
        self.assertEqual(doc.select_one('a[href$="2"]').text, "L2")
        self.assertEqual(len(doc.select('a[href^="/"]')), 2)

    def test_text_is_in_document_order(self):
        doc = parse("<p>one <b>two</b> three</p>")
        self.assertEqual(doc.select_one("p").text, "one two three")

    def test_malformed_markup_does_not_raise(self):
        doc = parse("<div><p>unclosed<div><span>x</div></p>")
        self.assertTrue(doc.select("span"))

    def test_script_and_meta(self):
        doc = parse('<meta property="og:video" content="/a.mp4">'
                    '<script type="application/json">{"k":1}</script>')
        self.assertEqual(doc.meta("og:video"), "/a.mp4")
        self.assertEqual(list(doc.script_texts("application/json")), ['{"k":1}'])


class TestUtil(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("https://c/a.m3u8")[0], "hls")
        self.assertEqual(classify("https://c/a.mpd")[0], "dash")
        self.assertEqual(classify("https://c/a.mp4?token=x")[0], "progressive")
        self.assertEqual(classify("https://c/page.html")[0], None)

    def test_lesson_path_is_not_a_manifest(self):
        # Regression: "/learn/lesson/hls/2" once classified as an HLS stream.
        self.assertEqual(classify("https://x/learn/lesson/hls/2")[0], None)
        self.assertEqual(classify("https://x/hls/master.m3u8")[0], "hls")

    def test_height_from_url(self):
        self.assertEqual(height_from_url("https://c/hls/1080p/i.m3u8"), 1080)
        self.assertEqual(height_from_url("https://c/v_720.mp4"), 720)
        self.assertIsNone(height_from_url("https://c/v.mp4"))

    def test_js_object_literal_is_parsed(self):
        js = '''jwplayer("p").setup({playlist:[{title:'L1',duration:754,
               sources:[{file:"https:\\/\\/c\\/m.m3u8",label:"720p"}]}]});'''
        blobs = list(json_blobs(js))
        self.assertTrue(blobs)
        found = list(json_media_urls(blobs[0]))
        self.assertEqual(found[0][0], "https://c/m.m3u8")
        # The title/duration sit on the parent playlist entry, not the source.
        self.assertEqual(found[0][2].get("title"), "L1")
        self.assertEqual(found[0][2].get("duration"), 754)


class TestProfiles(unittest.TestCase):
    def test_json_path(self):
        blob = {"c": {"lessons": [{"url": "/a"}, {"url": "/b"}]},
                "deep": {"x": {"hls_url": "https://c/x.m3u8"}}}
        self.assertEqual(list(json_path(blob, "c.lessons[*].url")), ["/a", "/b"])
        self.assertEqual(list(json_path(blob, "**.hls_url")), ["https://c/x.m3u8"])
        self.assertEqual(list(json_path(blob, "c.lessons[1].url")), ["/b"])
        self.assertEqual(list(json_path(blob, "nope.missing")), [])

    def test_builtin_whizlabs_profile_loads_and_matches(self):
        profiles = load_profiles()
        names = {p.name for p in profiles}
        self.assertIn("whizlabs", names)
        wl = next(p for p in profiles if p.name == "whizlabs")
        url = ("https://business.whizlabs.com/learn/course/"
               "besa-generative-ai-labs-subscription/4418")
        self.assertTrue(wl.matches_url(url))
        self.assertFalse(wl.matches_url("https://example.com/learn/course/x/1"))
        caps = wl.captures(url)
        self.assertEqual(caps["group2"], "4418")

    def test_inline_profile_drives_extraction(self):
        prof = Profile({
            "name": "t", "match": {"host_patterns": ["127.0.0.1"]},
            "videos": [{"regex": r'"(/media/[^"]+\.mp4)"', "kind": "progressive"}],
        })
        with FixtureSite() as site:
            fetcher = Fetcher()
            pipe = Pipeline(fetcher=fetcher, extractors=[prof.build()], max_depth=0)
            page_url = site.url("/embed/player?v=x")
            res = pipe.run(page_url)
            self.assertTrue(any(v.url.endswith("rag-1080.mp4") for v in res.videos))


class TestPipelineScope(unittest.TestCase):
    def test_registrable(self):
        self.assertEqual(registrable("business.whizlabs.com"), "whizlabs.com")
        self.assertEqual(registrable("a.b.co.uk"), "b.co.uk")
        self.assertEqual(registrable("example.com"), "example.com")


class TestExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = FixtureSite()
        cls.site.__enter__()
        cls.result = extract(cls.site.course_url, fetcher=Fetcher(), max_depth=3)

    @classmethod
    def tearDownClass(cls):
        cls.site.__exit__()

    def urls(self):
        return {v.url.replace(self.site.base, "") for v in self.result.videos}

    def test_no_errors(self):
        self.assertEqual(self.result.errors, [])

    def test_finds_html5_video_sources(self):
        self.assertIn("/media/lesson1.mp4", self.urls())
        self.assertIn("/media/lesson1-480.mp4", self.urls())

    def test_quality_label_becomes_height(self):
        v = next(v for v in self.result.videos if v.url.endswith("lesson1.mp4"))
        self.assertEqual(v.height, 720)          # from <source label="720p">

    def test_finds_jwplayer_config_with_escaped_url(self):
        self.assertIn("/media/master.m3u8", self.urls())

    def test_expands_hls_master_into_variants(self):
        variants = {v.height for v in self.result.videos if v.meta.get("variant")}
        self.assertEqual(variants, {360, 720, 1080})
        top = next(v for v in self.result.videos if v.height == 1080
                   and v.meta.get("variant"))
        self.assertEqual(top.bitrate, 5200000)
        self.assertEqual(top.codecs, "avc1.640028,mp4a.40.2")

    def test_follows_iframe_embed(self):
        self.assertIn("/media/rag-1080.mp4", self.urls())

    def test_reads_json_ld(self):
        v = next(v for v in self.result.videos if v.url.endswith("finetune-hd.mp4"))
        self.assertEqual(v.duration, 1269.0)     # PT21M9S
        self.assertEqual(v.title, "Fine-tuning lab")

    def test_embed_inherits_lesson_title_not_player_title(self):
        v = next(v for v in self.result.videos if v.url.endswith("rag-1080.mp4"))
        self.assertEqual(v.title, "RAG lab")

    def test_crawls_lessons_from_embedded_state(self):
        visited = {u.replace(self.site.base, "") for u in self.result.visited}
        self.assertIn("/learn/lesson/bonus/9", visited)   # only in __INITIAL_STATE__

    def test_does_not_crawl_login_or_pricing(self):
        visited = {u.replace(self.site.base, "") for u in self.result.visited}
        self.assertNotIn("/login", visited)
        self.assertNotIn("/pricing", visited)

    def test_audio_rendition_is_flagged(self):
        v = next(v for v in self.result.videos if v.url.endswith("audio_en.m3u8"))
        self.assertEqual(v.meta.get("track_type"), "audio")

    def test_scope_blocks_offsite_pages(self):
        pipe = Pipeline(fetcher=Fetcher(), scope="site")
        from vgrab.models import Lead, PAGE, EMBED
        self.assertFalse(pipe.in_scope(Lead(url="https://evil.com/x", kind=PAGE),
                                       "https://business.whizlabs.com/c"))
        self.assertTrue(pipe.in_scope(Lead(url="https://cdn.whizlabs.com/x", kind=PAGE),
                                      "https://business.whizlabs.com/c"))
        self.assertTrue(pipe.in_scope(Lead(url="https://cdn.other.com/x", kind=EMBED),
                                      "https://business.whizlabs.com/c"))


class TestDashAndAuth(unittest.TestCase):
    def test_dash_manifest(self):
        with FixtureSite() as site:
            res = extract(site.url("/media/manifest.mpd"), fetcher=Fetcher(),
                          max_depth=1)
            heights = {v.height for v in res.videos if v.height}
            self.assertEqual(heights, {480, 1080})
            v = next(v for v in res.videos if v.height == 1080)
            self.assertEqual(v.bitrate, 5000000)
            self.assertEqual(v.duration, 754.0)         # PT12M34S

    def test_auth_required_site(self):
        with FixtureSite(require_cookie="session=letmein") as site:
            res = extract(site.course_url, fetcher=Fetcher(), max_depth=2)
            self.assertEqual(res.videos, [])
            self.assertTrue(any("authentication" in n for n in res.notes))

            fetcher = Fetcher()
            from vgrab.auth import make_cookie
            fetcher.cookiejar.set_cookie(
                make_cookie("session", "letmein", "127.0.0.1"))
            res2 = extract(site.course_url, fetcher=fetcher, max_depth=3)
            self.assertTrue(res2.videos)

    def test_tolerant_cookie_file(self):
        from http.cookiejar import CookieJar
        from vgrab.auth import load_netscape, save_netscape
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.txt")
            # Both quirks real exporters produce: #HttpOnly_ and a
            # domain_specified flag that MozillaCookieJar rejects outright.
            with open(path, "w") as fh:
                fh.write("# Netscape HTTP Cookie File\n"
                         "#HttpOnly_business.whizlabs.com\tTRUE\t/\tTRUE\t"
                         "2000000000\tsession\tabc\n")
            jar = CookieJar()
            self.assertEqual(load_netscape(path, jar), 1)
            out = os.path.join(d, "out.txt")
            save_netscape(jar, out)
            jar2 = CookieJar()
            self.assertEqual(load_netscape(out, jar2), 1)


class TestDownload(unittest.TestCase):
    def test_safe_names(self):
        self.assertEqual(safe_name('a<b>:c/d"e|f?g*h'), "a_b__c_d_e_f_g_h")
        self.assertTrue(safe_name("CON.mp4").startswith("_"))

    def test_pick_best_groups_variants_with_their_lesson(self):
        vids = [
            Video(url="https://s/l1", kind=HLS, page_url="https://s/lesson1",
                  meta={"master": True}),
            Video(url="https://s/360", kind=HLS, height=360, page_url="https://s/l1",
                  meta={"variant": True}),
            Video(url="https://s/1080", kind=HLS, height=1080, page_url="https://s/l1",
                  meta={"variant": True}),
            Video(url="https://s/aud", kind=HLS, page_url="https://s/l1",
                  meta={"track_type": "audio"}),
        ]
        best = pick_best(vids)
        self.assertEqual([v.url for v in best], ["https://s/1080"])

    def test_direct_download_with_resume(self):
        with FixtureSite() as site, tempfile.TemporaryDirectory() as out:
            dl = Downloader(Fetcher(), out_dir=out)
            v = Video(url=site.url("/media/lesson1.mp4"), kind=PROGRESSIVE,
                      title="Intro", height=720)
            path = dl.download(v, 1)
            self.assertTrue(os.path.exists(path))
            size = os.path.getsize(path)
            self.assertGreater(size, 1000)
            # A partially written .part must be resumed, not restarted.
            os.remove(path)
            with open(path + ".part", "wb") as fh:
                fh.write(b"\x00" * 100)
            path2 = dl.download(v, 1)
            self.assertEqual(os.path.getsize(path2), size)

    def test_native_hls_picks_top_variant(self):
        with FixtureSite() as site, tempfile.TemporaryDirectory() as out:
            dl = Downloader(Fetcher(), out_dir=out, backend="hls")
            v = Video(url=site.url("/media/master.m3u8"), kind=HLS, title="lesson")
            path = dl.download(v, 1)
            self.assertTrue(path.endswith(".ts"))       # not a remuxed mp4
            with open(path, "rb") as fh:
                self.assertIn(b"TS-SEGMENT-1", fh.read())

    @unittest.skipUnless(has_cryptography(), "cryptography not available")
    def test_aes128_hls_is_decrypted_correctly(self):
        with FixtureSite() as site, tempfile.TemporaryDirectory() as out:
            dl = Downloader(Fetcher(), out_dir=out, backend="hls")
            v = Video(url=site.url("/media/enc/index.m3u8"), kind=HLS, title="enc")
            path = dl.download(v, 1)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), b"".join(SEG_PLAIN))

    def test_drm_is_refused_with_a_clear_message(self):
        from vgrab.download import DownloadError
        dl = Downloader(Fetcher(), out_dir=".")
        v = Video(url="https://x/drm", meta={"drm": True, "provider": "vdocipher"})
        with self.assertRaises(DownloadError) as cm:
            dl.download(v, 1)
        self.assertIn("DRM", str(cm.exception))


class TestRegistry(unittest.TestCase):
    def test_importing_vgrab_registers_everything(self):
        # Regression: build() used to return only the extractors whose module
        # happened to have been imported, which looked like "found nothing".
        import vgrab
        from vgrab.registry import all_extractor_classes
        names = {c.name for c in all_extractor_classes()}
        for expected in ("generic", "hls", "dash", "course", "youtube",
                         "vimeo", "learnyst", "network", "json"):
            self.assertIn(expected, names)
        self.assertNotIn("network", {e.name for e in build()})   # opt-in only

    def test_only_and_exclude(self):
        self.assertEqual([e.name for e in build(only=["hls"])], ["hls"])
        self.assertNotIn("generic", [e.name for e in build(exclude=["generic"])])


class TestProviders(unittest.TestCase):
    """Pure URL logic, so it is testable without touching those services."""

    def page(self, url, text="<html><title>T</title></html>"):
        from vgrab.models import Lead, Page, EMBED
        return Page(url=url, requested_url=url, status=200,
                    headers={"content-type": "text/html"}, text=text,
                    lead=Lead(url=url, kind=EMBED, title="Lesson 3"))

    def run_one(self, cls, url):
        from vgrab.registry import Context
        ex = cls()
        page = self.page(url)
        ctx = Context(Fetcher())
        if not ex.matches(page, ctx):
            return None
        return list(ex.extract(page, ctx))

    def test_youtube_forms(self):
        from vgrab.extractors.providers import YouTube
        for url in ("https://www.youtube.com/embed/dQw4w9WgXcQ",
                    "https://youtu.be/dQw4w9WgXcQ",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"):
            got = self.run_one(YouTube, url)
            self.assertTrue(got, url)
            self.assertEqual(got[0].meta["video_id"], "dQw4w9WgXcQ")
            self.assertEqual(got[0].kind, "external")

    def test_cloudflare_stream_manifest_is_derived(self):
        from vgrab.extractors.providers import CloudflareStream
        got = self.run_one(
            CloudflareStream,
            "https://iframe.videodelivery.net/abcdef0123456789abcdef0123456789")
        urls = {v.url for v in got}
        self.assertTrue(any(u.endswith("/manifest/video.m3u8") for u in urls))
        self.assertTrue(any(u.endswith("/manifest/video.mpd") for u in urls))

    def test_bunny_and_mux(self):
        from vgrab.extractors.providers import BunnyStream, MuxStream
        got = self.run_one(BunnyStream,
                           "https://iframe.mediadelivery.net/embed/12345/abcd-efgh-1234")
        self.assertTrue(got[0].url.endswith("/playlist.m3u8"))
        got = self.run_one(MuxStream, "https://stream.mux.com/AbCdEf12345.m3u8")
        self.assertEqual(got[0].kind, "hls")

    def test_drm_provider_is_reported_not_silently_dropped(self):
        from vgrab.extractors.providers import VdoCipher
        got = self.run_one(VdoCipher, "https://player.vdocipher.com/v2/?otp=xyz")
        self.assertTrue(got[0].meta.get("drm"))


class TestLearnyst(unittest.TestCase):
    def page(self, url, text):
        from vgrab.models import Lead, PAGE, Page
        return Page(url=url, requested_url=url, status=200,
                    headers={"content-type": "text/html"}, text=text,
                    lead=Lead(url=url, kind=PAGE))

    def test_reads_curriculum_out_of_state_blob(self):
        from vgrab.extractors.learnyst import Learnyst
        from vgrab.models import Lead
        from vgrab.registry import Context
        html = ("<html><body><script>window.__NST = "
                '{"course":{"lessons":[{"id":7,"title":"Lab 1",'
                '"url":"/learn/lesson/lab-1/7"}]}};</script></body></html>')
        page = self.page("https://business.whizlabs.com/learn/course/besa/4418", html)
        ex = Learnyst()
        ctx = Context(Fetcher())
        self.assertTrue(ex.matches(page, ctx))
        leads = [x for x in ex.extract(page, ctx) if isinstance(x, Lead)]
        self.assertIn("https://business.whizlabs.com/learn/lesson/lab-1/7",
                      [l.url for l in leads])
        self.assertEqual(leads[0].title, "Lab 1")

    def test_never_leaves_the_site(self):
        from vgrab.extractors.learnyst import Learnyst
        from vgrab.models import Lead
        from vgrab.registry import Context
        html = ('<html><body><script>window.__NST = {"lessons":'
                '[{"id":1,"url":"https://evil.example/learn/lesson/x/1"}]};'
                "</script></body></html>")
        page = self.page("https://business.whizlabs.com/learn/course/besa/4418", html)
        leads = [x for x in Learnyst().extract(page, Context(Fetcher()))
                 if isinstance(x, Lead)]
        self.assertEqual(leads, [])

    def test_warns_when_curriculum_is_client_side(self):
        from vgrab.extractors.learnyst import Learnyst
        from vgrab.registry import Context
        msgs = []
        ctx = Context(Fetcher(), options={"probe_api": False},
                      log=lambda lvl, m: msgs.append((lvl, m)))
        page = self.page("https://business.whizlabs.com/learn/course/besa/4418",
                         "<html><body><div id=app></div></body></html>")
        list(Learnyst().extract(page, ctx))
        self.assertTrue(any("--browser" in m for lvl, m in msgs if lvl == "warn"))


class TestCLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "videograb.py"), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def test_json_output(self):
        with FixtureSite() as site:
            p = self.run_cli("extract", site.course_url, "--depth", "3", "--json")
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(p.stdout)
            self.assertGreater(data["count"], 5)
            self.assertTrue(all("url" in v for v in data["videos"]))

    def test_urls_only(self):
        with FixtureSite() as site:
            p = self.run_cli("extract", site.course_url, "--urls")
            self.assertTrue(all(l.startswith("http") for l in p.stdout.split()))

    def test_url_shorthand_defaults_to_extract(self):
        with FixtureSite() as site:
            p = self.run_cli(site.url("/learn/lesson/intro/1"), "--urls")
            self.assertIn("lesson1.mp4", p.stdout)

    def test_exit_code_2_when_nothing_found(self):
        with FixtureSite() as site:
            p = self.run_cli("extract", site.url("/pricing"))
            self.assertEqual(p.returncode, 2)
            self.assertIn("--browser", p.stderr)

    def test_download_dry_run(self):
        with FixtureSite() as site, tempfile.TemporaryDirectory() as out:
            p = self.run_cli("download", site.course_url, "--depth", "3",
                             "-o", out, "--dry-run")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("dry-run", p.stdout)
            self.assertEqual(os.listdir(out), [])

    def test_profiles_and_extractors_commands(self):
        p = self.run_cli("profiles")
        self.assertIn("whizlabs", p.stdout)
        p = self.run_cli("extractors")
        self.assertIn("generic", p.stdout)
        self.assertIn("network", p.stdout)


class TestPlugin(unittest.TestCase):
    def test_python_plugin_is_loaded_and_used(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = os.path.join(d, "mysite.py")
            with open(plugin, "w") as fh:
                fh.write(
                    "from vgrab.registry import Extractor, register\n"
                    "from vgrab.models import Video, PAGE\n"
                    "@register\n"
                    "class Mine(Extractor):\n"
                    "    name = 'mine'\n"
                    "    priority = 5\n"
                    "    kinds = (PAGE,)\n"
                    "    def matches(self, page, ctx):\n"
                    "        return 'pricing' in page.url\n"
                    "    def extract(self, page, ctx):\n"
                    "        yield Video(url='https://cdn/secret.mp4', title='secret')\n")
            with FixtureSite() as site:
                p = subprocess.run(
                    [sys.executable, os.path.join(ROOT, "videograb.py"), "extract",
                     site.url("/pricing"), "--plugin", plugin, "--urls"],
                    capture_output=True, text=True, cwd=ROOT)
                self.assertIn("https://cdn/secret.mp4", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
