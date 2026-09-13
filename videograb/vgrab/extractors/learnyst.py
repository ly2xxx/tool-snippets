"""Learnyst-hosted schools, including business.whizlabs.com.

Status, stated plainly: this extractor was written without live access to
business.whizlabs.com (the sandbox it was built in blocks that host), so it
does *not* hardcode an API contract it cannot verify. Instead it does three
things that hold regardless of the exact backend:

  1. Detects the platform from page markers and the /learn/course/<slug>/<id>
     URL shape, and records course id/slug as metadata.
  2. Harvests the curriculum from whatever embedded state the page ships
     (Learnyst renders lesson lists into inline JSON), following only URLs
     that actually appear in that JSON - it never fabricates endpoints.
  3. Optionally probes a short list of candidate curriculum endpoints, all
     failures silent, and hands any JSON it gets to the JSON extractor.

If all three come up empty - which is what happens when the lesson list is
rendered client-side after an authenticated XHR - it says so and points at
`--browser`, which drives a real logged-in browser and always works.
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional

from ..http import absolute, host_of
from ..models import API, PAGE, Lead, Page
from ..registry import Context, Extractor, register
from ..util import json_blobs

COURSE_URL = re.compile(r"/learn/course/(?P<slug>[^/]+)/(?P<id>\d+)")
LESSON_URL = re.compile(r"/learn/(?:lesson|topic|content|video)/", re.I)
MARKERS = ("learnyst", "nst-app", "lstAppData", "window.__NST", "learnyst.com")

# Keys that, in LMS state blobs, hold the list of lessons.
CURRICULUM_KEYS = ("lessons", "topics", "sections", "chapters", "contents",
                   "curriculum", "modules", "units", "items", "course_contents")
# Keys holding a per-lesson link.
LINK_KEYS = ("url", "slug", "permalink", "link", "path", "web_url", "share_url")


@register
class Learnyst(Extractor):
    name = "learnyst"
    priority = 25
    kinds = (PAGE,)

    def matches(self, page: Page, ctx: Context) -> bool:
        if not page.is_html:
            return False
        if COURSE_URL.search(page.url) or LESSON_URL.search(page.url):
            return True
        head = (page.text or "")[:400_000].lower()
        return any(m.lower() in head for m in MARKERS)

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        m = COURSE_URL.search(page.url)
        course_id = m.group("id") if m else None
        slug = m.group("slug") if m else None
        host = host_of(page.url)
        if course_id:
            ctx.debug(f"learnyst course id={course_id} slug={slug} host={host}")

        found_any = False

        # 1. Curriculum out of embedded state ------------------------------
        for lead in self._leads_from_state(page, ctx):
            found_any = True
            yield lead

        # 2. Lesson links already in the markup ----------------------------
        for a in page.dom.select("a[href]"):
            href = a.get("href") or ""
            url = absolute(page.url, href)
            if LESSON_URL.search(url) and host_of(url) == host:
                found_any = True
                yield ctx.lead(page, url, PAGE,
                               title=(a.get("title") or a.text or "").strip()[:200] or None,
                               meta={"platform": "learnyst"})

        # 3. Candidate curriculum endpoints --------------------------------
        if course_id and not found_any and ctx.opt("probe_api", True):
            for url in self._candidate_endpoints(host, course_id, slug):
                if self._is_json(ctx, url, page):
                    found_any = True
                    yield ctx.lead(page, url, API,
                                   meta={"platform": "learnyst", "probed": True})

        if not found_any and course_id:
            ctx.warn(
                "learnyst: course page recognised but no curriculum found in the "
                "server-rendered HTML. The lesson list is almost certainly loaded "
                "by an authenticated XHR after page load - rerun with --browser "
                "(and --cookies/--storage-state for your logged-in session).")

    # -- helpers -----------------------------------------------------------
    def _leads_from_state(self, page: Page, ctx: Context) -> Iterator[Lead]:
        """Pull lesson URLs out of inline JSON, without inventing any."""
        seen = set()
        for body in page.dom.script_texts():
            if len(body) > 4_000_000:
                continue
            for blob in json_blobs(body, ("window.__NST", "lstAppData",
                                          "__INITIAL_STATE__", "__NEXT_DATA__",
                                          "courseData", "curriculum")):
                for node in _iter_lesson_nodes(blob):
                    url = _lesson_url(node, page.url)
                    if not url or url in seen:
                        continue
                    if host_of(url) != host_of(page.url):
                        continue
                    seen.add(url)
                    title = _first_str(node, ("title", "name", "lesson_title",
                                              "display_name", "heading"))
                    yield ctx.lead(page, url, PAGE, title=title,
                                   meta={"platform": "learnyst",
                                         "lesson_id": node.get("id"),
                                         "lesson_type": node.get("type")})

    @staticmethod
    def _candidate_endpoints(host: str, course_id: str,
                             slug: Optional[str]) -> List[str]:
        """Shapes worth one cheap GET each. Unverified by design - anything
        that does not return JSON is dropped silently."""
        base = f"https://{host}"
        return [
            f"{base}/api/v1/courses/{course_id}/curriculum",
            f"{base}/api/v1/course/{course_id}/sections",
            f"{base}/api/course/{course_id}/contents",
            f"{base}/learn/api/course/{course_id}",
            f"{base}/learn/course/{slug}/{course_id}.json" if slug else "",
        ]

    @staticmethod
    def _is_json(ctx: Context, url: str, page: Page) -> bool:
        if not url:
            return False
        try:
            probe = ctx.fetcher.fetch(
                url,
                headers={"Accept": "application/json", "Referer": page.url,
                         "X-Requested-With": "XMLHttpRequest"},
                retries=0, timeout=float(ctx.opt("probe_timeout", 8.0)))
        except Exception:
            return False
        if probe.status >= 400:
            return False
        return "json" in probe.content_type or probe.text.lstrip()[:1] in "{["


def _iter_lesson_nodes(blob) -> Iterator[dict]:
    """Dicts that sit inside a curriculum-shaped list."""
    stack = [(blob, "")]
    while stack:
        node, key = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, list) and str(k).lower() in CURRICULUM_KEYS:
                    for item in v:
                        if isinstance(item, dict):
                            yield item
                            stack.append((item, str(k)))
                elif isinstance(v, (dict, list)):
                    stack.append((v, str(k)))
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append((item, key))


def _lesson_url(node: dict, base: str) -> Optional[str]:
    for k in LINK_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            if v.startswith(("http://", "https://", "/")):
                return absolute(base, v)
            # A bare slug is only usable if the page told us the shape.
            if k == "slug" and "/learn/" in base and node.get("id"):
                return absolute(base, f"/learn/lesson/{v}/{node['id']}")
    return None


def _first_str(node: dict, keys) -> Optional[str]:
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    return None
