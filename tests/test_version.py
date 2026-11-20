"""The app's own version: pyproject and sespy_app.__version__ must agree, and the
__version__ line must stay in the shape the release script's sed expects."""
import re
import tomllib
from pathlib import Path

import sespy_app

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_version_matches_dunder():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == sespy_app.__version__


def test_version_line_is_sed_able():
    lines = (ROOT / "sespy_app" / "__init__.py").read_text(encoding="utf-8").splitlines()
    pattern = re.compile(r'^__version__ = "\d+\.\d+\.\d+"$')
    assert any(pattern.match(line) for line in lines)
