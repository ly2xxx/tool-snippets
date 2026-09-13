"""videograb - extensible video discovery and download.

Public surface:

    from vgrab import extract, Fetcher
    result = extract("https://example.com/course/1", fetcher=Fetcher())
    for v in result.videos:
        print(v.url, v.label)
"""

from .models import Video, Lead, Page, Result, PAGE, EMBED, MANIFEST, API, \
    PROGRESSIVE, HLS, DASH, EXTERNAL
from .http import Fetcher
from .registry import Extractor, Context, register, build, load_plugins
from .pipeline import Pipeline, extract

# Importing the package must populate the registry: otherwise `build()` quietly
# returns a partial extractor set and callers get mystifying empty results.
# Both modules are import-safe - browser.py only touches Playwright at launch.
from . import extractors    # noqa: E402,F401
from . import browser       # noqa: E402,F401

__version__ = "0.1.0"
__all__ = [
    "Video", "Lead", "Page", "Result", "Fetcher", "Extractor", "Context",
    "register", "build", "load_plugins", "Pipeline", "extract",
    "PAGE", "EMBED", "MANIFEST", "API", "PROGRESSIVE", "HLS", "DASH", "EXTERNAL",
]
