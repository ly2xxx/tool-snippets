"""A tiny dependency-free DOM + CSS-subset selector engine.

BeautifulSoup/lxml would be nicer, but the rest of this tool runs on the
standard library alone and HTML scraping is the one place a hard dependency
would be felt. `html.parser` plus ~200 lines gets us everything the
extractors actually need: find elements, read attributes, read text, and
pull out <script> bodies.

Supported selector syntax (deliberately a subset):

    tag                       div
    #id                       #player
    .class                    .lesson-item
    [attr]                    [data-video]
    [attr=value]              [type="application/json"]
    [attr*=value]             a[href*="/lesson/"]
    [attr^=value] [attr$=]    img[src$=".jpg"]
    descendant combinator     .lessons a[href*=lesson]
    groups                    video, iframe, source
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, Iterable, Iterator, List, Optional

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# Attributes that may plausibly carry a media or page URL. Used by the
# generic extractor to sweep a page without knowing its markup.
URL_ATTRS = (
    "src", "href", "data-src", "data-url", "data-video", "data-video-url",
    "data-video-src", "data-file", "data-mp4", "data-hls", "data-m3u8",
    "data-manifest", "data-stream", "data-player", "data-setup", "content",
    "value", "data-config", "data-lesson-url", "data-asset",
)


class Element:
    """One markup node. `nodes` keeps text and child elements interleaved in
    document order so `.text` reads the way a browser would render it."""

    __slots__ = ("tag", "attrs", "nodes", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["Element"]):
        self.tag = tag
        self.attrs = attrs
        self.nodes: List[object] = []
        self.parent = parent

    @property
    def children(self) -> List["Element"]:
        return [n for n in self.nodes if isinstance(n, Element)]

    # -- attribute helpers -------------------------------------------------
    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.attrs.get(name.lower(), default)

    @property
    def classes(self) -> List[str]:
        return (self.get("class") or "").split()

    @property
    def text(self) -> str:
        """Concatenated text of this element and its descendants."""
        return re.sub(r"\s+", " ", self._raw_text()).strip()

    def _raw_text(self) -> str:
        out: List[str] = []
        for n in self.nodes:
            out.append(n._raw_text() if isinstance(n, Element) else n)
        return "".join(out)

    @property
    def own_text(self) -> str:
        """Text directly inside this element (what a <script> body is)."""
        return "".join(n for n in self.nodes if isinstance(n, str))

    # -- traversal ---------------------------------------------------------
    def walk(self) -> Iterator["Element"]:
        yield self
        for n in self.nodes:
            if isinstance(n, Element):
                yield from n.walk()

    def select(self, selector: str) -> List["Element"]:
        return [e for e in self.walk() if e is not self and _matches_chain(e, selector)]

    def select_one(self, selector: str) -> Optional["Element"]:
        for e in self.select(selector):
            return e
        return None

    def urls(self) -> Iterator[str]:
        for attr in URL_ATTRS:
            v = self.attrs.get(attr)
            if v:
                yield v

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {self.attrs}>"


class Document(Element):
    def __init__(self):
        super().__init__("#document", {}, None)
        self.scripts: List[Element] = []
        self.title: Optional[str] = None

    def script_texts(self, type_filter: Optional[str] = None) -> Iterator[str]:
        for s in self.scripts:
            if type_filter and (s.get("type") or "").lower() != type_filter:
                continue
            body = s.own_text
            if body.strip():
                yield body

    def meta(self, name: str) -> Optional[str]:
        """<meta property=name content=...> or <meta name=name content=...>."""
        low = name.lower()
        for e in self.walk():
            if e.tag != "meta":
                continue
            key = (e.get("property") or e.get("name") or e.get("itemprop") or "").lower()
            if key == low:
                return e.get("content")
        return None


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.doc = Document()
        self.stack: List[Element] = [self.doc]

    def handle_starttag(self, tag, attrs):
        el = Element(tag, {k.lower(): (v if v is not None else "") for k, v in attrs},
                     self.stack[-1])
        self.stack[-1].nodes.append(el)
        if tag == "script":
            self.doc.scripts.append(el)
        if tag not in VOID_TAGS:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        el = Element(tag, {k.lower(): (v if v is not None else "") for k, v in attrs},
                     self.stack[-1])
        self.stack[-1].nodes.append(el)
        if tag == "script":
            self.doc.scripts.append(el)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # Unbalanced close tag: ignore, matching browser leniency.

    def handle_data(self, data):
        cur = self.stack[-1]
        cur.nodes.append(data)
        if cur.tag == "title" and data.strip() and not self.doc.title:
            self.doc.title = data.strip()

    def error(self, message):  # pragma: no cover - py<3.10 compat shim
        pass


def parse(html: str) -> Document:
    b = _Builder()
    try:
        b.feed(html or "")
        b.close()
    except Exception:
        # Malformed markup should degrade, never crash a crawl.
        pass
    return b.doc


# ---------------------------------------------------------------------------
# Selector engine
# ---------------------------------------------------------------------------

_SIMPLE_RE = re.compile(
    r"""
    (?P<tag>^[A-Za-z][\w-]*|^\*)?
    (?P<rest>(?:\#[\w-]+|\.[\w-]+|\[[^\]]+\])*)
    $""",
    re.VERBOSE,
)
_ATTR_RE = re.compile(r"""\[\s*([\w:.-]+)\s*(?:([~^$*|]?=)\s*("[^"]*"|'[^']*'|[^\]]*?)\s*)?\]""")


def _matches_simple(el: Element, sel: str) -> bool:
    sel = sel.strip()
    if not sel:
        return False
    m = _SIMPLE_RE.match(sel)
    if not m:
        return False
    tag = m.group("tag")
    if tag and tag != "*" and el.tag != tag.lower():
        return False
    rest = m.group("rest") or ""
    for part in re.findall(r"\#[\w-]+|\.[\w-]+|\[[^\]]+\]", rest):
        if part.startswith("#"):
            if el.get("id") != part[1:]:
                return False
        elif part.startswith("."):
            if part[1:] not in el.classes:
                return False
        else:
            am = _ATTR_RE.match(part)
            if not am:
                return False
            name, op, raw = am.group(1).lower(), am.group(2), am.group(3)
            have = el.attrs.get(name)
            if have is None:
                return False
            if op is None:
                continue
            want = (raw or "").strip()
            if len(want) >= 2 and want[0] == want[-1] and want[0] in "\"'":
                want = want[1:-1]
            if op == "=" and have != want:
                return False
            if op == "*=" and want not in have:
                return False
            if op == "^=" and not have.startswith(want):
                return False
            if op == "$=" and not have.endswith(want):
                return False
            if op == "~=" and want not in have.split():
                return False
            if op == "|=" and not (have == want or have.startswith(want + "-")):
                return False
    return True


def _matches_chain(el: Element, selector: str) -> bool:
    """Match `selector`, supporting comma groups and descendant combinators."""
    for group in selector.split(","):
        parts = [p for p in group.strip().split() if p]
        if not parts:
            continue
        if not _matches_simple(el, parts[-1]):
            continue
        node = el.parent
        remaining = parts[:-1]
        while remaining and node is not None:
            if _matches_simple(node, remaining[-1]):
                remaining = remaining[:-1]
            node = node.parent
        if not remaining:
            return True
    return False


def select(root: Element, selector: str) -> List[Element]:
    return root.select(selector)


def iter_attribute_urls(doc: Document) -> Iterable[str]:
    """Every attribute value on the page that looks like it could be a URL."""
    for el in doc.walk():
        for v in el.urls():
            v = v.strip()
            if v and not v.startswith(("javascript:", "mailto:", "tel:", "#")):
                yield v
