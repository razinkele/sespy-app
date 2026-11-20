"""Behavioural tests for deploy/deploy.sh (K1 Task 17; library split Task 6).

The script runs for real, under bash, against a fake server tree in a temp
dir: ``ssh`` and ``scp`` are stubs on PATH that execute locally (``ssh`` runs
its remote command with bash, ``scp`` strips ``fake@host:`` and runs
``cp -r``), so the assertions are about real file-system state. There is no
network: the stubs are verified to shadow the real ssh/scp before any run.

Two trees are faked per test: the app checkout (``repo``, built from this
repo's files) and the pinned SESPy library checkout (``lib``, a real git repo
tagged ``v<PIN>``, where PIN comes from this repo's pyproject direct
reference). The deploy vendors ``lib/sespy`` beside the app's ``sespy_app``.

The fake server starts empty, in the OLD layout (``sespy/`` is the monolith
with ``modules/`` and the server-owned ``logs/feedback.db``) for the cutover
tests, or in the NEW layout (``sespy/`` library + ``sespy_app/`` holding
``logs/``) for the repeat-deploy tests.

The temp repo has NO ``deploy/config.env`` (the real one would be sourced
after the env and point the run at the production host); only
test_skip_var_in_config_env_is_ignored writes one, inside its own temp repo.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sespy  # noqa: E402
from sespy import r_kb  # noqa: E402

from sespy_app.kb_reads import LAGUNA_R_DATA_DIR  # noqa: E402

# The library's sespy/ package, and its checkout's tests/parity (check_live.py
# lives in a library checkout, not in an installed wheel).
LIB_SRC = Path(sespy.__file__).resolve().parent
LIB_PARITY = LIB_SRC.parent / "tests" / "parity"
if (LIB_PARITY / "check_live.py").is_file():
    sys.path.insert(0, str(LIB_PARITY))

_PIN_MATCH = re.search(r"@v(\d+\.\d+\.\d+)", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
assert _PIN_MATCH, "pyproject.toml has no pinned sespy direct reference (@vX.Y.Z)"
PIN = _PIN_MATCH.group(1)

BASH = shutil.which("bash")
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(BASH is None or GIT is None, reason="bash and git are needed")

SERVER = "fake@host"
OVERRIDE_NAMES = ["ses_knowledge_db.json", "country_governance_db.json"]
SAFE = re.compile(r"^/[A-Za-z0-9._/@+-]+$")


def _check_live():
    return pytest.importorskip("check_live", reason="needs a library checkout with tests/parity/check_live.py")


def _posix(p: Path | str) -> str:
    # cygpath exists only in Git Bash / Cygwin; on Linux CI the path is
    # already POSIX. Same fallback as deploy.sh's native_path().
    out = subprocess.run(
        [BASH, "-c",
         'if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf "%s" "$1"; fi',
         "_", str(p)],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _native(posix: str) -> str:
    """What deploy.sh's native_path() hands python for a POSIX path."""
    out = subprocess.run(
        [BASH, "-c",
         'if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf "%s" "$1"; fi',
         "_", posix],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _copy_lf(src: Path, dst: Path) -> None:
    """Copy as bytes with CRLF -> LF (the committed blob is LF; a Windows
    checkout may hold CRLF, which bash cannot run)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))


_GIT_CFG = ["-c", "user.name=deploy-test", "-c", "user.email=deploy-test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "core.autocrlf=false",
            "-c", "core.hooksPath=.no-hooks"]


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run([GIT, "-C", str(repo), *_GIT_CFG, *args], capture_output=True, text=True)
    assert cp.returncode == 0, (args, cp.stdout, cp.stderr)
    return cp.stdout


def _build_lib(path: Path, pin: str, *, check_live: bool = True) -> Path:
    """A fake library checkout: a git repo tagged v<pin> with sespy/__init__.py,
    the shipped r_kb data the pre-flight reads and a stub check_live.py."""
    _write_lf(path / "sespy" / "__init__.py", f'"""fake sespy library"""\n\n__version__ = "{pin}"\n')
    dst = path / "sespy" / "data" / "r_kb"
    dst.mkdir(parents=True, exist_ok=True)
    for src in (LIB_SRC / "data" / "r_kb").iterdir():
        if src.is_file():
            shutil.copyfile(src, dst / src.name)
    _write_lf(path / ".gitignore", "logs/\n__pycache__/\n")
    if check_live:
        _write_lf(path / "tests" / "parity" / "check_live.py", "# executed via the SESPY_PY stub\n")
    if (LIB_PARITY / "r_sync.json").is_file():
        _copy_lf(LIB_PARITY / "r_sync.json", path / "tests" / "parity" / "r_sync.json")
    _git(path, "init", "-q")
    _git(path, "config", "core.autocrlf", "false")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", f"library {pin}")
    _git(path, "tag", f"v{pin}")
    # A git-ignored local runtime DB: the tree stays clean, but the stage must
    # still strip it (it must never ship).
    _write_lf(path / "sespy" / "logs" / "local.db", "LOCAL-LIB-DB")
    return path


STUB_SSH = """#!/usr/bin/bash
cmd="${@: -1}"
printf 'ssh %s\\n' "${cmd//$'\\n'/ }" >> "$STUB_LOG"
# fault injection: fail the remote command that contains $STUB_SSH_FAIL
if [ -n "${STUB_SSH_FAIL:-}" ] && [[ "$cmd" == *"$STUB_SSH_FAIL"* ]]; then
  exit "${STUB_SSH_FAIL_RC:-255}"
fi
exec /usr/bin/bash -c "${STUB_SSH_PRELUDE:-}"$'\\n'"$cmd"
"""

STUB_SCP = """#!/usr/bin/bash
printf 'scp %s\\n' "$*" >> "$STUB_LOG"
args=()
for a in "$@"; do
  case "$a" in -*) continue ;; esac
  args+=("${a#fake@host:}")
done
exec cp -r "${args[@]}"
"""

STUB_SUDO = """#!/usr/bin/bash
printf 'sudo %s\\n' "$*" >> "$STUB_LOG"
if [ "${STUB_SUDO_OK:-}" = 1 ] && [ "$1 $2 $3" = "-n -u shiny" ]; then
  shift 3
  exec "$@"
fi
exit 1
"""

# SESPY_PY stub: prints the allow-listed names with CRLF (as Windows python
# does) and snapshots .live at call time ($STAGE is gone when the test asserts).
STUB_CHECK_LIVE = """#!/usr/bin/bash
printf 'check_live %s\\n' "$*" >> "$STUB_LOG"
printf 'pythonpath %s\\n' "${PYTHONPATH:-}" >> "$STUB_LOG"
for a in "$@"; do
  if [ "$a" = --print-override-names ]; then
    [ "${STUB_NO_NAMES:-}" = 1 ] && exit 0
    printf 'ses_knowledge_db.json\\r\\ncountry_governance_db.json\\r\\n'
    exit 0
  fi
done
prev=""
for a in "$@"; do
  if [ "$prev" = --live ]; then
    rm -rf "$STUB_DIR/seen_live"
    if command -v cygpath >/dev/null 2>&1; then a="$(cygpath -u "$a")"; fi
    cp -r "$a" "$STUB_DIR/seen_live"
  fi
  prev="$a"
done
exit "${STUB_EXIT:-0}"
"""


class Fake:
    def __init__(self, base: Path, lib_template: Path):
        base = base.resolve()
        self.base = base
        self.repo = base / "repo"
        self.lib = base / "lib"
        self.server = base / "server"
        self.app = self.server / "app"
        self.live_dir = self.server / "marinesabres" / "data"
        self.stubs = base / "stubs"
        self.log = base / "stub.log"
        self._build_repo()
        shutil.copytree(lib_template, self.lib)
        self._build_stubs()
        self.app_posix = _posix(self.app)
        self.lib_posix = _posix(self.lib)
        self.live_posix = _posix(self.live_dir)
        assert SAFE.match(self.app_posix), f"temp path unusable for the test: {self.app_posix}"
        self.env = self._env()
        self._assert_stubs_shadow()

    # -- setup ---------------------------------------------------------
    def _build_repo(self) -> None:
        r = self.repo
        _copy_lf(ROOT / "deploy" / "deploy.sh", r / "deploy" / "deploy.sh")
        _copy_lf(ROOT / "pyproject.toml", r / "pyproject.toml")
        _write_lf(r / "app.py", "print('app')\n")
        _write_lf(r / "sespy_app" / "__init__.py", '__version__ = "1.0.0"\n')
        # a local runtime DB that must never ship
        _write_lf(r / "sespy_app" / "logs" / "local.db", "LOCAL-DEV-DB")
        _write_lf(r / "docs" / "MANUAL.md", "# Manual\n")
        (r / "docs" / "screenshots").mkdir(parents=True, exist_ok=True)
        assert not (r / "deploy" / "config.env").exists()

    def _build_stubs(self) -> None:
        for name, body in (("ssh", STUB_SSH), ("scp", STUB_SCP), ("sudo", STUB_SUDO),
                           ("fake_check_live", STUB_CHECK_LIVE)):
            _write_lf(self.stubs / name, body)
            # A shebang alone does not make a file runnable on POSIX (Git Bash
            # on Windows ignores the mode, which hid this locally).
            (self.stubs / name).chmod(0o755)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for k in ("DEPLOY_REPO_ROOT", "DEPLOY_LIBRARY_ROOT", "DEPLOY_MOSAIC_DIR", "DEPLOY_SUDO_TEST",
                  "SESPY_SKIP_LIVE_CHECK", "STUB_EXIT", "SESPY_R_DATA_DIR"):
            env.pop(k, None)
        env.update(
            DEPLOY_SERVER=SERVER,
            DEPLOY_DIR=self.app_posix,
            DEPLOY_LIBRARY_ROOT=self.lib_posix,
            SESPY_PY="fake_check_live",
            STUB_LOG=_posix(self.log),
            STUB_DIR=_posix(self.stubs),
            PATH=str(self.stubs) + os.pathsep + env.get("PATH", ""),
        )
        return env

    def _assert_stubs_shadow(self) -> None:
        out = subprocess.run([BASH, "-c", "command -v ssh scp sudo fake_check_live"],
                             env=self.env, capture_output=True, text=True)
        found = out.stdout.split()
        want = _posix(self.stubs)
        assert len(found) == 4 and all(f.startswith(want + "/") for f in found), (
            f"stubs do not shadow the real tools; refusing to run deploy.sh: {found}")

    # -- server state ----------------------------------------------------
    def put(self, rel: str, data: bytes | str = b"x") -> Path:
        p = self.app / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        return p

    def old_layout(self, db: bytes | None = None, wal: bytes | None = None) -> None:
        """Pre-split server: sespy/ is the monolith and owns logs/."""
        self.put("sespy/__init__.py", '__version__ = "1.17.1"\n')
        self.put("sespy/modules/legacy_view.py", "monolith UI")
        if db is not None:
            self.put("sespy/logs/feedback.db", db)
        if wal is not None:
            self.put("sespy/logs/feedback.db-wal", wal)

    def new_layout(self, db: bytes | None = None, wal: bytes | None = None) -> None:
        """Post-cutover server: sespy/ is the library, sespy_app/ owns logs/."""
        self.put("sespy/__init__.py", '__version__ = "1.17.9"\n')
        self.put("sespy_app/__init__.py", '__version__ = "0.9.0"\n')
        if db is not None:
            self.put("sespy_app/logs/feedback.db", db)
        if wal is not None:
            self.put("sespy_app/logs/feedback.db-wal", wal)

    def settings(self, text: str) -> None:
        self.put("server_settings.env", text)

    def live_files(self, names=OVERRIDE_NAMES, version_info: bool = True) -> None:
        self.live_dir.mkdir(parents=True, exist_ok=True)
        for n in names:
            (self.live_dir / n).write_text("{}", encoding="utf-8")
        if version_info:
            (self.live_dir.parent / "VERSION_INFO.json").write_text('{"version": "1.35.6"}')

    def settings_on(self, **kw) -> None:
        self.live_files(**kw)
        self.settings(f"SESPY_R_DATA_DIR={self.live_posix}\n")

    # -- run ---------------------------------------------------------------
    def run(self, *args: str, _prelude: str = "", **env: str) -> subprocess.CompletedProcess:
        """`_prelude` runs in bash before exec'ing the script (e.g. to export a
        set-but-empty variable, which a Windows env block may not carry)."""
        e = dict(self.env)
        e.update(env)
        script = _posix(self.repo / "deploy" / "deploy.sh")
        cmd = ([BASH, "-c", _prelude + '\nexec "$BASH" "$0" "$@"', script, *args]
               if _prelude else [BASH, script, *args])
        cp = subprocess.run(cmd, env=e, capture_output=True, text=True, timeout=180)
        self.out = cp.stdout + cp.stderr
        return cp

    def log_lines(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def calls(self, kind: str) -> list[str]:
        return [ln for ln in self.log_lines() if ln.startswith(kind + " ")]

    def check_calls(self) -> list[str]:
        """check_live calls other than --print-override-names."""
        return [c for c in self.calls("check_live") if "--print-override-names" not in c]

    def fetch_scps(self) -> list[str]:
        return [c for c in self.calls("scp") if f"{SERVER}:" in c.split()[2]]

    def incoming_scps(self) -> list[str]:
        return [c for c in self.calls("scp") if c.split()[-1].endswith(".incoming")]

    @property
    def seen_live(self) -> Path:
        return self.stubs / "seen_live"


@pytest.fixture(scope="module")
def lib_template(tmp_path_factory) -> Path:
    return _build_lib(tmp_path_factory.mktemp("libtpl").resolve() / "lib", PIN)


@pytest.fixture
def make_fake(tmp_path, lib_template):
    counter = iter(range(100))

    def _make() -> Fake:
        return Fake(tmp_path / f"f{next(counter)}", lib_template)
    return _make


@pytest.fixture
def fake(make_fake) -> Fake:
    return make_fake()


def _ok(cp: subprocess.CompletedProcess, fake: Fake) -> None:
    assert cp.returncode == 0, fake.out


def _rescued(fake: Fake) -> list[Path]:
    return sorted(p for p in fake.app.iterdir() if p.name.startswith("logs.rescued-"))


SWAP_APP = "mv -T sespy_app.incoming sespy_app"
SWAP_LIB = "mv -T sespy.incoming sespy"


def _swap_cmd(fake: Fake) -> str:
    cmds = [c for c in fake.calls("ssh") if SWAP_APP in c or SWAP_LIB in c]
    assert len(cmds) == 1, cmds
    return cmds[0]


# ---------------------------------------------------------------------------
def test_bash_syntax(fake):
    cp = subprocess.run([BASH, "-n", _posix(fake.repo / "deploy" / "deploy.sh")],
                        capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr


def test_first_deploy_without_existing_sespy(fake):
    assert not fake.app.exists()
    _ok(fake.run(), fake)
    assert (fake.app / "sespy" / "__init__.py").read_text() == (fake.lib / "sespy" / "__init__.py").read_text()
    assert (fake.app / "sespy" / "data" / "r_kb" / "manifest.json").is_file()
    assert (fake.app / "sespy_app" / "__init__.py").is_file()
    for leftover in ("sespy.old", "sespy.incoming", "sespy_app.old", "sespy_app.incoming"):
        assert not (fake.app / leftover).exists(), leftover
    assert not (fake.app / "sespy" / "logs").exists(), "the library's local logs must never ship"
    assert not (fake.app / "sespy_app" / "logs").exists(), "the app's local logs must never ship"
    assert (fake.app / "app.py").is_file()
    assert (fake.app / "docs" / "MANUAL.md").is_file()
    assert (fake.app / "restart.txt").is_file()


def test_uses_incoming_swap(fake):
    fake.new_layout()
    fake.put("sespy/old_module.py", "old")
    fake.put("sespy_app/old_view.py", "old")
    _ok(fake.run(), fake)
    targets = sorted(c.split()[-1] for c in fake.incoming_scps())
    assert targets == [f"{SERVER}:{fake.app_posix}/sespy.incoming",
                       f"{SERVER}:{fake.app_posix}/sespy_app.incoming"]
    swap = _swap_cmd(fake)
    assert SWAP_APP in swap and SWAP_LIB in swap
    assert not (fake.app / "sespy" / "old_module.py").exists()
    assert not (fake.app / "sespy_app" / "old_view.py").exists()


def test_app_py_uploaded_after_swap(fake):
    _ok(fake.run(), fake)
    lines = fake.log_lines()
    i_incoming = max(i for i, ln in enumerate(lines)
                     if ln.startswith("scp ") and ln.endswith(".incoming"))
    i_swap = next(i for i, ln in enumerate(lines)
                  if ln.startswith("ssh ") and SWAP_LIB in ln)
    i_app = next(i for i, ln in enumerate(lines)
                 if ln.startswith("scp ") and "/app.py" in ln)
    assert i_incoming < i_swap < i_app
    app_scp = lines[i_app].split()
    assert not any(a.endswith(("/sespy", "/sespy_app")) for a in app_scp), (
        "sespy and sespy_app must be excluded from the glob scp")


def test_logs_moved_into_incoming_before_swap(fake):
    payload = bytes(range(256)) * 7
    fake.new_layout(db=payload)
    fake.put("sespy_app/old_view.py", "old")
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy_app" / "old_view.py").exists()
    assert not (fake.app / "sespy_app" / "logs" / "local.db").exists(), "local logs must never ship"
    assert not (fake.app / "sespy_app" / "logs" / "logs").exists()
    assert not (fake.app / "sespy" / "logs").exists()
    swap = _swap_cmd(fake)
    assert swap.index("mv -T sespy_app/logs sespy_app.incoming/logs") < swap.index(SWAP_APP)


def test_recovers_interrupted_swap(fake):
    """The swap died between the two sespy_app renames."""
    payload = b"FEEDBACK-OLD-" * 50
    fake.put("sespy/__init__.py", "library")
    fake.put("sespy_app.old/logs/feedback.db", payload)
    fake.put("sespy_app.old/stale.py", "old")
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy_app.old").exists()
    assert not (fake.app / "sespy_app" / "stale.py").exists()
    assert _rescued(fake) == []


def test_recovers_pre_cutover_interrupted_swap(fake):
    """The OLD script died between its two sespy renames (no sespy_app yet):
    the monolith comes back, then the cutover moves its logs."""
    payload = b"FEEDBACK-PRECUT-" * 50
    fake.put("sespy.old/logs/feedback.db", payload)
    fake.put("sespy.old/modules/x.py", "old")
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy" / "logs").exists()
    assert not (fake.app / "sespy" / "modules").exists()
    assert not (fake.app / "sespy.old").exists()
    assert _rescued(fake) == []


def test_recovers_logs_left_in_incoming(fake):
    """Died after logs moved into sespy_app.incoming but before the swap: the
    prologue must rescue them before it wipes sespy_app.incoming."""
    payload = b"FEEDBACK-INCOMING-" * 40
    fake.new_layout()
    fake.put("sespy_app.incoming/logs/feedback.db", payload)
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy_app.incoming").exists()


def test_recovers_cutover_logs_left_in_incoming(fake):
    """The cutover died after sespy/logs moved into sespy_app.incoming: with
    no sespy_app/ yet the logs return to sespy/logs (the old layout, intact;
    never a bare sespy_app/ MosaicSES would import), and the rerun migrates."""
    payload = b"FEEDBACK-CUTOVER-" * 40
    fake.old_layout()
    fake.put("sespy_app.incoming/logs/feedback.db", payload)
    fake.put("sespy_app.incoming/__init__.py", "half uploaded")
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy" / "logs").exists()
    assert not (fake.app / "sespy_app.incoming").exists()
    prologue = [c for c in fake.calls("ssh") if "rescue_logs sespy_app.incoming" in c]
    assert len(prologue) == 1
    assert "rm -rf sespy_app.old sespy_app.incoming sespy.old sespy.incoming" in prologue[0]


def test_recovers_pre_cutover_logs_left_in_incoming(fake):
    """The OLD script died after moving sespy/logs into sespy.incoming."""
    payload = b"FEEDBACK-OLD-INCOMING-" * 40
    fake.old_layout()
    fake.put("sespy.incoming/logs/feedback.db", payload)
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy" / "logs").exists()
    assert not (fake.app / "sespy.incoming").exists()


def test_recovers_logs_when_swap_died_between_moves(fake):
    """sespy_app -> sespy_app.old done, incoming (holding logs) not yet renamed."""
    payload = b"FEEDBACK-MIDSWAP-" * 40
    fake.put("sespy/__init__.py", "library")
    fake.put("sespy_app.old/__init__.py", "old")
    fake.put("sespy_app.incoming/logs/feedback.db", payload)
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy_app.old").exists()
    assert not (fake.app / "sespy_app.incoming").exists()


def test_recovers_deploy_keep_logs(fake):
    payload = b"FEEDBACK-KEEP-" * 30
    fake.put(".deploy-keep/logs/feedback.db", payload)
    fake.put("sespy/__init__.py", "half-deployed by the old script")
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == payload
    assert not (fake.app / "sespy" / "logs").exists()
    assert not (fake.app / ".deploy-keep").exists()


def test_incoming_removed_before_upload(fake):
    fake.put("sespy.incoming/x", "leftover")
    fake.put("sespy_app.incoming/x", "leftover")
    _ok(fake.run(), fake)
    for pkg in ("sespy", "sespy_app"):
        assert not (fake.app / f"{pkg}.incoming").exists()
        assert not (fake.app / pkg / "x").exists()
        assert not (fake.app / pkg / pkg).exists(), "upload nested into a leftover dir"
        assert (fake.app / pkg / "__init__.py").is_file()


def test_settings_file_created_only_if_absent_with_0644(make_fake):
    existing = make_fake()
    original = b"SESPY_R_DATA_DIR=off\r\n# hand-edited, keep\r\n"
    existing.put("server_settings.env", original)
    _ok(existing.run(), existing)
    assert (existing.app / "server_settings.env").read_bytes() == original

    fresh = make_fake()
    _ok(fresh.run(), fresh)
    created = (fresh.app / "server_settings.env").read_bytes()
    assert created == b"SESPY_R_DATA_DIR=/srv/shiny-server/marinesabres/data\n"
    # NTFS under MSYS cannot show Unix modes (stat reports 644 whatever chmod
    # did), so the 0644 contract is asserted on the remote command instead.
    # chmod sits outside the creation branch: an existing file is chmodded too.
    final = fresh.calls("ssh")[-1]
    assert "umask 022" in final and "[ ! -e " in final
    create_branch, after = final.split(" fi ", 1)
    assert "chmod 0644" not in create_branch and "chmod 0644" in after
    assert "chmod 0644" in existing.calls("ssh")[-1]


def test_settings_off_skips_fetch(fake):
    fake.live_files()
    fake.settings("SESPY_R_DATA_DIR=OFF\n")
    _ok(fake.run(), fake)
    assert fake.fetch_scps() == []
    checks = fake.check_calls()
    assert len(checks) == 1 and "--stage" in checks[0]
    assert "--live" not in checks[0] and "--perms" not in checks[0]
    assert not any("--print-override-names" in c for c in fake.calls("check_live"))


def test_fetch_failure_recorded_not_fatal(fake):
    fake.settings_on(names=["ses_knowledge_db.json"], version_info=False)
    _ok(fake.run(), fake)
    checks = fake.check_calls()
    assert len(checks) == 1 and "--live" in checks[0] and "--perms" in checks[0]
    missing = (fake.seen_live / "missing.txt").read_text().splitlines()
    assert missing == ["country_governance_db.json"]  # absent VERSION_INFO.json is not recorded
    assert (fake.seen_live / "ses_knowledge_db.json").is_file()
    perms = (fake.seen_live / "perms.txt").read_text().splitlines()
    assert any(ln.startswith("stat ") and ln.endswith(f"{fake.live_posix}/ses_knowledge_db.json")
               for ln in perms)
    assert any(ln.startswith("stat ") and ln.endswith(f" {fake.live_posix}") for ln in perms)
    assert any(ln.startswith("stat ") and ln.endswith(" /") for ln in perms)
    assert (fake.app / "sespy" / "__init__.py").is_file(), "the run continued to the upload"


def test_version_info_fetched_from_parent(fake):
    fake.settings_on()
    _ok(fake.run(), fake)
    assert (fake.seen_live / "VERSION_INFO.json").is_file()
    assert not (fake.seen_live / "missing.txt").exists()


def test_check_live_nonzero_aborts_before_upload(fake):
    fake.old_layout(db=b"DB")
    fake.put("sespy/marker.py", "live code")
    fake.settings_on()
    cp = fake.run(STUB_EXIT="2")
    assert cp.returncode == 2, fake.out
    assert fake.incoming_scps() == []
    assert not any("mv -T sespy" in c for c in fake.calls("ssh"))
    assert (fake.app / "sespy" / "marker.py").read_text() == "live code"
    assert (fake.app / "sespy" / "logs" / "feedback.db").read_bytes() == b"DB"
    assert not (fake.app / "sespy_app").exists()
    assert not (fake.app / "restart.txt").exists()


def test_rollback_passes_warn_only(make_fake):
    f1 = make_fake()
    f1.settings_on()
    _ok(f1.run("--rollback"), f1)
    checks = f1.check_calls()
    assert len(checks) == 1 and "--warn-only" in checks[0]

    f2 = make_fake()  # a normal deploy does not pass it
    _ok(f2.run(), f2)
    assert "--warn-only" not in f2.check_calls()[0]

    f3 = make_fake()  # rollback is never gated, even by a failing check
    cp = f3.run("--rollback", STUB_EXIT="2")
    assert cp.returncode == 0, f3.out
    assert (f3.app / "sespy" / "__init__.py").is_file()


def test_skip_env_var_guards_check_live_call(fake):
    fake.settings_on()
    _ok(fake.run(SESPY_SKIP_LIVE_CHECK="hotfix 42, R server down"), fake)
    assert fake.calls("check_live") == []
    assert fake.fetch_scps() == []
    assert "skipping live check: hotfix 42, R server down" in fake.out
    assert (fake.app / "sespy" / "__init__.py").is_file()


def test_override_names_from_check_live(fake):
    fake.settings_on()
    _ok(fake.run(), fake)
    assert any("--print-override-names" in c for c in fake.calls("check_live"))
    fetched = [c.split()[2] for c in fake.fetch_scps()]
    prefix = f"{SERVER}:{fake.live_posix}/"
    names = [f[len(prefix):] for f in fetched if f.startswith(prefix)]
    assert names == OVERRIDE_NAMES  # exactly the printed names, CR stripped, in order
    assert sorted(p.name for p in fake.seen_live.iterdir()) == sorted(
        [*OVERRIDE_NAMES, "VERSION_INFO.json", "perms.txt"])
    assert OVERRIDE_NAMES == [r.name for r in r_kb.ALLOW_LIST if r.override]


def test_outputs_fit_the_real_check_live_contract(fake):
    """The stub stands in for check_live; check the argv and perms.txt that
    deploy.sh produced against the REAL parser and perms reader."""
    check_live = _check_live()
    fake.settings_on()
    _ok(fake.run(), fake)
    argv = fake.check_calls()[0].split()[2:]  # drop "check_live" and the script path
    args = check_live.build_parser().parse_args(argv)
    assert args.stage and args.live and args.perms and not args.warn_only
    problems = check_live.check_perms((fake.seen_live / "perms.txt").read_text())
    # mode/owner verdicts are meaningless on MSYS; every path must have its line
    assert not [p for p in problems if "no permission line" in p or "no stat line" in p], problems


def test_mosaic_restart_conditional(make_fake):
    with_mosaic = make_fake()
    mosaic = with_mosaic.server / "mosaic"
    mosaic.mkdir(parents=True)
    _ok(with_mosaic.run(DEPLOY_MOSAIC_DIR=_posix(mosaic)), with_mosaic)
    assert (mosaic / "restart.txt").is_file()
    assert (with_mosaic.app / "restart.txt").is_file()

    without = make_fake()
    mosaic2 = without.server / "mosaic"
    mosaic2.mkdir(parents=True)
    _ok(without.run(), without)
    assert not (mosaic2 / "restart.txt").exists()
    assert not any("/mosaic/restart.txt" in c for c in without.calls("ssh"))
    assert sum(c.count("restart.txt") for c in without.calls("ssh")) == 1


def test_gitignore_has_server_settings():
    lines = [ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "server_settings.env" in lines


def test_config_example_documents_optional_settings():
    text = (ROOT / "deploy" / "config.env.example").read_text(encoding="utf-8")
    for key in ("DEPLOY_LIBRARY_ROOT", "DEPLOY_MOSAIC_DIR", "DEPLOY_REPO_ROOT", "DEPLOY_SUDO_TEST"):
        assert key in text


def test_absent_check_live_skips_fetch_and_check(fake):
    lib = _build_lib(fake.base / "lib_without_check_live", PIN, check_live=False)
    fake.settings_on()
    _ok(fake.run(DEPLOY_LIBRARY_ROOT=_posix(lib)), fake)
    assert fake.calls("check_live") == []
    assert fake.fetch_scps() == []
    assert "check_live.py is absent" in fake.out
    assert (fake.app / "sespy" / "__init__.py").is_file()


def test_unreadable_settings_aborts_unless_rollback(make_fake):
    f1 = make_fake()
    (f1.app / "server_settings.env").mkdir(parents=True)  # exists, but cat fails
    f1.live_files()
    cp = f1.run()
    assert cp.returncode != 0
    assert "cannot be read" in f1.out
    assert not (f1.app / "sespy").exists()
    assert f1.check_calls() == []

    f2 = make_fake()
    (f2.app / "server_settings.env").mkdir(parents=True)
    cp = f2.run("--rollback")
    assert cp.returncode == 0, f2.out
    checks = f2.check_calls()
    assert len(checks) == 1 and "--warn-only" in checks[0] and "--live" not in checks[0]
    assert (f2.app / "sespy" / "__init__.py").is_file()


@pytest.mark.parametrize("value", ["/srv/a b", "'/srv/quoted'", "relative/dir", "/srv/dätä"])
def test_unsafe_settings_dir_is_fetch_failure(fake, value):
    fake.settings(f"SESPY_R_DATA_DIR={value}\n")
    cp = fake.run()
    assert cp.returncode == 0, fake.out  # the stub check passes; the real one blocks on missing.txt
    assert fake.fetch_scps() == []
    assert not any("stat -c" in c for c in fake.calls("ssh"))
    assert (fake.seen_live / "missing.txt").read_text().splitlines() == OVERRIDE_NAMES
    assert "--live" in fake.check_calls()[0]


def test_sudo_test_only_when_enabled(make_fake):
    plain = make_fake()
    plain.settings_on()
    _ok(plain.run(), plain)
    assert not any("sudo -n" in c for c in plain.calls("ssh"))
    assert plain.calls("sudo") == []

    sudo = make_fake()
    sudo.settings_on()
    _ok(sudo.run(DEPLOY_SUDO_TEST="1"), sudo)
    assert any("sudo -n -u shiny true" in c for c in sudo.calls("ssh"))
    assert sudo.calls("sudo") == ["sudo -n -u shiny true"]  # tried once, then stat fallback
    perms = (sudo.seen_live / "perms.txt").read_text().splitlines()
    assert perms and all(ln.startswith("stat ") for ln in perms)


def test_sudo_test_success_lines_accepted_by_check_live(fake):
    check_live = _check_live()
    fake.settings_on()
    _ok(fake.run(DEPLOY_SUDO_TEST="1", STUB_SUDO_OK="1"), fake)
    perms = (fake.seen_live / "perms.txt").read_text().splitlines()
    assert perms == [f"sudo-test {fake.live_posix}/{n} ok" for n in OVERRIDE_NAMES]
    assert check_live.check_perms("\n".join(perms)) == []


def test_repo_root_override_deploys_other_checkout(fake):
    """Rollback mechanics, app version to app version: the CURRENT script
    deploys an older sespy-app worktree with the library tag it pins."""
    old = fake.base / "old_app"
    _write_lf(old / "app.py", "print('old app')\n")
    _write_lf(old / "sespy_app" / "__init__.py", '__version__ = "0.9.0"\n')
    _write_lf(old / "pyproject.toml",
              'dependencies = ["sespy[pdf] @ git+https://example.invalid/SESPy@v1.17.9"]\n')
    _write_lf(old / "docs" / "MANUAL.md", "# Old manual\n")
    (old / "docs" / "screenshots").mkdir(parents=True)
    old_lib = _build_lib(fake.base / "old_lib", "1.17.9")
    fake.new_layout(db=b"KEEP-ME")
    fake.settings_on()
    _ok(fake.run("--rollback", DEPLOY_REPO_ROOT=_posix(old), DEPLOY_LIBRARY_ROOT=_posix(old_lib)), fake)
    assert (fake.app / "app.py").read_text() == "print('old app')\n"
    assert (fake.app / "sespy_app" / "__init__.py").read_text() == '__version__ = "0.9.0"\n'
    assert '__version__ = "1.17.9"' in (fake.app / "sespy" / "__init__.py").read_text()
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == b"KEEP-ME"
    checks = fake.check_calls()
    assert len(checks) == 1 and "--warn-only" in checks[0]
    assert _native(_posix(old_lib / "tests" / "parity" / "check_live.py")) in checks[0]


def test_rollback_still_validates_the_library(fake):
    old = fake.base / "old_app"
    _write_lf(old / "app.py", "print('old app')\n")
    _write_lf(old / "sespy_app" / "__init__.py", '__version__ = "0.9.0"\n')
    _write_lf(old / "pyproject.toml", 'dependencies = ["sespy @ git+https://example.invalid/SESPy@v1.17.9"]\n')
    cp = fake.run("--rollback", DEPLOY_REPO_ROOT=_posix(old))  # library still at v<PIN>
    assert cp.returncode != 0
    assert "v1.17.9" in fake.out
    assert fake.calls("scp") == [] and fake.calls("ssh") == []


def test_default_dir_used_when_settings_absent(fake):
    _ok(fake.run(), fake)
    fetched = [c.split()[2] for c in fake.fetch_scps()]
    assert f"{SERVER}:/srv/shiny-server/marinesabres/data/ses_knowledge_db.json" in fetched
    assert sorted((fake.seen_live / "missing.txt").read_text().splitlines()) == sorted(OVERRIDE_NAMES)


# ---------------------------------------------------------------------------
# Fix round 1: stranded logs (I1), non-blocking tail (M6), gates (M4/M5/M8),
# failing swap (M10)
# ---------------------------------------------------------------------------
def test_incoming_logs_kept_when_sespy_logs_recreated(fake):
    """Swap died after logs moved into sespy_app.incoming; a worker then
    recreated an empty sespy_app/logs. The real history must not be deleted
    with incoming."""
    real = b"REAL-HISTORY-" * 64
    fake.new_layout(db=b"NEW-EMPTY")
    fake.put("sespy_app.incoming/logs/feedback.db", real)
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == b"NEW-EMPTY"
    rescued = _rescued(fake)
    assert len(rescued) == 1
    assert (rescued[0] / "feedback.db").read_bytes() == real
    assert "sespy_app.incoming/logs was stranded" in fake.out
    assert not (fake.app / "sespy_app.incoming").exists()


def test_old_logs_kept_when_sespy_present(make_fake):
    real = b"REAL-OLD-HISTORY-" * 64
    # sespy_app present WITH recreated logs: old logs go aside
    f1 = make_fake()
    f1.new_layout(db=b"NEW-EMPTY")
    f1.put("sespy_app.old/logs/feedback.db", real)
    _ok(f1.run(), f1)
    assert (f1.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == b"NEW-EMPTY"
    rescued = _rescued(f1)
    assert len(rescued) == 1 and (rescued[0] / "feedback.db").read_bytes() == real
    assert "sespy_app.old/logs was stranded" in f1.out
    assert not (f1.app / "sespy_app.old").exists()

    # sespy_app present WITHOUT logs: old logs are restored in place
    f2 = make_fake()
    f2.new_layout()
    f2.put("sespy_app.old/logs/feedback.db", real)
    _ok(f2.run(), f2)
    assert (f2.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == real
    assert _rescued(f2) == []
    assert not (f2.app / "sespy_app.old").exists()


def test_failing_swap_step_aborts(fake):
    fake.new_layout(db=b"DB")
    cp = fake.run(STUB_SSH_FAIL=SWAP_LIB, STUB_SSH_FAIL_RC="1")
    assert cp.returncode != 0
    assert len(fake.incoming_scps()) == 2, "the run reached the swap"
    assert "the swap on" in fake.out
    assert not any("/app.py" in c for c in fake.calls("scp")), "app.py uploaded after a failed swap"
    assert not (fake.app / "restart.txt").exists()
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == b"DB"


def test_settings_create_failure_still_restarts(fake):
    # printf as a failing function: the settings write fails remotely
    cp = fake.run(STUB_SSH_PRELUDE="printf() { return 1; }")
    assert cp.returncode == 0, fake.out
    assert "could not create" in fake.out
    assert (fake.app / "restart.txt").is_file()


def test_mosaic_touch_failure_only_warns(fake):
    cp = fake.run(DEPLOY_MOSAIC_DIR=_posix(fake.server / "no" / "such" / "dir"))
    assert cp.returncode == 0, fake.out
    assert "could not touch" in fake.out
    assert "==> Done." in fake.out
    assert (fake.app / "restart.txt").is_file()


def test_settings_read_ssh_failure_gates_unless_rollback(make_fake):
    f1 = make_fake()
    cp = f1.run(STUB_SSH_FAIL="|| exit 4")
    assert cp.returncode == 2, f1.out
    assert not (f1.app / "sespy").exists()

    f2 = make_fake()
    cp = f2.run("--rollback", STUB_SSH_FAIL="|| exit 4")
    assert cp.returncode == 0, f2.out
    assert "could not read" in f2.out
    checks = f2.check_calls()
    assert len(checks) == 1 and "--live" not in checks[0] and "--warn-only" in checks[0]
    assert (f2.app / "sespy" / "__init__.py").is_file()


def test_empty_skip_var_counts_as_unset(fake):
    fake.settings_on()
    _ok(fake.run(_prelude="export SESPY_SKIP_LIVE_CHECK="), fake)
    assert "set but empty" in fake.out
    assert "skipping live check" not in fake.out
    assert len(fake.check_calls()) == 1


def test_skip_var_in_config_env_is_ignored(fake):
    # The one test that writes a config.env, inside its own temp repo. It also
    # pins DEPLOY_LIBRARY_ROOT precedence: the env value beats config.env's
    # (so a rollback can pass an older library per run).
    _write_lf(fake.repo / "deploy" / "config.env",
              f"DEPLOY_SERVER={SERVER}\nDEPLOY_DIR={fake.app_posix}\n"
              "DEPLOY_LIBRARY_ROOT=/no/such/library\n"
              "SESPY_SKIP_LIVE_CHECK=from-config\n")
    fake.settings_on()
    _ok(fake.run(), fake)
    assert "skipping live check" not in fake.out
    assert len(fake.check_calls()) == 1


def test_no_override_names_gates_unless_rollback(make_fake):
    f1 = make_fake()
    f1.settings_on()
    cp = f1.run(STUB_NO_NAMES="1")
    assert cp.returncode == 2, f1.out
    assert "gave no names" in f1.out
    assert not (f1.app / "sespy").exists()

    f2 = make_fake()
    f2.settings_on()
    cp = f2.run("--rollback", STUB_NO_NAMES="1")
    assert cp.returncode == 0, f2.out
    assert "gave no names" in f2.out
    checks = f2.check_calls()
    assert len(checks) == 1 and "--live" not in checks[0]
    assert (f2.app / "sespy" / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# Library split (Task 6): pinned library vendored, sespy_app swapped first,
# feedback.db moves to sespy_app/logs once
# ---------------------------------------------------------------------------
def _move_past_tag(lib: Path) -> None:
    _write_lf(lib / "NEWS.md", "unreleased\n")
    _git(lib, "add", "-A")
    _git(lib, "commit", "-q", "-m", "one commit past the tag")


def _make_dirty(lib: Path) -> None:
    _write_lf(lib / "sespy" / "stray.py", "# untracked, would ship with cp -R\n")


def _version_mismatch(lib: Path) -> None:
    _write_lf(lib / "sespy" / "__init__.py", '__version__ = "9.9.9"\n')
    _git(lib, "add", "-A")
    _git(lib, "commit", "-q", "-m", "wrong version")
    _git(lib, "tag", "-f", f"v{PIN}")


@pytest.mark.parametrize("breakage", [_move_past_tag, _make_dirty, _version_mismatch],
                         ids=["wrong_tag", "dirty", "version_mismatch"])
def test_library_root_must_match_pin(fake, breakage):
    fake.old_layout(db=b"DB")
    fake.settings_on()
    breakage(fake.lib)
    cp = fake.run()
    assert cp.returncode != 0, fake.out
    assert f"v{PIN}" in fake.out
    assert fake.calls("scp") == [], "nothing may be fetched or uploaded"
    assert fake.calls("ssh") == [], "the library is validated before any remote call"
    assert fake.calls("check_live") == []
    assert not (fake.app / "sespy_app").exists()
    assert (fake.app / "sespy" / "logs" / "feedback.db").read_bytes() == b"DB"


def test_library_root_unset_aborts(fake):
    cp = fake.run(DEPLOY_LIBRARY_ROOT="")
    assert cp.returncode != 0
    assert "DEPLOY_LIBRARY_ROOT" in fake.out and f"v{PIN}" in fake.out
    assert fake.calls("scp") == [] and fake.calls("ssh") == []


def test_cutover_moves_feedback_db_once(fake):
    db = b"SQLite format 3\x00" + bytes(range(256)) * 9
    wal = b"WAL-FRAMES-" * 33
    fake.old_layout(db=db, wal=wal)
    _ok(fake.run(), fake)
    logs = fake.app / "sespy_app" / "logs"
    assert (logs / "feedback.db").read_bytes() == db
    assert (logs / "feedback.db-wal").read_bytes() == wal
    assert not (fake.app / "sespy" / "logs").exists()
    assert not (fake.app / "sespy" / "modules").exists(), "the monolith is replaced by the library"
    assert _rescued(fake) == []
    swap = _swap_cmd(fake)
    assert swap.index("mv -T sespy/logs sespy_app.incoming/logs") < swap.index(SWAP_APP)

    # once: the next deploy keeps the store where it now lives
    fake.log.unlink()
    _ok(fake.run(), fake)
    assert (logs / "feedback.db").read_bytes() == db
    assert (logs / "feedback.db-wal").read_bytes() == wal
    assert not (fake.app / "sespy" / "logs").exists()
    assert _rescued(fake) == []


def test_two_feedback_dbs_abort_before_swap(fake):
    fake.old_layout(db=b"MONOLITH-DB")
    fake.put("sespy_app/__init__.py", "app")
    fake.put("sespy_app/logs/feedback.db", b"APP-DB")
    cp = fake.run()
    assert cp.returncode != 0
    assert "two feedback stores" in fake.out
    assert f"{fake.app_posix}/sespy/logs" in fake.out and f"{fake.app_posix}/sespy_app/logs" in fake.out
    assert fake.incoming_scps() == []
    assert not any(SWAP_APP in c or SWAP_LIB in c for c in fake.calls("ssh"))
    assert (fake.app / "sespy" / "logs" / "feedback.db").read_bytes() == b"MONOLITH-DB"
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == b"APP-DB"
    assert not (fake.app / "restart.txt").exists()


def test_swap_order_uploads_then_one_rename_chain(fake):
    fake.old_layout(db=b"DB")
    _ok(fake.run(), fake)
    lines = fake.log_lines()
    renames = ("mv -T sespy_app sespy_app.old", SWAP_APP, "mv -T sespy sespy.old", SWAP_LIB)
    i_app_in = next(i for i, ln in enumerate(lines)
                    if ln.startswith("scp ") and ln.endswith(f"{SERVER}:{fake.app_posix}/sespy_app.incoming"))
    i_lib_in = next(i for i, ln in enumerate(lines)
                    if ln.startswith("scp ") and ln.endswith(f"{SERVER}:{fake.app_posix}/sespy.incoming"))
    i_first_rename = next(i for i, ln in enumerate(lines)
                          if ln.startswith("ssh ") and any(r in ln for r in renames))
    assert i_app_in < i_first_rename and i_lib_in < i_first_rename
    chain = lines[i_first_rename]
    positions = [chain.index(r) for r in renames]
    assert positions == sorted(positions), "sespy_app pair first, then the sespy pair"
    assert sum(any(r in ln for r in renames) for ln in lines if ln.startswith("ssh ")) == 1, (
        "both rename pairs sit in one ssh command")
    i_app_py = next(i for i, ln in enumerate(lines) if ln.startswith("scp ") and "/app.py" in ln)
    assert i_app_py > i_first_rename


def test_old_worker_db_after_swap_is_rescued(fake):
    """An old-code worker recreates sespy/logs inside the NEW library right
    after the sespy rename: it is moved aside, never deleted."""
    db = b"REAL-FEEDBACK-" * 40
    fake.old_layout(db=db)
    prelude = ('mv() { command mv "$@" || return; '
               'if [ "$*" = "-T sespy.incoming sespy" ]; then '
               'mkdir -p sespy/logs && printf OLD-WORKER > sespy/logs/feedback.db; fi; }')
    _ok(fake.run(STUB_SSH_PRELUDE=prelude), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == db
    assert not (fake.app / "sespy" / "logs").exists()
    rescued = _rescued(fake)
    assert len(rescued) == 1
    assert (rescued[0] / "feedback.db").read_bytes() == b"OLD-WORKER"
    assert "WARNING" in fake.out and rescued[0].name in fake.out


def test_old_worker_db_in_old_package_is_rescued(fake):
    """An old worker recreates sespy/logs between the logs move and the sespy
    rename: it lands in sespy.old, which must not be deleted with it."""
    db = b"REAL-FEEDBACK-" * 40
    fake.old_layout(db=db)
    prelude = ('mv() { command mv "$@" || return; '
               'if [ "$*" = "-T sespy_app.incoming sespy_app" ]; then '
               'mkdir -p sespy/logs && printf OLD-WORKER-2 > sespy/logs/feedback.db; fi; }')
    _ok(fake.run(STUB_SSH_PRELUDE=prelude), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == db
    assert not (fake.app / "sespy.old").exists()
    rescued = _rescued(fake)
    assert len(rescued) == 1
    assert (rescued[0] / "feedback.db").read_bytes() == b"OLD-WORKER-2"


def test_repeat_deploy_keeps_app_db(fake):
    db = b"SQLite format 3\x00" + b"APP-ROWS" * 300
    wal = b"APP-WAL" * 20
    fake.new_layout(db=db, wal=wal)
    _ok(fake.run(), fake)
    assert (fake.app / "sespy_app" / "logs" / "feedback.db").read_bytes() == db
    assert (fake.app / "sespy_app" / "logs" / "feedback.db-wal").read_bytes() == wal
    assert not (fake.app / "sespy" / "logs").exists()
    assert _rescued(fake) == []


def test_check_live_runs_from_library_root(fake):
    fake.settings_on()
    _ok(fake.run(), fake)
    script = _native(fake.lib_posix + "/tests/parity/check_live.py")
    checks = fake.check_calls()
    assert len(checks) == 1 and checks[0].split()[1] == script
    assert all(c.split()[1] == script for c in fake.calls("check_live"))
    assert fake.calls("pythonpath") and all(
        ln == f"pythonpath {_native(fake.lib_posix)}" for ln in fake.calls("pythonpath"))
    assert not (fake.repo / "tests").exists(), "the app checkout carries no check_live.py"


# ---------------------------------------------------------------------------
# Settings parse parity: deploy.sh's bash mirror vs sespy.r_kb (Ruling F10)
# ---------------------------------------------------------------------------
PARSE_CASES = {
    "absent": None,
    "empty_file": "",
    "no_matching_line": "OTHER=1\nFOO=/x\n",
    "empty_value": "SESPY_R_DATA_DIR=\n",
    "off_lower": "SESPY_R_DATA_DIR=off\n",
    "off_upper": "SESPY_R_DATA_DIR=OFF\n",
    "off_title": "SESPY_R_DATA_DIR=Off\n",
    "off_padded": "SESPY_R_DATA_DIR=  off \t\n",
    "plain_path": "SESPY_R_DATA_DIR=/srv/data/x\n",
    "double_quoted": 'SESPY_R_DATA_DIR="/srv/data/x"\n',
    "single_quoted": "SESPY_R_DATA_DIR='/srv/data/x'\n",
    "quoted_off": 'SESPY_R_DATA_DIR="off"\n',
    "whitespace_around": "SESPY_R_DATA_DIR= \t/srv/data/x \t \n",
    "whitespace_only": "SESPY_R_DATA_DIR=   \n",
    "leading_space_line": "  SESPY_R_DATA_DIR=/srv/data/x\n",
    "export_prefix": "export SESPY_R_DATA_DIR=/srv/data/x\n",
    "spaces_around_equals": "SESPY_R_DATA_DIR = /srv/data/x\n",
    "lowercase_key": "sespy_r_data_dir=/srv/data/x\n",
    "comment_line": "# SESPY_R_DATA_DIR=off\n",
    "comment_then_value": "# SESPY_R_DATA_DIR=off\nSESPY_R_DATA_DIR=/srv/data/y\n",
    "trailing_comment": "SESPY_R_DATA_DIR=/srv/data/x # note\n",
    "duplicates_last_wins": "SESPY_R_DATA_DIR=/a\nOTHER=1\nSESPY_R_DATA_DIR=/b\n",
    "duplicates_last_empty": "SESPY_R_DATA_DIR=/a\nSESPY_R_DATA_DIR=\n",
    "duplicates_last_off_first_path": "SESPY_R_DATA_DIR=off\nSESPY_R_DATA_DIR=/c\n",
    "crlf_path": "SESPY_R_DATA_DIR=/srv/data/x\r\n",
    "crlf_off": "OTHER=1\r\nSESPY_R_DATA_DIR=off\r\n",
    "crlf_empty": "SESPY_R_DATA_DIR=\r\n",
    "lone_cr": "OTHER=1\rSESPY_R_DATA_DIR=/srv/data/z\r",
    "no_trailing_newline": "SESPY_R_DATA_DIR=/srv/data/x",
    "value_with_equals": "SESPY_R_DATA_DIR=/a=b\n",
    "bom_first_line": "\ufeffSESPY_R_DATA_DIR=/srv/data/x\n",
    "bom_off": "﻿SESPY_R_DATA_DIR=off\n",
    "bom_second_line_not_stripped": "OTHER=1\n﻿SESPY_R_DATA_DIR=off\n",
    "us_trailing_off": "SESPY_R_DATA_DIR=off\x1f\n",
    "us_around_path": "SESPY_R_DATA_DIR=\x1f /srv/data/x\x1f\t\n",
    "us_interior_kept": "SESPY_R_DATA_DIR=/srv/a\x1fb\n",
    "fs_gs_rs_split": "OTHER=1\x1cSESPY_R_DATA_DIR=/srv/fs\x1dX=1\x1eY=2\n",
    "bom_then_value":"\ufeffOTHER=1\nSESPY_R_DATA_DIR=/srv/data/x\n",
    "form_feed_split": "OTHER=1\fSESPY_R_DATA_DIR=/srv/data/ff\n",
    # bytes: not valid UTF-8 (final review F1); the app decodes with errors="replace"
    "non_utf8_comment": "# Šiauliai\n".encode("cp1257") + b"SESPY_R_DATA_DIR=/srv/data/x\n",
    "non_utf8_before_newline_off": b"OTHER=caf\xe9\nSESPY_R_DATA_DIR=off\n",
    "non_utf8_after_bom": b"\xef\xbb\xbf# \xff\xfe\r\nSESPY_R_DATA_DIR=/srv/data/x\r\n",
    "non_utf8_after_value_line": b"SESPY_R_DATA_DIR=/srv/data/x\n# \x81\x8d\x8f\n",
    # Known difference, not a case: bash `read` drops NUL bytes, so deploy.sh
    # reads "/srv/da\0ta" as /srv/data, while the app keeps the NUL, cannot
    # stat the path and uses the shipped copy (fails closed; test_r_kb_registry).
}


def _app_text(v: str | bytes | None) -> str | None:
    """What Registry._read_settings hands the parser for file contents `v`."""
    if v is None:
        return None
    raw = v if isinstance(v, bytes) else v.encode("utf-8")
    return raw.decode("utf-8-sig", errors="replace")

PARSE_SCRIPT = """
source "$1"
if [ "$2" = absent ]; then
  resolve_override_dir absent
else
  resolve_override_dir present < "$3"
fi
printf '|rc=%s' "$?"
"""


@pytest.fixture(scope="module")
def deploy_copy(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("deploy_parse").resolve()
    _copy_lf(ROOT / "deploy" / "deploy.sh", d / "deploy.sh")
    (d / "parse.sh").write_bytes(PARSE_SCRIPT.encode())
    return d


@pytest.mark.parametrize("case", list(PARSE_CASES))
def test_settings_parse_matches_python(deploy_copy, case):
    text = PARSE_CASES[case]
    data = deploy_copy / f"{case}.env"
    if text is not None:  # bytes: no newline translation
        data.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    cp = subprocess.run(
        [BASH, _posix(deploy_copy / "parse.sh"), _posix(deploy_copy / "deploy.sh"),
         "absent" if text is None else "present", _posix(data)],
        capture_output=True)
    out = cp.stdout.decode("utf-8")
    assert out.endswith("|rc=0"), (out, cp.stderr)
    bash_value = out[: -len("|rc=0")]
    # the app reads the file as utf-8-sig (Registry._read_settings): a BOM is dropped
    py = r_kb.override_dir_from({}, _app_text(text), LAGUNA_R_DATA_DIR)
    assert bash_value == ("" if py is None else py), case


def test_parse_cases_cover_both_outcomes():
    results = {r_kb.override_dir_from({}, _app_text(t), LAGUNA_R_DATA_DIR)
               for t in PARSE_CASES.values()}
    assert None in results and LAGUNA_R_DATA_DIR in results
    assert "/srv/data/x" in results


SAFE_CASES = ["/srv/shiny-server/marinesabres/data", "/a_b/c.d/e@f+g-h", "/", "//", "/srv/a b",
              "relative", "'/q'", "/srv/dätä", "/srv/~x", "/srv/$(id)", "/srv/a;b", "", "/srv/a\tb"]


def test_safe_dir_rule_matches_check_live(deploy_copy):
    check_live = _check_live()
    # Values go through a file: MSYS re-parses argv quotes and would strip '...'.
    cases = deploy_copy / "safe_cases.txt"
    cases.write_bytes("".join(v + "\n" for v in SAFE_CASES).encode("utf-8"))
    script = ('source "$1"; while IFS= read -r v; do '
              'if is_safe_remote_dir "$v"; then echo 1; else echo 0; fi; done < "$2"')
    cp = subprocess.run([BASH, "-c", script, "_", _posix(deploy_copy / "deploy.sh"), _posix(cases)],
                        capture_output=True)
    got = cp.stdout.decode().split()
    want = ["1" if check_live.SAFE_REMOTE.match(v) else "0" for v in SAFE_CASES]
    assert got == want, list(zip(SAFE_CASES, got, want))


def test_deploy_default_matches_app_constant():
    text = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    m = re.search(r'^DEFAULT_R_DATA_DIR="([^"]*)"', text, re.M)
    assert m, "DEFAULT_R_DATA_DIR not found in deploy.sh"
    assert m.group(1) == LAGUNA_R_DATA_DIR
