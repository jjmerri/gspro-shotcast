# Design: the local round record

How the booth stores rounds in `season.db`, for self-hosted and cloud mode alike. Today a round is
inferred from timestamps and names each time it's read; this makes it a stored record with an ID,
players with slot IDs, a change counter, and edits kept apart from what was captured. Self-hosted
uses it for speed (stored totals, cached reels: `stats-longevity.md` steps 4 and 5); cloud mode
uses the same record to upload rounds (shotcast-cloud `docs/architecture.html`, "Data design (rev 4)").
One model, one set of write paths, one importer.

Nothing here changes what the stats page or the booth shows. Existing databases are migrated in
place, older app versions still open them, and nothing about the cloud is visible until it's turned
on.

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

### Tables (schema version 6)

All of these are created in every database, in either mode, so triggers that name them always
resolve. (SQLite resolves every table a trigger names even when its `WHEN` is false.)

- **`meta`**: one row.
  - `dataset_id`: a UUID made once per `season.db`. It's the importer's source namespace, so a
    copied database is recognisable.
  - `mode`: `self_hosted`, `importing` or `cloud`.
  - `bay_id`: null in self-hosted mode.
  - `writer_generation`, `feed_watermark`: cloud mode only.
  - `quiet`: a guard flag, `0`, `remote` or `bookkeeping` (below).
- **`round_records`**: one row per round, and one per *loose batch* (below).
  - Identity: `uuid` (primary key), `kind` (`round` or `loose`).
  - What it is: `session_id`, `course`, `started_at`, `ended_at`, `state` (`in_progress`,
    `finished`, `abandoned`).
  - `writer_bay`: the bay whose PC captures and revises it. Null in self-hosted mode, meaning this
    PC.
  - `resident`: whether its shots and lines are on this PC (cloud mode can evict them).
  - `seq` and `gen`: the change counters (below).
  - `assigned`: how its rows were grouped, `live` or `inferred` (the migration).
  - `continues`: the UUID of a record it may continue (restart, below), or null.
- **`round_slots`**: one row per player in a round.
  - `slot_id` (UUID), `round_uuid`.
  - `seat`: the GSPro setup-screen index, when known.
  - `name`: as GSPro shows it, updated by a rename.
  - `side`: the side key `rounds.side` uses.
  - `player`: the league roster name it was matched to, or null.
  - `matched_by`: `exact`, `alias`, `fuzzy`, `manual` or `legacy`. This is the record that rev 4's
    "how each link was made" correction reads.
  - `person_id`: cloud mode, from the feed.
- **New columns.** Older versions don't write them; an `AFTER INSERT` trigger fills `uid` even for
  them.
  - `shots`: `uid`, `record_uuid` (its round, or its loose batch), `slot_id` and `derived_from`
    (a gimme's source shot, below).
  - `lines`: `uid` and `record_uuid`.
  - `uid` is a random ID. Everything that refers to a shot or line from outside its table
    (overrides, `captured`, upload bodies) uses `uid`, never the integer `id`. Without
    `AUTOINCREMENT`, SQLite reuses the highest `id` after it's deleted, which eviction can do, so an
    integer reference could land on an unrelated new shot. Rehydration restores each row's `uid`.
  - `rounds`: `round_uuid`.
  - `player_rounds`: `round_uuid` and `slot_id`.

  `week_results` keeps pointing at a `rounds` row, which now carries the UUID. Existing columns,
  `shots.player` included, stay as they are. Every reader keeps working, and readers move to the new
  columns one at a time.
- **`overrides`** and **`captured`**: edits (below).
- **`upload_outbox`** and **`row_journal`**: cloud mode's queue (below). They're empty and
  unused in self-hosted mode.

### Loose batches

Shots captured outside any round (range sessions, a round never recorded) belong to a loose batch:
one per session per calendar day, with its own UUID. A loose batch has the same `seq` and, in cloud
mode, the same upload path as a round (rev 4's "unattached shots"). A shot later assigned to a round
moves from the batch to the round, which changes both. That happens to opening drives adopted by
`_adopt_opening_shots`, and to late gimmes.

### Writing it

- **Round activation.** One transaction in `Engine._start_round`, after `roll_session_if_idle`, so
  the record's session is the one its shots go into.
  - It inserts the `round_records` row (`in_progress`) and a slot for every player.
  - It moves the adopted opening shots into the record.
  - It resolves a restart (below).

  `Round` gets its UUID at construction (replacing the `Round._ids` counter) but writes nothing until
  activation.
- **Players.**
  - `Round.add_player` adds a slot.
  - A rename (`rename_shots`) updates the slot's `name` and its shots' `player`. The slot ID stays.
  - A reassignment (`move_shot_player`: a shot was someone else's) is a separate operation. It moves
    the shot, and its gimme row, to the other player's slot and name in one statement pair.
- **Shots and lines** are inserted with the live record's UUID and the shooter's slot. With no round
  live, they go to the session's loose batch for the day. `record_gimme` copies `record_uuid` and
  `slot_id` from the shot it follows, like it copies the rest.
- **Late gimmes.** A gimme conceded within `CLOSER_SECONDS` of the round ending gets that round's
  record directly, since the engine still knows the round. The `_late_gimmes` time rule is needed
  only by the migration.
- **No more ID ranges.** `mark_league`, `mark_tournament` and `rename_shots` select by
  `record_uuid` (and slot), not `id > shot_floor`. Today they can catch any later shot, which in
  cloud mode includes rows that arrived from the feed.
- **Round end.**
  - `_record_round_stats` writes the `rounds` and `player_rounds` rows with the UUID and slots, and
    sets the record `finished` (`abandoned` for `complete=False`) and `ended_at`, in the same
    transaction.
  - The early return for a round quit before any hole was finished sets the record `abandoned` too.
    The shots it has are kept, as today.
- **A booth restart mid-round.** Today the time rules join the two halves whenever the later one ends
  on the same course within 8 hours, so a fresh game with the same players can be joined to a
  crashed one. The record makes that a choice.
  - The new `Round` adopts the open `in_progress` record (its UUID and slots) only on positive
    evidence of continuing:
    - the same course;
    - the same GSPro roster, by seat when seats were read;
    - the opening hole is past the record's last hole played;
    - no "new round" signal from GSPro in between (the setup screen, a scorecard reset; the events
      `handle_round_start` already reads).
  - Without that evidence it starts a new record, and the open one becomes `abandoned`.
  - When the evidence is mixed (same course and players, but a roster correction, say), it starts a
    new record with `continues` set. The stats page then offers "join these rounds" as an edit.
    Nothing is merged on a guess.
  - An `in_progress` record that nothing adopts within `ROUND_SPAN_SECONDS` becomes `abandoned`. It's
    kept, never dropped.
- **Duplicate names** stay refused for now. With slots, two "Mike"s could be allowed later without a
  data change; that's an engine change for another day.

### The change counters (`seq` and `gen`)

Two counters, because "what this bay must upload" and "what a cache must recompute" differ:

- **`seq`** moves when what this bay *captured* changes: capture, a backfill, a repair, a state
  change. It drives the upload.
- **`gen`** moves when what the record *shows* changes: everything that moves `seq`, plus an edit
  (override) and anything that arrives from the server (feed revisions, overrides, person links). It
  drives the caches.

SQLite triggers move them, not the write methods, so no write path can forget, an older app
version's included:

- `AFTER INSERT/UPDATE/DELETE` on `shots`, `lines`, `rounds`, `player_rounds`, `round_slots` and
  `captured` bumps the row's record. When `record_uuid` itself changes, both records are bumped.
- `AFTER UPDATE OF state, ended_at, course` on `round_records` bumps that record. Recursive triggers
  are off, so this doesn't re-fire.
- Triggers on `week_results` (through its `rounds` row) and on `overrides` (through its target, `gen`
  only) do the same.
- **`meta.quiet`** says who is writing:
  - `0`: normal. Both `seq` and `gen` move.
  - `remote`: applying the server's data (feed, bootstrap, a cloud override) or a local override to
    its columns. Only `gen` moves.
  - `bookkeeping`: eviction, rehydration, the migration. Neither moves, since what the record holds
    hasn't changed, only where it's kept.

  Each of these is set for one transaction by the code doing it. Older versions never set it.
- A statement touching 200 shots bumps the counters 200 times. That's harmless: they're only
  compared, never counted.

Readers that cache anything per record store the `gen` they computed from and recompute when it
differs:

- contribution rows (`stats-longevity.md` step 4);
- the reel cache (step 5), whose key also covers the commentary sessions the round touches.

The cloud uploader reads `seq`.

### Edits are overrides

Today the page's edits overwrite columns the capture paths also write (`mark_league` and
`set_round_league` both set `is_league`). A later backfill can undo an edit, and the captured value is
lost. Instead, edits and captured values are kept apart:

- **`overrides`** is the edit log. Rows are never changed except to stamp `undone_at`.
  - `id`, `uid` (stable across PCs), `target` (a shot's `uid` or a round UUID), `field`, `value`.
  - `origin` (`local` or `cloud`), `by`, `at`, `undone_at`.
  - The before-value shown in the log is what was effective when the edit was made, written into
    the row at insert.
- **`captured`** holds, per physical row and field (`tbl`, `row_uid`, `field`, `value`), what the
  capture paths last wrote to a field that's overridden. It's kept per row, because a round edit
  covers rows with different captured values: one side's rows league, say, and another's not.
- **Effective value.** For a field on a row, the effective value is the latest active override on
  that row, or on its round. Otherwise it's the captured value. The column always holds the effective
  value, so every reader works unchanged, including `stats_web.connect`'s excluded and deleted views.
- **Two kinds of field.**
  - *Edit-only*: `left_out` (shots) and `excluded`, which is also the deleted mark (shots, `lines`,
    `rounds`, `player_rounds`). No capture path sets them. Only the page does, and new rows copy
    them (`record_gimme` from the shot before; a line from its shot or round, `store.py` ~1029). Their
    captured value is always 0, so they need no `captured` rows.
  - *Captured*: `is_league` and `is_tournament` (shots, `rounds`, `player_rounds`). Capture writes
    them, and rows in one round can differ (one side league, another not).
- **Enforced in the database, not in Store methods**, so it holds for older app versions after a
  downgrade:
  - **Updates to an edit-only field.** Any write while `meta.quiet = 0` is an edit, since nothing
    else writes these. So the trigger records it as an `overrides` row (`by = legacy`) and leaves
    it in effect. An older version's exclude, delete or left-out after a downgrade therefore comes
    back as a proper, undoable edit.
  - **Updates to a captured field.** When `meta.quiet = 0` and an active override covers the row,
    the trigger stores the new value in `captured` and puts the column back to the override's value.
    So an older version's capture writes are kept, and the edit still wins. An older version's
    *page edit* of league, tournament or practice on a round the new version has overridden also
    lands as a capture. That case needs a downgrade plus a re-edit of the same round. It's
    documented in the release notes, and the edit can be redone after upgrading.
  - **New rows.** An `AFTER INSERT` trigger handles a row attached to a record with an active round
    override (a late gimme, a closing line).
    - Edit-only fields are set from the overrides covering the row. Whatever was copied in is ignored.
    - For captured fields, an ordinary insert's value is its captured value.
    - A derived row is different. A gimme is inserted with `derived_from` set to the `uid` of the
      shot it follows, and its captured value is that shot's: its `captured` row, else its column.
      It's never borrowed from another row.
    - The trigger then writes the effective values.
  - **Legacy inserts.** An older version's insert has no `record_uuid`, so no override applies
    yet, and its columns hold what it copied, which matches what the round showed at that moment.
    Reconciliation attaches it later. Before attaching, it sets `derived_from` on legacy gimme rows
    (`outcome = 'gimme'`: the shot before it by the same player on the same hole, which is exactly
    what `record_gimme` copied from).
  - **Moving between records.** An `AFTER UPDATE OF record_uuid` trigger recomputes a row that
    changes record (an opening drive adopted, a late gimme, a reconciliation, a reassignment). It
    restores the captured value where the old record's override no longer covers the row, and
    applies the new record's.
- **Making an edit** inserts the override with `quiet = 0`, so it's journaled during an import.
  - Then, in `quiet = remote`, it copies each covered row's current value into `captured` (where not
    there already) and writes the new effective values.
  - **Undo** stamps `undone_at` and recomputes the effective value from what's left: a later active
    override, or else `captured`. Undoing an older edit never disturbs a later one.
  - Tests:
    - Exclude a round, then a late gimme and a closer arrive, then undo. The gimme and the line
      must come back included.
    - A round whose sides were captured differently (one league, one not) is overridden to
      tournament. A late gimme arrives on each side. After undo, each side's rows, the gimmes
      included, return to their own captured values.
    - Both run once with the current writers, and once with an old version's inserts followed by a
      re-upgrade.
  - A `captured` row is removed once no active override covers it.
- **Self-hosted:** the page's buttons make local overrides. Nothing else changes for the user.
- **Cloud mode:**
  - The buttons call the API. The override comes back (in the response, and to other bays through
    the feed) and is stored with `origin = cloud`.
  - Offline, the buttons are disabled with a note. Queued edits would be a second sync path for
    little gain.
  - **What a revision carries.** The uploaded revision carries captured values only:
    - for `is_league` and `is_tournament`, the column, or `captured` where one exists;
    - edit-only fields are left out of the body altogether, since their captured value is always 0.

    The server holds overrides apart, as rev 4 requires, so an upload never erases an edit and an
    undo on the server can't reveal an edit baked into a revision.
  - A test: exclude a round, a backfill changes it and it uploads, the exclusion is undone on the
    server. The round must come back included.

### Migrating an existing database

A one-off step at the first start on the new version. It runs after the start-of-booth backup
(`backup.py`) and the step 1 indexes, in `quiet = bookkeeping`.

0. **Give every existing shot and line a `uid`**, where it hasn't one already, so a retried
   migration keeps the ones it made. Then check every row has one and they're unique, before
   anything refers to them.
1. **Assign rounds from the raw tables.** It doesn't go through `stats_web.connect`, which hides
   excluded and deleted rows. It uses one new unfiltered membership function, extracted from
   `round_groups`, `player_round_of`, `shot_rounds`, `_late_gimmes` and the line-matching half of
   `_round_rows`, without the edit-eligibility checks (`_round_rows` refuses a deleted round). The page
   then reads the same function, so the two can't drift.

   Each group becomes a record with `assigned = inferred`: `finished`, or `abandoned` where
   `complete = 0`. Deleted and excluded rounds become records too, with their marks as they are.
2. **Make slots:** one per distinct player name in each round, with `matched_by = legacy` and
   `player` from the roster's current matching.
3. **Make loose batches** for every shot and line left over.
4. **Turn the page edits it can recognise into overrides.**
   - Only the page ever sets `left_out` and `excluded` (`stats_web.set_left_out`,
     `set_round_excluded`, `delete_round`; `Store.set_left_out` is the same hand edit). So a
     non-zero value is an edit for certain, and what was captured was 0.
   - Those become `overrides` rows (`by = legacy`), with `captured` = 0.
   - League, tournament and practice can't be told apart: capture and the page both wrote them, and
     nothing recorded which. They stay as they are, as the *captured* values, not as overrides.
   - That's safe, because after the migration no capture path writes the classification of a past
     round. `mark_league` and `mark_tournament` now act only on the live record. A later page edit of
     an old round's class becomes an ordinary override on top of today's value.
5. **Set `user_version = 6`**, last, in the same transaction, after the `uid` check.

**Reconciling after a downgrade.** An older version writes rows with no `record_uuid` and may finish
a round whose earlier shots were already put in a loose batch.

- At the next start, the new version finds the rows with no `record_uuid`.
  - It takes the time span they cover, widened by `ROUND_SPAN_SECONDS` on each side, since a round
    joins shots across sessions.
  - It reruns the membership function over every raw row in that span, assigned or not.
  - If the result touches a record reaching outside the span, the span grows to cover it, and this
    repeats until nothing new is touched.
  - It compares the result with the records in the span that are `assigned = inferred` or loose.
  - A group that matches an existing record keeps its UUID.
  - Rows move between inferred records and loose batches where the rerun says so.
  - Every record that gains or loses a row has its counters bumped (`quiet = 0`).
  - `live` records are never regrouped. Their membership came from the engine, not a guess.
- Fixtures cover:
  - upgrade → edit → downgrade → capture and page edit → re-upgrade;
  - a round begun before the upgrade (its shots already in a loose batch), finished by an old
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

## Self-hosted and cloud mode

| | Self-hosted | Cloud |
|---|---|---|
| Round record, slots, `seq`, overrides | yes | yes, same code |
| Stored contributions and rollups | computed locally from all history | server's rollups from bootstrap; local contributions for this bay's rounds not yet in the feed |
| Edits | local overrides | API, mirrored as overrides |
| Upload outbox | empty | used |
| History kept | all, forever | a bounded window of detail, plus stubs |
| Local stats page | as today | opens the web stats (see open questions) |

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

- Overrides and person links on any record are applied: they're the server's to own, and stored
  apart from captured data.
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
  and lines and sets `resident = 0`. The record, slots, sides, `captured` rows and each row's `uid`
  stay.
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

**Enqueueing.** `upload_outbox` has one row per record that has changed since it was last
acknowledged. A trigger on `round_records` marks the row dirty whenever `seq` changes on a record
whose `writer_bay` is this bay and that isn't `in_progress`. It runs in the same transaction as the
change. In self-hosted mode `writer_bay` and `bay_id` are null, so it never fires.

**Building a revision.** The uploader builds the record's body from one read transaction. The body
holds captured values, slots, sides, player rounds, week results, shots and lines.

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
`news_heard`), `sessions` and local `overrides` (made, or undone, before the cutover) change on
their own, sometimes in the background after a round (hole layouts,
`engine.py` ~8256). So they get their own journal: `row_journal` (`tbl`, `row_key`, `change_no`).

- **Recording.** Triggers on those tables upsert a journal row whenever `meta.mode` isn't
  `self_hosted` and `meta.quiet = 0`, in the same transaction as the write.
  - It's local changes only. What the server sent (feed, bootstrap, rehydration) is never sent back.
  - It covers older versions too, since the triggers live in the database.
  - An override is sent by its `uid` with its `undone_at`, so resending is idempotent.
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
- Overrides go as overrides, with their history.
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
- `overrides(target, field)`, `captured(tbl, row_uid, field)`, `shots(uid)`, `lines(uid)`;
- `round_records(state, ended_at)`.

The `EXPLAIN QUERY PLAN` test from step 1 covers the triggers' lookups as well.

## Order

1. Step 1 indexes (stats-longevity).
2. Record, slots, live writing, triggers, and migration with its equality test. Readers stay on
   today's rules, and the stored grouping is checked against them.
3. Readers switch to the stored grouping (`round_groups`, `shot_rounds`, `player_round_of`,
   `_round_rows`).
4. Overrides, with the page's edits moved onto them.
5. Contributions and rollups keyed by record and slot (stats-longevity step 4), then reels (step 5).
6. Cloud mode, hidden: outbox, uploader, ownership guard, feed records, eviction (with the shotcast-cloud pilot).

Steps 1 to 5 help self-hosted on their own and ship without the cloud.

## Open questions

- **The local stats page in cloud mode.** The booth keeps only a window of detail, so a local page
  can't show lifetime stats. Proposed: it opens the web stats. The cloud doc currently says the local
  page's edit buttons call the API; that would become "edits happen on the web page".
- **Loose batch size.** A batch is per session per day. A venue running a bay all day as a range
  makes one large batch that changes all day; per session per hour may be better. Measure on real
  data.
- **Restart evidence.** Which GSPro signals count as "new round" vs "resume" needs checking against
  real logs from a crashed booth. Until then, mixed evidence starts a new record with `continues`
  set, and the user joins the two on the page.
