# Architecture

A map of the codebase for someone (or an agent) picking it up. Read this with
[How a shot sounds](docs/booth.md#how-a-shot-sounds) open; this adds the module layout and the invariants that are easy
to break.

## The shape of it

Three input feeds get reconciled into one live game state, the engine turns each event into
facts, a model phrases the facts, a checker guards the phrasing, and TTS speaks it. All the
counting and arithmetic happen in code: the model is handed statements ("hit 3 of 6 fairways",
"carries 268 to a pin 4 down"), never raw data to tally or interpret, because that is where
small models invent things.

```
sources/  ─►  engine/    ─►  facts.py  ─►  prompts.py ─► LLM ─► grounding.py ─► speech.py ─► TTS
(3 feeds)     (orchestrate   (statements   (compose)            (reject bad     (queue +
              + project)      from state)                        lines)          play)
                   │
                   └─► store/ (season.db) , round.py (scorecard/format/handicap), usage.py (cost)
```

## Modules

### Core pipeline
- **`__main__.py`** CLI entry and subcommands (`run`, `sniff`, `calibrate`, `ocr-test`, `say`,
  `banter`, `check`, `eval`, `review`, `stats`, `stats-web`, `publish`, `import-league`, `cost`, `squabbit`,
  `fixture`, `notify-test`).
- **`engine/`** the orchestrator, and by far the largest part of the code. Owns the live loop: the
  three-way pre-shot split, the impact carry/offline projection, the ball at rest (GSPro's round file), reactions,
  water handling, the round lifecycle (opener/closer), and speech scheduling. Most behavior
  questions start here. It is one class, `Engine`, written as one part per job and put together in
  `core.py`; every part shares the engine's state through `self`, so a method in one file calls
  methods in the others directly:
  - `core.py` what the engine holds (`__init__`) and its main loop (`run`)
  - `common.py` the constants, small helpers and records the parts share
  - `hud_watch.py` watching GSPro's screen between shots: the HUD, the minimap, the putt read, and the setup it emits
  - `round_detect.py` telling when a round starts, goes on and ends, from the screen and GSPro's log, and keeping the log's scores
  - `services.py` the booth's side jobs: the commentary switch, weather, bookings, the schedule and the league page
  - `recap.py` the league week's recap (commentator/recap.py)
  - `asides.py` what the booth says between shots: news, stories, course trivia and live scores
  - `round_file.py` gSPro's round file: watching it, and matching its strokes, names and clubs to the booth's shots
  - `chat.py` live chat: what the booth reads from it and answers on air
  - `on_air.py` who is playing and what's been called: names on air, the calls made, score facts, the hole's wrap and closest to the pin
  - `facts.py` the facts a pre-shot line can use: the player's season, form, recall, the field and the stakes
  - `identity.py` whose shot it is: GSPro's slots, names from the round file and the log, and each shot's row
  - `preshot.py` the pre-shot: the setup line, the plan for the shot's flight, and the reactions prepared for it
  - `shot.py` the shot: reading the screen at impact and handling the shot itself
  - `shot_reads.py` what's read around a shot: carry, the finish, trees, the lie, and which hole it was on
  - `results.py` a shot's result once it's known: corrections from the log and the round file, pick-ups, and what's recorded
  - `in_flight.py` calls while the ball is in the air, and the change to the next hole
  - `course.py` the course card, tee boxes, handicaps and the board for the round
  - `squabbit.py` posting the round to Squabbit as it's played
  - `league_round.py` whether a round is the league's: its week, its course, and what its card says
  - `round_start.py` starting a round: building it, its players, and the shots hit before it was known
  - `setup_watch.py` gSPro's setup screen before a round: its players, format, holes and teams, and the round prepared from them
  - `week.py` the league week: Squabbit's leaderboard and the sim's closest to the pin
  - `round_end.py` the scorecard, the end of a hole and of the round, and the sign-off
  - `live.py` the live stream
  - `upkeep.py` publishing the stats page, backing up the season file, and health checks
  - `generation.py` generating and checking what's said: the model calls, grounding, and the transcript
- **`config.py`** the Pydantic config schema and loader. The source of truth for every setting
  and its default (including the GSPro/connector log paths and course dirs).
- **`facts.py`** turns launch data and state into plain statements so the model never interprets
  numbers or counts.
- **`prompts.py`** the system prompts (league and practice variants), the line and reaction
  pickers, and the result-outcome vocabulary.
- **`flight_pick.py`** chooses which pre-written branch to fire: the flight line for the
  trajectory, and the resting-lie reaction per material (fairway, green, semi/regular/deep
  rough, sand, pine straw, cart path, structure, water).
- **`minimap.py`** the optional Jev hazard read (`minimap.provider: jev`). Jev boxes the ball and
  pin, code splits the minimap along the line between them, and Jev says what each half holds
  (open water). Red penalty and white out-of-bounds lines are measured by code instead: each
  long red or white run is placed against the ball-to-pin line, its closest approach turned into
  yards by the map's scale, and its hazard side found (beyond a white line; on a red line's
  side with less grass, so a cliff edge that crosses the line of play is called on the right
  side). Lines more than 60 yards off aren't part of the hole, and the ground past an OB line
  is greyed before Jev looks for water. Jev also
  reads the yardage numbers as digit choices. Bunkers carry no number, so code finds
  sand-coloured patches near the line, Jev confirms each is a bunker cut into grass (not a beach,
  shoreline or roof), and the map's scale (ball-to-pin pixels over the yards to the pin) gives their
  distance; overlapping pieces of one bunker merge. Trees the same way (textured canopies near
  the line, Jev confirms; a lone tree gets one distance, a stand a range), plus a stymie when the line to the pin runs through one within 60 yards.
  On an approach Jev says which side of the green the pin is on (front/back comes from GSPro's
  front and back numbers); from the tee a dogleg shows in GSPro's aim dot (found as a pale disc,
  confirmed by Jev on a close-up) sitting 10°+ off the ball-to-pin line, or failing that in Jev
  saying the fairway bows off that line. On a long tee shot whose map stops short of the green, the
  only dot is the aim point: Jev reads its number on a close-up, and when that's well short of the
  to-pin, sides and distances are measured to it and the pin and dogleg reads are skipped. Only answers above `threshold` become facts and
  hazards; a late or failed read falls back to the `preshot` vision call. Each side's main hazard
  (`FlightPlan.named`: water, then a bunker where the shot likely lands, then out of bounds) may be
  named by that side's trouble flight line, which `pick_line` plays only when that hazard is in play
  (for a measured line, only when the miss reaches the line where the ball comes down).
- **`putt_read.py`** the optional putt-break read (`putt_read.enabled`). GSPro's green grid has
  white dots creeping slowly downhill along its lines; a still frame says nothing, so the booth
  grabs ~20 frames of the strip between the ball and the hole a few hundredths of a second apart,
  matches each dot to itself in the next frame (mutual nearest neighbour; a dot wrapping back across
  its cell is too far to match), and averages the drift. Sideways drift on the cross lines is the
  break, read in thirds up the screen so a double breaker shows; drift toward or away from the golfer
  is uphill or downhill. Gentle drift can't be told from a flat putt, so it says nothing. Started at
  setup when the distance is in feet; bounded by `putt_read.timeout_seconds`. The HUD's measured
  rise or fall (from 3 inches, spoken in inches and feet) replaces the grid's uphill/downhill.
- **`grounding.py`** the checker. Rejects any line whose numbers are not in the facts, that
  claims where an airborne ball lands, or that names a course feature without a screenshot. Its
  checks of a line against its facts are `shotcast_scoring.grounding`'s (shared with Shotcast Cloud).
- **`interest.py`** scores how notable a shot is and whether to comment at all.

### Shot and game state
- **`models.py`** the shot and event types: `Shot` (ball/club data, `is_putt`), `ShotEvent`,
  `PreShotEvent`, `HoleChangeEvent`, `RoundStartEvent`, `ScorecardEvent` (a read scorecard, or none when GSPro's log
  posted a score), `RoundEndEvent`.
- **`hud.py`** parses OCR of the GSPro 3.1.x HUD (player box, hole box, tile panels, minimap lie
  and slope, club box) into structured fields.
- **`screen.py`** screen capture and OCR (RapidOCR/ONNX, CPU). Region reads, and the per-slot
  hash cache (`read_region_lines_cached`) that re-OCRs only changed regions. Regions are fractions
  of the screen; the HUD defaults carry the aspect they were measured at (16:10) and the edge their
  panel is pinned to, and `Region.to_pixels` rescales them for other screen shapes, because GSPro
  sizes its HUD to the screen width. The ON AIR badge (`overlay.badge_geometry`) does the same.
- **`carry_model.py`** how far GSPro actually lets a shot carry relative to the launch monitor: a
  course baseline from its altitude and a per-lie penalty by carry and launch angle (from GSPro's code
  and the league's shots). Nothing is learned in play; a shot well off the prediction is logged as a
  `carry check`.
- **`carry_estimate.py`** GSPro's no-penalty carry from the ball numbers, for the mid-flight call
  when the monitor sends no carry (springbok's connector), flown by `ball_flight.py`; none for a
  launch under 2 degrees. Never stored as a measured carry.
- **`ball_flight.py`** GSPro's ball flight (its air and step, our own drag and lift curves fitted
  to its output): a shot's carry from its ball numbers, how a course's altitude changes it, and its
  whole flight (hang time, the curve off its start line, the height on the way) for the mid-flight
  call's deadline, the projection's drift (`flight_pick.py`) and a tree hit's height (`flight.py`).
- **`course_card.py`** reads a GSPro course's static card (par, stroke index, yardages,
  altitude) from its `.unity3d.csv` / `coursedetails.txt`, and, with commentary on, the GSPro
  listing's notes from `coursedetails.txt` and the course file (`.GKD`): the description and style
  tags. These are background for the course-trivia asides and a touch of colour in the opener,
  whose substance stays the format and the players (`facts.course_listing_facts`).
- **`round.py`** a league round: who is in it, the format, handicaps, hole scores, and the
  derived numbers (net, fairways, GIR, putts, penalties). Opener/closer trigger logic.
- **`store/`** the SQLite season history (`season.db`): records every shot and line, serves
  season bests, backs `stats`. In-memory for
  replays/evals so fake shots never persist. One class, `Store`, written as one part per job and put
  together in `core.py`, as the engine is:
  - `core.py` opening the season file, sessions, round records and migrations
  - `common.py` the schema, constants and small helpers the parts share
  - `shots.py` recording a shot, and each hole's layout and image
  - `finishes.py` where a shot finished: filled from the next shot, and backfilled
  - `shot_edits.py` changing a recorded shot: its result, player, round class, hole, position and track
  - `round_file.py` applying GSPro's round file to the recorded shots
  - `session.py` what the booth notes as it goes: the format, gimmes, the lines spoken
  - `stats.py` reads for the booth's facts: drives, closest to the pin, proximity, putting and scoring
  - `repairs.py` repairs to rows an older version or a missed read left wrong, and finishes filled later
  - `memory.py` what the booth remembers saying: form mentions, asides, news and predictions
  - `rounds.py` rounds, week results, and each player's and team's season
- **`stats_web.py`** / **`stats_web.html`** / **`season_analytics.py`** the `stats-web` viewer: a
  localhost-only server that opens `season.db` read-only (its own connection;
  `Store` would insert a session row) and a single-page UI. `season_analytics.py` holds the
  heavier season reads: strokes gained, gapping, recovery, trends, courses, highlights, booth
  stats, the shot log and broadcast transcripts. The booth itself doesn't import them.
- **`hole_replay.py`** each round's holes as drawn on the Rounds tab: shot lines from the logged
  ball positions, aims, tee and pin (`hole_layouts`, or the pin fitted to the shots' distances),
  over the hole's minimap kept off the tee (`hole_images`, `minimap.snapshot`). A stroke GSPro's
  round file tracked (`shot_tracks`) is drawn along its real flight and roll.
- **`round_file.py`** GSPro's own record of the round in play (`currentRound.dat`, 3.59+), as
  tracks: each stroke's start, touchdown, stop, flight and roll, lies and finish. The engine's
  `_round_file_loop` applies it with `Store.apply_round_file` whenever the file changes: matched to
  the stored shots by hole, stroke number, ball speed and player, it replaces the booth's start,
  carry, total (`total_from_log = 2`), lies (a finer HUD lie stays) and finish, and the setters
  that read those later leave it be.
  At round end `archive` copies the file to `log_dir/round-files/<start>-<course>.dat` (the
  newest 200 kept; a problem report takes the newest few), as GSPro overwrites it next round.
- **`log_backfill.py`** `backfill-log`: matches a saved GSPro log's shots to stored ones in order
  and fills their positions, aims and the hole layouts.
- **`stats_site.py`** builds the stats page as a static site (per-context summary JSON plus a small
  `stats.db` of shots and transcripts that the page queries in the browser with the vendored
  sql.js-httpvfs over HTTP range requests) and uploads it to S3 (`publish`; the booth runs it in a
  subprocess after each round). `stats-web` serves the same site locally.
  `tools/aws/stats-site.yaml` is the S3 + CloudFront + password stack.
- **`live_stream.py`** the Live tab's stream: starts and stops OBS's HLS output over its WebSocket
  with each round, and mirrors the playlist and segments to the bucket's `live/` as they're written.
  **`obs_setup.py`** the tray's setup: downloads portable OBS, writes its profile and WebSocket
  settings, and adds the GSPro capture.
- **`state.py`** small in-session state (`ShotRecord`, `SessionState`).
- **`league.py`** the league file model (teams, players, `gspro_name`/`aliases`, handicaps,
  schedule, storylines) and the course-name matcher.
- **`history.py`** earlier-season standings as facts, built by `import-league`.
- **`league_import.py`** builds `league.json` and history from the yearly spreadsheets.

### External data
- **`squabbit.py`** reads live handicaps and leaderboards from Squabbit at round start, and the leaderboards again once
  the booth has finished posting a round there (best-effort),
  and the season's week cards, rounds and leaderboards for `squabbit --cards`/`--validate`.
- **`squabbit_live.py`** opt-in live posting (`squabbit.live_updates`): when a league round starts, a
  `popup.py` question on the sim's screen asks whether to update Squabbit. On yes, each finished hole's
  score (a shared ball's on every teammate's round) and CTP marks go on the team's week card as one
  atomic Firestore commit with update-time preconditions. The booth stops posting a card when anyone
  else enters or clears anything on it. A CTP mark goes on once that player has finished the hole. One
  the booth isn't sure of goes up as a `popup.Entry` box, prefilled with its guess for the players to
  post, correct or leave to enter themselves. That covers a screen read GSPro's log disagrees with, a
  tee shot conceded farther out than `squabbit.ctp_sure_gimme_feet` (on the green or the fringe?), and
  a lifted scramble ball. What the booth wrote, and the players' answers, are kept in
  `squabbit_live.json`, next to the season db.
- **`handicap.py`** the Squabbit side of the handicap rules: the week card built from a Squabbit
  tournament (its allowances, max index and max hole score), a team's net scored from its players'
  Squabbit rounds, and the season validation that recomputes leaderboard nets. The rules themselves
  (course handicap, strokes, stroke allocation, caps, a team's net per hole by format) are
  `shotcast_scoring.handicap`'s (below), re-exported here. `round.py` uses them, with the week card
  when one is saved.
- **`board.py`** the week's posted results and the season standings from Squabbit's leaderboards,
  read with the handicaps at round start: `Board.from_squabbit` and the Squabbit team matching. The
  places, points and standings (places from the nets, tied places splitting their points) are
  `shotcast_scoring.board`'s `Board`, which this one extends; its names are re-exported here. (The
  package's Board also does flights; this league has none, so it's one field.) The engine's week
  facts: the week so far, standings, the closer's projected place, the net stakes late in a round,
  and the week's closest to the pin (`Store.week_ctp`).
- **`scoring/`** (`shotcast_scoring`, its own package): the pure league scoring rules, shared with
  Shotcast Cloud, which installs it from a tag (`...#subdirectory=scoring`). Plain data in and out:
  no I/O, no app imports, no third-party dependencies. `round` there puts one round together (a side's
  strokes, the holes they fall on, a team's net per hole): `commentator/round.py` scores its live round
  with it. `recap` there is the league week's recap with no bucket or model (its facts, the week as
  one tournament, the clips, the request for the lines): `commentator/recap.py` imports it. The app
  ships it in its own install
  (pyproject.toml's package discovery). A rule change needs a version bump and a tag; see
  `scoring/README.md`.
- **`news.py`** fetches real golf headlines (RSS/Atom) for between-shots banter, because the
  model's own news is stale. Teasers with no facts in them are skipped. At the second hole played
  of each nine the engine has the closer's model write and voice that nine's two-line asides (news,
  course trivia, live scores) up to its airtime, less any still waiting from the nine before: live
  scores claim their check-ins first, then news and trivia take turns at what's left, each within
  its configured share (`news.per_nine`, `trivia.per_round`). Each is
  held for a quiet shot, or for the kind of shot its story is about (a long drive, trouble), with a
  bridging first line for that moment. At most one every three holes, never on the first or last two.

### Output
- **`speech.py`** the speech queue: ordering, backlog limits, barge-in (cutting off a line in
  progress), and audio playback. On a cloud voice it voices queued lines in parallel and plays
  them in order, hedges a slow request with a second one, and drops a line that misses its
  deadline (and the rest of its exchange) instead of playing it late.
- **`overlay.py`** the clickable ON AIR / OFF AIR badge over GSPro: a Tk window on its own
  thread, never focused, re-asserted topmost every second. A click calls `Engine.set_commentary`
  on the booth's loop (off air mutes and cuts speech and skips model calls; recording carries on).
  Starting it makes the whole booth process per-monitor DPI-aware, so under Windows display scaling
  everything (badge placement, screen capture) works in real pixels rather than scaled ones.
- **`usage.py`** per-call token logging to `usage.jsonl`, the cost rollups, and the per-session voice (audio) cost line.
- **`perf.py`** process-priority / core-cap helpers so OCR bursts do not starve GSPro.
- **`winshot.py`** occlusion-proof window capture (PrintWindow) for `screen.capture: window`, when a
  window sits over GSPro. Off by default; the default is a plain monitor grab.
- **`dotenv.py`** a tiny `.env` loader (no dependency).
- **`backend.py`** where every AWS client (S3, DynamoDB) is made: boto3's own credentials by
  default, swappable in one place with `backend.use()`.
- **`settings_owner.py`** who owns each setting (`web`, `pc` or `service`) for a future hosted
  service; unused self-hosted. `tools/settings_schema.py` writes it with the config's JSON Schema.

### `llm/` and `tts/` (one file per provider, swapped by config)
- `llm/`: `base.py`, `ollama_client.py`, `anthropic_client.py`, `openai_client.py`.
- `tts/`: `base.py`, `console_tts.py`, `kokoro_tts.py`, `openai_tts.py`.

### `sources/` (the three feeds, see [docs/tracking.md](docs/tracking.md#where-the-data-comes-from))
- `base.py` the `ShotSource` ABC. `__init__.py` picks the source from `source.type`.
- `connector_log.py` tails GSPconnect's `ConnectDebug.txt` for the shot feed (the default).
- `gspro_log_shots.py` the shot feed from GSPro's own log (`LaunchExt Vars` lines): any launch
  monitor, ball data only. `connector_log` falls back to it when `ConnectDebug.txt` is missing.
  GSPro 3.59 logs only the fire, so there each fire waits for `gspro_files.py`: the shot GSPro saves
  in `currentRound.dat` (every stroke of the round) or `GSPro.db`'s `LastShot` (practice) once the
  ball stops. The source asks the booth for its impact snapshot (`on_impact`) half a second after
  the fire, and the engine skips mid-flight calls for a shot that arrives after the ball is down.
  Faster where a monitor logs the launch with a carry (`club_feeds.py`, given to the source as
  `club_feed` by the engine): a full swing's fire takes that line (`ClubFeed.take_launch`) and airs
  at once, in flight; GSPro's saved copy with the same ball speed is then dropped. The
  `connector_log` fallback passes such a shot straight through instead of holding it for
  GSPconnect's copy.
- `gspro_sniffer.py` sniffs GSPro Open Connect loopback (scapy, needs Npcap + admin).
- `club_feeds.py` club data for shots that come without it: follows FlightScope's lines in
  `ConnectDebug.txt`, rela's log, OpenSkyPlus's (BepInEx) logs and springbok's
  MLM2PRO-GSPro-Connector's log (found from its running exe), and the engine's `_pump_source`
  gives a shot the reading with its ball speed. FlightScope's and rela's readings also carry the
  launch (`ClubReading.launch`), for the 3.59 fast path above.
- `gspro_log.py` tails GSPro's `output_log.txt` into a live `GsproState` game-truth model,
  including each fire's ball position and minimap aim, and the hole list of a completed round.
  GSPro 3.59 (public beta) writes `Player.log` beside it instead; `live_log` picks whichever was
  written last and the follower switches when GSPro does. The engine sets `Screen.ui` from the
  logged version (`engine.gspro_ui`: "new" from 3.59), which swaps in `config.NEW_UI_REGIONS` for
  the moved setup-screen regions; on 3.59 a region in the config's `screen.regions_new` wins, and
  `screen.regions` applies only to boxes 3.59 didn't move (`Screen.base_region`). `screencheck.py`
  keeps a label set per version (`GROUPS` / `GROUPS_NEW`), tells the version from the screen's own
  labels (`detect_screen`), and writes `--fix` to that version's list. The setup checks read either
  menu: a tab is lit when bright (3.1) or blue-underlined (3.59, `Tone.blue`), a name pill is filled
  when grey (3.1) or team-coloured (3.59, `Tone.color`). The HUD parsers take both layouts as is.
- `replay.py` replays a `.jsonl` shot feed offline. `stream.py` is shared async plumbing.

## Invariants that are easy to break

These were each the cause of a real bug. Preserve them.

- **The ball at rest is GSPro's round file's, not the screen's.** `engine._wait_for_file` waits
  for the stroke with the shot's ball speed and player in currentRound.dat (written in every
  course mode, at rest) until `_file_wait_over` gives a reason it won't come. The screen isn't
  followed after the swing; its offline and raw-carry tiles are read at the next setup
  (`_apply_late_tiles`).
- **`impact_snap` is a 7-tuple.** Index 6 is the per-player shot number captured at impact. Any
  code that unpacks `impact_snap` must unpack all 7 (this caused a crash when one reader still
  expected 6). The putt `base_shot` comes from `impact_snap[6]`, not a live read.
- **Unknown putt outcomes are not graded.** `_wait_putt_result` returns `"unknown"` on timeout
  (vs `"missed"` on a real stroke advance). An `"unknown"` outcome writes no result and grades
  no prediction, and its reaction is suppressed, so a holed putt that timed out does not get a
  "missed" reaction.
- **OCR is genuinely slow; do not add concurrency to beat it.** A full HUD read is ~2.6-2.8s,
  dominated by the player-box region (~1.4s). More OCR threads oversubscribe the CPU and make it
  slower, and starve GSPro.
- **League vs practice gates the prompt.** The `system` prompt is league-only when a league
  round is actually in progress (`r is not None and not r.ended and r.is_league`), else practice.
  The closer runs when `r.ended` is true, so it passes its variant explicitly rather than
  reading the property. Practice must never claim points are on the line.
- **Water is learned from the log, fast.** `GsproState.wet(player, since=)` and
  `_shot_wet(player, since=impact)` must be queried with the impact timestamp, or a stale wet
  flag from the previous ball fires the splash a shot late.
- **A log shot belongs to the score line after it, not the last select.** GSPro hands the clock
  over with "Next Player" and no new select block (and the round's first shot has none), so the
  player last selected is often not the one swinging. `GsproState` holds each "Shot Fired", and
  any "Hole out" or splash during it, unowned until the shooter's own score line credits it.
  Crediting by the last select recorded partners' holed putts as misses and flagged the wrong
  swing as a mulligan.
- **A stroke played on isn't holed.** At the next tee GSPro's log can still show the last hole's
  hole-out, which once marked two tee shots `holed`. `Store` checks both ways (when the next
  stroke is recorded, and when a late `holed` result arrives): if the same player's next stroke
  is on the same hole with a higher stroke number, the putt becomes `missed` and a full shot
  loses its outcome. A mulligan's re-hit keeps the stroke number, so it doesn't count.
- **Every full shot gets a finishing distance.** The booth sets `finish_yards` from GSPro's log
  at the player's next shot. It once skipped that whenever the old screen follow hadn't seen
  touchdown, which lost about half of them. As a safety net, `Store` also fills a missing
  finish from the same player's very next stroke on the hole (its `to_pin_yards`, or the gimme
  radius), and backfills old rows at startup. A penalty in between leaves the finish empty,
  because the next stroke was played from a drop. A full shot that holed out gets 0.
- **Replays and evals must not write the season DB.** They use an in-memory database. If you add
  a new persistence path, keep that separation or fake shots pollute `season.db`.

## Tests

`tests/` (400, run with `pytest`; CI in `.github/workflows/tests.yml`): `test_core.py` (engine,
HUD, round file wait, OCR cache),
`test_round.py` (rounds, formats, handicaps, setup watcher), `test_handicap.py` (Squabbit's
handicap rules and week cards), `test_board.py` (week results, standings, net and CTP facts),
`scoring/tests/` (the shared scoring package's own; `pytest` collects them too), `test_gspro_log.py` (the log
reader), `test_model_switch.py`, `test_connector.py`, `test_replay.py`, `test_minimap.py`,
`test_putt_read.py`, `test_gspro_log_shots.py`, `test_hole_replay.py`. They cover logic, not
real GSPro/Uneekor traffic or live screen layouts. `tools/` holds the offline harnesses, an OCR
thread benchmark and screen-calibration helpers.
