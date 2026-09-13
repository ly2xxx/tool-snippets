"""Browser-backend tests. Skipped unless Playwright and a Chromium are present.

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from fixture_server import FixtureSite          # noqa: E402
from vgrab import Fetcher                       # noqa: E402
from vgrab.pipeline import Pipeline             # noqa: E402
from vgrab.registry import all_extractor_classes, build   # noqa: E402


def browser_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    from vgrab.browser import find_chromium

    return find_chromium() is not None or True   # let launch decide


@unittest.skipUnless(browser_available(), "playwright not installed")
class TestBrowserBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from vgrab.browser import BrowserFetcher, PlaywrightUnavailable

        cls.site = FixtureSite()
        cls.site.__enter__()
        cls.fetcher = BrowserFetcher(headless=True, settle_ms=1200)
        try:
            cls.fetcher.start()
        except PlaywrightUnavailable as e:
            cls.site.__exit__()
            raise unittest.SkipTest(f"no usable Chromium: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.fetcher.close()
        cls.site.__exit__()

    def all_extractors(self):
        names = [c.name for c in all_extractor_classes()
                 if c.enabled_by_default or c.name == "network"]
        return build(only=names)

    def test_static_fetch_finds_nothing_on_a_spa_page(self):
        res = Pipeline(fetcher=Fetcher(), max_depth=1).run(
            self.site.url("/learn/lesson/spa/5"))
        self.assertEqual(res.videos, [])

    def test_browser_renders_and_sniffs(self):
        res = Pipeline(fetcher=self.fetcher, extractors=self.all_extractors(),
                       max_depth=2).run(self.site.url("/learn/lesson/spa/5"))
        urls = {v.url.replace(self.site.base, "") for v in res.videos}
        # From the DOM the script built:
        self.assertIn("/media/agents-720.mp4", urls)
        # From the network request the "player" made:
        self.assertIn("/media/master.m3u8", urls)
        # And the sniffed manifest still gets expanded into renditions:
        self.assertTrue({v.height for v in res.videos if v.meta.get("variant")})

    def test_sniffed_videos_are_marked(self):
        res = Pipeline(fetcher=self.fetcher, extractors=self.all_extractors(),
                       max_depth=1).run(self.site.url("/learn/lesson/spa/5"))
        self.assertTrue(any(v.meta.get("captured") for v in res.videos))

    def test_segments_are_not_reported_as_videos(self):
        res = Pipeline(fetcher=self.fetcher, extractors=self.all_extractors(),
                       max_depth=2).run(self.site.url("/learn/lesson/spa/5"))
        self.assertFalse([v for v in res.videos if v.url.endswith(".ts")])


@unittest.skipUnless(browser_available(), "playwright not installed")
class TestBrowserCLI(unittest.TestCase):
    def test_cli_browser_flag(self):
        with FixtureSite() as site:
            p = subprocess.run(
                [sys.executable, os.path.join(ROOT, "videograb.py"), "extract",
                 site.url("/learn/lesson/spa/5"), "--browser", "--settle", "1200",
                 "--urls"],
                capture_output=True, text=True, cwd=ROOT)
            if "could not launch Chromium" in p.stderr:
                self.skipTest("no usable Chromium")
            self.assertEqual(p.returncode, 0, p.stderr[-800:])
            self.assertIn("agents-720.mp4", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
