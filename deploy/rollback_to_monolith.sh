#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Roll production back from the split layout (sespy library + sespy_app) to
# the pre-split SESPy monolith v1.17.x. See deploy/ROLLBACK.md and spec 9.
#
# Two steps, the second only if the first exits 0:
#   1. the monolith's own deploy script, from a clean v1.17.x worktree:
#        DEPLOY_MOSAIC_DIR= DEPLOY_REPO_ROOT=$MONOLITH_ROOT bash $MONOLITH_ROOT/deploy/deploy.sh --rollback
#      It replaces sespy/ with the monolith and app.py with the old one, but
#      copies with scp and never deletes, so sespy_app/ (holding the live
#      feedback store in sespy_app/logs) survives it. DEPLOY_MOSAIC_DIR is
#      emptied for this step so MosaicSES is NOT restarted while sespy_app/
#      still exists; the chain restarts it once sespy_app/ is gone.
#   2. ONE remote && chain in $DEPLOY_DIR, rename first:
#      a. [ -d sespy/modules ] (the monolith is really there), else stop;
#      b. mv -T sespy_app sespy_app.retired (only while sespy_app/ is the
#         installed package, i.e. holds __init__.py). Workers find the store
#         by the path string $APP_DIR/sespy_app/logs, so from here on nothing
#         can write into .retired, and anything that appears at sespy_app/ is
#         a stray. The MosaicSES symlink dangles from this instant;
#      c. when sespy_app.retired/logs exists (the live store): set aside a
#         sespy/logs the redeployed monolith already wrote (logs.rescued-<ts>),
#         then mv -T sespy_app.retired/logs sespy/logs;
#      d. a sespy_app/ a stray worker recreated: its logs/ set aside to
#         logs.rescued-<ts>, then the empty dirs removed with rmdir (never
#         rm -rf: anything else in it stops the chain, naming it);
#      e. rm -rf sespy_app.retired, only once it holds no logs/;
#      f. touch restart.txt for the app and for DEPLOY_MOSAIC_DIR.
#      No feedback store is ever deleted, and the live sespy/logs is never
#      displaced by a stray. A rerun is safe after a death between any two
#      steps: a .retired still holding logs/ is resumed (c), a stray is set
#      aside (d). Both sespy_app/__init__.py and sespy_app.retired present is
#      not a state this chain leaves; it stops and asks for a human.
#
# Without --run it checks everything and only prints both steps.
#
# Settings: MONOLITH_ROOT (env only) is the ABSOLUTE path to a clean SESPy
# worktree exactly on a v1.17.* tag, with sespy/modules. Use a FRESH worktree
# with no deploy/config.env (not a reused tree that has one):
#   git -C <SESPy repo> worktree add --detach C:/Users/arturas.baziukas/AppData/Local/sespy-v1171-rb v1.17.1
# DEPLOY_SERVER,
# DEPLOY_DIR and the optional DEPLOY_MOSAIC_DIR come from this repo's
# deploy/config.env (sourced after the env, as deploy.sh does) or the env;
# DEPLOY_SERVER and DEPLOY_DIR are exported to the monolith's script, since a
# fresh v1.17.x worktree has no config.env of its own. If the worktree does
# have one, the monolith's script sources it after the env, so its values win
# (it could even re-set DEPLOY_MOSAIC_DIR); a warning is printed, and it must
# name the same server and dir. DEPLOY_DIR and DEPLOY_MOSAIC_DIR must be
# absolute paths of [A-Za-z0-9._/@+-] with no "." or ".." segment.
#
# Usage (from anywhere):
#   MONOLITH_ROOT=C:/Users/arturas.baziukas/AppData/Local/sespy-v1171-rb bash deploy/rollback_to_monolith.sh         # print only
#   MONOLITH_ROOT=C:/Users/arturas.baziukas/AppData/Local/sespy-v1171-rb bash deploy/rollback_to_monolith.sh --run   # do it
# ---------------------------------------------------------------------------
set -euo pipefail

RUN=0
for arg in "$@"; do
  case "$arg" in
    --run) RUN=1 ;;
    *) echo "usage: MONOLITH_ROOT=/abs/path/to/sespy-v1.17.x deploy/rollback_to_monolith.sh [--run]" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. MONOLITH_ROOT. Every check runs before any remote call.
M="${MONOLITH_ROOT:-}"
mono_abort() {
  echo "!! MONOLITH_ROOT${M:+=$M} must name a clean checkout of the SESPy monolith at its rollback tag: $1. Aborting before any remote call." >&2
  exit 2
}
[ -n "$M" ] || mono_abort "it is not set (pass it in the env)"
case "$M" in
  /* | [A-Za-z]:[/\\]*) ;;
  *) mono_abort "it is not an absolute path" ;;
esac
[ -d "$M" ] || mono_abort "no such directory"
M="$(cd "$M" && pwd)"
[ -d "$M/sespy/modules" ] || mono_abort "it has no sespy/modules, so it is not the pre-split monolith"
[ -f "$M/deploy/deploy.sh" ] || mono_abort "it has no deploy/deploy.sh"
mono_tag="$(git -C "$M" describe --tags --exact-match --match 'v1.17.*' 2>/dev/null | tr -d '\r')" || mono_tag=""
case "$mono_tag" in
  v1.17.*) ;;
  *) mono_abort "HEAD is not exactly on a v1.17.* tag (git describe --tags --exact-match failed)" ;;
esac
mono_status="$(git -C "$M" status --porcelain 2>/dev/null)" || mono_abort "git status failed"
[ -z "$mono_status" ] || mono_abort "the working tree is not clean (git status --porcelain is not empty)"

# 2. Server settings: this repo's config.env, then export them for the
#    monolith's script.
# shellcheck source=/dev/null
[ -f "$SCRIPT_DIR/config.env" ] && source "$SCRIPT_DIR/config.env"
if [ -z "${DEPLOY_SERVER:-}" ] || [ -z "${DEPLOY_DIR:-}" ]; then
  echo "!! DEPLOY_SERVER and DEPLOY_DIR must both be set (in $SCRIPT_DIR/config.env or the env). Aborting before any remote call." >&2
  exit 2
fi
# Absolute, only [A-Za-z0-9._/@+-] (the values are single-quoted into a remote
# command), and no "." or ".." segment (cd must land exactly there).
safe_dir() {
  local LC_ALL=C re='^/[A-Za-z0-9._/@+-]+$'
  [[ "$1" =~ $re ]] || return 1
  case "$1/" in
    */./* | */../*) return 1 ;;
  esac
}
safe_dir "$DEPLOY_DIR" || {
  echo "!! DEPLOY_DIR '$DEPLOY_DIR' is not an absolute path of [A-Za-z0-9._/@+-] without . or .. segments. Aborting before any remote call." >&2
  exit 2
}
DEPLOY_MOSAIC_DIR="${DEPLOY_MOSAIC_DIR:-}"
if [ -n "$DEPLOY_MOSAIC_DIR" ] && ! safe_dir "$DEPLOY_MOSAIC_DIR"; then
  echo "!! DEPLOY_MOSAIC_DIR '$DEPLOY_MOSAIC_DIR' is not an absolute path of [A-Za-z0-9._/@+-] without . or .. segments. Aborting before any remote call." >&2
  exit 2
fi
export DEPLOY_SERVER DEPLOY_DIR

# Existence only; the file is never read here.
if [ -e "$M/deploy/config.env" ]; then
  echo "    ! WARNING: $M/deploy/config.env exists. The v1.17.x script sources it after the env, so inside step 1 its values override the exported DEPLOY_SERVER/DEPLOY_DIR (and it could re-set DEPLOY_MOSAIC_DIR). It must name the same server and dir as $SCRIPT_DIR/config.env (${DEPLOY_SERVER}:${DEPLOY_DIR})." >&2
fi

# The remote chain (step 2). aside_logs DIR WHY: move DIR/logs to a fresh
# logs.rescued-<ts>[-DIR-n], never deleting it.
MOSAIC_TOUCH=""
if [ -n "$DEPLOY_MOSAIC_DIR" ]; then
  MOSAIC_TOUCH=" &&
  { touch '$DEPLOY_MOSAIC_DIR/restart.txt' || echo '!! WARNING: could not touch $DEPLOY_MOSAIC_DIR/restart.txt; restart MosaicSES by hand' >&2; }"
fi
CHAIN='aside_logs() {
  base="logs.rescued-$(date +%Y%m%d%H%M%S)"
  r="$base"
  n=0
  while [ -e "$r" ]; do n=$((n + 1)); r="$base-$1-$n"; done
  mv -T "$1/logs" "$r" &&
    echo "!! NOTE: $1/logs $2; kept it as $PWD/$r - merge it by hand if it holds rows" >&2
}'"
cd '$DEPLOY_DIR' &&
  { [ -d sespy/modules ] || { echo '!! $DEPLOY_DIR/sespy/modules is absent: the monolith redeploy did not happen; nothing was changed' >&2; exit 3; }; } &&
  if [ -e sespy_app/__init__.py ]; then
    if [ -e sespy_app.retired ]; then
      echo '!! $DEPLOY_DIR has both an installed sespy_app/ and sespy_app.retired/; which holds the live feedback store needs a human. Nothing was changed' >&2; exit 4;
    fi;
    mv -T sespy_app sespy_app.retired;
  fi &&
  if [ -d sespy_app.retired/logs ]; then
    if [ -e sespy/logs ]; then aside_logs sespy 'was written by the redeployed monolith before the live store moved back'; fi &&
    mv -T sespy_app.retired/logs sespy/logs;
  fi &&
  if [ -e sespy_app/logs ]; then aside_logs sespy_app 'was recreated by a split-layout worker that outlived the rollback'; fi &&
  if [ -e sespy_app ]; then
    rmdir sespy_app || { echo '!! $DEPLOY_DIR/sespy_app was recreated and holds more than logs/; move it away by hand, then rerun' >&2; exit 5; };
  fi &&
  if [ -d sespy_app.retired ] && [ ! -e sespy_app.retired/logs ]; then rm -rf sespy_app.retired; fi &&
  touch '$DEPLOY_DIR/restart.txt'$MOSAIC_TOUCH
"
printf -v MONO_CMD 'DEPLOY_MOSAIC_DIR= DEPLOY_REPO_ROOT=%q bash %q --rollback' "$M" "$M/deploy/deploy.sh"

if [ "$RUN" != 1 ]; then
  echo "==> Rollback to the SESPy monolith $mono_tag on ${DEPLOY_SERVER}:${DEPLOY_DIR} (print only; nothing was run)"
  echo "    step 1, the monolith deploy (MosaicSES is not restarted here):"
  echo "      $MONO_CMD"
  echo "    step 2, only if step 1 exits 0, one remote chain:"
  echo "      ssh $DEPLOY_SERVER \"$CHAIN\""
  echo "==> Rerun with --run to do both."
  exit 0
fi

# 3. The monolith deploy, with MosaicSES's restart left to the chain.
echo "==> step 1/2: redeploying the SESPy monolith $mono_tag from $M"
mono_rc=0
DEPLOY_MOSAIC_DIR="" DEPLOY_REPO_ROOT="$M" bash "$M/deploy/deploy.sh" --rollback || mono_rc=$?
if [ "$mono_rc" -ne 0 ]; then
  echo "!! the monolith deploy ($M/deploy/deploy.sh --rollback) failed (exit $mono_rc); the layout chain did NOT run. Fix the cause and rerun with --run." >&2
  exit "$mono_rc"
fi

# 4. On exit 0 only: the chain.
echo "==> step 2/2: retiring sespy_app/ and moving the feedback store back into sespy/logs on ${DEPLOY_SERVER}"
chain_rc=0
ssh "$DEPLOY_SERVER" "$CHAIN" || chain_rc=$?
if [ "$chain_rc" -ne 0 ]; then
  echo "!! the layout chain on ${DEPLOY_SERVER} failed (exit $chain_rc) after the monolith deploy. The remote may look like a half-cutover (monolith sespy/ beside sespy_app/ or sespy_app.retired/); fix what is named above and rerun with --run (both steps are rerun-safe). See deploy/ROLLBACK.md." >&2
  exit "$chain_rc"
fi
echo "==> Done. ${DEPLOY_DIR} runs the SESPy monolith $mono_tag; run MosaicSES deploy/verify_live.py."
exit 0
