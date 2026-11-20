#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy the SESPy app (sespy_app) and its pinned SESPy library to a Shiny
# Server app directory via scp.
#
# Ships ONLY the runtime files — app.py, sespy_app/, data/, www/,
# environment.yml, pyproject.toml, README/CHANGELOG, docs/MANUAL.md +
# docs/screenshots/ — plus the library package sespy/, vendored from the
# DEPLOY_LIBRARY_ROOT checkout, to $APP_DIR on $SERVER, excluding
# caches/tests/docs/build; then touches restart.txt so Shiny Server reloads the
# app on the next request.
#
# Before staging, DEPLOY_LIBRARY_ROOT must be a clean checkout exactly at the
# tag the app's pyproject.toml pins (sespy @ ...@vX.Y.Z), with a matching
# sespy/__init__.py __version__; anything else aborts before any remote call.
#
# Before uploading, a live reference-data pre-flight (K1 spec 4.8) reads the
# server's $APP_DIR/server_settings.env, fetches the live R data files it
# points at into the stage and runs the LIBRARY's tests/parity/check_live.py on
# them. A structural problem aborts the deploy before anything on the server
# changes. Both packages are uploaded as sespy_app.incoming and sespy.incoming
# and swapped in with rename pairs in ONE remote command, sespy_app first, so a
# worker never sees a half-copied package (K1 spec 4.10); between the renames
# of a pair that package is briefly absent. A rerun recovers an interrupted
# swap in step 3, after the step-2 pre-flight (skip it with
# SESPY_SKIP_LIVE_CHECK if it is what blocks the rerun).
#
# Feedback store (spec 9): the server owns sespy_app/logs/ (feedback.db plus
# its SQLite WAL sidecars feedback.db-wal / feedback.db-shm). The unit moved is
# always the whole logs/ directory, never feedback.db alone. The first deploy
# onto the pre-split layout (sespy/ = monolith, owning sespy/logs/) moves
# sespy/logs once into the new sespy_app/; if both sespy/logs and
# sespy_app/logs exist the deploy aborts before any rename. No feedback store
# is ever deleted: a stranded one is kept as $APP_DIR/logs.rescued-<timestamp>.
#
# Prerequisites:
#   * passwordless (key-based) SSH to $SERVER for the deploy user
#   * $APP_DIR writable by that user (here: razinka & shiny both have write)
#   * the server already has a Python env with the SESPy deps and shiny-server
#     configured to run this app's app.py (this script ships code, not the env;
#     rebuild the env from the shipped environment.yml if it changed)
#
# Config: set DEPLOY_SERVER, DEPLOY_DIR and DEPLOY_LIBRARY_ROOT in a gitignored
# deploy/config.env (copy deploy/config.env.example), or pass them as env vars.
# The real host/path are intentionally NOT hard-coded here so this script is
# safe in a public repo.
# NOTE: deploy/config.env is sourced AFTER the environment, so its values win
# over env vars of the same name — except DEPLOY_LIBRARY_ROOT, where a
# non-empty env value wins (a rollback names an older library for one run).
#
# Required setting:
#   DEPLOY_LIBRARY_ROOT  ABSOLUTE path to a SESPy library checkout at the tag
#                      the app's pyproject.toml pins (e.g. v1.18.0), with a
#                      clean working tree. Its sespy/ is vendored as
#                      $APP_DIR/sespy and its tests/parity/check_live.py runs
#                      the pre-flight.
#
# Optional settings (config.env or env):
#   DEPLOY_MOSAIC_DIR  MosaicSES app dir on the same server; its restart.txt is
#                      touched too, so MosaicSES reloads the swapped sespy/ and
#                      sespy_app/ (it symlinks both).
#   DEPLOY_REPO_ROOT   deploy this sespy-app checkout instead of the script's
#                      own repo (rollback: deploy a worktree of an older app
#                      tag with the CURRENT script). Use an ABSOLUTE path: a
#                      relative one resolves against the caller's current
#                      directory, not the repo. Pass it per run; never leave it
#                      set in config.env, or every normal deploy ships that tree.
#   DEPLOY_SUDO_TEST   1 = check live-file readability with `sudo -n -u shiny`
#                      (a denied sudo can raise auth alerts; default is stat).
#   SESPY_PY           python command for check_live.py
#                      (default: micromamba run -n shiny python).
#
# Environment only (read before config.env is sourced; setting it there does
# nothing):
#   SESPY_SKIP_LIVE_CHECK="<reason>"  skip the live pre-flight entirely. The
#                      reason must be non-empty: set-but-empty counts as unset
#                      and the pre-flight runs.
#
# Usage (run from anywhere; the script resolves the repo root):
#   deploy/deploy.sh               # normal deploy, gated by check_live.py
#   deploy/deploy.sh --rollback    # never gated: check_live.py --warn-only
#
# Rollback to an older app tag, app version to app version (always with the
# CURRENT script; the library checkout must be at the tag that older app pins):
#   git worktree add /abs/path/sespy-app-rollback v1.0.0
#   git -C /abs/path/SESPy worktree add /abs/path/sespy-lib-rollback v1.18.0
#   DEPLOY_REPO_ROOT=/abs/path/sespy-app-rollback \
#   DEPLOY_LIBRARY_ROOT=/abs/path/sespy-lib-rollback deploy/deploy.sh --rollback
# A pre-split monolith tag (SESPy <= v1.17.x, no sespy_app/) cannot be deployed
# by this script; it aborts before staging.
# ---------------------------------------------------------------------------

# --- settings-file parse rule ------------------------------------------------
# Mirrors sespy/r_kb.py::parse_settings_text + override_dir_from (with no env
# override) exactly; tests/test_deploy_script.py checks both over one case table.
DEFAULT_R_DATA_DIR="/srv/shiny-server/marinesabres/data"

# stdin: the settings file text. Prints the raw value of the LAST line that
# starts with "SESPY_R_DATA_DIR=" (no leading whitespace), with surrounding
# whitespace stripped; returns 1 when no line matches. Never sources the file.
# Python's str.splitlines() also breaks lines on CR, VT, FF and FS/GS/RS, and
# the app reads the file as utf-8-sig (a leading BOM is dropped).
settings_raw_value() {
  local LC_ALL=C line value="" found=1 first=1
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$first" = 1 ]; then
      line="${line#$'\xef\xbb\xbf'}"
      first=0
    fi
    case "$line" in
      SESPY_R_DATA_DIR=*) value="${line#SESPY_R_DATA_DIR=}"; found=0 ;;
    esac
  done < <(tr '\r\013\014\034\035\036' '\n\n\n\n\n\n')
  [ "$found" -eq 0 ] || return 1
  # Python's str.strip() whitespace, ASCII part: space, TAB, LF, VT, FF, CR and
  # FS/GS/RS/US (0x1c-0x1f; the first three already split lines above).
  # Known difference: non-ASCII whitespace that Python also strips or splits on
  # (U+0085, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F, U+205F,
  # U+3000) is kept here as bytes. Such a value fails DIR validation, so the
  # deploy fails closed (missing.txt) rather than checking a different dir.
  local ws=$' \t\n\v\f\r\034\035\036\037'
  while [ -n "$value" ] && [[ "$ws" == *"${value:0:1}"* ]]; do value="${value:1}"; done
  while [ -n "$value" ] && [[ "$ws" == *"${value: -1}"* ]]; do value="${value:0:${#value}-1}"; done
  printf '%s' "$value"
}

# $1 = "absent" (no settings file) or "present" (text on stdin).
# Prints the override directory, or nothing when the override is off.
resolve_override_dir() {
  local raw
  if [ "$1" = absent ]; then
    printf '%s' "$DEFAULT_R_DATA_DIR"
    return 0
  fi
  if ! raw="$(settings_raw_value)"; then
    printf '%s' "$DEFAULT_R_DATA_DIR"
    return 0
  fi
  case "${raw,,}" in
    "" | off) return 0 ;;
  esac
  printf '%s' "$raw"
}

# Same rule as check_live.py: absolute, and only [A-Za-z0-9._/@+-] (scp may
# hand the remote path to a shell, so never fetch an odd path).
is_safe_remote_dir() {
  local LC_ALL=C re='^/[A-Za-z0-9._/@+-]+$'
  [[ "$1" =~ $re ]]
}

# Sourced (by the parity test) for the functions above only.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi
# -----------------------------------------------------------------------------

set -euo pipefail

ROLLBACK=0
for arg in "$@"; do
  case "$arg" in
    --rollback) ROLLBACK=1 ;;
    *) echo "usage: deploy/deploy.sh [--rollback]" >&2; exit 2 ;;
  esac
done
SKIP_LIVE=0
if [ -n "${SESPY_SKIP_LIVE_CHECK:-}" ]; then
  SKIP_LIVE=1
  echo "==> skipping live check: ${SESPY_SKIP_LIVE_CHECK}"
elif [ -n "${SESPY_SKIP_LIVE_CHECK+x}" ]; then
  echo "    ! SESPY_SKIP_LIVE_CHECK is set but empty; a reason is required, so the live check runs" >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# A non-empty DEPLOY_LIBRARY_ROOT from the environment beats config.env (the
# one exception to "config.env wins"), so a rollback can name the older
# library tag for one run while config.env keeps the current one.
ENV_LIBRARY_ROOT="${DEPLOY_LIBRARY_ROOT:-}"
# shellcheck source=/dev/null
[ -f "$SCRIPT_DIR/config.env" ] && source "$SCRIPT_DIR/config.env"

SERVER="${DEPLOY_SERVER:?set DEPLOY_SERVER (in deploy/config.env or the env), e.g. user@host}"
APP_DIR="${DEPLOY_DIR:?set DEPLOY_DIR (in deploy/config.env or the env), e.g. /srv/shiny-server/SESPy}"

REPO_ROOT="$(cd "${DEPLOY_REPO_ROOT:-$SCRIPT_DIR/..}" && pwd)"
cd "$REPO_ROOT"

# 0. The pinned library. Every check runs before staging and before any
#    remote call, so an abort here changes nothing anywhere.
PIN="$(sed -nE '/@v[0-9]+\.[0-9]+\.[0-9]+/{s/.*@v([0-9]+\.[0-9]+\.[0-9]+).*/\1/p;q;}' pyproject.toml 2>/dev/null)" \
  || PIN=""
if [ -z "$PIN" ]; then
  echo "!! $REPO_ROOT/pyproject.toml pins no sespy library tag (a direct reference ending @vX.Y.Z); is this a sespy-app checkout? Aborting before upload." >&2
  exit 2
fi
if [ ! -d sespy_app ]; then
  echo "!! $REPO_ROOT has no sespy_app/ package, so it is not a sespy-app checkout (a pre-split monolith tag cannot be deployed with this script). Aborting before upload." >&2
  exit 2
fi
LIB="${ENV_LIBRARY_ROOT:-${DEPLOY_LIBRARY_ROOT:-}}"
lib_abort() {
  echo "!! DEPLOY_LIBRARY_ROOT${LIB:+=$LIB} must be a clean checkout of the sespy library at tag v$PIN (pinned in $REPO_ROOT/pyproject.toml): $1. Aborting before upload." >&2
  exit 2
}
[ -n "$LIB" ] || lib_abort "it is not set (env or deploy/config.env)"
case "$LIB" in
  /* | [A-Za-z]:[/\\]*) ;;
  *) lib_abort "it is not an absolute path" ;;
esac
[ -d "$LIB" ] || lib_abort "no such directory"
LIB="$(cd "$LIB" && pwd)"
lib_tag="$(git -C "$LIB" describe --tags --exact-match 2>/dev/null | tr -d '\r')" || lib_tag=""
if [ "$lib_tag" != "v$PIN" ]; then
  if [ -n "$lib_tag" ]; then
    lib_abort "HEAD is tagged $lib_tag"
  else
    lib_abort "HEAD is not exactly on a tag (git describe --tags --exact-match failed)"
  fi
fi
lib_status="$(git -C "$LIB" status --porcelain 2>/dev/null)" || lib_abort "git status failed"
[ -z "$lib_status" ] || lib_abort "the working tree is not clean (git status --porcelain is not empty)"
lib_version="$(sed -nE "/^__version__/{s/^__version__[[:space:]]*=[[:space:]]*['\"]([^'\"]*)['\"].*/\\1/p;q;}" \
  "$LIB/sespy/__init__.py" 2>/dev/null | tr -d '\r')" || lib_version=""
[ "$lib_version" = "$PIN" ] || lib_abort "sespy/__init__.py has __version__ '$lib_version'"

# Native Windows python (micromamba.exe) does not understand MSYS /tmp/... paths.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}
CHECK_LIVE="$LIB/tests/parity/check_live.py"
run_check_live() {
  # SESPY_PY is word-split on purpose (default is a multi-word command).
  # shellcheck disable=SC2086
  PYTHONPATH="$(native_path "$LIB")" ${SESPY_PY:-micromamba run -n shiny python} \
    "$(native_path "$CHECK_LIVE")" "$@"
}

# Runtime files to ship. Anything not listed (tests/, docs/, build/, dist/,
# .git/, *.egg-info/, LITERATURE/, .superpowers/) is intentionally excluded.
# README.md + CHANGELOG.md are runtime docs: the About modal renders them via
# read_project_doc(), so they must ship with the app (not just dev metadata).
# The library package sespy/ is staged from $LIB below, never from this repo.
RUNTIME=(app.py sespy_app data www environment.yml pyproject.toml README.md CHANGELOG.md)

VERSION="$(git describe --tags --always 2>/dev/null || echo unknown)"
echo "==> Deploying SESPy app ${VERSION} with sespy library v${PIN} to ${SERVER}:${APP_DIR}$([ "$ROLLBACK" = 1 ] && echo ' (rollback)')"

# 1. Stage a clean copy locally so no __pycache__/*.pyc (stale bytecode) ships.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
for item in "${RUNTIME[@]}"; do
  if [ -e "$item" ]; then
    cp -R "$item" "$STAGE/"
  else
    echo "    ! missing '$item' — skipping" >&2
  fi
done
# The About modal's Manual tab renders docs/MANUAL.md and serves
# docs/screenshots/ as static assets; ship exactly those two, not docs/.
mkdir -p "$STAGE/docs"
cp docs/MANUAL.md "$STAGE/docs/"
cp -R docs/screenshots "$STAGE/docs/"
# The pinned library, vendored (the server has no pip install of it).
cp -R "$LIB/sespy" "$STAGE/sespy"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$STAGE" -name '*.pyc' -delete
# Never ship a runtime feedback DB / logs. The server OWNS its own
# sespy_app/logs/ (feedback.db, surfaced in the Feedback modal); shipping a
# local copy would clobber production feedback with dev/test entries. The
# server's copy is moved into the incoming package in the swap (step 4).
rm -rf "$STAGE/sespy/logs" "$STAGE/sespy_app/logs"

# 2. Live reference-data pre-flight (K1 spec 4.8). Nothing on the server has
#    changed yet, so an abort here leaves production untouched.
SETTINGS="$APP_DIR/server_settings.env"
if [ "$SKIP_LIVE" = 1 ]; then
  :
elif [ ! -f "$CHECK_LIVE" ]; then
  echo "==> tests/parity/check_live.py is absent from the library checkout $LIB; skipping the live fetch and check"
else
  LIVE="$STAGE/.live"
  CHECK_ARGS=(--stage "$(native_path "$STAGE")")
  [ "$ROLLBACK" = 1 ] && CHECK_ARGS+=(--warn-only)

  # 2a. Read the settings file without sourcing it: exit 3 = absent,
  #     4 = present but unreadable (same split as check_live.py --fetch).
  settings_rc=0
  settings_text="$(ssh "$SERVER" "if [ -e '$SETTINGS' ]; then cat '$SETTINGS' || exit 4; else exit 3; fi")" \
    || settings_rc=$?
  LIVE_DIR=""
  case "$settings_rc" in
    0) LIVE_DIR="$(printf '%s' "$settings_text" | resolve_override_dir present)" ;;
    3) LIVE_DIR="$(resolve_override_dir absent)" ;;
    4)
      if [ "$ROLLBACK" = 1 ]; then
        echo "    ! WARNING: $SETTINGS exists but cannot be read; live checks skipped (--rollback)" >&2
      else
        echo "!! $SETTINGS exists but cannot be read; the live override setting is unknown. Aborting before upload." >&2
        exit 2
      fi
      ;;
    *)
      if [ "$ROLLBACK" = 1 ]; then
        echo "    ! WARNING: could not read $SETTINGS over ssh (exit $settings_rc); live checks skipped (--rollback)" >&2
      else
        echo "!! could not read $SETTINGS over ssh (exit $settings_rc). Aborting before upload." >&2
        exit 2
      fi
      ;;
  esac

  if [ "$settings_rc" -ne 0 ] && [ "$settings_rc" -ne 3 ]; then
    :  # unreadable settings under --rollback: shipped-copy check only
  elif [ -z "$LIVE_DIR" ]; then
    echo "==> live override is off on the server; fetching nothing"
  else
    # 2b. Fetch each override file (names from check_live, the one allow-list)
    #     plus the optional VERSION_INFO.json one level up.
    if ! names_out="$(run_check_live --print-override-names)"; then
      if [ "$ROLLBACK" = 1 ]; then
        echo "    ! WARNING: check_live.py --print-override-names failed; live checks skipped (--rollback)" >&2
        names_out=""
      else
        echo "!! check_live.py --print-override-names failed. Aborting before upload." >&2
        exit 2
      fi
    fi
    NAMES=()
    while IFS= read -r n; do
      [ -n "$n" ] && NAMES+=("$n")
    done < <(printf '%s\n' "$names_out" | tr -d '\r')
    if [ "${#NAMES[@]}" -eq 0 ]; then
      if [ "$ROLLBACK" = 1 ]; then
        echo "    ! WARNING: check_live.py --print-override-names gave no names; live checks skipped (--rollback)" >&2
      else
        echo "!! check_live.py --print-override-names gave no names, so nothing could be checked. Aborting before upload." >&2
        exit 2
      fi
    fi

    if [ "${#NAMES[@]}" -gt 0 ]; then
      mkdir -p "$LIVE"
      while [ "${#LIVE_DIR}" -gt 1 ] && [ "${LIVE_DIR%/}" != "$LIVE_DIR" ]; do
        LIVE_DIR="${LIVE_DIR%/}"
      done
      if ! is_safe_remote_dir "$LIVE_DIR"; then
        echo "    ! SESPY_R_DATA_DIR '$LIVE_DIR' is not an absolute path of [A-Za-z0-9._/@+-]; not fetching" >&2
        printf '%s\n' "${NAMES[@]}" > "$LIVE/missing.txt"
        : > "$LIVE/perms.txt"
      else
        echo "==> fetching live reference data from ${SERVER}:${LIVE_DIR}"
        for n in "${NAMES[@]}"; do
          scp -q "$SERVER:$LIVE_DIR/$n" "$LIVE/" || echo "$n" >> "$LIVE/missing.txt"
        done
        scp -q "$SERVER:$(dirname "$LIVE_DIR")/VERSION_INFO.json" "$LIVE/" 2>/dev/null || true

        # 2c. Can user shiny read each file and traverse every parent dir?
        STAT_PATHS=()
        add_path() {
          local p
          for p in "${STAT_PATHS[@]}"; do [ "$p" = "$1" ] && return 0; done
          STAT_PATHS+=("$1")
        }
        FILES=()
        for n in "${NAMES[@]}"; do
          f="$LIVE_DIR/$n"
          FILES+=("'$f'")
          add_path "$f"
          d="$(dirname "$f")"
          while [ "$d" != "/" ]; do
            add_path "$d"
            d="$(dirname "$d")"
          done
          add_path "/"
        done
        quoted_paths="$(printf "'%s' " "${STAT_PATHS[@]}")"
        stat_cmd="stat -c 'stat %a %U %G %n' $quoted_paths"
        if [ "${DEPLOY_SUDO_TEST:-}" = 1 ]; then
          remote="if sudo -n -u shiny true 2>/dev/null; then
            for f in ${FILES[*]}; do
              if sudo -n -u shiny test -r \"\$f\"; then echo \"sudo-test \$f ok\"; else echo \"sudo-test \$f denied\"; fi
            done
          else $stat_cmd; fi"
        else
          remote="$stat_cmd"
        fi
        ssh "$SERVER" "$remote" > "$LIVE/perms.txt" 2>/dev/null || true
      fi
      CHECK_ARGS+=(--live "$(native_path "$LIVE")" --perms "$(native_path "$LIVE/perms.txt")")
    fi
  fi

  # 2d. Check. Structural problems abort (exit 2); --rollback never gates.
  echo "==> running check_live.py"
  check_rc=0
  run_check_live "${CHECK_ARGS[@]}" || check_rc=$?
  if [ "$check_rc" -ne 0 ]; then
    if [ "$ROLLBACK" = 1 ]; then
      echo "    ! WARNING: check_live.py exited $check_rc; continuing (--rollback)" >&2
    else
      echo "!! check_live.py exited $check_rc. Aborting before upload (set SESPY_SKIP_LIVE_CHECK=\"<reason>\" to override)." >&2
      exit "$check_rc"
    fi
  fi
fi
rm -rf "$STAGE/.live"

# 3. Recovery prologue: repair what an interrupted earlier deploy left behind,
#    then clear any half-uploaded incoming package. It covers both this
#    script's sespy_app/sespy pairs and the pre-split script's sespy pair.
#    - a package gone but its .old there: a swap died between a pair's two
#      mv's; the .old comes back.
#    - logs/ in sespy_app.old, sespy_app.incoming, sespy.old or
#      sespy.incoming: a swap died after moving the logs. A directory that
#      still holds logs/ is NEVER deleted with them: the logs go back to the
#      feedback store's home, or, when a worker has already recreated
#      <home>/logs (feedback_store mkdirs it on every call), aside to
#      $APP_DIR/logs.rescued-<timestamp> with a warning. The home is
#      sespy_app once that package exists, else (the pre-split layout) sespy,
#      whose logs the swap's one-time migration then moves; a bare
#      sespy_app/ holding only logs/ is never created, since MosaicSES
#      symlinks sespy_app and would import it as an empty package.
#    - .deploy-keep/logs: the pre-K1 script died between stash and restore
#      (left in place if <home>/logs already exists).
#    - sespy/logs AND sespy_app/logs both present: two feedback stores, which
#      only a human can merge; abort before any upload or swap.
# Remote helpers, defined in every ssh that may delete a .old/.incoming dir.
# aside_logs DIR WHY: move DIR/logs to a fresh logs.rescued-<ts>[-DIR-n].
# rescue_logs DIR HOME: move DIR/logs to HOME/logs, or aside if that exists.
REMOTE_RESCUE='aside_logs() {
  base="logs.rescued-$(date +%Y%m%d%H%M%S)"
  r="$base"
  n=0
  while [ -e "$r" ]; do n=$((n + 1)); r="$base-$1-$n"; done
  mv -T "$1/logs" "$r" &&
    echo "!! WARNING: $1/logs $2; kept it as $PWD/$r - merge it by hand" >&2
}
rescue_logs() {
  [ -e "$1/logs" ] || return 0
  if [ ! -e "$2/logs" ]; then
    mkdir -p "$2" && mv -T "$1/logs" "$2/logs"
  else
    aside_logs "$1" "was stranded and $2/logs already exists"
  fi
}'
TWO_STORES="two feedback stores: merge $APP_DIR/sespy/logs and $APP_DIR/sespy_app/logs by hand"
prologue_rc=0
ssh "$SERVER" "$REMOTE_RESCUE
  mkdir -p '$APP_DIR' &&
  cd '$APP_DIR' &&
  if [ ! -e sespy_app ] && [ -d sespy_app.old ]; then mv -T sespy_app.old sespy_app; fi &&
  if [ ! -e sespy ] && [ -d sespy.old ]; then mv -T sespy.old sespy; fi &&
  if [ -d sespy_app ]; then home=sespy_app; else home=sespy; fi &&
  rescue_logs sespy_app.old \"\$home\" &&
  rescue_logs sespy_app.incoming \"\$home\" &&
  rescue_logs sespy.old \"\$home\" &&
  rescue_logs sespy.incoming \"\$home\" &&
  if [ ! -e \"\$home/logs\" ] && [ -d .deploy-keep/logs ]; then
    mkdir -p \"\$home\" && mv -T .deploy-keep/logs \"\$home/logs\";
  fi &&
  if [ -d .deploy-keep ] && [ ! -e .deploy-keep/logs ]; then rm -rf .deploy-keep; fi &&
  rm -rf sespy_app.old sespy_app.incoming sespy.old sespy.incoming &&
  if [ -e sespy/logs ] && [ -e sespy_app/logs ]; then exit 3; fi
" || prologue_rc=$?
if [ "$prologue_rc" -eq 3 ]; then
  echo "!! ${TWO_STORES}. Aborting before upload; nothing was swapped." >&2
  exit 2
elif [ "$prologue_rc" -ne 0 ]; then
  echo "!! the recovery step on ${SERVER} failed (exit $prologue_rc). Aborting before upload." >&2
  exit "$prologue_rc"
fi

# 4. Upload both packages beside the live ones, BOTH before any rename, then
#    swap in ONE remote && chain:
#    - the feedback store moves into sespy_app.incoming as a whole logs/ dir:
#      sespy_app/logs on repeat deploys, or, once, the pre-split sespy/logs;
#    - the sespy_app rename pair, then the sespy rename pair. sespy_app goes
#      first: at the cutover MosaicSES then resolves sespy_app against the
#      still-old sespy/ (compatible: 1.17.1 is a superset of the 1.18.0
#      library API); on later releases the two packages differ for at most
#      the length of this one command;
#    - logs a worker recreated meanwhile in sespy_app.old or sespy.old are
#      rescued before those dirs are deleted, and a sespy/logs that an
#      old-code worker recreated inside the new library is moved aside.
#    mv -T fails (instead of nesting the package) if a worker recreated a
#    package dir in the instant between a pair's two renames. Any failure
#    stops the chain; the next run's step 3 repairs what it left.
scp -rq "$STAGE/sespy_app" "$SERVER:$APP_DIR/sespy_app.incoming"
scp -rq "$STAGE/sespy" "$SERVER:$APP_DIR/sespy.incoming"
swap_rc=0
ssh "$SERVER" "$REMOTE_RESCUE
  cd '$APP_DIR' &&
  if [ -e sespy/logs ] && [ -e sespy_app/logs ]; then
    echo '!! $TWO_STORES' >&2; exit 3;
  fi &&
  if [ -d sespy_app/logs ]; then mv -T sespy_app/logs sespy_app.incoming/logs;
  elif [ -d sespy/logs ]; then mv -T sespy/logs sespy_app.incoming/logs; fi &&
  if [ -e sespy_app ]; then mv -T sespy_app sespy_app.old; fi &&
  mv -T sespy_app.incoming sespy_app &&
  if [ -e sespy ]; then mv -T sespy sespy.old; fi &&
  mv -T sespy.incoming sespy &&
  rescue_logs sespy_app.old sespy_app &&
  rescue_logs sespy.old sespy_app &&
  if [ -e sespy/logs ]; then aside_logs sespy 'was recreated by a worker still running the old code'; fi &&
  rm -rf sespy_app.old sespy.old &&
  rm -rf '$APP_DIR/__pycache__' '$APP_DIR/docs/screenshots'
" || swap_rc=$?
if [ "$swap_rc" -ne 0 ]; then
  echo "!! the swap on ${SERVER} failed (exit $swap_rc); app.py, the settings file and restart.txt were not touched. Rerun deploy/deploy.sh: its step 3 repairs a half-done swap." >&2
  exit "$swap_rc"
fi

# 5. The other runtime items (stale top-level bytecode and screenshots were
#    cleared above, since scp overwrites but never deletes).
OTHERS=()
for p in "$STAGE"/*; do
  case "$(basename "$p")" in
    sespy | sespy_app) ;;
    *) OTHERS+=("$p") ;;
  esac
done
if [ "${#OTHERS[@]}" -gt 0 ]; then
  scp -rq "${OTHERS[@]}" "$SERVER:$APP_DIR/"
fi

# 6. Create the server settings file if absent (an existing one keeps its
#    content), chmod 0644 either way so user shiny can read what the
#    pre-flight read; then reload: Shiny Server restarts an app's workers when
#    restart.txt changes. A settings failure only warns: it must not skip the
#    reload of the code that is already swapped in.
ssh "$SERVER" "
  if [ ! -e '$SETTINGS' ]; then
    ( umask 022 && printf 'SESPY_R_DATA_DIR=%s\n' '$DEFAULT_R_DATA_DIR' > '$SETTINGS' ) ||
      echo '!! WARNING: could not create $SETTINGS' >&2
  fi
  if [ -e '$SETTINGS' ]; then
    chmod 0644 '$SETTINGS' || echo '!! WARNING: could not chmod 0644 $SETTINGS' >&2
  fi
  touch '$APP_DIR/restart.txt'
"
echo "==> Done. ${APP_DIR} updated to ${VERSION}; Shiny Server reloads on next request."

# 7. MosaicSES symlinks this sespy/ and sespy_app/; restart it too. The SESPy deploy has
#    already succeeded, so a failure here only warns.
if [ -n "${DEPLOY_MOSAIC_DIR:-}" ]; then
  if ssh "$SERVER" "touch '$DEPLOY_MOSAIC_DIR/restart.txt'"; then
    echo "==> MosaicSES at ${DEPLOY_MOSAIC_DIR} restarted; run its deploy/verify_live.py."
  else
    echo "!! WARNING: could not touch ${DEPLOY_MOSAIC_DIR}/restart.txt; restart MosaicSES by hand." >&2
  fi
fi
exit 0
