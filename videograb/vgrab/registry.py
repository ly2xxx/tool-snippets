"""Extractor contract + registry.

Adding support for a new site means writing one class with two methods and
decorating it. Nothing else in the tool needs to change.

    from vgrab.registry import Extractor, register
    from vgrab.models import Video, Lead, PAGE

    @register
    class MySite(Extractor):
        name = "mysite"
        priority = 20                 # lower runs first; 50 is the default
        kinds = (PAGE,)               # lead kinds this handles

        def matches(self, page, ctx):
            return "mysite.com" in page.url

        def extract(self, page, ctx):
            for a in page.dom.select("a.lesson"):
                yield ctx.lead(page, a.get("href"), PAGE, title=a.text)
            yield Video(url=..., kind=HLS, title=page.dom.title)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Type

from .http import Fetcher, absolute
from .models import Lead, PAGE, Page


class Context:
    """Everything an extractor is allowed to reach for."""

    def __init__(self, fetcher: Fetcher, options: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str, str], None]] = None):
        self.fetcher = fetcher
        self.options = options or {}
        self._log = log or (lambda level, msg: None)

    def debug(self, msg: str) -> None:
        self._log("debug", msg)

    def warn(self, msg: str) -> None:
        self._log("warn", msg)

    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def lead(self, page: Page, url: str, kind: str = PAGE, **kw) -> Lead:
        """Build a resolved, depth-incremented lead relative to `page`."""
        parent = page.lead or Lead(url=page.url, depth=0)
        return parent.child(absolute(page.url, url), kind=kind, **kw)


class Extractor:
    name: str = "extractor"
    priority: int = 50
    kinds: tuple = (PAGE,)
    # Set False for extractors that should only run when explicitly enabled.
    enabled_by_default: bool = True

    def matches(self, page: Page, ctx: Context) -> bool:  # pragma: no cover
        raise NotImplementedError

    def extract(self, page: Page, ctx: Context) -> Iterable[object]:  # pragma: no cover
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name} priority={self.priority}>"


_REGISTRY: List[Type[Extractor]] = []


def register(cls: Type[Extractor]) -> Type[Extractor]:
    _REGISTRY.append(cls)
    return cls


def all_extractor_classes() -> List[Type[Extractor]]:
    return sorted(_REGISTRY, key=lambda c: (c.priority, c.name))


def build(only: Optional[Iterable[str]] = None,
          exclude: Optional[Iterable[str]] = None,
          extra: Optional[Iterable[Extractor]] = None,
          enable: Optional[Iterable[str]] = None) -> List[Extractor]:
    """Instantiate the active extractor set.

    `only` restricts to exactly those names; `exclude` drops names from the
    default set; `enable` switches on an opt-in extractor (such as `network`)
    without turning the default set into a whitelist - which would silently
    drop every site profile.
    """
    only_set = {s.strip() for s in only} if only else None
    excl_set = {s.strip() for s in exclude} if exclude else set()
    on_set = {s.strip() for s in enable} if enable else set()
    out: List[Extractor] = []
    for cls in all_extractor_classes():
        if only_set is not None:
            if cls.name not in only_set:
                continue
        elif cls.name in excl_set:
            continue
        elif not cls.enabled_by_default and cls.name not in on_set:
            continue
        out.append(cls())
    for inst in extra or []:
        if only_set is not None and inst.name not in only_set:
            continue
        if inst.name in excl_set:
            continue
        out.append(inst)
    out.sort(key=lambda e: (e.priority, e.name))
    return out


def load_plugins(paths: Iterable[str]) -> List[str]:
    """Import user Python files so their @register classes join the registry.

    This is the escape hatch for anything the YAML profiles can't express.
    """
    import importlib.util
    import os

    loaded = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            files = [os.path.join(p, f) for f in sorted(os.listdir(p))
                     if f.endswith(".py") and not f.startswith("_")]
        else:
            files = [p]
        for f in files:
            spec = importlib.util.spec_from_file_location(
                "vgrab_plugin_" + os.path.basename(f)[:-3], f)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            loaded.append(f)
    return loaded
