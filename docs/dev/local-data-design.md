# Design: the local round record

How the booth stores rounds in `season.db`, for self-hosted and cloud mode alike. Today a round is
inferred from timestamps and names each time it's read; this makes it a stored record with an ID,
players with slot IDs and a change counter, and edits wait till the booth is done with a round. Self-hosted
uses it for speed (stored totals, cached reels: `stats-longevity.md` steps 4 and 5); cloud mode
uses the same record to upload rounds (shotcast-cloud `docs/architecture.html`, "Data design (rev 4)").
One model, one set of write paths, one importer.

Nothing here changes what the stats page or the booth shows. Existing databases are migrated in
place, and older app versions still open them.

**Local first.** Shotcast Cloud may never ship, so the design comes in two layers:

- **The local layer** is worth having on its own: stored rounds, slots, the change counter, the
  migration, edits only on settled rounds, stored totals and cached reels. It ships in the app with no cloud code,
  no cloud configuration and no cloud tables.
- **The cloud layer** is added only if the cloud goes ahead, by its own later migration. It adds
  writer ownership, residency, the upload counter, the outbox and journal, and their triggers.
  Removing it, or never building it, leaves the local layer untouched.

Sections marked *(cloud layer)* describe the second layer. Nothing in the local layer refers to
them.

## What's there today

- `shots` have no round. A shot's round is the next `rounds` row on the same course that ended
  within 8 hours (`stats_web.shot_rounds`, `ROUND_SPAN_SECONDS`), and gimmes conceded just after a
  round was recorded are added to it (`_late_gimmes`, `CLOSER_SECONDS`).
- `rounds` has one row per side, written at round end (`Store.record_round`). A round is the set of
  rows from one session and course that ended within 30 s (`round_groups`, `ROUND_GAP_SECONDS`); its
  ID is the lowest row ID. `player_rounds` (one row per player) is matched to it by session, course
  and end time (`player_round_of`); `week_results.round_id` points at one of its rows.
- Players are names. `Round` keys its roster by name (duplicates refused) and counts rounds in
  memory (`Round._ids`); a booth restart mid-round starts a new `Round`, and the time rules above
  join the two halves.
- Rows change long after capture: finishes and rests (`fill_finish`, `set_shot_rest`), lies,
  mulligans, renames (`rename_shots`), format changes (`set_format`), league and tournament marks
  (`mark_league`, `mark_tournament` by `id > after_id`), the log backfill (`log_backfill.py`,
  `fill_log_totals`), repair migrations, and the stats page's edits (`stats_web.py`: `set_left_out`,
  `set_round_league`/tournament/practice, `set_round_excluded`, `delete_round`). The page's edits
  overwrite the same columns the capture paths write.
- Only two indexes (`shots(player)`, `shots(lie_before)`).

## The record

### Tables (schema version 6, local layer)

- **`meta`**: one row.
  - `dataset_id`: a UUID made once per `season.db`, so a copied database is recognisable. (The
    cloud layer would use it as the importer's source namespace.)
  - `quiet`: a guard flag, `0` or `bookkeeping` (below).
- **`round_records`**: one row per round, and one per *practice stretch* (below).
  - Identity: `uuid` (primary key), `kind` (`round` or `practice`).
  - What it is: `session_id`, `course`, `started_at`, `ended_at`, `state` (`in_progress`,
    `finished`, `abandoned`).
  - `gen`: the change counter (below).
  - `assigned`: how its rows were grouped, `live` or `inferred` (the migration).
- **`round_slots`**: one row per player in a round.
  - `slot_id` (UUID), `round_uuid`.
  - `seat`: the GSPro setup-screen index, when known.
  - `name`: as GSPro shows it, updated by a rename.
  - `side`: the side key `rounds.side` uses.
  - `player`: the league roster name it was matched to, or null.
  - `matched_by`: `booth` (one of the round's players as it was played, `player` from the booth's
    roster match), `shot` (a name on a shot that's none of the round's players: a misread, dropped
    when its shots are corrected), `legacy` (made by the migration), and later `manual` (a
    correction). (Rev 4's "how each link was
    made" correction would read this.)
- **New columns.** Older versions don't write them; an `AFTER INSERT` trigger fills `uid` even for
  them.
  - `shots`: `uid`, `record_uuid` (its round, or its practice stretch), `slot_id` and `derived_from`
    (a gimme's source shot, below).
  - `lines`: `uid` and `record_uuid`.
  - `uid` is a random ID. Everything that refers to a shot or line from outside its table (a
    gimme's `derived_from`, the cloud layer) uses `uid`, never the integer `id`. Without `AUTOINCREMENT`, SQLite
    reuses the highest `id` after it's deleted, so an integer reference could land on an unrelated
    new shot. (The cloud layer's eviction deletes rows, which is what makes this matter.)
  - `rounds`: `round_uuid`.
  - `player_rounds`: `round_uuid` and `slot_id`.

  `week_results` keeps pointing at a `rounds` row, which now carries the UUID. Existing columns,
  `shots.player` included, stay as they are. Every reader keeps working, and readers move to the new
  columns one at a time.
The cloud layer's tables and columns (below) come with their own migration, and their triggers with
them, so a trigger never names a table that isn't there. (SQLite resolves every table a trigger
names even when its `WHEN` is false.)

### Practice stretches

Shots captured outside any round (warm-ups, range time, a round never recorded) belong to a
*practice stretch*, the nearest thing to a round they have.

- A stretch begins with the first shot outside a round.
- It ends when a round starts, when the session rolls over, or after the bay has been idle for
  `PRACTICE_IDLE_SECONDS` (30 minutes, to be tuned).
- It's a record like a round, with `kind = practice` and its own UUID, and it's `finished` when it
  ends. So it changes only while it's open, like a round. (In the cloud layer it uploads like a
  round: rev 4's "unattached shots".)

A shot later assigned to a round moves from the stretch to the round, which changes both. That
happens to opening drives adopted by `_adopt_opening_shots`, and to late gimmes.

### Writing it

- **Round activation.** One transaction in `Engine._start_round`, after `roll_session_if_idle`, so
  the record's session is the one its shots go into.
  - It inserts the `round_records` row (`in_progress`) and a slot for every player.
  - It moves the adopted opening shots into the record, and ends any open practice stretch.
  - It resolves a restart (below).

  `Round` gets its UUID at construction (alongside the `Round._ids` counter the engine still keys
  on) but writes nothing until activation. Activation takes in every shot after the round's shot
  floor that's in practice or in no record, so its opening drives come with it.
- **Players.**
  - A player added or dropped mid-round (`Round.add_player`, `Round.remove_player`) updates the
    slots: each player's slot takes their GSPro name, roster name and seat, and a slot nobody has any
    more (no shot or card on it) goes. So does a seat order the booth learns later, and the round's
    end syncs them once more. A slot is matched by name, case aside, or roster name.
  - A rename (`rename_shots`) updates the slot's `name` and its shots' `player`. The slot ID stays.
  - A reassignment (`move_shot_player`: a shot was someone else's) is a separate operation. It moves
    the shot, and its gimme row, to the other player's slot and name in one statement pair.
- **Shots and lines** are inserted with the live record's UUID and the shooter's slot. A shot takes
  the record of the round it was struck in, even when it's stored after that round ended or another
  began. Its slot is found by GSPro's name or by the roster name the booth gives a league player. A line on a
  shot takes its shot's record. A line on no shot goes to the round being played, else the round
  that ended within `CLOSER_SECONDS` (its closer), else practice. With no round live, they go to the
  open practice stretch, which starts one if needed. When a round starts, the lines on no shot
  that practice got after its last shot (the opener, aired over the course load) move to the
  round, as the time rules had it. `record_gimme` copies `record_uuid` and
  `slot_id` from the shot it follows, like it copies the rest.
- **Late gimmes.** A gimme conceded within `CLOSER_SECONDS` of the round ending gets that round's
  record directly, since the engine still knows the round. The `_late_gimmes` time rule is needed
  only by the migration.
- **No more ID ranges.** `mark_league`, `mark_tournament`, `rename_shots`, `players_after` and
  `restamp_shots` select by `record_uuid`, not `id > shot_floor`. Today they can catch any later
  shot, a practice shot included (and, in the cloud layer, rows that arrived from the feed). Rows
  after the floor written with no record (a path that stamps none) still count, and a round with no
  record (an older caller) falls back to the floor.
- **Round end.**
  - `_record_round_stats` writes the `rounds` and `player_rounds` rows with the UUID and slots, and
    sets the record `finished` (`abandoned` for `complete=False`) and `ended_at`, in the same
    transaction.
  - The early return for a round quit before any hole was finished sets the record `abandoned` too.
    The shots it has are kept, as today.
- **Shotcast restarted mid-round.** A crash, a Windows update reboot or the tray being closed
  stops the booth. There's no auto-restart, so it's whoever notices (a player, venue staff)
  relaunching it. It's rare.
  - Shots played while it's down are missed, as today. (The log backfill fills in details of
    stored shots; it doesn't create missing ones.) The booth already handles starting mid-round,
    and the scorecard read at the end gets the scores right.
  - The new `Round` continues the open `in_progress` record (its UUID and slots) when the course is
    the same, its players match the record's players' slots (case aside), and it's had a row within
    `ROUND_SPAN_SECONDS`. Otherwise it starts a new record,
    and the open one becomes `abandoned`, ended at its last row. A round this same booth began is
    never continued: a new round started without the last one ending abandons it.
  - At start, the booth ends the practice stretch the last one left open, and abandons an
    `in_progress` round with no row for `ROUND_SPAN_SECONDS`. Both are kept, never dropped. (Opening
    the file for maintenance, such as the stats page or a backfill, doesn't.)
  - A practice stretch that ends with no rows is dropped.
  - Tested by stopping Shotcast mid-round on a sim and starting it again.
- **Duplicate names** stay refused for now. With slots, two "Mike"s could be allowed later without a
  data change; that's an engine change for another day.

### The change counter (`gen`)

`gen` moves whenever what a record shows changes: capture, a backfill, a repair, a state change, a
page edit. Readers that cache anything per record store the `gen` they computed from and recompute when it
differs:

- contribution rows (`stats-longevity.md` step 4);
- the reel cache (step 5), whose key also covers the commentary sessions the round touches.

SQLite triggers move it, not the write methods, so no write path can forget, an older app version's
included:

- `AFTER INSERT/UPDATE/DELETE` on `shots`, `lines`, `rounds`, `player_rounds` and `round_slots`
  bumps the row's record. When `record_uuid` itself changes, both records are bumped.
- `AFTER UPDATE OF state, ended_at, course` on `round_records` bumps that record. Recursive triggers
  are off, so this doesn't re-fire.
- Triggers on `week_results` (through its `rounds` row) do the same.
- **`meta.quiet`** says who is writing:
  - `0`: normal.
  - `bookkeeping`: the migration. `gen` doesn't move, since what the record shows hasn't changed.

  Each of these is set for one transaction by the code doing it. Older versions never set it.
- A statement touching 200 shots bumps `gen` 200 times. That's harmless: it's only compared, never
  counted.

### Edits wait till a round is settled

The page's edits (leave a shot out; make a round league, tournament or practice; exclude or delete a
round) write the columns directly, as they always have. No edit log, no second copy of what was
captured: the edit *is* the source of truth. That's safe because of one rule: **a round is edited
only once it's settled**, meaning its record isn't `in_progress` and it ended at least
`CLOSER_SECONDS` (3 minutes) ago (`round_record.in_play`).

- **Nothing writes those columns after that.** The only other writers of `is_league` and
  `is_tournament` (`mark_league`, `mark_tournament`, `set_shot_round_class`) run while the round is
  being played or as it ends. Only the page writes `excluded` and `left_out`. The later writers (log
  totals, hole layouts, the log backfill, repairs) change other columns of the round's shots.
- **What arrives late** (a putt conceded after the last shot, the closer's lines) lands inside the
  window, so it's there before any edit can be made.
- **A booth that stopped mid-round** leaves its record `in_progress` till it next starts (which
  continues or abandons it). Till then the round isn't editable.
- **Shots too.** Leaving a shot out goes by its round record the same way (`shot_in_play`). A
  practice shot can always be left out. A shot with no record yet (an older version wrote it) goes
  by its own time.
- **On the page**, the summary sends `unsettled` (each unsettled round, with when it settles, or
  null while its record is open) and `unsettled_shots` (the same for each shot held back, by its
  record: a round being played isn't on the page yet). The Manage actions and the shot's "Leave out" don't show till then, and the server
  refuses an edit anyway (`RoundInPlay`, HTTP 409: "the round just ended: try again in a few
  minutes"), for a page left open. Undo is the existing toggle back.
- **Not covered:** a downgrade. An older version can edit any round and doesn't know the rule. Its
  rows are reconciled at the next start like any other it writes.

### Migrating an existing database

A one-off step at the first start on the new version (`round_record.migrate`). It runs after the
start-of-booth backup (`backup.py`) and the step 1 indexes, in `quiet = bookkeeping`, in one
transaction.

1. **Assign rounds from the raw tables.** It doesn't go through `stats_web.connect`, which hides
   excluded and deleted rows. It uses one new unfiltered membership function, extracted from
   `round_groups`, `player_round_of`, `shot_rounds`, `_late_gimmes` and the line-matching half of
   `_round_rows`, without the edit-eligibility checks (`_round_rows` refuses a deleted round). The page
   then reads the same function, so the two can't drift.

   Each group becomes a record with `assigned = inferred`: `finished`, or `abandoned` where
   `complete = 0`. Deleted and excluded rounds become records too, with their marks as they are.
2. **Make slots:** one per distinct player name in each round, with `matched_by = legacy`. `player`
   stays null: the roster isn't in the database, so the roster matching fills it later.
3. **Make practice stretches** for every shot and line left over, split as the live rule splits
   them (a round's start, a session change, an idle gap).
4. **Give every shot and line a `uid`**, in the same pass that writes its record (and any left
   over after). Then check every row has one, and build the unique index on it, which fails (and
   rolls everything back) if a uid repeated.
5. **Set `user_version = 6`**, last, in the same transaction, after the `uid` check.

On the 3-year scaled copy of a busy league (211k shots), steps 1–5 take about 11 s, once.

**Reconciling after a downgrade.** An older version writes rows with no `record_uuid` and may finish
a round whose earlier shots were already put in a practice stretch.

- At the next start, the new version finds the rows with no `record_uuid`.
  - It reruns the membership function over the whole history, assigned or not. (Growing a time span
    until nothing new is touched gives the same answer; it's rare enough not to be worth it.)
  - It compares the result with the records that are `assigned = inferred` or practice.
  - A group keeps the UUID of the existing record holding most of its rows. A late side or card of a
    round the booth recorded joins that live record.
  - Rows move between inferred records and practice stretches where the rerun says so.
  - Every record that gains or loses a row has its counters bumped (`quiet = 0`).
  - `live` records are never regrouped. Their membership came from the engine, not a guess.
- Fixtures cover:
  - a round begun before the upgrade (its shots already in a practice stretch), finished by an old
    version in a new session after a booth restart, then re-upgraded;
  - legacy inserts.

The page keeps its numeric round IDs (lowest `rounds` row ID) in URLs, so published links still
work. The UUID is the identity for everything new.

**Equality test.** On fixtures, the grouping the migration stores must equal what the membership
function computes. The fixtures are the redacted demo season, a booth restart mid-round, and an
excluded and a deleted round. Readers then switch to the stored grouping.

**Cost.** One pass, using the step 1 indexes. The benchmark harness (step 2) measures it on the
3-year copy. If it takes more than about 10 s, it runs per session in the background instead, with
readers using the function until `meta` says it's done.

## The cloud layer *(only if Shotcast Cloud ships)*

Everything from here to "Indexes" is the cloud layer. It's added by its own migration, after the
local layer has shipped, and only if the cloud goes ahead. It adds:

- to `meta`: `mode` (`self_hosted`, `importing` or `cloud`), `bay_id`, `writer_generation`,
  `feed_watermark`;
- to `round_records`: `writer_bay`, `resident`, and `seq`;
- `upload_outbox`, `row_journal` and a staging table, with their triggers;
- a `remote` value for `meta.quiet`, for applying the server's data. It moves `gen` but not `seq`.
  Eviction and rehydration use `bookkeeping`.

**Two counters.** "What this bay must upload" and "what a cache must recompute" differ, so the cloud
layer adds `seq` next to `gen`. `seq` moves on the same triggers as `gen`, but only with
`quiet = 0`: capture, a backfill, a repair, a state change. It drives the upload. The server's data
(including web edits) moves only `gen`.

| | Self-hosted | Cloud |
|---|---|---|
| Round record, slots, `gen` | yes | yes, same code |
| Stored contributions and rollups | computed locally from all history | server's rollups from bootstrap; local contributions for this bay's rounds not yet in the feed |
| Edits | on the local page, once settled | on the web stats page only; the server owns the edit fields |
| Upload outbox | absent | used |
| History kept | all, forever | a bounded window of detail, plus stubs |
| Local stats page | as today | none: the tray's "Stats" opens the web stats |

### Who may change a record

Cloud mode separates three things that are easy to conflate:

- `writer_bay`: who may revise a record.
- `resident`: whether its detail is on this PC.
- Its outbox state: what's been sent.

**Ownership.**

- Only a record whose `writer_bay` is this PC's `meta.bay_id` can be captured or revised here. A
  `BEFORE INSERT/UPDATE/DELETE` trigger on its child tables raises for any other record, unless
  `quiet` is set.
- A replacement PC paired to the same bay owns that bay's records once bootstrap has given it their
  current revisions, however their detail arrived.

**What the server may change here.** Applying the server's data (`quiet = remote`) follows one rule:
it never replaces this bay's captured data while that data hasn't been acknowledged.

- Edit fields (`excluded`, `left_out`, a finished round's league/tournament/practice mark, a
  deletion) and person links on any record are applied: they're the server's to own. The PC sends
  them only in a round's first revision (below), so applying them can't fight an upload.
- Another bay's revision replaces that record's detail. Nothing here writes it.
- For one of this bay's own records, a captured revision from the server is compared with the
  outbox row:
  - **Older than the acknowledged revision:** ignored.
  - **Equal revision and hash:** the echo of this PC's own upload. That's the normal case, and it
    counts as an acknowledgement if the real one was lost.
  - **Equal revision, different hash:** an error, flagged. The server shouldn't allow it.
  - **Newer:**
    - When the record is clean (not `in_progress`, nothing pending, not dirty), the revision is
      applied. That happens only after a handoff or a restore.
    - Otherwise the revision is kept in a staging table and the record is flagged. The owner
      chooses, as in rev 4's flagged batch. Nothing is overwritten.

**Eviction and rehydration.**

- **What's evicted.** Eviction (`quiet = bookkeeping`) drops a clean, acknowledged record's shots
  and lines and sets `resident = 0`. The record, slots, sides and each row's `uid` stay.
- **Hydrate first.** Code that reads or changes detail asks the Store to make the records it needs
  resident first: by record for a round's page, by session and time for a log backfill. An
  `UPDATE` aimed at an evicted shot would otherwise match nothing and fire no trigger.
  - The log backfill gets an explicit first phase: find the sessions its logs cover, hydrate their
    records, then apply. Logs reaching back further than the window cost a download, not a wrong
    result.
- **Guard.** The child-table guard also raises on an insert into a non-resident record. That's a
  second line of defence against a forgotten path, never the main mechanism.
- **Order.** Eviction takes the oldest records first, and never takes a record touched by any
  capture path in the last `ROUND_SPAN_SECONDS`.

### Upload outbox (cloud mode only)

Two kinds of thing are uploaded: round records, and rows outside any record.

**Upload once, when settled.** A round is uploaded once, after it's settled (not `in_progress`,
and `CLOSER_SECONDS` past its end: the same rule as page edits), so the late putt and the closer
are in the first upload. Live scores during play are a separate, throwaway feed (the cloud design's
"Live scores"), never this outbox.

**Enqueueing.** `upload_outbox` has one row per record that has changed since it was last
acknowledged. A trigger on `round_records` marks the row dirty whenever `seq` changes on a record
whose `writer_bay` is this bay. It runs in the same transaction as the change. In self-hosted mode
`writer_bay` and `bay_id` are null, so it never fires. The uploader skips a dirty row till its
record is settled.

**Later revisions are rare.** After the first upload, `seq` moves only when something the booth
owns changes a finished round's shots: log totals and hole layouts filled in after the round
(`fill_log_totals`, `fill_finish`, `set_shot_rest`), the log backfill CLI, a repair migration. Each
of those makes the next revision.

**Building a revision.** The uploader builds the record's body from one read transaction. The body
holds captured values, slots, sides, player rounds, week results, shots and lines. The first
revision also carries the edit fields, as the server's starting values (the booth's own league and
tournament marks); **later revisions never do**: they're the server's from then on, so a re-upload
after a backfill can't undo a web edit.

- If its hash equals the last acknowledged hash, and `seq` hasn't moved since the read, the dirty
  mark is cleared and nothing is sent.
- Otherwise it writes the next revision number into the outbox row, in one transaction, along with
  the compressed body, its hash and the `seq` it was built from. From then on that revision is
  fixed.

**Sending.** The pending revision is sent, and retried as the *same bytes* until acknowledged,
across restarts. Changes made meanwhile only mark the row dirty again; they never alter the pending
body. So a lost acknowledgement followed by a backfill can't send different content under a number
the server already accepted.

**Acknowledgement.** On acknowledgement, the row records the acknowledged revision and hash and
drops the pending body. If the row is dirty (`seq` moved past the one built from), the next revision
is built.

**Durability.**

- Nothing pending or dirty is pruned, evicted or replaced by the feed.
- A fenced handoff (rev 4) makes the old PC's pending revisions fail. Restored later, they go
  through rev 4's flagged-batch import.
- The tray shows an outbox that hasn't drained.

**Rows outside records.** Course geometry (`hole_layouts`, `logged_pins`, `hole_images`,
`course_tees`, `tee_lengths`), predictions, the booth-memory tables (`form_mentions`, `asides`,
`news_heard`) and `sessions` change on
their own, sometimes in the background after a round (hole layouts,
`engine.py` ~8256). So they get their own journal: `row_journal` (`tbl`, `row_key`, `change_no`).

- **Recording.** Triggers on those tables upsert a journal row whenever `meta.mode` isn't
  `self_hosted` and `meta.quiet = 0`, in the same transaction as the write.
  - It's local changes only. What the server sent (feed, bootstrap, rehydration) is never sent back.
  - It covers older versions too, since the triggers live in the database.
  - A test bootstraps, applies feed rows and rehydrates a record, and requires no new journal
    entries.
  - **Sessions.** A session is identified to the server as `dataset_id` plus its local `id`.
    - Round bodies name their session by that pair, so the server can accept a round whose session
      row hasn't arrived yet, and fill the session in when it does.
    - The uploader sends pending session rows before the rounds that name them, when both are
      waiting.
- **Coverage test.** Every table in the schema must be listed in exactly one place:
  - round serialization;
  - this journal;
  - an explicit "local only" list (`upload_outbox`, `row_journal`, `meta`, staging).

  So a table added later can't be silently left out of sync.
- **Sending.** The uploader sends each row's current content (or its deletion).
- **Clearing.** On acknowledgement, a journal entry is removed only if its `change_no` hasn't moved
  since it was sent.

**The body format** is versioned and defined once in the app. The shared fixtures feed it to both
the local stats code and the cloud's.

### The working set in cloud mode

- Other bays' rounds arrive from the feed, written in `quiet = remote` under their own `writer_bay`.
- Detail older than the window (rev 4: 30 days, capped at 2,000 rounds or 500 MB) is evicted oldest
  first, as above.
- The booth's history facts read rollups plus whatever detail is local, with the same code as
  self-hosted.

### Switching from self-hosted to cloud

**What the importer sends.** It reads this model.

- Each record goes as one round body, built as the outbox builds it.
- Edit fields go once, separately, as the server's starting values for them. After the cutover
  they're the server's.
- Course data, scorecards and anything else outside a record go as they are (rev 4, "Switching from
  self-hosted").
- `meta.dataset_id` is the source namespace, which is how a copied database is caught.

**The journal.** The booth keeps playing during an import, so the import follows rev 4's snapshot,
journal and drain:

- **Snapshot.** The import starts by setting `meta.mode = importing` and taking its snapshot, in one
  transaction.
  - From that moment `row_journal` records every change outside the records, so no write can fall
    between the snapshot and the journal.
  - The manifest is built from the snapshot: each record's `seq` and every row outside the records.
- **Drain.** After the manifest is confirmed, records whose `seq` moved since are sent again as
  their next revision, and journal entries are sent as above. This repeats until a pass finds little
  left.

**The cutover** happens between rounds, after the last pass, in one transaction:

- it sets `mode = cloud` and `bay_id`;
- it stamps `writer_bay` on this PC's records;
- it seeds each record's outbox row with exactly the revision and hash the server confirmed for it,
  and the `seq` that revision was built from.

A record changed after its last confirmed revision starts dirty, so it's sent as the next revision.
The journal carries on unchanged through the cutover: whatever it holds, and anything written after
it, goes out through the outbox. Nothing captured during the import is lost.

## Indexes

Step 1's indexes, plus these, so that the triggers' lookups are indexed too:

- `shots(record_uuid)`, `lines(record_uuid)`;
- `rounds(round_uuid)`, `player_rounds(round_uuid)`;
- `round_slots(round_uuid)`;
- `shots(uid)`, `lines(uid)`;
- `round_records(state, ended_at)`.

The `EXPLAIN QUERY PLAN` test from step 1 covers the triggers' lookups as well.

## Order

1. Step 1 indexes and hot spots (stats-longevity).
2. The record: tables, slots, live writing, practice stretches, `gen` triggers, and the migration
   with its equality test. Readers stay on today's rules, and the stored grouping is checked against
   them.
3. Readers switch to the stored grouping (`round_groups`, `shot_rounds`, `player_round_of`,
   `_late_gimmes`, `round_rows` for `_round_rows`), keeping their shapes. They read it once every
   row has a record (`round_record.recorded`), else the time rules (`_timed_*`, which `membership`
   and `reconcile` keep using): a file not yet migrated, or rows an older version wrote since the
   last start. On a migrated copy the two give the same pages; they differ only for rounds played
   since, where practice on the round's course before it is no longer the round's.
4. Page edits only on settled rounds (`round_record.in_play`; the page hides Manage and the server
   answers 409 till then).
5. Contributions and rollups keyed by record and slot (stats-longevity step 4), then reels (step 5).
   They're a cache: recomputed when a record's `gen` moves, rebuilt in full in the background after
   each app update, and on demand from a "Rebuild stats" option. A test checks them against a full
   recompute.

Steps 1 to 5 are the local layer. They need nothing from the cloud and ship on their own.

6. *(cloud layer, only if it ships)* Its migration, the outbox, uploader, ownership guard, feed
   records and eviction, with the shotcast-cloud pilot.

## Open questions

- **`PRACTICE_IDLE_SECONDS`.** 30 minutes is a guess; tune it on real data.
