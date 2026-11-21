#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Roll production back from the split layout (sespy library + sespy_app) to
# the pre-split SESPy monolith v1.17.x. See deploy/ROLLBACK.md and spec 9.
#
# Two steps, the second only if the first exits 0:
#   1. the monolith's own deploy script, from a clean v1.17.x worktree:
#        DEPLOY_REPO_ROOT=$MONOLITH_ROOT bash $MONOLITH_ROOT/deploy/deploy.sh --rollback
#      It replaces sespy/ with the monolith and app.py with the old one, but
#      copies with scp and never deletes, so sespy_app/ (holding the live
#      feedback store in sespy_app/logs) survives it.
#   2. ONE remote && chain in $DEPLOY_DIR:
#      - [ -d sespy/modules ] (the monolith is really there), else stop;
#      - when sespy_app/logs exists: set aside a sespy/logs the redeployed
#        monolith already wrote (logs.rescued-<ts>), then
#        mv -T sespy_app/logs sespy/logs;
#      - rm -rf sespy_app (a logs/ a worker recreated there is set aside
#        first), so the MosaicSES symlink dangles and its bridge falls back
#        to sespy.*;
#      - touch restart.txt for the app and for DEPLOY_MOSAIC_DIR.
#      No feedback store is ever deleted. A rerun is safe: with no
#      sespy_app/logs the chain keeps sespy/logs as the store.
#
# Without --run it checks everything and only prints both steps.
#
# Settings: MONOLITH_ROOT (env only) is the ABSOLUTE path to a clean SESPy
# worktree exactly on a v1.17.* tag, with sespy/modules. DEPLOY_SERVER,
# DEPLOY_DIR and the optional DEPLOY_MOSAIC_DIR come from this repo's
# deploy/config.env (sourced after the env, as deploy.sh does) or the env, and
# are exported to the monolith's script: a fresh v1.17.x worktree has no
# config.env of its own. (If the worktree does have one, the monolith's
# script sources it after the env, so it must name the same server and dir.)
#
# Usage (from anywhere):
#   MONOLITH_ROOT=/abs/path/sespy-v1171 deploy/rollback_to_monolith.sh         # print only
#   MONOLITH_ROOT=/abs/path/sespy-v1171 deploy/rollback_to_monolith.sh --run   # do it
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
safe_dir() {
  local LC_ALL=C re='^/[A-Za-z0-9._/@+-]+$'
  [[ "$1" =~ $re ]]
}
safe_dir "$DEPLOY_DIR" || {
  echo "!! DEPLOY_DIR '$DEPLOY_DIR' is not an absolute path of [A-Za-z0-9._/@+-]. Aborting before any remote call." >&2
  exit 2
}
DEPLOY_MOSAIC_DIR="${DEPLOY_MOSAIC_DIR:-}"
if [ -n "$DEPLOY_MOSAIC_DIR" ] && ! safe_dir "$DEPLOY_MOSAIC_DIR"; then
  echo "!! DEPLOY_MOSAIC_DIR '$DEPLOY_MOSAIC_DIR' is not an absolute path of [A-Za-z0-9._/@+-]. Aborting before any remote call." >&2
  exit 2
fi
export DEPLOY_SERVER DEPLOY_DIR DEPLOY_MOSAIC_DIR

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
  if [ -d sespy_app/logs ]; then
    if [ -e sespy/logs ]; then aside_logs sespy 'was written by the redeployed monolith before the live store moved back'; fi &&
    mv -T sespy_app/logs sespy/logs;
  fi &&
  if [ -e sespy_app/logs ]; then aside_logs sespy_app 'was recreated by a split-layout worker'; fi &&
  rm -rf sespy_app &&
  touch '$DEPLOY_DIR/restart.txt'$MOSAIC_TOUCH
"
printf -v MONO_CMD 'DEPLOY_REPO_ROOT=%q bash %q --rollback' "$M" "$M/deploy/deploy.sh"

if [ "$RUN" != 1 ]; then
  echo "==> Rollback to the SESPy monolith $mono_tag on ${DEPLOY_SERVER}:${DEPLOY_DIR} (print only; nothing was run)"
  echo "    step 1, the monolith deploy:"
  echo "      $MONO_CMD"
  echo "    step 2, only if step 1 exits 0, one remote chain:"
  echo "      ssh $DEPLOY_SERVER \"$CHAIN\""
  echo "==> Rerun with --run to do both."
  exit 0
fi

# 3. The monolith deploy.
echo "==> step 1/2: redeploying the SESPy monolith $mono_tag from $M"
mono_rc=0
DEPLOY_REPO_ROOT="$M" bash "$M/deploy/deploy.sh" --rollback || mono_rc=$?
if [ "$mono_rc" -ne 0 ]; then
  echo "!! the monolith deploy ($M/deploy/deploy.sh --rollback) failed (exit $mono_rc); the layout chain did NOT run. Fix the cause and rerun with --run." >&2
  exit "$mono_rc"
fi

# 4. On exit 0 only: the chain.
echo "==> step 2/2: moving the feedback store back into sespy/logs and removing sespy_app/ on ${DEPLOY_SERVER}"
chain_rc=0
ssh "$DEPLOY_SERVER" "$CHAIN" || chain_rc=$?
if [ "$chain_rc" -ne 0 ]; then
  echo "!! the layout chain on ${DEPLOY_SERVER} failed (exit $chain_rc) after the monolith deploy. The remote may look like a half-cutover (monolith sespy/ beside sespy_app/); rerun with --run (both steps are rerun-safe). See deploy/ROLLBACK.md." >&2
  exit "$chain_rc"
fi
echo "==> Done. ${DEPLOY_DIR} runs the SESPy monolith $mono_tag; run MosaicSES deploy/verify_live.py."
exit 0
