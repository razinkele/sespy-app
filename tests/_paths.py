"""Shared paths for tests: assets that live in the sespy library, not in this repo."""

from pathlib import Path

import sespy

TRANSLATIONS = Path(sespy.__file__).parent / "translations"
