# Plan: keeping self-hosted Shotcast fast for years

Developer plan, not a user doc. Independent of Shotcast Cloud: it applies to every self-hosted booth,
with or without site publishing. Where the cloud design (shotcast-cloud `docs/architecture.html`, rev 4)
solves the same problem, the shapes are kept alike so the two can share test fixtures, but nothing here
depends on the cloud.

## What was measured

The redacted demo season (`demo/season.db` in the demo bucket: 4,222 shots, 81 rounds, about three
weeks of a busy league) was copied 8, 25 and 50 times, each copy shifted back in time with new IDs. At
this league's pace that's about 6 months, 1.5 years and 3 years of history; a solo home user grows
several times slower. Timings are on the development Mac; the sim PC will be slower.

"Site build" is `stats_site.build`. It's what every publish runs, and it's also what the local stats
page runs: `stats_web.make_server` serves a static build cached by the database's modification time, so
the first load after any change (every round changes the database) waits for a full build. Local-only
users pay it too.

As the code is today:

| History | Shots | Site build (publish, or local page after a change) | One `summary` call | One booth lookup |
|---|---|---|---|---|
| 3 weeks | 4k | 5 s | 0.9 s | |
| 6 months | 34k | 246 s | 64 s | 18 ms |
| 3 years | 211k | not run (hours) | not run | 99 ms |

With only the indexes of step 1 added:

| History | Site build | Upload per publish | One `summary` call | One booth lookup |
|---|---|---|---|---|
| 3 weeks | 3 s | 19 MB | 0.5 s | |
| 6 months | 28 s | 65 MB | 4 s | 0.1 ms |
| 1.5 years | 133 s | 179 MB | 14 s | |
| 3 years | 357 s | 347 MB | 33 s | |

A build calls `stats_web.summary` once per context (four). "One booth lookup" is
`Store.shot_struck_before`'s query, which runs during play. A backup of the 3-year copy (taken after
every round) takes 20 s and is 62 MB gzipped.

The copies repeat the same players and courses, so per-player work is understated, and hole images
(9 MB, not copied) are a smaller share than on a real long history. Read the numbers as the shape of
the growth, not exact figures.

## Why it grows

1. **Missing indexes.** `shots` has only `player` and `lie_before` indexes (`store.py`). Nothing
   indexes `shots.session_id`, `shots.ts`, `lines.session_id`, `lines.shot_id`, `rounds.session_id`
   or `player_rounds.session_id`, so every per-session or per-time lookup scans every shot ever
   recorded:
   - `season_analytics.broadcasts` runs two such queries per session, over all sessions: 50 of one
     `summary` call's 71 s (profiled) at 6 months.
   - The booth's lookups during play that filter by session (`Store.shot_struck_before` / `_after`
     and others) cost 99 ms each at 3 years instead of 0.1 ms.
2. **Quadratic loops in `highlights.rounds`** (13.6 s of the 6-month build, even with indexes):
   - each league round scans every shot ever recorded for late gimmes
     (`[r for r in every if late.get(r["id"]) == g["round"]]`);
   - each round deep-copies every player's running bests (`before_round`): rounds × players.
3. **Everything is rebuilt from all history, every time.**
   - `stats_site.build` runs `stats_web.summary` once per context (four, `CONTEXTS`; 24 s at 6 months
     with indexes) and writes every player's file.
   - It rebuilds `stats.db` with every shot, transcript and replay, then VACUUMs it (8 s).
   - `stats_site.upload` sends the whole new version, `stats.db` included, after every round.
   - The local page runs the same full build the first time it's opened after a change.
4. **Backups copy the whole database after every round** (`backup.py`): linear and fine for years,
   but the next thing to grow.

## Targets

At 5 years of a busy league (about 350k shots), on the sim PC:

- A booth lookup during play: under 5 ms. Nothing during a round scans history.
- The local stats page: under 2 s to load, including the first load after a round.
- A publish after a round: under 30 s, uploading a few MB, about the same at year 1 and year 5.
- No step costs more than linear in history, and per-round work doesn't depend on history.

## Steps

Each step ships on its own (step 5 after step 4), keeps today's page and data, and needs nothing from
the cloud.

### 1. Indexes and the two hot spots (small; first)

- Add the indexes above in `store.py` (`CREATE INDEX IF NOT EXISTS`, so existing databases get them
  at the next start; a one-off few seconds on a large database), then `ANALYZE`.
- Rewrite `broadcasts` as one grouped query instead of two per session.
- `highlights.rounds`: group late gimmes by round once; snapshot only the round's own players' bests.
- A test that runs `EXPLAIN QUERY PLAN` on the booth's per-shot queries and fails on a full scan of
  `shots` or `lines`.
- Expected from the measurements: the 6-month build (publish, and the local page after a round) goes
  from 246 s to about 28 s, and booth lookups stop growing.

### 2. Benchmark harness

- `tools/scale_bench.py`: builds the scaled copies from a redacted season (as above) and times the
  build, the local page's first load after a change (through `make_server`), the booth's lookups and
  a backup.
- Run by hand before a release that touches stats; not in CI (too slow). Compare against the targets.

### 3. Build and publish only what changed

This helps local-only users as much as publishers, since the local page runs the same build.

- **Split `stats.db` by season.** A past season's file is built once, named by a hash of its content,
  and rebuilt and re-uploaded only if that season's data changes (an edit, a stats-code change). Each
  build rebuilds only the current season's file. The page (`stats_web.html`, through sql.js-httpvfs)
  queries the current season first and older ones as the user pages back or filters by date.
  `latest.json` lists the season files.
- **Content-addressed files.** Summary and player files are named by a hash of their content, so an
  unchanged file is skipped on upload instead of being sent again under a new version folder. Pruning
  never deletes a file that `latest.json` still points at.
- **Split the summaries by season too.** Today each context's summary file holds every round,
  scorecard, broadcast and player history, so any new round changes it and the whole history is
  serialized and uploaded again. Those lists move into per-season files (built once for a past season,
  like `stats.db`), leaving a small current summary: the current season's lists, the lifetime totals and
  a list of the season files. The page loads an older season's lists when the user opens it.
- **Nothing that changes with new rounds goes into a frozen season file.** Each round's summary today
  embeds the player's current all-history averages (`add_round_summaries`, the `vs` figures), which
  every new round moves. Those comparisons come out of the round lists and are joined in by the page
  from the current lifetime totals. A test checks an old round's summary after a new-season round is
  added.
- Expected: build time and upload size for shots and lists depend on the current season, not on
  history.

### 3b. Repeated and quadratic work in the build (done)

A profile of the full build on the 3-year, 211k-shot copy found work that didn't need stored totals:
`hole_replay.round_tees` and `hole_replays` found each round's next round with a scan over every
round (now a sorted list per session and a binary search); `booth`, `round_gimmes`, the handicap
indexes and `shot_rounds` were recomputed for every context, and `holes_played` twice per context
(now computed once per build, inside `round_record.frozen`). Output is identical on the 1-week,
8-week and 3-year copies; the 3-year build went from 297 s to 194 s. The rest is real passes over all
history, which step 4 addresses.

### 4. Stored totals instead of recomputing from every shot

- A per-player-round contribution table (score, putts, fairways, greens, distances, bests, strokes
  gained), written when a round is recorded and rewritten when it's edited (`set_left_out`,
  `set_round_league`, `set_round_excluded`, `delete_round`, a log backfill). Same shape as the cloud's
  contribution rows, so both can be checked against the same fixtures.
- **Shots outside any round count too.** `Store.season_stats`, `player_profile` and the highlights'
  bests all use shots that never belonged to a recorded round. Those get their own per-player, per-day
  contribution rows, written at capture and on edit, and moved (never counted twice) if a shot is later
  assigned to a round.
- Rollups by day, month and season, built from contributions and recomputed (never added to and
  subtracted from) for the periods a change touches. Lifetime figures read seasons; "this season" reads
  months and days.
- **Global baselines.** Some figures depend on something computed across all rounds, not just the
  round's own shots: strokes gained uses the usual gimme radius (`usual_gimme`), which any added,
  excluded or reclassified round can move. Contributions store the parts that don't depend on such a
  baseline, and the baseline is applied when totals are read, where the maths allows. Where it doesn't,
  the stored rows record the baseline they used, and a change of baseline queues a background recompute
  of the affected rows. The stats code lists every such baseline in one place.
- `stats_web.summary` and the booth's history facts (`Store.season_stats`, `player_profile`, the carry
  model's inputs) read rollups plus the current season's detail instead of every shot.
- A rebuild command recomputes everything from shots, after a stats-code change or as a check. A test
  over fixtures, including older seasons with unattached shots and a change of usual gimme, requires the
  stored rollups to equal a from-scratch computation.
- Expected: the summary part of a build stays roughly flat as history grows.

### 5. Reels cached per round (needs step 4)

- `highlights.rounds` results are stored per round. A reel depends on more than the round's shots:
  - its players' bests before it;
  - its round metadata: the `rounds` rows grouped into it, its end time, league status and which
    players are on which side;
  - which shots belong to it (`shot_rounds`, which can join a round across a booth restart);
  - the commentary that aired, its timings and the swings that followed, which set where each clip is
    cut (`_talk`, `_post`), in every session the round touches.

  So the cache key hashes all of that: the round's grouped rows and shot membership, the content (shots
  and lines, with air times) of every session it touches, the before-round bests it used (from step 4's
  running bests) and the highlights code version. Any change in a session recomputes the reels of the
  rounds in it, which is bounded by the session.
- An edit or backfill to an earlier round changes later rounds' before-round bests for those players,
  so their reels are recomputed; others are reused.
- A test builds reels with and without the cache and requires the same result after: an edit to an
  earlier round, a change to a later round in the same session, a `rounds` row added without new shots,
  and commentary before a booth restart in the middle of a round.
- Expected: a build computes reels only for new or affected rounds.

### 6. Backups (later, when measurements call for it)

- Keep the full copy at booth start; after a round, copy only if that round's data changed, and
  consider weekly full copies with daily copies of just the current season. Restore stays one step. Not
  needed for a few years at the measured sizes.

## Order

Step 1 is about a day's work with a large effect and goes first; step 2 comes with it so each later
step is measured. Steps 3 and 4 are the real fix and are independent of each other; both matter to
everyone, since the local page runs the same build as a publish. Step 5 needs step 4's running bests.
Step 6 waits for data.

## Risks

- Splitting by season changes how the page loads shots: shot search across all years must still work
  (query each season file, newest first, stop when the page is full).
- Stored totals can drift from the shots if a write path forgets to update them; the rebuild command
  and the equality test are the guard.
- An existing large database gets its indexes at the first start after the update, which is a few
  seconds slower once. They're added at startup, never during a round.
