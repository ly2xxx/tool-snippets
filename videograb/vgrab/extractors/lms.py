"""Course/LMS crawling.

Learning platforms (Learnyst - which is what Whizlabs Business runs on -
Teachable, Thinkific, Kajabi, Moodle, Docebo, TalentLMS, LearnDash, ...) all
share one shape: a curriculum page listing lessons, each lesson page holding
one player. So rather than a per-platform extractor, we detect that *shape*.

Guard rail: lesson leads are only emitted when a page looks like a curriculum
(several lesson-ish links, or an explicit curriculum container). Without that
check, pointing the tool at any site would crawl its whole navigation.
"""

from __future__ import annotations

import re
from typing import Dict, Iterator

from ..http import absolute, host_of
from ..models import PAGE, Page
from ..registry import Context, Extractor, register

# URL shapes that mean "a single unit of a course".
LESSON_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"/learn/(?:lesson|lecture|topic|unit|chapter|section|content|video|quiz|module)s?/",
        r"/courses?/[^/]+/(?:lessons?|lectures?|topics?|modules?|units?|chapters?)/",
        r"/(?:lesson|lecture|topic|module|unit|chapter)s?/\d+",
        r"/enrolled/[^/]+/",
        r"/mod/(?:page|resource|scorm|url|lesson)/view\.php",
        r"/learn/course/[^/]+/\d+/",         # Learnyst deep links
        r"[?&](?:lesson|lecture|topic|unit|chapter|content|item)_?id=",
        r"/player/[\w-]+",
        r"/watch/[\w-]+",
    )
]
# Containers that mark a curriculum in practice.
CURRICULUM_SELECTORS = (
    ".curriculum a[href]", ".course-curriculum a[href]", ".lessons a[href]",
    ".lesson-list a[href]", ".chapter a[href]", ".syllabus a[href]",
    "[data-lesson-id]", "[data-lecture-id]", "[data-topic-id]",
    ".section-item a[href]", "nav.course a[href]", "#curriculum a[href]",
    ".playlist a[href]", ".video-list a[href]", ".module-list a[href]",
)
SKIP = re.compile(
    r"/(login|signin|signup|register|logout|checkout|cart|pricing|billing|"
    r"account|profile|settings|support|help|faq|terms|privacy|contact|blog|"
    r"certificate|discussion|forum|review|feedback|report)\b", re.I)


def is_lesson_url(url: str) -> bool:
    if SKIP.search(url):
        return False
    return any(p.search(url) for p in LESSON_PATTERNS)


@register
class CourseCurriculum(Extractor):
    name = "course"
    priority = 40
    kinds = (PAGE,)

    def matches(self, page: Page, ctx: Context) -> bool:
        if ctx.opt("no_course"):
            return False
        return page.is_html

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        doc = page.dom
        candidates: Dict[str, str] = {}     # url -> title

        for a in doc.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = absolute(page.url, href)
            if not url.startswith("http"):
                continue
            if is_lesson_url(url) and url.split("#")[0] != page.url.split("#")[0]:
                candidates.setdefault(url, (a.get("title") or a.text or "").strip()[:200])

        # Curriculum containers: trust these even if the URL shape is odd.
        structural: Dict[str, str] = {}
        for sel in CURRICULUM_SELECTORS:
            for el in doc.select(sel):
                href = el.get("href") or el.get("data-href") or el.get("data-url")
                if not href:
                    continue
                url = absolute(page.url, href)
                if url.startswith("http") and not SKIP.search(url) \
                        and host_of(url) == host_of(page.url):
                    structural.setdefault(url, (el.get("title") or el.text or "").strip()[:200])

        merged = dict(candidates)
        merged.update(structural)
        threshold = int(ctx.opt("course_min_links", 2) or 2)
        if len(merged) < threshold and not structural:
            # Not a curriculum - probably a single lesson page. Leave it to the
            # media extractors rather than wandering off.
            return

        limit = int(ctx.opt("max_lessons", 0) or 0)
        for i, (url, title) in enumerate(merged.items()):
            if limit and i >= limit:
                ctx.debug(f"max_lessons={limit} reached, skipping the rest")
                break
            yield ctx.lead(page, url, PAGE, title=title or None,
                           meta={"lesson_index": i})


@register
class FollowPatterns(Extractor):
    """`--follow REGEX` / `--follow-selector CSS`: explicit user-driven crawl.

    The escape hatch for sites whose structure nothing else recognises.
    """

    name = "follow"
    priority = 41
    kinds = (PAGE,)

    def matches(self, page: Page, ctx: Context) -> bool:
        return bool(ctx.opt("follow") or ctx.opt("follow_selector")) and page.is_html

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        pats = [re.compile(p, re.I) for p in (ctx.opt("follow") or [])]
        seen = set()
        for a in page.dom.select("a[href]"):
            href = a.get("href") or ""
            url = absolute(page.url, href)
            if not url.startswith("http") or url in seen:
                continue
            if any(p.search(url) for p in pats):
                seen.add(url)
                yield ctx.lead(page, url, PAGE, title=(a.text or "").strip()[:200] or None)
        for sel in (ctx.opt("follow_selector") or []):
            for el in page.dom.select(sel):
                href = el.get("href") or el.get("src") or el.get("data-url")
                if not href:
                    continue
                url = absolute(page.url, href)
                if url.startswith("http") and url not in seen:
                    seen.add(url)
                    yield ctx.lead(page, url, PAGE,
                                   title=(el.text or "").strip()[:200] or None)
