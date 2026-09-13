"""Importing this package registers every built-in extractor."""

from . import generic       # noqa: F401
from . import manifests     # noqa: F401
from . import providers     # noqa: F401
from . import lms           # noqa: F401
from . import learnyst      # noqa: F401
from . import whizlabs      # noqa: F401

__all__ = ["generic", "manifests", "providers", "lms", "learnyst", "whizlabs"]
