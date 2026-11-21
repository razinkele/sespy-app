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
2. `git -C <SESPy repo> worktree add /abs/path/sespy-lib-rollback v1.18.0`
3. `DEPLOY_REPO_ROOT=/abs/path/sespy-app-rollback DEPLOY_LIBRARY_ROOT=/abs/path/sespy-lib-rollback bash deploy/deploy.sh --rollback`

Never run an older tree's own `deploy.sh` (for example
`/abs/path/sespy-app-rollback/deploy/deploy.sh`). Always run the CURRENT
script with `DEPLOY_REPO_ROOT`: an older script lacks the current safety
checks, and over an interrupted monolith rollback it can create the "exit 4"
state below.

## B. Back to the pre-split monolith (SESPy v1.17.1)

Use this only to leave the split layout entirely.

**Valid only while MosaicSES 0.9.x is live.** The 0.9.x bridge works on both
layouts. After MosaicSES 0.10.0 is live, first roll MosaicSES back to 0.9.3
with its own release flow, and only then roll the app back with the steps
below.

1. Create a FRESH v1.17.1 worktree of the SESPy repo, with no
   `deploy/config.env`:
   `git -C <SESPy repo> worktree add --detach <path-to>/sespy-v1171-rb v1.17.1`
   Do not reuse `sespy-v1171`: it has its own `deploy/config.env`, which the
   v1.17.1 script reads after the env. That makes it a second source of server
   settings, and it re-sets `DEPLOY_MOSAIC_DIR`, so the v1.17.1 deploy would
   restart MosaicSES too early (see below). The script prints a WARNING if the
   tree you give it has a `deploy/config.env`.
2. Check what will run (prints both steps; changes nothing):
   `MONOLITH_ROOT=<path-to>/sespy-v1171-rb bash deploy/rollback_to_monolith.sh`
3. Run it:
   `MONOLITH_ROOT=<path-to>/sespy-v1171-rb bash deploy/rollback_to_monolith.sh --run`
4. Verify MosaicSES from the MosaicSES checkout:
   `micromamba run -n shiny python deploy/verify_live.py`
5. Later, once sessions opened before the rollback have ended (and whenever
   MosaicSES `verify_live.py` fails), check that no `sespy_app/` came back:
   `ssh <server> ls -d <app dir>/sespy_app`
   It should say "No such file or directory". If `sespy_app/` is there, a
   worker that outlived the rollback recreated it (usually just
   `sespy_app/logs`). Rerun step 3: it sets that stray store aside as
   `logs.rescued-<timestamp>` and removes the empty directory, keeping the
   live `sespy/logs`.

What step 3 does:

- **Step 1** runs `deploy/deploy.sh --rollback` from the v1.17.1 tree. It gets
  `DEPLOY_SERVER` and `DEPLOY_DIR` from this repo's `deploy/config.env`, and
  an EMPTY `DEPLOY_MOSAIC_DIR`, so it does not restart MosaicSES while
  `sespy_app/` still exists. (A `deploy/config.env` in the v1.17.1 tree could
  re-set it; that is why step 1 above asks for a fresh tree.)
- **Step 2** runs only if step 1 exits 0. It is one remote `&&` chain in the
  app directory:
  1. check that `sespy/modules` is present (the monolith is really deployed),
     and stop otherwise (exit 3). Also stop (exit 7) if `sespy_app.old/logs`
     or `sespy_app.incoming/logs` exists: see "Exit 7" below;
  2. rename `sespy_app` to `sespy_app.retired` before anything reads the
     store. Workers find the store by its path, so nothing can write into
     `.retired` after this, and the MosaicSES symlink dangles from here;
  3. if `sespy_app.retired/logs` exists (the live store): first set aside any
     `sespy/logs` the monolith already wrote, as `logs.rescued-<timestamp>`,
     then `mv -T sespy_app.retired/logs sespy/logs`. This set-aside happens
     only on this path, while the live store is still in `.retired`; on a
     rerun after the store has moved, `sespy/logs` is kept as it is;
  4. if a worker recreated `sespy_app/`, set its `logs/` aside as
     `logs.rescued-<timestamp>` and remove the empty directory with `rmdir`.
     It is never `rm -rf`'d: if it holds anything else, the chain stops and
     names it, for you to move by hand before rerunning;
  5. delete `sespy_app.retired`, only once its `logs/` has moved out;
  6. touch `restart.txt` for the app and for MosaicSES (`DEPLOY_MOSAIC_DIR`).
     If `DEPLOY_MOSAIC_DIR` is empty, the script warns that MosaicSES will
     not be restarted; touch its `restart.txt` by hand.
- If the monolith deploy fails, the chain does not run.
- No feedback store is ever deleted, and a stray store never replaces the
  live `sespy/logs`. Rerunning step 3 is safe after a stop between any two
  steps: a `sespy_app.retired` still holding `logs/` is resumed, not deleted.
- If both an installed `sespy_app/` and a `sespy_app.retired/` exist, the
  chain stops without changing anything (exit 4): see "Exit 4" below.

### If it stops halfway

- **Stopped after the v1.17.1 deploy but before the chain:** the remote looks
  like a half-cutover, with the monolith `sespy/` beside the leftover
  `sespy_app/` that holds the live `sespy_app/logs`. Rerun step 3 to finish.
  If instead a later sespy-app cutover deploy runs over that state, it sets
  the monolith's newer `sespy/logs` aside as `logs.rescued-<timestamp>` and
  keeps `sespy_app/logs`. Nothing is lost, but the rows in the rescued
  directory need a manual merge into the live store.
- **Stopped inside the chain, with `sespy_app.retired` left on the server:**
  rerun step 3. Until you do, `deploy/deploy.sh` refuses to deploy: it aborts
  before any upload, because the live store may still be in
  `sespy_app.retired/logs`.

### Exit 4: an installed `sespy_app/` beside `sespy_app.retired/`

The rollback stops (exit 4) and `deploy.sh` refuses too (exit 6). One of the
two directories holds the live feedback store and the other a stale one.
Running an older tree's own `deploy.sh` over an interrupted rollback can
create this state. Never `rm -rf` either directory.

Run these on the server, in the app directory (`ssh <server>`, then
`cd <app dir>`). The first two only read.

1. Compare the modification times (include the `-wal` files):
   `ls -l --time-style=full-iso sespy_app/logs sespy_app.retired/logs`
2. Compare row counts and the newest row (needs the `sqlite3` tool; without it, go by step 1):
   `sqlite3 -readonly sespy_app/logs/feedback.db "select count(*), max(created_at) from feedback"`
   `sqlite3 -readonly sespy_app.retired/logs/feedback.db "select count(*), max(created_at) from feedback"`
   The live store is the one with the full history, usually `sespy_app.retired`.
3. If the installed `sespy_app/` is the stale one, set it aside:
   `mv -T sespy_app/logs logs.rescued-$(date +%Y%m%d%H%M%S)`
   `mv -T sespy_app sespy_app.stale-$(date +%Y%m%d%H%M%S)`
4. If `sespy_app.retired/` is the stale one, set it aside instead:
   `mv -T sespy_app.retired/logs logs.rescued-$(date +%Y%m%d%H%M%S)`
   `mv -T sespy_app.retired sespy_app.stale-$(date +%Y%m%d%H%M%S)`
5. Rerun step 3 of section B. It adopts the remaining live store.

Merge any rows in the `logs.rescued-*` directory into the live store by hand.
Keep the `sespy_app.stale-*` directory until you have checked it holds no
`logs/`.

### Exit 7: a store left by an interrupted sespy-app deploy

The rollback stops (exit 7) before changing anything, because
`sespy_app.old/logs` or `sespy_app.incoming/logs` holds a feedback store. Fix
it one of two ways, then rerun step 3 of section B:

- Repair it by running the app's deploy once from this checkout. Its first
  step moves the store back into `sespy_app/logs`:
  `bash deploy/deploy.sh`
- Or move it by hand on the server, in the app directory. Only do this if
  neither `sespy_app/__init__.py` nor `sespy_app.retired` exists. Use the
  directory the message named:
  `mkdir sespy_app.retired && mv -T sespy_app.old/logs sespy_app.retired/logs`
