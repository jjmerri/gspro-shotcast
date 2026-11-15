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
- Later, from a 6-year copy (100 copies of the base, 422k shots):
  - **ANALYZE again as the season grows** (`store.planner_counts_stale`). It ran once, when the indexes
    were made, and SQLite kept planning by those counts: on the 6-year copy (counted when it was the
    size of the base) the shot list's lookup of 500 shots' lines read every line each time,
    quadratic: `shot_log` 28 s instead of 8. Now `Store` runs it again at start when a table with
    indexes has twice the rows last counted (or 1,000 never counted): a few times in a file's life,
    0.3 s each on 6 years; the check is a few ms.
  - **`shots(course COLLATE NOCASE)`**: `course_name` (as each round is saved, as the booth recalls a
    hole, and for every round course at each start) read the shots back to front for a course: 80 ms
    a call on 3 years, 3.7 s of each start. Now a lookup in the index (ms; the file 1.4% bigger).
    A start on 3 years: 5.4 s to 1.2 s; on 6 years 2.2 s.
  - `_backfill_finishes`, a safety net for finishing distances play didn't fill, read every shot at
    each start (1.2 s on 3 years). Now it notes the change counter of each round record it checked
    (`round_records.finishes_gen`) and reads only the sessions of records changed since, and those a
    shot was deleted from (a trigger notes them in `finish_recheck`): a start on
    3 years is 0.2 s. It reads every shot the first time (and while any shot has no record).

### 2. Benchmark harness (built)

- `tools/scale_bench.py scale SRC OUT --copies N` makes a bigger file from a real one;
  `tools/scale_bench.py run DB` times, on a copy, a cold build, a build with nothing changed, a build
  after one round is edited, the page's first load after an edit, the booth's lookups and a backup.
- Run by hand before a release that touches stats; not in CI (too slow). Compare against the targets.

### 3. Stats frozen per night, merged for any range (in progress)

Stats aren't kept per season: the page's rolling ranges (3, 6, 9 months, a year) cross seasons. The
unit is the night (a session date). A round resumed after midnight spans two nights; its pieces are
merged like any other, and its record is in both nights' signatures.

- **Night partials** (`night_totals.py`). Each stat family keeps, per night and per round context
  (league / tournament / practice), the counts behind its figures, in the stats cache (4a) under the
  night's signature: its sessions and the change counters of the records with rows in them (each
  record's sessions are kept with its change counter, so only a changed record is looked up). An edit to an old round
  works out that one night again. Lifetime, a season and the rolling ranges merge the nights in range
  (about 150 a year: milliseconds).
- **The count behind every average is kept**, so a merged average is total over total, never an average
  of averages. Bests and top lists keep the night's top rows with their tie keys (value, time, id);
  event lists keep their rows; distributions keep fixed buckets. Sums of floats are kept exactly (as
  `math.fsum` keeps them, `night_totals.exact_add`), so however the nights are split the figure is
  the same: the true sum, correctly rounded.
- **Done:**
  - players (`players()`, `player()`, `three_putts`): 5.5 s to 0.3 s on a warm build of the 3-year
    copy.
  - the holes family (strokes gained, recovery, trends, round summaries' strokes gained and best and
    worst shots, and the highlights' candidates: each player's best of the night, all a range can
    take from it). `holes_played` no longer runs on a warm build: 74 s to 39 s for the 3-year copy.
  - the rounds list's facts from the shots (`_round_facts`: who played, hole lengths, first shot,
    longest drive, mulligans, putts per hole): 6.1 s to 3.5 s (the rest is the tee boxes, below).
  - a digest per round record (`round_record.record_shots`, cached by the record's change counter):
    its shot ids, sessions, first and last shot, holes with their times, players, courses and gimme
    votes, with and without excluded rows. `shot_rounds`, the late gimmes, `round_spans`,
    `round_gimmes`, the tee boxes and the hole replays' and reels' round scans take it from there;
    an edit reads only its record's rows again.
  - the reels' before-round bests: per night, the night's bests and each round's in-night bests
    before its start (`reel_bests`), merged in time order. Where nights overlap in time (a clock
    change), the old full read is used.
  - the records (`records_table`): each player's best of the night per record (with its tie key), a
    range's record the best of its nights'.
  - the courses (`courses`, the summary's and each player's): per night, course and player, the cards'
    rounds, last date, best nine and each hole's scores (a count per score: the average is total over
    total, birdies and bogeys go by the first card's par). The best nine is now the truly lowest, the
    earlier round taking a tie (it compared a round's nine with the best's rounded one).
  - the booth tab (`booth`): per night, each speaker's lines, words and kinds, and the rounds by date;
    the signature phrases from a second partial per night (each phrase's count and first line), merged
    over the last year's nights only (below). Putt calls stay a direct query (cheap, and predictions
    aren't in a night's signature).
  - the handicap index (`handicap_index.indexes`, also run by the booth): not split per night (each
    round's differential depends on the index before it, and a round's tee can be picked from the
    course's layouts on other nights), but its lookups no longer grow with history: the layout batches
    are grouped by course and session once instead of scanned per round (rounds × layouts before),
    and each player's index is worked out once per card. 0.65 s on 3 years; 1.28 s to 0.67 s with
    layouts logged for every round.
  - the usual gimme: `round_groups` (the rounds as played, which it and a dozen others read) is worked
    out once a build instead of 59 times; the usual gimme went from 0.84 s a build to 0.07 s on 3 years.
  - broadcasts and the page's filters: per night, its sessions with lines (courses and players in
    order of first appearance), and the courses, clubs and lies played and whether any round was a
    league or tournament one. Each worked out once a build (broadcasts were four times).
  - Python's cycle collector runs rarely while a build runs (`stats_site.gc_paused`, process-wide,
    so the booth's thread too): a build makes next to no cycles (the same peak memory without it),
    but each full collection went over every object the build holds. Its thresholds go to
    `GC_BUILD_THRESHOLDS` (100,000 / 50 / 10, from Python 3.12's 700 / 10 / 10), so young cycles
    are still freed, and back when the last build ends or after `GC_TIME_LIMIT` (10 minutes),
    whichever is first. On 3 years the collector took 2.0 s of an 8.5 s warm build (now 6.5 s; 6.3 s with it
    fully off) and 5.8 s of a 63.2 s cold one (now 58.7 s).
    Loading the kept nights' results is about 0.9 s of what's left; it grows with the nights kept.
  - Hole replays load a year at a time, for the year files being built: the year split and its
    signatures only need each round's signature and sessions (`hole_replay.replay_rounds`), so a warm
    build with no year to build reads none. On 3 years, peak Python memory went from 495 MB to 364 MB
    on a warm build (the process's from 907 MB to 768 MB, up to 346 MB of which is the season file
    mapped in), and a cold build holds one year's replays at a time.
  - Nights' partials aren't kept for the rest of the build once merged (`per_night(memo=False)`):
    holes, players, courses and records merge every context's (all, league, tournament, practice)
    from one read, and the others are read once. What a warm 3-year build holds to its end went from
    440 MB to 180 MB (96 MB of it the holes' merged moments, streaks and trends, which the highlights
    are picked from); peak Python memory from 364 MB to 253 MB, the process's from 768 MB to 640 MB.
  - A cold build works out the nights `BATCH_NIGHTS` (64) at a time (`night_totals.per_night`), not
    all history in one go: the holes' rows and holes of every night were held at once. On 3 years a
    cold build's peak memory went from 825 MB to 598 MB.
  - Output identical to before on the 1- and 50-season copies.
- **Where it stands** (warm build, nothing changed): 18 s on 1.5 years, 37 s on 3, 74 s on 6: still
  linear. On 6 years: writing the page's shot list and replays 37 s (`build_data_db`, every shot
  every build), the summary 32 s. Both are what's left below; the target is flat. With the round
  digests and reel bests: 3 years 31.6 s (`build_data_db` 17.3 s, the summary 12.8 s), 6 years
  66.6 s (36.3 s and 26.9 s). Still linear: those two are the page files and the families left.
  With the per-year page files: 3 years 17.8 s warm, 23.5 s after an edit to the newest night; 6
  years 33.4 s and 44.8 s. What's left growing is the summary (the families still to port).
- **Bench at 1, 3, 5 and 10 years** (copies of the 6-year file, `tools/scale_bench.py`; 72k, 211k,
  350k and 692k shots), before the cold build was batched:

  | Years | Cold build | Warm, nothing changed | After a round | Backup | Peak footprint |
  |---|---|---|---|---|---|
  | 1 | 20.1 s | 2.5 s | 7.5 s | 3.5 s | 530 MB |
  | 3 | 58.6 s | 7.1 s | 11.9 s | 9.9 s | 1.2 GB |
  | 5 | 102.6 s | 12.2 s | 16.6 s | 16.8 s | 2.0 GB |
  | 10 | 211 s | 25.1 s | 30.0 s | 88 s | 3.9 GB |

  After a round is at the 30 s target at 10 years but still grows. A warm 10-year build (31.7 s
  under the profiler): gapping 7.3 s (every full swing a player hit; it waits on the bag resets),
  `rounds` 6.1 s, the handicap index 4.2 s, merging the kept nights about 11 s (holes 3.7 s,
  highlights 2.5 s, players 2.0 s, firsts 1.5 s) and loading them about 3 s.
- **Predictions** need no port: about 1,600 rows at 3 years, read in under 1 ms.
- **To port:** the page files' remaining lists (below).
- **Decisions:**
  - The usual gimme stays as it is (the most common radius); the families it feeds put it in their
    signature, so a change in it (rare) works them all out once. No number changes.
  - Putting buckets are kept per `PUTT_EDGES` interval and merged at the usual gimme when shown.
  - The round summaries' `vs` figures (against the player's lifetime numbers, which every new round
    moves) are worked out by the page when it shows a round, not stored in the round.
  - Gapping goes by the player's bag: a whole-bag reset and per-club resets, set on the local stats
    page, instead of a time window.
  - The booth: rates are total over total (today `season_rounds` averages per-round rates), band
    lookups snap to 1 ft / 5 yd buckets, it merges the frozen nights once at startup and adds tonight
    live; `spoken_lines` looks back 12 months, `side_rounds` gets a limit. (The history reads turned
    out exact without buckets, read per player when first asked: see "Booth history reads" below.)
  - The booth tab's signature phrases go by the last 12 months (the 1y range's nights); its other
    numbers stay lifetime. Older nights' phrase partials are dropped from the cache.
- **Page files (done for the shot log).** The page's `stats.db` is one file per calendar year of play
  (`stats_site.page_files`):
  - A session's shots, swing times and transcript go in its date's year; a round's hole replays in its
    first hole's year, with the minimaps they draw on. Rows of a session not on file go in the newest.
  - A year is built again only when its signature changes (stats cache, kind `page_year`, per site):
    its nights' signatures, the records of a line about a shot in another year, its rounds' replay
    signatures and the speakers' names. Otherwise the last build's file is kept.
  - Files are `data/<sha256[:20]>.db`, served immutable; `v/<version>/data.json` and `latest.json`'s
    `data` list them newest first with their year and ranges (times, sessions, rounds, shot ids).
    Building a year again from scratch gives the same bytes, so an unchanged year is never re-sent.
  - `stats_site.upload` sends only data files the bucket lacks, and deletes those no kept version
    reads (each one's `data.json`: the site's copy, else the bucket's; if one can't be read, none).
  - The page picks the files a query can find rows in, takes each one's 500 newest, merges them by
    time and keeps 500, skipping a file whose times are all older than the 500th kept; a worker per file, kept across versions. Same rows, same order as the single file
    (checked on 1 and 3 years).
  - A page left open across the first publish with data files can't find `v/<version>/stats.db` and
    needs a refresh.
  - Still to do: summary and player files content-addressed, and past years' lists (rounds,
    broadcasts, trends) in per-year files the page loads when paged back.
- **Bench** at 1, 3, 5 and 10 years: the after-a-round times must be flat.

### 3a. The build reads the file, not a copy in memory (done)

The build used to copy the whole database into memory first (`stats_web.connect`), because a minute
of reading the file in SQLite's default journal mode would lock out the booth's writes. A copy that
grows with the years, held for every build, is the wrong cost. Now:

- The season file is in WAL (write-ahead log) mode: `Store` sets it, and the file keeps it. Writes
  go to `season.db-wal` beside the file and are checkpointed into it later; readers don't block
  writers or each other.
- The build opens the file and holds one read transaction for the whole build: every query sees the
  file as it was when the build started, and the stats cache's signatures and figures come from that
  same snapshot. The booth's shots, and the page's own edits, carry on meanwhile.
- It's opened read-write and made `query_only`: a read-only connection can't open a WAL file no one
  else has open (it makes the `-shm` beside it). The diagnostic report and `tools/db_replay.py` too.
- It's read mapped (`mmap_size`, the first GB): as quick as the copy was (3-year copy, warm: 37 s
  either way; 43 s reading a page at a time) with no copy of its own.
- The local page rebuilds when the file or its `-wal` changes (`stats_web.file_stamp`); the file alone
  can go unchanged for a while.
- Where WAL can't be set (a network share) the file stays as it was and the build reads a copy, as
  before. Old versions of the app read and write a WAL file as usual.
- Copies of the file go through SQLite (the backups already did; `log_backfill`'s dry run and the demo
  build now too): a plain file copy can miss what's still in the `-wal`. Copying `season.db` by hand
  while the app runs has the same risk. Backups stay single files (not in WAL mode).
- A restore saves the current file with what's still in its `-wal` (after a crash the last shots may be
  only there), read through SQLite; a file SQLite can't read is saved byte for byte, as before.

### 3b. Repeated and quadratic work in the build (done)

A profile of the full build on the 3-year, 211k-shot copy found work that didn't need stored totals:
`hole_replay.round_tees` and `hole_replays` found each round's next round with a scan over every
round (now a sorted list per session and a binary search); `booth`, `round_gimmes`, the handicap
indexes and `shot_rounds` were recomputed for every context, and `holes_played` twice per context
(now computed once per build, inside `round_record.frozen`). Output is identical on the 1-week,
8-week and 3-year copies; the 3-year build went from 297 s to 194 s. The rest is real passes over all
history, which step 4 addresses.

### 4a. The stats cache (built: hole replays and reels)

Results worked out per round are kept between builds in a cache file beside the database
(`<db>.stats-cache`, `commentator/stats_cache.py`), not in the database itself, so no write path has
to keep it up to date and it can't drift from the shots:

- Each result is stored with a signature: the round's record change counter (`gen`) plus whatever
  outside the record it reads. A build reuses results whose signature matches and works out the rest
  in one pass (a key with no result, such as a round with too few moments for a reel, is kept too).
- The whole file is thrown away when the app's code (a hash of the package's `.py` files), the schema
  version or the database's `dataset_id` changes. `backup.restore` deletes it; a file copied back by
  hand is put right by its next build, which re-keeps each changed round under the `gen` it went back
  to. (One copied back by hand and edited before any build can't be told apart: the counters reach the
  values kept with other rows. Rebuild stats is the way back; a random per-change token would close
  it, at the cost of a schema change.) A
  cache file that can't be read or written is worked around, not an error. `stats_cache.clear`
  empties it (for a Rebuild stats option).
- It's only used on a file where every row has its record (`round_record.recorded`); otherwise the
  build works everything out as before.
- **Hole replays** (`hole_replay.hole_replays`): signed by the round's `gen`, its sessions, the start of
  the next round in them (which bounds the layout it takes), and the layouts and images of its courses.
- **Reels** (`highlights.rounds`, step 5): signed by the round's `gen`, its end and course, its players'
  bests before it, and the content of every session it touches (the `gen` of every record with shots or
  lines there, or a line on a shot there), since the booth's words and the next swings set where clips
  end. Changed rounds' reels are worked out from their sessions' shots and lines only.

Tests (`tests/test_stats_cache.py`) build with and without the cache after an edit to an earlier round,
the booth's words and practice in a later round's session, a side added with no shots and a layout
learned, and require the same result; and check that an unchanged build reuses every round.

Measured on the 3-year copy: output identical to the build before the cache, cold and warm. A cold
build takes as long as before (145 s, against 149 s for the previous commit timed the same day); a
warm one takes 115 s, with replays down from about 25 s to 3 s and reels from about 23 s to 3 s. The rest of a warm build is
the summaries (`highlights_by_period`, `rounds`, `strokes_gained`, `booth`, `holes_played`), which the
rest of step 4 addresses.

**Records and highlights in one pass.** The Records tab's ranges were each worked out with their own
queries, for every context summary and again for every player's file (190 sets of sorted queries on the
3-year copy). Now each context's records and highlights are worked out once, for every player and range
together (`records_table`, `highlights_table`), and a tie goes to the earlier shot (before, whichever
SQLite's plan put first). Same output as the previous build with that tie rule; on the 3-year copy a
cold build went from 128 s to 103 s and a warm one to 79 s.

`rounds()` runs once per context, but what it reads from the shots (tees, lengths, drives, putts,
mulligans) doesn't depend on the context: that part is now worked out once a build. 3-year copy: cold
93 s, warm 70 s.

Then, all with the same output: strokes gained worked out once per context rather than three times;
the shots behind each context's holes read once a build and split by context in Python; the booth's
phrase count done with per-word flags; and the shots file packed with `VACUUM INTO` a new file rather
than `VACUUM` (which packs into a temporary file and copies it back: 9 s down to 3 s on the 330 MB
file). 3-year copy: cold 80 s, warm 56 s. What's left is spread thin: the players' rows (many small
queries per player), the records' one-pass tables, the shot log and its JSON, a few seconds each.

**First build in the background.** The tray's stats page used to build before the booth started, so
the first start after an update (a cold build) held the booth up. It now builds on its own thread
(`make_server(build_in_background=True)`) and the booth starts at once. Pages wait for that build; if
it fails, each request tries again and answers with an error rather than the last version's page.

**Booth history reads (done, `commentator/booth_history.py`).** At 10 years (692k shots) each per-shot
history read (`season_stats`, `season_longest_drive`, `player_profile`, `approach_proximity`,
`putting_make_rate`, `three_putt_rate`, `fairways_hit`, `recovery_proximity`, `putt_band_rate`,
`approach_leave`, `length_norm`) took 150–470 ms, several per tee shot. Now a player's nights before
today are read once, the first time the booth asks about them, into sorted lists (SQLite still works out
which rows count, with the old queries' filters); today's rows are read each time. Instead of the
1 ft / 5 yd buckets agreed above, a band is two binary searches over the exact distances, so every
figure is the old query's (a tie SQLite left to its plan goes to the earliest row; the favourite club's
to the first by name). `tests/test_booth_history.py` keeps the old queries and checks them against the
new reads through the booth's writes, the page's, rollbacks, replaced rows and a new day.
- What's kept is dropped when anything it read may have changed: another connection's commit
  (`PRAGMA data_version`), or this connection's write to a shot of an earlier day, to a session, or one
  replacing another row (temp triggers set a random value, so a rolled-back write is noticed too).
- 10 years: a player's first read 0.5–2.3 s once a night (it was that much each shot); after that all
  12 reads together 2–10 ms, about 10 ms just after a new shot; 128 MB peak for the process with four
  players read.
- The other reads (hole, team and broadcast stats, `season_rounds`, `team_course_history`,
  `hole_scoring`, `prediction_record`, `recent_form_mentions`, `today_longest_drive`) take under 11 ms
  at 10 years; `side_rounds` 47 ms, read once a round.
- The carry model starts from what it had learned before today (`Store.carry_model`, the
  `carry_snapshot` table) and learns only today's shots on top, so it comes out exactly as a replay of
  every shot would (the model is an order-dependent average, so a window would change it). Triggers on
  `shots`, kept in season.db so every connection and process fires them, delete the snapshot when a
  write touches a shot it learned from or a shot in one of its sessions (a stroke's next one says where
  it came to rest); an edit to a column it doesn't read leaves it be. At 10 years a start took 15.5 s
  here; now 0.5 s, or about 8 s at the first start and the next start after such an edit. A shot write costs
  about 12 µs more.
- The phrases the booth won't repeat come from the last year's lines (`Engine.PHRASE_MEMORY_DAYS`),
  not every line ever: at 10 years (345k lines) reading them and their phrases took about 14 s at each
  start (timed under tracemalloc), 1.6 s for one year's.

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

### 5. Reels cached per round (built, see 4a)

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
