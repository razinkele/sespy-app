# Rolling back the SESPy app

Two kinds of rollback exist. Pick the one that matches where you are going.
The design is in spec section 9 ("Deploy and cutover", Rollback) of
`docs/superpowers/specs/2026-11-19-library-app-split-design.md`.

All commands are single lines. Run them from the sespy-app checkout unless
the step says otherwise.

## A. Back to an earlier sespy-app release (app to app)

Use this when production runs sespy-app and you want an older sespy-app tag.
It stays in the split layout. Use `deploy.sh --rollback` from the current
tree, pointed at a worktree of the older app tag and a library worktree at
the tag that older app pins:

1. `git worktree add /abs/path/sespy-app-rollback v1.0.0`
2. `git -C /abs/path/SESPy worktree add /abs/path/sespy-lib-rollback v1.18.0`
3. `DEPLOY_REPO_ROOT=/abs/path/sespy-app-rollback DEPLOY_LIBRARY_ROOT=/abs/path/sespy-lib-rollback bash deploy/deploy.sh --rollback`

## B. Back to the pre-split monolith (SESPy v1.17.1)

Use this only to leave the split layout entirely.

**Valid only while MosaicSES 0.9.x is live.** The 0.9.x bridge works on both
layouts. After MosaicSES 0.10.0 is live, first roll MosaicSES back to 0.9.3
with its own release flow, and only then roll the app back with the steps
below.

1. Get a clean v1.17.1 worktree of the SESPy repo. Either reuse the existing
   `sespy-v1171` tree, or create one:
   `git -C /abs/path/SESPy worktree add /abs/path/sespy-v1171 v1.17.1`
   If the reused tree has its own `deploy/config.env`, the v1.17.1 script
   reads it last, so it must name the same server and directory as this
   repo's `deploy/config.env`.
2. Check what will run (prints both steps; changes nothing):
   `MONOLITH_ROOT=/abs/path/sespy-v1171 bash deploy/rollback_to_monolith.sh`
3. Run it:
   `MONOLITH_ROOT=/abs/path/sespy-v1171 bash deploy/rollback_to_monolith.sh --run`
4. Verify MosaicSES from the MosaicSES checkout:
   `micromamba run -n shiny python deploy/verify_live.py`

What step 3 does:

- It runs `deploy/deploy.sh --rollback` from the v1.17.1 tree, with the
  server settings from this repo's `deploy/config.env` exported to it.
- Only if that exits 0, it runs one remote `&&` chain in the app directory:
  1. check that `sespy/modules` is present (the monolith is really deployed),
     and stop otherwise;
  2. set aside any `sespy/logs` the monolith already wrote, as
     `logs.rescued-<timestamp>`;
  3. move the live feedback store back with `mv -T sespy_app/logs sespy/logs`;
  4. remove `sespy_app/`, so the MosaicSES symlink dangles and its bridge
     falls back to `sespy.*`;
  5. touch `restart.txt` for the app and for MosaicSES (`DEPLOY_MOSAIC_DIR`).
- If the monolith deploy fails, the chain does not run.
- No feedback store is ever deleted. Rerunning step 3 is safe.

### If it stops halfway

If a monolith rollback ever stops after the v1.17.1 deploy but without the
chain, the remote looks like a half-cutover: the monolith `sespy/` beside a
leftover `sespy_app/` holding the live `sespy_app/logs`. Rerun step 3 to
finish it.

If instead a later sespy-app cutover deploy runs over that state, it sets the
monolith's newer `sespy/logs` aside as `logs.rescued-<timestamp>` and keeps
`sespy_app/logs`. Nothing is lost, but the rows in the rescued directory need
a manual merge into the live store.
