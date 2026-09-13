"""Declarative site profiles.

A profile is a YAML file describing how one site is laid out. It exists so
that supporting a new site is a 15-line config change rather than a code
change - which matters because the sites that need supporting are exactly the
ones nobody else has written an extractor for.

    name: acme-academy
    match:
      hosts: [learn.acme.com]
      url_patterns: ['/course/\\d+']
    headers:
      X-Requested-With: XMLHttpRequest
    options:
      max_lessons: 0
    follow:
      - selector: "a.lesson-link"        # -> page leads
      - regex: '"lessonUrl":"([^"]+)"'
      - json: {markers: [__DATA__], path: 'course.lessons[*].url'}
      - template: 'https://{host}/api/courses/{group1}/items'
        kind: api
    videos:
      - selector: "video source"
        attr: src
      - regex: 'hlsUrl":"([^"]+)"'
        kind: hls
      - json: {path: '**.playback_url', kind: hls}

Rule types: `selector` (+`attr`), `regex` (capture group 1), `json`
(`markers` + `path`), `template` (format string). Every rule may set `kind`,
`title`, and `headers`. Anything a profile cannot express is a signal to
write a Python extractor instead - see registry.load_plugins.
"""

from __future__ import annotations

import json as _json
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .http import absolute, host_of
from .models import API, EMBED, MANIFEST, PAGE, Page, Video
from .registry import Context, Extractor
from .util import classify, height_from_url, json_blobs, unescape_url

try:                                     # PyYAML is optional
    import yaml                          # type: ignore
except ImportError:                      # pragma: no cover
    yaml = None

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "profiles")


class ProfileError(Exception):
    pass


def _load_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if path.endswith((".yaml", ".yml")):
        if yaml is None:
            raise ProfileError(
                f"{path}: YAML profiles need PyYAML (pip install pyyaml), "
                "or convert the profile to .json")
        data = yaml.safe_load(raw)
    else:
        data = _json.loads(raw)
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: profile must be a mapping")
    data.setdefault("name", os.path.splitext(os.path.basename(path))[0])
    data["_path"] = path
    return data


def discover(paths: Sequence[str] = (), include_builtin: bool = True) -> List[Dict]:
    """Load profiles from the built-in dir, ~/.config/videograb/profiles and
    any explicit files/dirs given on the command line."""
    out: List[Dict] = []
    search: List[str] = []
    if include_builtin and os.path.isdir(BUILTIN_DIR):
        search.append(BUILTIN_DIR)
    user_dir = os.path.join(os.path.expanduser("~"), ".config", "videograb", "profiles")
    if os.path.isdir(user_dir):
        search.append(user_dir)
    search.extend(paths or [])
    for p in search:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.endswith((".yaml", ".yml", ".json")) and not f.startswith("_"):
                    out.append(_load_file(os.path.join(p, f)))
        elif os.path.isfile(p):
            out.append(_load_file(p))
        else:
            raise ProfileError(f"profile path not found: {p}")
    return out


# ---------------------------------------------------------------------------
# JSON path: a.b[*].c, with ** meaning "any key at any depth"
# ---------------------------------------------------------------------------

def json_path(obj: Any, path: str) -> Iterator[Any]:
    parts = [p for p in re.split(r"\.(?![^\[]*\])", path) if p]
    yield from _walk_path(obj, parts)


def _walk_path(node: Any, parts: List[str]) -> Iterator[Any]:
    if not parts:
        yield node
        return
    head, rest = parts[0], parts[1:]
    if head == "**":
        if not rest:
            yield node
            return
        key = rest[0]
        for found in _deep_key(node, key):
            yield from _walk_path(found, rest[1:])
        return
    m = re.match(r"^([^\[]*)((?:\[[^\]]*\])*)$", head)
    name, idx = (m.group(1), m.group(2)) if m else (head, "")
    cur = node
    if name:
        if not isinstance(cur, dict) or name not in cur:
            return
        cur = cur[name]
    for token in re.findall(r"\[([^\]]*)\]", idx):
        if token in ("*", ""):
            if not isinstance(cur, list):
                return
            for item in cur:
                yield from _walk_path(item, rest)
            return
        try:
            cur = cur[int(token)]
        except (ValueError, IndexError, TypeError, KeyError):
            return
    yield from _walk_path(cur, rest)


def _deep_key(node: Any, key: str) -> Iterator[Any]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key:
                    yield v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(x for x in cur if isinstance(x, (dict, list)))


# ---------------------------------------------------------------------------

class Profile:
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.name = data.get("name", "profile")
        self.path = data.get("_path", "<inline>")
        m = data.get("match") or {}
        self.hosts = [h.lower() for h in (m.get("hosts") or [])]
        self.host_patterns = [re.compile(p, re.I) for p in (m.get("host_patterns") or [])]
        self.url_patterns = [re.compile(p, re.I) for p in (m.get("url_patterns") or [])]
        self.body_contains = [s for s in (m.get("body_contains") or [])]
        self.headers: Dict[str, str] = dict(data.get("headers") or {})
        self.options: Dict[str, Any] = dict(data.get("options") or {})
        self.follow: List[Dict] = list(data.get("follow") or [])
        self.videos: List[Dict] = list(data.get("videos") or [])
        self.priority = int(data.get("priority", 26))

    def matches_url(self, url: str) -> bool:
        host = host_of(url)
        if self.hosts and any(host == h or host.endswith("." + h) for h in self.hosts):
            return True
        if any(p.search(host) for p in self.host_patterns):
            return True
        if self.hosts or self.host_patterns:
            return False
        return bool(self.url_patterns) and any(p.search(url) for p in self.url_patterns)

    def matches_page(self, page: Page) -> bool:
        if not self.matches_url(page.url) and not self.matches_url(page.requested_url):
            return False
        if self.url_patterns and not any(
                p.search(page.url) or p.search(page.requested_url)
                for p in self.url_patterns):
            return False
        if self.body_contains:
            body = page.text or ""
            if not any(s in body for s in self.body_contains):
                return False
        return True

    def captures(self, url: str) -> Dict[str, str]:
        """Named + positional captures from the first matching url_pattern,
        exposed to `template` rules as {group1}, {name}, {host}."""
        out = {"host": host_of(url), "url": url}
        for p in self.url_patterns:
            m = p.search(url)
            if m:
                for i, g in enumerate(m.groups(), 1):
                    out[f"group{i}"] = g or ""
                out.update({k: v or "" for k, v in (m.groupdict() or {}).items()})
                break
        return out

    def build(self) -> "ProfileExtractor":
        return ProfileExtractor(self)


class ProfileExtractor(Extractor):
    kinds = (PAGE, EMBED, API, MANIFEST)
    enabled_by_default = True

    def __init__(self, profile: Profile):
        self.profile = profile
        self.name = f"profile:{profile.name}"
        self.priority = profile.priority

    def matches(self, page: Page, ctx: Context) -> bool:
        return self.profile.matches_page(page)

    def extract(self, page: Page, ctx: Context) -> Iterator[object]:
        for rule in self.profile.follow:
            for url, title in self._values(rule, page, ctx):
                kind = rule.get("kind", PAGE)
                yield ctx.lead(page, url, kind, title=title,
                               headers=dict(rule.get("headers") or {}),
                               meta={"profile": self.profile.name})
        for rule in self.profile.videos:
            for url, title in self._values(rule, page, ctx):
                full = absolute(page.url, url)
                kind = rule.get("kind", "auto")
                guessed, container = classify(full)
                if kind == "auto":
                    if guessed is None:
                        continue
                    kind = guessed
                yield Video(url=full, kind=kind, container=container,
                            title=title, page_url=page.url,
                            height=height_from_url(full),
                            headers=dict(rule.get("headers") or {}),
                            meta={"profile": self.profile.name})

    # -- rule evaluation ---------------------------------------------------
    def _values(self, rule: Dict, page: Page, ctx: Context):
        title_spec = rule.get("title")
        page_title = page.dom.title

        if "selector" in rule:
            attr = rule.get("attr")
            for el in page.dom.select(rule["selector"]):
                val = None
                if attr:
                    val = el.get(attr)
                else:
                    for cand in ("href", "src", "data-src", "data-url", "content"):
                        val = el.get(cand)
                        if val:
                            break
                if not val:
                    continue
                yield unescape_url(val), self._title(title_spec, el, page_title)

        if "regex" in rule:
            flags = re.I if rule.get("ignore_case", True) else 0
            for m in re.finditer(rule["regex"], page.text or "", flags):
                val = m.group(1) if m.groups() else m.group(0)
                if val:
                    yield unescape_url(val), self._title(title_spec, None, page_title)

        if "json" in rule:
            spec = rule["json"] or {}
            markers = spec.get("markers") or ()
            path = spec.get("path") or "**.url"
            title_key = spec.get("title")
            bodies = list(page.dom.script_texts()) if page.is_html else [page.text]
            for body in bodies:
                for blob in json_blobs(body, markers):
                    if title_key:
                        # Walk items so url and title stay paired.
                        parent_path = path.rsplit(".", 1)[0]
                        key = path.rsplit(".", 1)[-1]
                        for item in json_path(blob, parent_path):
                            if isinstance(item, dict) and item.get(key):
                                yield (unescape_url(str(item[key])),
                                       str(item.get(title_key) or page_title or "") or None)
                        continue
                    for val in json_path(blob, path):
                        if isinstance(val, str) and val.strip():
                            yield unescape_url(val), page_title

        if "template" in rule:
            caps = self.profile.captures(page.url)
            try:
                yield rule["template"].format(**caps), page_title
            except KeyError as e:
                ctx.warn(f"{self.name}: template missing capture {e}")

    @staticmethod
    def _title(spec, el, page_title) -> Optional[str]:
        if spec in (None, "", "page"):
            return (el.text.strip()[:200] if el is not None and el.text else None) \
                or page_title
        if spec == "text" and el is not None:
            return (el.text or "").strip()[:200] or page_title
        if isinstance(spec, str) and spec.startswith("attr:") and el is not None:
            return (el.get(spec[5:]) or "").strip()[:200] or page_title
        return page_title


def load(paths: Sequence[str] = (), include_builtin: bool = True) -> List[Profile]:
    return [Profile(d) for d in discover(paths, include_builtin)]


def for_url(profiles: Sequence[Profile], url: str) -> List[Profile]:
    return [p for p in profiles if p.matches_url(url)]
