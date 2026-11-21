"""No tracked app file may still name a path of the pre-split monolith.

The app used to live inside the `sespy` package (`sespy.modules`, `sespy/www`,
`ROOT / "sespy" / ...`). Those paths no longer exist; a leftover string would
only fail at runtime (or worse, pass against a stale monolith on the path).
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PATTERN = re.compile(
    r"sespy\.(modules|dashboard|event_bus|browser_token|feedback_store|ui_i18n|ui_r_kb)\b"
    r"|from sespy import [^\n]*\b(modules|dashboard|event_bus|browser_token|feedback_store|ui_i18n|ui_r_kb)\b"
    r"|sespy/(www|logs|modules|translations)"
    r"|/ ?\"sespy\""
    r"|\"sespy\" ?/"
)
ROOTS = ["sespy_app", "app.py", "tests", ".github", "pyproject.toml", "deploy"]
# Deliberate exceptions: deploy.sh and its test carry the old-layout `sespy/logs`
# migration; CHANGELOG narrates history; this file defines the pattern.
EXCLUDED = {
    "tests/test_no_monolith_strings.py",
    "tests/conftest.py",  # its guard message names the monolith module
    "CHANGELOG.md",
    "deploy/deploy.sh",
    "tests/test_deploy_script.py",
}


def test_no_monolith_strings():
    files = subprocess.run(
        ["git", "ls-files", *ROOTS], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    hits = []
    for rel in files:
        if rel in EXCLUDED:
            continue
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not hits, "monolith paths remain:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    "line, should_match",
    [
        ("from sespy import feedback_store", True),
        ("from sespy import data_structure, modules", True),
        ("from sespy import data_structure", False),
    ],
)
def test_pattern_self_check(line, should_match):
    assert bool(PATTERN.search(line)) is should_match
