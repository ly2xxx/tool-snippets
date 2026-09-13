"""Whizlabs extractor (business.whizlabs.com and whizlabs.com).

Whizlabs courses are rendered by a React SPA. Course curriculum and video
metadata are loaded via the AWS API Gateway backend:
https://90myjn812m.execute-api.us-east-1.amazonaws.com/b2b-prod/online-courses/course-section
Video lectures are hosted on Vimeo and require Referer: https://business.whizlabs.com/.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Iterator, Optional

from ..models import EXTERNAL, PAGE, Page, Video
from ..registry import Context, Extractor, register

COURSE_URL = re.compile(r"/learn/course/(?P<slug>[^/]+)/(?P<id>\d+)")
API_ENDPOINT = "https://90myjn812m.execute-api.us-east-1.amazonaws.com/b2b-prod/online-courses/course-section"


def _extract_token(ctx: Context) -> Optional[str]:
    # 1. From Authorization header
    auth = ctx.fetcher.headers.get("Authorization") or ctx.fetcher.headers.get("authorization")
    if auth:
        return auth.replace("Bearer ", "").strip()

    # 2. From browser storage_state if configured
    storage_state = getattr(ctx.fetcher, "storage_state", None)
    if storage_state and os.path.exists(storage_state):
        try:
            with open(storage_state, "r", encoding="utf-8") as f:
                data = json.load(f)
            for origin in data.get("origins", []):
                for item in origin.get("localStorage", []):
                    if item.get("name") in ("user_token", "token"):
                        return item.get("value")
        except Exception:
            pass

    # 3. From cookies (userData contains URL-encoded JSON with token)
    for c in ctx.fetcher.cookiejar:
        if c.name == "userData" and c.value:
            try:
                val = urllib.parse.unquote(c.value)
                obj = json.loads(val)
                token = obj.get("data", {}).get("token")
                if token:
                    return token
            except Exception:
                pass
        if c.name in ("token", "user_token") and c.value:
            return c.value

    # 4. Check local wl.json as fallback
    for candidate in ("wl.json", os.path.expanduser("~/.config/videograb/wl.json")):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for origin in data.get("origins", []):
                    for item in origin.get("localStorage", []):
                        if item.get("name") in ("user_token", "token"):
                            return item.get("value")
            except Exception:
                pass

    return None


@register
class Whizlabs(Extractor):
    name = "whizlabs"
    priority = 15  # Runs before generic LMS and profile
    kinds = (PAGE,)

    def matches(self, page: Page, ctx: Context) -> bool:
        if "whizlabs.com" not in page.url and "whizlabs.com" not in page.requested_url:
            return False
        return bool(COURSE_URL.search(page.url) or COURSE_URL.search(page.requested_url))

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        m = COURSE_URL.search(page.url) or COURSE_URL.search(page.requested_url)
        if not m:
            return
        course_id = m.group("id")
        token = _extract_token(ctx)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://business.whizlabs.com/",
            "Origin": "https://business.whizlabs.com",
            "User-Agent": ctx.fetcher.user_agent or "Mozilla/5.0",
        }
        if token:
            headers["Authorization"] = token

        payload = json.dumps({"course_id": str(course_id)}).encode("utf-8")
        try:
            res = ctx.fetcher.fetch(API_ENDPOINT, headers=headers, method="POST", data=payload)
            data = json.loads(res.text)
        except Exception as e:
            ctx.warn(f"whizlabs: course-section API error: {e}")
            return

        items = data.get("data") or []
        current_sec = ""
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("section_heading"):
                current_sec = item["section_heading"].strip()
            video_code = item.get("video_code")
            video_name = item.get("video_name")
            if not video_code or not video_name:
                continue

            full_title = f"{current_sec} - {video_name}" if current_sec else video_name
            embed_url = f"https://player.vimeo.com/video/{video_code}"
            # Whizlabs embeds Vimeo via player.vimeo.com/video/<code\>
            yield Video(
                url=embed_url,
                kind=EXTERNAL,
                title=full_title,
                page_url=f"{page.url}#video-{video_code}",
                headers={"Referer": "https://business.whizlabs.com/"},
                meta={"provider": "vimeo", "resolver": "yt-dlp",
                      "course_id": course_id, "video_code": video_code}
            )
