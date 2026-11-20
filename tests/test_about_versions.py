"""About modal shows both the app version and the library version."""
import sespy
import sespy_app
from sespy_app.modules.topbar_actions import _about_modal


def test_about_shows_both_versions():
    html = str(_about_modal(None))
    assert f"SESPy app {sespy_app.__version__}" in html
    assert f"sespy library {sespy.__version__}" in html
