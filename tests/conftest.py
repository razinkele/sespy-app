"""Session guard: the app tests must run against the sespy LIBRARY, not the monolith.

Before the split, `sespy` was one package holding both the library and the UI
(`sespy.modules`, ...). If an old checkout/editable install of that monolith is
on the path, `import sespy` silently succeeds and tests exercise stale code.
The monolith is recognisable by `sespy.modules` being importable.

The check runs twice on purpose: in `pytest_sessionstart`, before collection
(a monolith on the path usually makes collection itself fail with an unrelated
ImportError, which would hide the real cause), and in a session-scoped autouse
fixture. The version line is written to the terminal outside output capture so
it shows on a green `-q` run too.
"""

import importlib.util

import pytest

MONOLITH_MESSAGE = (
    "the monolith sespy is on the path (sespy.modules importable): "
    "set PYTHONPATH to the library checkout"
)


def _monolith_on_path() -> bool:
    import sespy  # noqa: F401  (a missing library fails loudly here)

    return importlib.util.find_spec("sespy.modules") is not None


def pytest_sessionstart(session):
    if _monolith_on_path():
        pytest.exit(MONOLITH_MESSAGE, returncode=3)
    import sespy

    session.config.get_terminal_writer().line(f"[conftest] sespy {sespy.__version__} from {sespy.__file__}")


@pytest.fixture(scope="session", autouse=True)
def _library_not_monolith():
    if _monolith_on_path():
        pytest.exit(MONOLITH_MESSAGE, returncode=3)
    yield
